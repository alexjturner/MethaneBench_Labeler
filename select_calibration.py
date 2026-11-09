#!/usr/bin/env python
"""
Pick calibration scenes with known answers from the MARS-S2L pull (each date
checked by a UNEP IMEO analyst as plume or no plume), in three tiers:

    obvious   MARS plume with flux >= --obvious-kgh (default 10,000 kg/h)
    harder    MARS plume with flux <= --harder-kgh  (default 2,000 kg/h, near the
              Sentinel-2/Landsat detection limit)
    no plume  MARS no-plume date; half at sites MARS has seen emit, half at
              sites with no MARS plumes

Scenes are drawn at random (--seed) from the MARS labels and chip cloud mask
only; the Methane/dR layers are not used, so the choice does not depend on what
our own layers show. Within a tier: one scene per site, countries taken in turn,
about --landsat-frac Landsat 8/9, chips with more than --max-cloud cloudy pixels
skipped. Order served: obvious first, then harder and no plume shuffled together.

Output, in the calibration list format with the expected answer in the comment
("# date | expected | why"), acq_idx into <pull>/normalized:

    <out>                 e.g. <pull>/calibration_mars.txt

Usage:
    pixi run python select_calibration.py "/Volumes/T7 Shield/MethaneBench/mars_s2l/imagery" \\
        --out "/Volumes/T7 Shield/MethaneBench/mars_s2l/imagery/calibration_mars.txt"
"""

import argparse, random, re
from collections import defaultdict
from pathlib import Path

import numpy as np
import netCDF4 as nc

CLOUD_THRESH = 0.5   # cloud_mask value counted as cloudy (as app.py)


def parse(pull: Path) -> list:
    rows = []
    for line in (pull / "calibration_gee.txt").read_text().splitlines():
        body, _, note = line.partition("#")
        p = body.split()
        if len(p) < 4:
            continue
        site = re.search(r"MARS (.+?) \(([^,]+), ([^,]+),", note)
        flux = re.search(r"ref: MARS plume (\d+)", note)
        rows.append(dict(instr=p[0], clat=float(p[1]), clon=float(p[2]), acq=int(p[3]),
                         date=note.split("|")[0].strip(), site=site.group(1), country=site.group(2),
                         sector=site.group(3), null_site="null site" in note,
                         flux=int(flux.group(1)) if flux else None))
    return rows


def chip_file(pull: Path, r: dict, cache={}) -> Path:
    key = (r["instr"], r["clat"], r["clon"])
    if key not in cache:
        f = pull / "normalized" / r["instr"] / f"{r['instr']}_channel_{r['clat']:g}_{r['clon']:g}.nc"
        if not f.exists():
            f = next(x for x in (pull / "normalized" / r["instr"]).glob("*.nc")
                     if abs(float(nc.Dataset(x)["clat"][0]) - r["clat"]) < 1e-3
                     and abs(float(nc.Dataset(x)["clon"][0]) - r["clon"]) < 1e-3)
        cache[key] = f
    return cache[key]


def cloud_frac(pull: Path, r: dict) -> float:
    with nc.Dataset(chip_file(pull, r)) as ds:
        cm = np.ma.filled(ds["cloud_mask"][r["acq"]].astype(float), np.nan)
    return float(np.mean(cm >= CLOUD_THRESH))


def draw(pool: list, n: int, landsat_frac: float, pull: Path, max_cloud: float, rng, used: set) -> list:
    """n scenes, one per site, countries in turn, ~landsat_frac Landsat."""
    out = []
    n_l = round(n * landsat_frac)
    for instr, k in (("sentinel2", n - n_l), ("landsat_89", n_l)):
        by_country = defaultdict(list)
        for r in pool:
            if r["instr"] == instr:
                by_country[r["country"]].append(r)
        for v in by_country.values():
            rng.shuffle(v)
        countries = sorted(by_country)
        rng.shuffle(countries)
        got = 0
        while got < k and any(by_country.values()):
            for c in countries:
                if got >= k:
                    break
                while by_country[c]:
                    r = by_country[c].pop()
                    if r["site"] in used or cloud_frac(pull, r) > max_cloud:
                        continue
                    out.append(r); used.add(r["site"]); got += 1
                    break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pull", type=Path, help="MARS-S2L pull (calibration_gee.txt + normalized/)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", default=15, type=int, help="scenes per tier (default: 15)")
    ap.add_argument("--obvious-kgh", default=10000, type=int)
    ap.add_argument("--harder-kgh", default=2000, type=int)
    ap.add_argument("--landsat-frac", default=1 / 3, type=float)
    ap.add_argument("--max-cloud", default=0.1, type=float, help="max cloudy fraction of the chip (default: 0.1)")
    ap.add_argument("--seed", default=0, type=int)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    rows = parse(args.pull)

    tiers = {}
    tiers["obvious"] = draw([r for r in rows if r["flux"] and r["flux"] >= args.obvious_kgh],
                            args.n, args.landsat_frac, args.pull, args.max_cloud, rng, set())
    tiers["harder"] = draw([r for r in rows if r["flux"] and r["flux"] <= args.harder_kgh],
                           args.n, args.landsat_frac, args.pull, args.max_cloud, rng, set())
    used = set()
    tiers["no plume"] = (draw([r for r in rows if r["flux"] is None and not r["null_site"]],
                              args.n - args.n // 2, args.landsat_frac, args.pull, args.max_cloud, rng, used)
                         + draw([r for r in rows if r["flux"] is None and r["null_site"]],
                                args.n // 2, args.landsat_frac, args.pull, args.max_cloud, rng, used))

    rest = [("harder", r) for r in tiers["harder"]] + [("no plume", r) for r in tiers["no plume"]]
    rng.shuffle(rest)
    order = [("obvious", r) for r in tiers["obvious"]] + rest

    lines = ["# Calibration scenes with known answers from MARS-S2L (UNEP IMEO analyst labels; CC BY-NC-SA 4.0).",
             f"# Written by select_calibration.py (seed {args.seed}); acq_idx refers to {args.pull.name}/normalized.",
             f"# Tiers: obvious = plume >= {args.obvious_kgh} kg/h; harder = plume <= {args.harder_kgh} kg/h; "
             "no plume = MARS no-plume date.",
             "# Served obvious first, then harder and no plume shuffled together.",
             "# instrument  clat  clon  acq_idx  # date | expected | why"]
    for tier, r in order:
        why = (f"{tier}: MARS plume {r['flux'] / 1000:.1f} t/h" if r["flux"] else
               f"no plume: MARS no-plume date at a {'site with no MARS plumes' if r['null_site'] else 'MARS emitter site'}")
        lines.append(f"{r['instr']:<12s} {r['clat']:9.4f} {r['clon']:9.4f} {r['acq']:5d}    # {r['date']} | "
                     f"{'no plume' if tier == 'no plume' else 'plume'} | {why}; MARS {r['site']} ({r['country']}, {r['sector']})")
    args.out.write_text("\n".join(lines) + "\n")
    for t, v in tiers.items():
        print(f"{t:9s} {len(v):3d}  S2 {sum(r['instr'] == 'sentinel2' for r in v)}  "
              f"countries {len({r['country'] for r in v})}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
