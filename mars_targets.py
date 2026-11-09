#!/usr/bin/env python
"""
Build a labeling target list from the MARS-S2L dataset (UNEP IMEO), whose
Sentinel-2 and Landsat 8/9 images were each checked by an analyst as plume or
no plume:

    Mateo-Garcia, Allen, Irakulis-Loitxate et al. 2025, "Artificial intelligence
    for methane detection: from continuous monitoring to verified mitigation",
    arXiv:2511.21777.  Data: huggingface.co/datasets/UNEP-IMEO/MARS-S2L
    (validated_images_all.csv, CC BY-NC-SA 4.0).

Onshore MARS locations closer than --merge-km are grouped into one chip site.
For each site and sensor, targets come from a single year (the year with most
plumes) so gee_fetch only pulls one year of background per sensor:

    plume sites     up to --k-plume plume dates + --k-clear no-plume dates
    no-plume sites  a sample of --null-sites sites (spread over countries),
                    --k-clear no-plume dates each

Only images MARS marked "clear" are used. The output is in the calibration
list format gee_fetch.py reads (acq_idx -1, MARS label in the comment), plus a
site table:

    <out>/mars_targets.txt
    <out>/mars_sites.csv

Usage:
    python mars_targets.py validated_images_all.csv --out mars_targets
    pixi run python gee_fetch.py --calibration mars_targets/mars_targets.txt --all --out imagery_mars
"""

import argparse, csv, math, random
from collections import defaultdict
from pathlib import Path

SENSOR = {"S2A": "sentinel2", "S2B": "sentinel2", "S2C": "sentinel2", "LC08": "landsat_89", "LC09": "landsat_89"}


def km(a, b):
    return math.hypot((a["lat"] - b["lat"]) * 111.0,
                      (a["lon"] - b["lon"]) * 111.0 * math.cos(math.radians(a["lat"])))


def spread(items: list, k: int) -> list:
    """k items evenly spaced through a sorted list."""
    if len(items) <= k:
        return items
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)] if k > 1 else [items[len(items) // 2]]


def load_sites(path: Path, merge_km: float) -> list:
    csv.field_size_limit(10 ** 9)
    locs = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["offshore"] == "False" and r["split_name"] != "Not Used" and r["satellite"] in SENSOR:
                locs[r["id_location"]].append(r)
    locs = [dict(lat=float(v[0]["lat"]), lon=float(v[0]["lon"]), rows=v,
                 n_plume=sum(r["isplume"] == "True" for r in v)) for v in locs.values()]
    locs.sort(key=lambda l: -l["n_plume"])   # the strongest emitter anchors each site
    sites = []
    for l in locs:
        s = next((s for s in sites if km(s, l) < merge_km), None)
        if s:
            s["members"].append(l)
        else:
            sites.append(dict(lat=l["lat"], lon=l["lon"], members=[l]))
    for s in sites:
        first = s["members"][0]["rows"][0]
        s.update(names=",".join(m["rows"][0]["location_name"] for m in s["members"]),
                 country=first["country"], sector=first["sector"], case_study=first.get("case_study", ""))
        imgs = {}   # (sensor, date) -> (is_plume, flux kg/h); a plume at any member counts
        for m in s["members"]:
            for r in m["rows"]:
                if r["observability"] != "clear":
                    continue
                key = (SENSOR[r["satellite"]], r["tile_date"][:10])
                plume = r["isplume"] == "True"
                flux = float(r["ch4_fluxrate"] or 0) if plume else 0.0
                old = imgs.get(key, (False, 0.0))
                imgs[key] = (old[0] or plume, max(old[1], flux))
        s["imgs"] = imgs
        s["n_plume"] = sum(p for p, _ in imgs.values())
    return sites


def pick_targets(s: dict, k_plume: int, k_clear: int, rng: random.Random) -> list:
    out = []
    for sensor in ("sentinel2", "landsat_89"):
        imgs = {d: v for (sn, d), v in s["imgs"].items() if sn == sensor}
        if not imgs:
            continue
        by_year = defaultdict(lambda: [0, 0])
        for d, (p, _) in imgs.items():
            by_year[d[:4]][0] += p
            by_year[d[:4]][1] += 1
        year = max(by_year, key=lambda y: (by_year[y][0], by_year[y][1] >= k_clear, y))
        dates = sorted(d for d in imgs if d.startswith(year))
        plumes = spread([d for d in dates if imgs[d][0]], k_plume) if k_plume else []
        clear = [d for d in dates if not imgs[d][0]]
        clear = sorted(rng.sample(clear, min(k_clear, len(clear))))
        out += [(sensor, d, imgs[d]) for d in sorted(plumes + clear)]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="MARS-S2L validated_images_all.csv")
    ap.add_argument("--out", default="mars_targets", type=Path)
    ap.add_argument("--merge-km", default=1.5, type=float, help="group MARS locations closer than this (default: 1.5)")
    ap.add_argument("--k-plume", default=2, type=int, help="plume dates per site and sensor (default: 2)")
    ap.add_argument("--k-clear", default=2, type=int, help="no-plume dates per site and sensor (default: 2)")
    ap.add_argument("--plume-sites", default=0, type=int, help="limit plume sites, spread over countries (0 = all)")
    ap.add_argument("--null-sites", default=100, type=int, help="no-plume sites to sample (default: 100)")
    ap.add_argument("--seed", default=0, type=int)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    sites = load_sites(args.csv, args.merge_km)

    def by_country(group: list, n: int) -> list:
        """Round-robin over countries so no single country dominates the sample."""
        if not n or n >= len(group):
            return group
        pools = defaultdict(list)
        for s in group:
            pools[s["country"]].append(s)
        for p in pools.values():
            rng.shuffle(p)
        picked = []
        while len(picked) < n:
            for c in sorted(pools, key=lambda c: -len(pools[c])):
                if pools[c] and len(picked) < n:
                    picked.append(pools[c].pop())
        return picked

    plume_sites = by_country(sorted((s for s in sites if s["n_plume"]), key=lambda s: -s["n_plume"]),
                             args.plume_sites)
    null_sites = by_country([s for s in sites if not s["n_plume"]], args.null_sites)

    args.out.mkdir(parents=True, exist_ok=True)
    lines = ["# MARS-S2L labeling targets (written by mars_targets.py)",
             "# Source: Mateo-Garcia et al. 2025, arXiv:2511.21777; huggingface.co/datasets/UNEP-IMEO/MARS-S2L",
             "# instrument  clat  clon  acq_idx(-1)  # date | MARS reference label", ""]
    rows, n_t = [], {"plume": 0, "no plume": 0}
    for group, ss, k_plume in (("plume site", plume_sites, args.k_plume), ("negative example", null_sites, 0)):
        for s in ss:
            targets = pick_targets(s, k_plume, args.k_clear, rng)
            if not targets:
                continue
            lines.append(f"# MARS {s['names']} ({s['country']}, {s['sector']}, {s['n_plume']} MARS plumes)"
                         + (" negative example" if group == "negative example" else ""))
            for sensor, d, (p, flux) in targets:
                ref = f"MARS plume {flux:.0f} kg/h" if p else "MARS no plume"
                n_t["plume" if p else "no plume"] += 1
                lines.append(f"{sensor:11s} {s['lat']:9.4f} {s['lon']:9.4f} {-1:5d}    # {d} | {ref}")
            lines.append("")
            rows.append(dict(site=s["names"], group=group, lat=round(s["lat"], 5), lon=round(s["lon"], 5),
                             country=s["country"], sector=s["sector"], case_study=s["case_study"],
                             mars_plumes=s["n_plume"], mars_clear_images=len(s["imgs"]),
                             targets=";".join(f"{sn}:{d}" for sn, d, _ in targets)))
    (args.out / "mars_targets.txt").write_text("\n".join(lines) + "\n")
    with open(args.out / "mars_sites.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    n_sites = {g: sum(r["group"] == g for r in rows) for g in ("plume site", "negative example")}
    print(f"{n_sites['plume site']} plume sites, {n_sites['negative example']} no-plume sites; "
          f"{n_t['plume']} plume targets, {n_t['no plume']} no-plume targets -> {args.out.resolve()}")


if __name__ == "__main__":
    main()
