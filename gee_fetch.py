#!/usr/bin/env python
"""
Pull calibration scenes from Google Earth Engine, with a cloud mask layer and
cross-sensor normalization, into the labeler's NetCDF layout.

For each calibration scene this downloads the target acquisition plus other
acquisitions from the same year (the Methane z-score layer compares each scene
against its same-year background). Everything is top-of-atmosphere (TOA)
reflectance at each sensor's native resolution:

    sentinel2   COPERNICUS/S2_HARMONIZED (L1C)        B2 B3 B4 B8 B11 B12   20 m
    landsat_89  LANDSAT/LC08|LC09/C02/T1_TOA           B2 B3 B4 B5 B6  B7    30 m
    landsat_45  LANDSAT/LT05|LT04/C02/T1_TOA           B1 B2 B3 B4 B5  B7    30 m

If a target date is missing from Tier 1, the matching Tier 2 Landsat collection
is tried for that date only (background acquisitions stay Tier 1).

Cloud mask (per-pixel `cloud_mask`, 1 = cloud or shadow, 0 = clear):
    Sentinel-2  Cloud Score+ cs_cdf < 0.60 (GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED;
                Google's recommended threshold; also catches cloud shadows)
    Landsat     USGS CFMask QA_PIXEL: dilated cloud | cirrus | cloud | cloud shadow
                (bits 1-4, high-confidence flags)

Normalization (done in Earth Engine) maps every sensor to Landsat 8/9 OLI using
the TOA cross-sensor regressions of Chastain et al. 2019 (RSE 221:274-285,
major-axis regression, Tables 4 and 6). Landsat 4/5 TM uses the ETM+ -> OLI
coefficients (TM treated as ETM+). Both a raw and a normalized copy are written:

    <out>/raw/<sensor>/<prefix>_channel_<lat>_<lon>.nc
    <out>/normalized/<sensor>/<prefix>_channel_<lat>_<lon>.nc
    <out>/calibration_gee.txt   target scenes (indices valid for both copies)
    <out>/manifest.json         scenes, collections, coefficients, grid

Each copy is a valid IMAGERY_PATH for the app, e.g.
    IMAGERY_PATH=imagery_gee/normalized CALIBRATION_FILE=imagery_gee/calibration_gee.txt \\
        DB_PATH=test_labels.db pixi run uvicorn app:app --port 8000

Re-running skips sites whose files already contain every target date, so an
interrupted run can be resumed.

Usage:
    pixi run python gee_fetch.py --all                     # every dated calibration scene
    pixi run python gee_fetch.py --per-sensor 4            # stratified subset
"""

import argparse, json, random, re, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import netCDF4 as nc
import ee

DEFAULT_PROJECT = "aerial-history-project"
BAND_NAMES = ["blue", "green", "red", "nir", "swir1", "swir2"]
NODATA = -9999.0
CS_CLEAR_THRESHOLD = 0.60           # Cloud Score+ cs_cdf >= this is clear
LANDSAT_CLOUD_BITS = 0b11110        # QA_PIXEL bits 1-4

SENSORS = {
    "sentinel2": dict(
        prefix="sentinel2", res=20,
        collections=["COPERNICUS/S2_HARMONIZED"], tier2=[],
        # B8 (not B8A) for NIR: the Chastain et al. coefficients are for B8
        bands=["B2", "B3", "B4", "B8", "B11", "B12"], scale=1e-4,
    ),
    "landsat_89": dict(
        prefix="landsat8_9", res=30,
        collections=["LANDSAT/LC08/C02/T1_TOA", "LANDSAT/LC09/C02/T1_TOA"],
        tier2=["LANDSAT/LC08/C02/T2_TOA", "LANDSAT/LC09/C02/T2_TOA"],
        bands=["B2", "B3", "B4", "B5", "B6", "B7"], scale=1.0,
    ),
    "landsat_45": dict(
        prefix="landsat4_5", res=30,
        collections=["LANDSAT/LT05/C02/T1_TOA", "LANDSAT/LT04/C02/T1_TOA"],
        tier2=["LANDSAT/LT05/C02/T2_TOA", "LANDSAT/LT04/C02/T2_TOA"],
        bands=["B1", "B2", "B3", "B4", "B5", "B7"], scale=1.0,
    ),
}

# Chastain et al. 2019, TOA, major-axis regression, bands blue..swir2, as published.
# Table 4: msi = oli * slope + intercept      Table 6: oli = etm * slope + intercept
_CHASTAIN_MSI_OLI = dict(slope=[1.0946, 1.0043, 1.0524, 0.8954, 1.0049, 1.0002],
                         intercept=[-0.0107, 0.0026, -0.0015, 0.0033, 0.0065, 0.0046])
_CHASTAIN_ETM_OLI = dict(slope=[1.03501, 1.00921, 1.01991, 1.14061, 1.04351, 1.05271],
                         intercept=[-0.0055, -0.0008, -0.0021, -0.0163, -0.0045, 0.00261])

# Expressed as oli = slope * x + offset for every sensor
HARMONIZATION = {
    "sentinel2": dict(   # invert Table 4: oli = (msi - intercept) / slope
        slope=[1 / m for m in _CHASTAIN_MSI_OLI["slope"]],
        offset=[-b / m for m, b in zip(_CHASTAIN_MSI_OLI["slope"], _CHASTAIN_MSI_OLI["intercept"])],
        source="Chastain et al. 2019 RSE 221:274-285, Table 4 (MSI-OLI, TOA, major axis), inverted",
    ),
    "landsat_45": dict(
        slope=_CHASTAIN_ETM_OLI["slope"], offset=_CHASTAIN_ETM_OLI["intercept"],
        source="Chastain et al. 2019 RSE 221:274-285, Table 6 (ETM+->OLI, TOA, major axis), applied to TM",
    ),
    "landsat_89": dict(slope=[1.0] * 6, offset=[0.0] * 6, source="reference sensor (OLI)"),
}

# Scene-level cloud property used to skip mostly cloudy images in --extra-years
TILE_CLOUD_PROP = {"sentinel2": "CLOUDY_PIXEL_PERCENTAGE", "landsat_89": "CLOUD_COVER", "landsat_45": "CLOUD_COVER"}

NULL_SITE_RE = re.compile(r"negative example|\b0% positive", re.I)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
FILE_RE = re.compile(r"_channel_(-?[\d.]+)_(-?[\d.]+)\.nc$")


# ── Calibration scenes ────────────────────────────────────────────────────────

def parse_calibration(path: Path) -> list:
    """Calibration entries that carry a date in their comment (others are skipped)."""
    entries, site = [], None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#"):
            site = line.lstrip("#").strip() or site
            continue
        body, _, note = line.partition("#")
        parts = body.split()
        m = DATE_RE.search(note)
        if len(parts) < 4 or not m:
            continue
        entries.append(dict(
            instr=parts[0], clat=float(parts[1]), clon=float(parts[2]), acq_idx=int(parts[3]),
            date=f"{m[1]}-{m[2]}-{m[3]}", site=site or "unknown",
            group="null site" if site and NULL_SITE_RE.search(site) else "emitter site",
            ref=note.partition("|")[2].strip(),   # e.g. a reference label from mars_targets.py
        ))
    return entries


def local_originals(imagery: Path, instr: str) -> list:
    """[(clat, clon)] parsed from original file names (no file reads)."""
    out = []
    for f in (imagery / instr).glob("*.nc"):
        m = FILE_RE.search(f.name)
        if m:
            out.append((float(m[1]), float(m[2])))
    return out


def has_original(e: dict, originals: list) -> bool:
    return any(abs(e["clat"] - a) < 0.01 and abs(e["clon"] - b) < 0.01 for a, b in originals)


def choose_subset(entries: list, per_sensor: int, imagery: Path, rng: random.Random) -> list:
    """Per sensor: one scene per site, at least one null site, prefer sites with a local original."""
    chosen = []
    for instr in SENSORS:
        originals = local_originals(imagery, instr)
        by_site = defaultdict(list)
        for e in entries:
            if e["instr"] == instr:
                by_site[e["site"]].append(e)
        sites = list(by_site)
        rng.shuffle(sites)
        null_sites = [s for s in sites if by_site[s][0]["group"] == "null site"]
        ordered = null_sites[:1] + sorted(
            (s for s in sites if s not in null_sites[:1]),
            key=lambda s: not any(has_original(e, originals) for e in by_site[s]),
        )
        for s in ordered[:per_sensor]:
            cands = [e for e in by_site[s] if has_original(e, originals)] or by_site[s]
            chosen.append(rng.choice(cands))
    return chosen


# ── Earth Engine ──────────────────────────────────────────────────────────────

def utm_epsg(lat: float, lon: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def chip_grid(lat: float, lon: float, res: float, half_km: float) -> dict:
    """computePixels grid: square UTM chip of 2*half_km centred on (lat, lon)."""
    epsg = utm_epsg(lat, lon)
    cx, cy = ee.Geometry.Point([lon, lat]).transform(f"EPSG:{epsg}", 0.01).coordinates().getInfo()
    n = int(round(2 * half_km * 1000 / res))
    return dict(
        dimensions=dict(width=n, height=n),
        affineTransform=dict(scaleX=res, shearX=0, translateX=cx - n * res / 2,
                             shearY=0, scaleY=-res, translateY=cy + n * res / 2),
        crsCode=f"EPSG:{epsg}",
    )


def sensor_collection(instr: str, point, tier2: bool = False) -> ee.ImageCollection:
    cfg = SENSORS[instr]
    ids = cfg["tier2"] if tier2 else cfg["collections"]
    col = ee.ImageCollection(ids[0])
    for c in ids[1:]:
        col = col.merge(ee.ImageCollection(c))
    col = col.filterBounds(point)
    if instr == "sentinel2":
        csp = ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED")
        col = col.linkCollection(csp, ["cs_cdf"])
    return col


def build_image(instr: str, day_col: ee.ImageCollection) -> ee.Image:
    """Mosaic one day's images → raw, normalized and cloud bands."""
    cfg, h = SENSORS[instr], HARMONIZATION[instr]
    img = day_col.mosaic()
    raw = img.select(cfg["bands"]).multiply(cfg["scale"]).rename(BAND_NAMES).toFloat()
    norm = (raw.multiply(ee.Image.constant(h["slope"]))
               .add(ee.Image.constant(h["offset"]))
               .rename([f"n_{b}" for b in BAND_NAMES]).toFloat())
    if instr == "sentinel2":
        cloud = img.select("cs_cdf").lt(CS_CLEAR_THRESHOLD)
    else:
        cloud = img.select("QA_PIXEL").bitwiseAnd(LANDSAT_CLOUD_BITS).neq(0)
    cloud = cloud.rename("cloud").toFloat()
    return raw.addBands(norm).addBands(cloud).unmask(NODATA, False)


def year_dates(col: ee.ImageCollection, year: int) -> list:
    """Sorted unique UTC acquisition datetimes (first image per day) in a year."""
    ts = col.filterDate(f"{year}-01-01", f"{year + 1}-01-01").aggregate_array("system:time_start").getInfo()
    by_day = {}
    for t in sorted(ts):
        dt = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        by_day.setdefault(dt.date(), dt)
    return [by_day[d] for d in sorted(by_day)]


def thin(dates: list, keep: set, max_n: int) -> list:
    """Evenly thin a date list to max_n, always keeping dates in `keep`."""
    if len(dates) <= max_n:
        return dates
    idx = np.linspace(0, len(dates) - 1, max_n).round().astype(int)
    picked = {dates[i] for i in idx} | {d for d in dates if d.date() in keep}
    return sorted(picked)


def fetch_day(instr: str, col, dt: datetime, grid: dict, retries: int = 5) -> np.ndarray:
    day = ee.Date(dt.strftime("%Y-%m-%d"))
    img = build_image(instr, col.filterDate(day, day.advance(1, "day")))
    for attempt in range(retries):
        try:
            arr = ee.data.computePixels(dict(expression=img, fileFormat="NUMPY_NDARRAY", grid=grid))
            # structured array (H, W) with one field per band → (H, W, 13)
            out = np.stack([arr[name] for name in arr.dtype.names], axis=-1).astype(np.float32)
            out[out == NODATA] = np.nan
            return out
        except ee.EEException as e:
            if attempt == retries - 1:
                raise
            print(f"    retry {dt.date()}: {str(e)[:80]}")
            time.sleep(2 ** attempt * 3)


# ── Output ────────────────────────────────────────────────────────────────────

def coord_str(x: float) -> str:
    return f"{round(x, 4)}"


REFL_SCALE = 1e-4      # reflectance stored as int16 counts of 1e-4 (Sentinel-2 L1C's own quantum)
REFL_FILL = -32768
REFL_MAX = 32767 * REFL_SCALE   # largest storable reflectance, 3.2767
CLOUD_FILL = 255


def write_nc(path: Path, channels, cloud, dates, clat, clon, res, attrs: dict):
    """Write one site's stack. Reflectance is packed to int16 (max error 5e-5) and the
    cloud mask to uint8, chunked one acquisition per chunk; readers that apply
    scale_factor/_FillValue (netCDF4, xarray) get float reflectance with NaN gaps back.
    Reflectance above REFL_MAX (e.g. hot gas flares in SWIR) is capped at REFL_MAX;
    values below -REFL_MAX are not physical and are stored as missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n, H, W, _ = channels.shape
    with nc.Dataset(path, "w") as ds:
        ds.createDimension("acquisitions", n)
        ds.createDimension("lat", H)
        ds.createDimension("lon", W)
        ds.createDimension("bands", 6)
        ds.createDimension("info", 1)
        v = ds.createVariable("channels", "i2", ("acquisitions", "lat", "lon", "bands"), zlib=True,
                              shuffle=True, chunksizes=(1, H, W, 6), fill_value=REFL_FILL)
        v.scale_factor = np.float32(REFL_SCALE)
        v.add_offset = np.float32(0.0)
        v.set_auto_scale(False)
        packed = np.round(np.minimum(channels, REFL_MAX) / REFL_SCALE)
        v[:] = np.where(np.isfinite(channels) & (channels >= -REFL_MAX), packed, REFL_FILL).astype("i2")
        c = ds.createVariable("cloud_mask", "u1", ("acquisitions", "lat", "lon"), zlib=True,
                              chunksizes=(1, H, W), fill_value=CLOUD_FILL)
        c.set_auto_scale(False)
        c[:] = np.where(np.isfinite(cloud), np.round(cloud), CLOUD_FILL).astype("u1")
        for name, val in (("clat", clat), ("clon", clon), ("resolution", res)):
            ds.createVariable(name, "f4", ("info",))[:] = val
        for name, vals in (("year", [d.year for d in dates]), ("month", [d.month for d in dates]),
                           ("day", [d.day for d in dates]),
                           ("hour", [d.hour + d.minute / 60 for d in dates])):
            ds.createVariable(name, "f4", ("acquisitions",))[:] = vals
        ds.setncatts({k: str(v) for k, v in attrs.items()})


def existing_days(path: Path):
    """Acquisition dates in an existing output file, or None if it can't be read."""
    try:
        with nc.Dataset(path) as ds:
            y, m, d = (ds.variables[k][:].astype(int) for k in ("year", "month", "day"))
            cloud = ds.variables["cloud_mask"][:]
        return [datetime(a, b, c).date() for a, b, c in zip(y, m, d)], cloud
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=DEFAULT_PROJECT, help=f"GEE cloud project (default: {DEFAULT_PROJECT})")
    ap.add_argument("--calibration", default="calibration_scenes.txt", type=Path)
    ap.add_argument("--imagery", default="imagery", type=Path,
                    help="original NetCDF imagery, used to prefer sites with a local original")
    ap.add_argument("--out", default="imagery_gee", type=Path)
    ap.add_argument("--all", action="store_true", help="pull every dated calibration scene")
    ap.add_argument("--per-sensor", default=4, type=int, help="subset size per sensor without --all (default: 4)")
    ap.add_argument("--max-per-year", default=30, type=int,
                    help="max acquisitions pulled per site-year as background for the ΔdR layer (default: 30)")
    ap.add_argument("--half-km", default=2.0, type=float, help="chip half-width in km (default: 2.0)")
    ap.add_argument("--workers", default=8, type=int, help="parallel download requests (default: 8)")
    ap.add_argument("--seed", default=0, type=int)
    ap.add_argument("--extra-years", default="",
                    help="also pull these years for each site, e.g. 2017-2025 or 2018,2020 (more plume chances)")
    ap.add_argument("--max-tile-cloud", default=40.0, type=float,
                    help="with --extra-years: skip extra-year images whose scene cloud %% exceeds this (default: 40)")
    ap.add_argument("--emitters-only", action="store_true", help="only emitter sites (skip null sites)")
    ap.add_argument("--sensors", default="", help="comma-separated subset, e.g. sentinel2,landsat_89")
    args = ap.parse_args()
    extra_years = set()
    for part in filter(None, args.extra_years.split(",")):
        a, _, b = part.partition("-")
        extra_years |= set(range(int(a), int(b or a) + 1))

    ee.Initialize(project=args.project)

    entries = parse_calibration(args.calibration)
    scenes = entries if args.all else choose_subset(entries, args.per_sensor, args.imagery,
                                                    random.Random(args.seed))
    if args.emitters_only:
        scenes = [e for e in scenes if e["group"] == "emitter site"]
    if args.sensors:
        scenes = [e for e in scenes if e["instr"] in args.sensors.split(",")]
    print(f"Scenes: {len(scenes)}" + (f", extra years {min(extra_years)}-{max(extra_years)}" if extra_years else ""))

    by_loc = defaultdict(list)
    for e in scenes:
        by_loc[(e["instr"], e["clat"], e["clon"])].append(e)

    manifest = dict(project=args.project, half_km=args.half_km, max_per_year=args.max_per_year,
                    extra_years=sorted(extra_years), max_tile_cloud=args.max_tile_cloud if extra_years else None,
                    cloud_mask=dict(sentinel2=f"Cloud Score+ cs_cdf < {CS_CLEAR_THRESHOLD}",
                                    landsat="QA_PIXEL bits 1-4 (dilated cloud, cirrus, cloud, shadow)"),
                    sensors={}, scenes=[], missing=[])
    for instr, cfg in SENSORS.items():
        manifest["sensors"][instr] = dict(collections=cfg["collections"], tier2_fallback=cfg["tier2"],
                                          bands=cfg["bands"], res_m=cfg["res"],
                                          harmonization=HARMONIZATION[instr])
    cal_lines = []

    for n_loc, ((instr, clat, clon), targets) in enumerate(by_loc.items(), 1):
        cfg = SENSORS[instr]
        name = f"{cfg['prefix']}_channel_{coord_str(clat)}_{coord_str(clon)}.nc"
        raw_path = args.out / "raw" / instr / name
        norm_path = args.out / "normalized" / instr / name
        target_days = {datetime.strptime(t["date"], "%Y-%m-%d").date() for t in targets}
        print(f"\n[{n_loc}/{len(by_loc)}] {instr} {clat}, {clon}  ({targets[0]['site']})")

        prev = existing_days(raw_path) if norm_path.exists() else None
        want_years = {d.year for d in target_days} | extra_years
        if prev and target_days <= set(prev[0]) and want_years <= {d.year for d in prev[0]}:
            day_list, cloud = prev[0], prev[1]
            print("  already pulled, skipping download")
        else:
            point = ee.Geometry.Point([clon, clat])
            col = sensor_collection(instr, point)
            grid = chip_grid(clat, clon, cfg["res"], args.half_km)

            jobs = []   # (datetime, collection)
            clear_col = col.filter(ee.Filter.lte(TILE_CLOUD_PROP[instr], args.max_tile_cloud))
            for year in sorted(want_years):
                if year not in {d.year for d in target_days}:
                    # Extra year: only reasonably clear images, no target dates to keep
                    jobs += [(d, col) for d in thin(year_dates(clear_col, year), set(), args.max_per_year)]
                    continue
                t1 = year_dates(col, year)
                jobs += [(d, col) for d in thin(t1, target_days, args.max_per_year)]
                for m in sorted(d for d in target_days - {d.date() for d in t1} if d.year == year):
                    found = None
                    if cfg["tier2"]:
                        col2 = sensor_collection(instr, point, tier2=True)
                        found = next((d for d in year_dates(col2, year) if d.date() == m), None)
                    if found:
                        print(f"  target {m} not in Tier 1, using Tier 2")
                        jobs.append((found, col2))
                    else:
                        print(f"  [warn] no {instr} image on target date {m}")
            if not jobs:
                print("  [skip] no images found")
                continue
            jobs.sort(key=lambda j: j[0])
            dates = [d for d, _ in jobs]

            print(f"  downloading {len(dates)} acquisitions on {grid['crsCode']} "
                  f"({grid['dimensions']['width']} px at {cfg['res']} m)")
            with ThreadPoolExecutor(args.workers) as pool:
                arrays = list(pool.map(lambda j: fetch_day(instr, j[1], j[0], grid), jobs))
            stack = np.stack(arrays)                        # (n, H, W, 13)
            raw, norm, cloud = stack[..., :6], stack[..., 6:12], stack[..., 12]

            attrs = dict(source="Google Earth Engine", collections=",".join(cfg["collections"]),
                         tier2_fallback=",".join(cfg["tier2"]) or "none",
                         bands=",".join(cfg["bands"]), processing="TOA reflectance",
                         crs=grid["crsCode"], cloud_mask=manifest["cloud_mask"][
                             "sentinel2" if instr == "sentinel2" else "landsat"] + "; 1 cloud, 0 clear",
                         created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            write_nc(raw_path, raw, cloud, dates, clat, clon, cfg["res"], dict(attrs, normalization="none"))
            write_nc(norm_path, norm, cloud, dates, clat, clon, cfg["res"],
                     dict(attrs, normalization=HARMONIZATION[instr]["source"]))
            day_list = [d.date() for d in dates]

        for t in targets:
            day = datetime.strptime(t["date"], "%Y-%m-%d").date()
            if day not in day_list:
                manifest["missing"].append(t)
                continue
            idx = day_list.index(day)
            cf = float(np.nanmean(cloud[idx]))
            cal_lines.append(f"{instr:11s} {clat:9.4f} {clon:9.4f} {idx:5d}    "
                             f"# {t['date']} | orig_acq={t['acq_idx']} | {t['group']} | {t['site']}"
                             + (f" | ref: {t['ref']}" if t["ref"] else ""))
            manifest["scenes"].append(dict(t, gee_acq_idx=idx, file=name, n_acquisitions=len(day_list),
                                           cloud_frac=cf))
            print(f"  target {t['date']} -> acq {idx}, cloud fraction {cf:.2f}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "calibration_gee.txt").write_text(
        "# GEE calibration scenes (written by gee_fetch.py)\n"
        "# instrument  clat  clon  acq_idx  # date | original acq_idx | group | site\n"
        + "\n".join(cal_lines) + "\n")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print(f"\nWrote {len(cal_lines)} target scenes to {args.out.resolve()}"
          f"  ({len(manifest['missing'])} target dates not found)")


if __name__ == "__main__":
    sys.exit(main())
