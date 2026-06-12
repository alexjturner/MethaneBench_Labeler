"""
Methane Plume Labeler - FastAPI Backend

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000

Environment variables:
    IMAGERY_PATH  - path to imagery folder (default: ./imagery)
    DB_PATH       - path to SQLite database file (default: ./labels.db)
"""

import io, json, base64, sqlite3, random, os
from pathlib import Path
from datetime import datetime
from typing import Optional, List

import numpy as np
import netCDF4 as nc
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.cm as cm
from matplotlib.colors import LinearSegmentedColormap

# Custom colormaps defined once at import time
#_NDVI_CMAP  = LinearSegmentedColormap.from_list('ndvi_tg',  ['#c8b89a', '#1a7a1a'])  # tan → forest green
#_NDVI_CMAP  = LinearSegmentedColormap.from_list('ndvi_tg',  ['#1a7a1a', '#e0e0e0'])  # tan → forest green
_NDVI_CMAP  = LinearSegmentedColormap.from_list('ndvi_tg',  ['#e0e0e0', '#1a7a1a'])  # tan → forest green
_CLOUD_CMAP = LinearSegmentedColormap.from_list('cloud_wg', ['#1c1c1e', '#e0e0e0'])  # dark → light grey
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

IMAGERY_PATH       = Path(os.environ.get("IMAGERY_PATH", "imagery"))
DB_PATH            = Path(os.environ.get("DB_PATH", "labels.db"))
CALIBRATION_FILE   = Path(os.environ.get("CALIBRATION_FILE", "calibration_scenes.txt"))
DISPLAY_SCALE      = 4        # upscale factor for display (129x138 → 516x552)

# Landsat 7 excluded due to scan-line striping artifacts
INSTRUMENTS = {
    "landsat_45": "landsat_45",
    "landsat_89": "landsat_89",
    "sentinel2":  "sentinel2",
}

# ── Global data store ─────────────────────────────────────────────────────────

cal_scenes:  list = []    # calibration scenes in order (from calibration_scenes.txt)
cal_set:     set  = set() # same as a set for O(1) lookup
scene_index: list = []    # [(instr, clat, clon, acq_idx), ...]  shuffled w/ seed 42
scene_by_id: dict = {}    # int scene_id → (instr, clat, clon, acq_idx)
scene_to_id: dict = {}    # (instr, clat, clon, acq_idx) → int scene_id
loaded_data: dict = {}    # "instr|clat|clon" → {channels, years, months, days, ...}


def _dkey(instr: str, clat: float, clon: float) -> str:
    return f"{instr}|{clat}|{clon}"


def load_all_data():
    for instr_key, folder_name in INSTRUMENTS.items():
        folder = IMAGERY_PATH / folder_name
        if not folder.exists():
            print(f"  [skip] folder not found: {folder}")
            continue
        for nc_file in sorted(folder.glob("*.nc")):
            try:
                ds       = nc.Dataset(nc_file)
                channels = ds.variables["channels"][:]   # (acq, H, W, 6)
                years    = ds.variables["year"][:]
                months   = ds.variables["month"][:]
                days     = ds.variables["day"][:]
                clat     = float(ds.variables["clat"][0])
                clon     = float(ds.variables["clon"][0])
                res      = float(ds.variables["resolution"][0])
                ds.close()

                dk = _dkey(instr_key, clat, clon)
                loaded_data[dk] = dict(
                    channels=channels, years=years, months=months, days=days,
                    clat=clat, clon=clon, resolution=res, instrument=instr_key,
                )
                for i in range(channels.shape[0]):
                    scene_index.append((instr_key, clat, clon, i))
                print(f"  Loaded {channels.shape[0]:4d} scenes: {nc_file.name}")
            except Exception as e:
                print(f"  [error] {nc_file.name}: {e}")

    # Deterministic shuffle
    rng = random.Random(42)
    rng.shuffle(scene_index)

    for i, scene in enumerate(scene_index):
        scene_by_id[i] = scene
        scene_to_id[scene] = i

    print(f"  Total scenes: {len(scene_index)}")
    _load_calibration_scenes()


def _load_calibration_scenes():
    """Read calibration_scenes.txt; fall back to first 10 shuffled scenes."""
    global cal_scenes, cal_set
    if CALIBRATION_FILE.exists():
        scenes = []
        with open(CALIBRATION_FILE) as f:
            for line in f:
                line = line.split('#')[0].strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    scenes.append((parts[0], float(parts[1]), float(parts[2]), int(parts[3])))
        # Build a lookup that tolerates float32 rounding: snap each requested
        # (instr, clat, clon, acq_idx) to the nearest loaded scene tuple.
        def _snap(instr, clat, clon, acq_idx):
            if (instr, clat, clon, acq_idx) in scene_to_id:
                return (instr, clat, clon, acq_idx)
            # Fall back to closest lat/lon match for the same instrument & acq_idx
            best, best_dist = None, float("inf")
            for (si, sc_lat, sc_lon, sc_idx) in scene_to_id:
                if si == instr and sc_idx == acq_idx:
                    d = (sc_lat - clat) ** 2 + (sc_lon - clon) ** 2
                    if d < best_dist:
                        best_dist, best = d, (si, sc_lat, sc_lon, sc_idx)
            return best  # None if no match

        resolved = []
        for s in scenes:
            snapped = _snap(*s)
            if snapped is not None:
                resolved.append(snapped)
                print(f"  Calibration scene: {snapped}" + ("" if snapped == s else f"  (snapped from {s})"))
            else:
                print(f"  Calibration scene NOT FOUND: {s}")
        cal_scenes = resolved
        print(f"  Calibration scenes loaded from file: {len(cal_scenes)}")
    else:
        cal_scenes = scene_index[:10]
        print(f"  Calibration file not found — using first 10 shuffled scenes")
    cal_set = set(cal_scenes)


# ── Image rendering ───────────────────────────────────────────────────────────

def _pct_stretch(arr: np.ndarray, lo: float = 2, hi: float = 98) -> np.ndarray:
    """Percentile-stretch a 2-D float array to uint8 [0, 255]."""
    valid = arr[~np.isnan(arr)]
    if not len(valid):
        return np.zeros(arr.shape, dtype=np.uint8)
    p_lo, p_hi = np.percentile(valid, lo), np.percentile(valid, hi)
    if p_hi <= p_lo:
        return np.zeros(arr.shape, dtype=np.uint8)
    out = np.nan_to_num(np.clip((arr - p_lo) / (p_hi - p_lo), 0, 1), nan=0.0)
    return (out * 255).astype(np.uint8)


def _colormap(data: np.ndarray, cmap_or_name, vmin: float, vmax: float) -> Image.Image:
    cmap   = cmap_or_name if callable(cmap_or_name) else cm.get_cmap(cmap_or_name)
    normed = np.clip((data - vmin) / max(vmax - vmin, 1e-9), 0, 1)
    normed = np.nan_to_num(normed, nan=0.5)
    rgb    = (cmap(normed)[:, :, :3] * 255).astype(np.uint8)
    return Image.fromarray(rgb)


def _get_band_data(instr: str, clat: float, clon: float, acq_idx: int, view: str):
    """Return (data_array, cmap, default_vmin, default_vmax) for a non-RGB view."""
    d     = loaded_data[_dkey(instr, clat, clon)]
    acq   = d["channels"][acq_idx]
    blue, green, red = acq[:,:,0], acq[:,:,1], acq[:,:,2]
    nir, swir1, swir2 = acq[:,:,3], acq[:,:,4], acq[:,:,5]
    eps   = 1e-10

    if view == "ndvi":
        with np.errstate(invalid="ignore", divide="ignore"):
            data = np.where((nir + red) > 0, (nir - red) / (nir + red + eps), np.nan)
        return data, _NDVI_CMAP, 0.0, 0.8

    elif view == "dr":
        with np.errstate(invalid="ignore", divide="ignore"):
            data = np.where((swir1 + swir2) > 0, (swir1 - swir2) / (swir1 + swir2 + eps), np.nan)
        valid = data[~np.isnan(data)]
        if len(valid):
            center = float(np.nanmedian(data))
            spread = max(float(np.percentile(np.abs(valid - center), 98)), 0.005)
        else:
            center, spread = 0.0, 0.1
        return data, "RdBu_r", center - spread, center + spread

    elif view == "zscore":
        MIN_ZSCORE_BG = 5
        all_ch  = d["channels"]
        yr_mask = (d["years"] == d["years"][acq_idx]).copy()
        yr_mask[acq_idx] = False
        with np.errstate(invalid="ignore", divide="ignore"):
            dr_all = np.where(
                (all_ch[:,:,:,4] + all_ch[:,:,:,5]) > 0,
                (all_ch[:,:,:,4] - all_ch[:,:,:,5]) / (all_ch[:,:,:,4] + all_ch[:,:,:,5] + eps),
                np.nan,
            )
            dr = np.where((swir1 + swir2) > 0, (swir1 - swir2) / (swir1 + swir2 + eps), np.nan)
        bg = dr_all[yr_mask]
        if bg.shape[0] < MIN_ZSCORE_BG:
            all_mask = np.ones(len(d["years"]), dtype=bool)
            all_mask[acq_idx] = False
            bg = dr_all[all_mask]
        if bg.shape[0] > 1:
            bg_mean = np.nanmean(bg, axis=0)
            bg_std  = np.nanstd(bg,  axis=0)
            data    = (dr - bg_mean) / np.where(bg_std > 1e-8, bg_std, 1e-8)
        else:
            data = np.zeros_like(dr)
        return data, "RdBu_r", -3.0, 3.0

    elif view == "cloud":
        brightness = (blue + green + red) / 3.0
        data       = np.clip(brightness / 0.40, 0, 1)
        return data, _CLOUD_CMAP, 0.0, 1.0

    else:
        raise ValueError(f"Unknown view: {view!r}")


def render_all_views(instr: str, clat: float, clon: float, acq_idx: int):
    """Return (views_dict, view_ranges_dict) for one acquisition.

    views_dict      : {view_name: PIL.Image}
    view_ranges_dict: {view_name: {'vmin': float, 'vmax': float}}  (non-RGB views only)
    """
    d    = loaded_data[_dkey(instr, clat, clon)]
    acq  = d["channels"][acq_idx]
    blue, green, red = acq[:,:,0], acq[:,:,1], acq[:,:,2]

    views       = {}
    view_ranges = {}

    # RGB — true-colour, percentile stretched (no colormap range)
    views["rgb"] = Image.fromarray(
        np.stack([_pct_stretch(red), _pct_stretch(green), _pct_stretch(blue)], axis=-1)
    )

    # All colormapped views via shared helper
    for vname in ("ndvi", "dr", "zscore", "cloud"):
        data, cmap, vmin, vmax = _get_band_data(instr, clat, clon, acq_idx, vname)
        views[vname]       = _colormap(data, cmap, vmin, vmax)
        view_ranges[vname] = {"vmin": round(float(vmin), 8), "vmax": round(float(vmax), 8)}

    return views, view_ranges


def _to_b64(img: Image.Image) -> str:
    """Upscale 4× (nearest-neighbour) and base64-encode as PNG."""
    w, h = img.size
    img2 = img.resize((w * DISPLAY_SCALE, h * DISPLAY_SCALE), Image.NEAREST)
    buf  = io.BytesIO()
    img2.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _scene_payload(sid: int, is_calibration: bool) -> dict:
    instr, clat, clon, acq_idx = scene_by_id[sid]
    d      = loaded_data[_dkey(instr, clat, clon)]
    year   = int(d["years"][acq_idx])
    month  = int(d["months"][acq_idx])
    day    = int(d["days"][acq_idx])
    H, W   = d["channels"].shape[1], d["channels"].shape[2]
    views, view_ranges = render_all_views(instr, clat, clon, acq_idx)
    return {
        "scene_id":       sid,
        "instrument":     instr,
        "clat":           round(clat, 6),
        "clon":           round(clon, 6),
        "acq_idx":        acq_idx,
        "date":           f"{year}-{month:02d}-{day:02d}",
        "is_calibration": is_calibration,
        "img_width":      W * DISPLAY_SCALE,
        "img_height":     H * DISPLAY_SCALE,
        "orig_width":     W,
        "orig_height":    H,
        "images":         {k: _to_b64(v) for k, v in views.items()},
        "view_ranges":    view_ranges,
    }


# ── Database ──────────────────────────────────────────────────────────────────

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,
            email      TEXT,
            expertise  INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS labels (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT,
            scene_id       INTEGER,
            instrument     TEXT,
            clat           REAL,
            clon           REAL,
            acq_idx        INTEGER,
            year           INTEGER,
            month          INTEGER,
            day            INTEGER,
            polygons       TEXT,
            no_plume       INTEGER,
            is_calibration INTEGER,
            created_at     TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.close()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Methane Plume Labeler")


@app.on_event("startup")
async def startup():
    print(f"Loading imagery from: {IMAGERY_PATH.resolve()}")
    load_all_data()
    _init_db()
    print("Ready.")


# ── Pydantic models ───────────────────────────────────────────────────────────

class UserLogin(BaseModel):
    username:  str
    expertise: int          # 1 = methane researcher, 2 = GHG scientist, 3 = other
    email:     Optional[str] = None


class LabelSubmit(BaseModel):
    user_id:  str
    scene_id: int
    polygons: List[List[List[float]]]   # list of polygons; each polygon is [[x,y], ...]
    no_plume: bool                      # True if user explicitly sees no plume


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/user/login")
async def user_login(body: UserLogin):
    body.username = body.username.strip()
    if not body.username:
        raise HTTPException(400, "Username cannot be empty")
    conn  = _get_db()
    user  = conn.execute("SELECT * FROM users WHERE id = ?", (body.username,)).fetchone()
    if not user:
        conn.execute(
            "INSERT INTO users (id, email, expertise, created_at) VALUES (?,?,?,?)",
            (body.username, body.email, body.expertise, datetime.utcnow().isoformat()),
        )
        conn.commit()
        is_new = True
    else:
        is_new = False
    n = conn.execute(
        "SELECT COUNT(*) FROM labels WHERE user_id = ?", (body.username,)
    ).fetchone()[0]
    conn.close()
    return {"user_id": body.username, "is_new": is_new, "labels_completed": n}


@app.get("/api/scene/next")
async def next_scene(user_id: str = Query(...)):
    conn = _get_db()
    labeled_ids = {
        r["scene_id"] for r in
        conn.execute("SELECT scene_id FROM labels WHERE user_id = ?", (user_id,)).fetchall()
    }
    n_cal_done = conn.execute(
        "SELECT COUNT(*) FROM labels WHERE user_id = ? AND is_calibration = 1", (user_id,)
    ).fetchone()[0]
    conn.close()

    # Serve calibration scenes first (in file order)
    if n_cal_done < len(cal_scenes):
        for scene_tuple in cal_scenes:
            sid = scene_to_id.get(scene_tuple)
            if sid is not None and sid not in labeled_ids:
                return _scene_payload(sid, is_calibration=True)

    # Then random unlabeled scene (excluding calibration scenes)
    cal_ids   = {scene_to_id[s] for s in cal_scenes if s in scene_to_id}
    available = [sid for sid in range(len(scene_index))
                 if sid not in labeled_ids and sid not in cal_ids]
    if not available:
        return {"done": True, "message": "You've labeled every available scene — thank you!"}

    return _scene_payload(random.choice(available), is_calibration=False)


@app.post("/api/label")
async def submit_label(body: LabelSubmit):
    if body.scene_id not in scene_by_id:
        raise HTTPException(404, "Unknown scene_id")
    instr, clat, clon, acq_idx = scene_by_id[body.scene_id]
    d      = loaded_data[_dkey(instr, clat, clon)]
    year   = int(d["years"][acq_idx])
    month  = int(d["months"][acq_idx])
    day    = int(d["days"][acq_idx])
    is_cal = int(scene_by_id[body.scene_id] in cal_set)

    conn = _get_db()
    conn.execute(
        """INSERT INTO labels
           (user_id, scene_id, instrument, clat, clon, acq_idx,
            year, month, day, polygons, no_plume, is_calibration, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (body.user_id, body.scene_id, instr, clat, clon, acq_idx,
         year, month, day, json.dumps(body.polygons),
         int(body.no_plume), is_cal, datetime.utcnow().isoformat()),
    )
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM labels WHERE user_id = ?", (body.user_id,)
    ).fetchone()[0]
    conn.close()
    return {"success": True, "labels_completed": n}


@app.get("/api/render")
async def render_view(scene_id: int, view: str, vmin: float, vmax: float):
    """Re-render a single non-RGB view with custom colormap range."""
    if scene_id not in scene_by_id:
        raise HTTPException(404, "Unknown scene_id")
    if view not in ("ndvi", "dr", "zscore", "cloud"):
        raise HTTPException(400, "view must be one of: ndvi, dr, zscore, cloud")
    instr, clat, clon, acq_idx = scene_by_id[scene_id]
    data, cmap, _, _ = _get_band_data(instr, clat, clon, acq_idx, view)
    img = _colormap(data, cmap, vmin, vmax)
    return {"image": _to_b64(img)}


@app.get("/api/stats")
async def stats():
    conn = _get_db()
    n_labels = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    n_users  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return {
        "total_labels": n_labels,
        "total_users":  n_users,
        "total_scenes": len(scene_index),
    }


@app.get("/api/export/labels")
async def export_labels():
    """Download all labels and user metadata as JSON."""
    conn   = _get_db()
    labels = [dict(r) for r in conn.execute("SELECT * FROM labels ORDER BY created_at").fetchall()]
    users  = [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()]
    conn.close()
    return {"labels": labels, "users": users, "exported_at": datetime.utcnow().isoformat()}


# Static files — must be mounted last
_static_dir = Path(__file__).parent / "static"

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(_static_dir / "index.html")

app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
