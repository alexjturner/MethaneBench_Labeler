#!/usr/bin/env python
"""
Rewrite GEE NetCDF files in the compact layout used by gee_fetch.write_nc:
reflectance as int16 (scale 1e-4, max error 5e-5), cloud mask as uint8, one
chunk per acquisition. Values, NaN gaps, dates and attributes are kept.

Writes to a new folder tree and verifies every file; never touches the source.

Usage:
    pixi run python compact_nc.py imagery_gee_extra imagery_gee_extra_compact
"""

import argparse, sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import netCDF4 as nc

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from gee_fetch import write_nc, REFL_SCALE, REFL_MAX   # noqa: E402


def compact(src: Path, dst: Path) -> tuple:
    with nc.Dataset(src) as ds:
        ch = np.ma.filled(ds["channels"][:].astype(np.float32), np.nan)
        cloud = np.ma.filled(ds["cloud_mask"][:].astype(np.float32), np.nan)
        y, m, d, h = (ds[k][:].astype(float) for k in ("year", "month", "day", "hour"))
        clat, clon, res = (float(ds[k][0]) for k in ("clat", "clon", "resolution"))
        attrs = {k: ds.getncattr(k) for k in ds.ncattrs()}
    dates = [datetime(int(a), int(b), int(c)) + timedelta(minutes=round(hh * 60))
             for a, b, c, hh in zip(y, m, d, h)]
    n_out = int(np.sum(np.abs(ch) > REFL_MAX))
    if n_out:
        attrs["out_of_range_values"] = f"{n_out} reflectance values beyond +-{REFL_MAX:.4f} capped (high) or set missing (low)"
    write_nc(dst, ch, cloud, dates, clat, clon, res, attrs)
    ch = np.where(ch < -REFL_MAX, np.nan, np.minimum(ch, REFL_MAX))   # what the file should now hold
    with nc.Dataset(dst) as ds:   # verify
        back = np.ma.filled(ds["channels"][:].astype(np.float32), np.nan)
        cback = np.ma.filled(ds["cloud_mask"][:].astype(np.float32), np.nan)
    ok = (np.array_equal(np.isnan(back), np.isnan(ch))
          and np.nanmax(np.abs(back - ch), initial=0) <= REFL_SCALE / 2 + 1e-6
          and np.array_equal(np.isnan(cback), np.isnan(cloud))
          and np.nanmax(np.abs(cback - cloud), initial=0) == 0)
    return ok, src.stat().st_size, dst.stat().st_size, n_out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    args = ap.parse_args()
    files = sorted(args.src.rglob("*.nc"))
    before = after = 0
    bad = []
    for i, f in enumerate(files, 1):
        out = args.dst / f.relative_to(args.src)
        ok, a, b, n_out = compact(f, out)
        before += a; after += b
        if not ok:
            bad.append(f)
        print(f"[{i}/{len(files)}] {f.relative_to(args.src)}  {a / 1e6:.0f} -> {b / 1e6:.0f} MB"
              + (f"  ({n_out} out-of-range values)" if n_out else "")
              + ("" if ok else "  VERIFY FAILED"), flush=True)
    for f in args.src.glob("*"):   # copy lists and manifests alongside
        if f.is_file() and f.suffix in (".txt", ".json"):
            (args.dst / f.name).write_bytes(f.read_bytes())
    print(f"\n{len(files)} files: {before / 1e9:.2f} GB -> {after / 1e9:.2f} GB"
          f"  ({len(bad)} failed verification)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
