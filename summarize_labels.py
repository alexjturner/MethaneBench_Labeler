"""
summarize_labels.py — Quick summary and CSV export of label data

Usage:
    python summarize_labels.py                             # read local labels.db
    python summarize_labels.py --csv                       # also write labels.csv
    python summarize_labels.py --db /path/to/labels.db    # custom local db
    python summarize_labels.py --url https://bench4.atmos.uw.edu  # fetch from server
    python summarize_labels.py --url https://bench4.atmos.uw.edu --csv
"""

import argparse
import csv
import json
import os
import sqlite3
import urllib.request
from collections import defaultdict
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_DB  = os.path.join(os.path.dirname(__file__), "labels.db")
DEFAULT_CSV = os.path.join(os.path.dirname(__file__), "labels.csv")

EXPERTISE = {1: "Methane researcher", 2: "GHG scientist", 3: "Other"}

# ── Data loading ──────────────────────────────────────────────────────────────

def load_from_db(db_path):
    """Load labels and users from a local SQLite file."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    labels = conn.execute("""
        SELECT l.*, u.email, u.expertise
        FROM labels l
        LEFT JOIN users u ON l.user_id = u.id
        ORDER BY l.created_at
    """).fetchall()
    users = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    conn.close()
    # Convert to plain dicts for uniform handling
    return [dict(r) for r in labels], [dict(r) for r in users]


def load_from_url(base_url):
    """Fetch labels from the /api/export/labels endpoint."""
    base_url = base_url.rstrip("/")
    url = f"{base_url}/api/export/labels"
    print(f"  Fetching from {url} ...")
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    # Build user lookup for expertise/email
    user_map = {u["id"]: u for u in data["users"]}

    labels = []
    for r in data["labels"]:
        u = user_map.get(r["user_id"], {})
        r["email"]    = u.get("email", "")
        r["expertise"] = u.get("expertise", "")
        labels.append(r)

    return labels, data["users"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def n_polygons(polygons_json):
    if isinstance(polygons_json, list):
        return len(polygons_json)
    try:
        return len(json.loads(polygons_json))
    except Exception:
        return 0


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(labels, users):
    if not labels:
        print("No labels yet.")
        return

    n_labels    = len(labels)
    n_users     = len(users)
    n_plume     = sum(1 for r in labels if not r["no_plume"] and n_polygons(r["polygons"]) > 0)
    n_no_plume  = sum(1 for r in labels if r["no_plume"])
    n_cal       = sum(1 for r in labels if r["is_calibration"])
    n_scenes    = len({r["scene_id"] for r in labels})
    total_polys = sum(n_polygons(r["polygons"]) for r in labels)

    print("=" * 52)
    print("  BENCH₄ Label Summary")
    print(f"  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 52)
    print(f"  Annotators           : {n_users}")
    print(f"  Total labels         : {n_labels}")
    print(f"    Plume detected     : {n_plume}  ({100*n_plume/n_labels:.1f}%)")
    print(f"    No plume           : {n_no_plume}  ({100*n_no_plume/n_labels:.1f}%)")
    print(f"    Calibration scenes : {n_cal}")
    print(f"  Unique scenes labeled: {n_scenes}")
    print(f"  Total polygons drawn : {total_polys}")

    # Per-instrument breakdown
    by_instr = defaultdict(lambda: {"n": 0, "plume": 0, "polys": 0})
    for r in labels:
        instr = r["instrument"]
        by_instr[instr]["n"]     += 1
        by_instr[instr]["polys"] += n_polygons(r["polygons"])
        if not r["no_plume"] and n_polygons(r["polygons"]) > 0:
            by_instr[instr]["plume"] += 1

    print()
    print("  By instrument:")
    for instr, d in sorted(by_instr.items()):
        print(f"    {instr:<15} {d['n']:4d} labels  "
              f"{d['plume']:4d} plumes  {d['polys']:4d} polygons")

    # Per-annotator breakdown
    by_user = defaultdict(lambda: {"n": 0, "plume": 0, "cal": 0, "first": "", "last": ""})
    for r in labels:
        uid = r["user_id"]
        by_user[uid]["n"]   += 1
        by_user[uid]["cal"] += int(bool(r["is_calibration"]))
        if not r["no_plume"] and n_polygons(r["polygons"]) > 0:
            by_user[uid]["plume"] += 1
        ts = r["created_at"]
        if not by_user[uid]["first"] or ts < by_user[uid]["first"]:
            by_user[uid]["first"] = ts
        if ts > by_user[uid]["last"]:
            by_user[uid]["last"] = ts

    print()
    print("  By annotator:")
    print(f"  {'User':<20} {'Labels':>6}  {'Plumes':>6}  {'Cal':>4}  {'First':>10}  {'Last':>10}")
    print("  " + "-" * 70)
    for uid, d in sorted(by_user.items(), key=lambda x: -x[1]["n"]):
        first = d["first"][:10] if d["first"] else "-"
        last  = d["last"][:10]  if d["last"]  else "-"
        print(f"  {uid:<20} {d['n']:>6}  {d['plume']:>6}  {d['cal']:>4}  {first:>10}  {last:>10}")

    print("=" * 52)


# ── CSV export ────────────────────────────────────────────────────────────────

def write_csv(labels, out_path):
    fieldnames = [
        "id", "created_at", "user_id", "email", "expertise",
        "scene_id", "instrument", "clat", "clon", "acq_idx",
        "year", "month", "day",
        "is_calibration", "no_plume", "n_polygons",
    ]

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in labels:
            writer.writerow({
                "id":             r["id"],
                "created_at":     r["created_at"],
                "user_id":        r["user_id"],
                "email":          r.get("email") or "",
                "expertise":      EXPERTISE.get(r.get("expertise"), r.get("expertise", "")),
                "scene_id":       r["scene_id"],
                "instrument":     r["instrument"],
                "clat":           r["clat"],
                "clon":           r["clon"],
                "acq_idx":        r["acq_idx"],
                "year":           r["year"],
                "month":          r["month"],
                "day":            r["day"],
                "is_calibration": int(r["is_calibration"]),
                "no_plume":       int(r["no_plume"]),
                "n_polygons":     n_polygons(r["polygons"]),
            })

    print(f"\n  CSV written to: {out_path}  ({len(labels)} rows)")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize BENCH4 label data")
    parser.add_argument("--db",      default=DEFAULT_DB,  help="Path to local labels.db")
    parser.add_argument("--url",     default=None, nargs="?", const="https://bench4.atmos.uw.edu",
                        help="Base URL of running app (default: https://bench4.atmos.uw.edu)")
    parser.add_argument("--csv",     action="store_true", help="Write a CSV file")
    parser.add_argument("--csv-out", default=DEFAULT_CSV, help="CSV output path")
    args = parser.parse_args()

    if args.url:
        labels, users = load_from_url(args.url)
    else:
        if not os.path.exists(args.db):
            print(f"Database not found: {args.db}")
            raise SystemExit(1)
        labels, users = load_from_db(args.db)

    print_summary(labels, users)

    if args.csv:
        write_csv(labels, args.csv_out)
