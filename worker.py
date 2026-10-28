import datetime
import hashlib
import importlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, ".fused", "data")
CACHE_DIR = os.path.join(HERE, ".fused", "cache", "v1")
JOBS_DIR = os.path.join(CACHE_DIR, "jobs")
REQUEST_DIR = os.path.join(CACHE_DIR, "requests")
SEARCH_DIR = os.path.join(CACHE_DIR, "search")
PREVIEW_DIR = os.path.join(CACHE_DIR, "previews")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
HISTORY_PATH = os.path.join(DATA_DIR, "exports.json")
DEFAULT_EXPORT_DIR = os.path.join(DATA_DIR, "exports")
DEFAULT_PACKAGE = "https://github.com/fusedio/udfs/tree/ee94a99798532e5f2686bb30a0af2f9e6ce61a26/public/sentinel_utils"
ENGINES = ("remote", "local")
LOCAL_DEPS = {"odc.stac": "odc-stac", "mercantile": "mercantile", "rio_tiler": "rio-tiler", "pystac": "pystac"}
APP_VERSION = "0.3.0"
MAX_SEARCH_DEG = 1.2
MAX_BOX_KM = 100
MAX_EXPORT_PX = 4096
SEARCH_TTL_S = 3600
SETTLE_S = 7 * 86400
TILE_GDAL = {"GDAL_OVERVIEW_OVERSAMPLING_THRESHOLD": "1.35", "GDAL_HTTP_MULTIRANGE": "SERIAL"}
STAC_API = "https://earth-search.aws.element84.com/v1/search"
COLLECTIONS = ("sentinel-2-c1-l2a", "sentinel-2-l2a")
LEGACY_UNTIL = "2023"
STAC_FIELDS = {"include": ["id", "geometry", "properties.datetime", "properties.eo:cloud_cover", "properties.platform",
                           "properties.s2:nodata_pixel_percentage"],
               "exclude": ["assets", "links", "bbox", "stac_extensions"]}
MGRS_COLUMNS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
MGRS_ROWS = "ABCDEFGHJKLMNPQRSTUV"
MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX"
PNG_WIDTHS = (1200, 1600, 2400)
RESOLUTIONS = (10, 20, 40, 80, 160)
KINDS = ("png", "tif_visual", "tif_data")
NODATA_FILL = (200, 200, 200)
LABEL_TILES = "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
LABEL_DIR = os.path.join(CACHE_DIR, "labels")
LIVE_PREVIEW_DIR = os.path.join(PREVIEW_DIR, "live")
WEB_MERCATOR_HALF = 20037508.342789244
JOB_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

with open(os.path.join(HERE, "layers.json")) as _f:
    LAYER_FILE = json.load(_f)
LAYERS = {layer["id"]: layer for layer in LAYER_FILE["layers"]}

_lock = threading.Lock()
_state = {}
_REMOTE_TILE_GATE = threading.Semaphore(6)


class _NoSdk(Exception):
    pass


def main(op: str = "ping", args: str = "{}"):
    fn = OPS.get(op)
    if fn is None:
        raise ValueError(f"unknown op {op!r}; allowed: {sorted(OPS)}")
    try:
        return fn(**json.loads(args or "{}"))
    except _NoSdk:
        return _run_clean(op, args)


def _sdk():
    import fused

    if hasattr(fused, "api") and hasattr(fused, "run"):
        return fused
    raise _NoSdk()


def _run_clean(op, args):
    r = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "--op", op, args or "{}"], cwd=HERE,
                       capture_output=True, text=True, timeout=55)
    lines = [line for line in r.stdout.splitlines() if line.startswith("{")]
    if r.returncode != 0 or not lines:
        raise RuntimeError((r.stderr or r.stdout or "the worker process failed")[-1500:])
    out = json.loads(lines[-1])
    if "error" in out and set(out) == {"error"}:
        raise ValueError(out["error"])
    return out["result"]


def ping():
    return {"ok": True, "version": APP_VERSION, "python": sys.version.split()[0], "exe": sys.executable, "pid": os.getpid()}


def config():
    s = _settings()
    return {"version": APP_VERSION, "settings": s, "export_dir": _export_dir(s),
            "default_export_dir": DEFAULT_EXPORT_DIR, "max_export_px": MAX_EXPORT_PX,
            "max_search_deg": MAX_SEARCH_DEG, "platform": sys.platform, "engine": s["engine"],
            "local": engine_check()}


def account(refresh: bool = False):
    creds = os.environ.get("FUSED_RENDER_FUSED_CREDENTIALS") or os.path.expanduser("~/.fused/credentials")
    if not os.path.isfile(creds):
        _state.pop("who", None)
        return {"logged_in": False}
    stamp = os.path.getmtime(creds)
    hit = _state.get("who")
    if hit and not refresh and hit[0] == stamp and time.time() - hit[1] < 600:
        return hit[2]
    fused = _sdk()
    try:
        info = fused.api.whoami()
        out = {"logged_in": True, "email": info.get("email"), "handle": info.get("handle")}
    except Exception as e:
        out = {"logged_in": False, "error": _short(e)}
    _state["who"] = (stamp, time.time(), out)
    return out


def save_settings(package: str = None, export_dir: str = None, engine: str = None):
    s = _settings()
    if engine is not None:
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {list(ENGINES)}")
        s["engine"] = engine
    if package is not None:
        p = package.strip()
        if p.startswith("https://"):
            if not p.startswith("https://github.com/") or "/tree/" not in p:
                raise ValueError("Use a GitHub URL with /tree/<commit>/ in it, so that the version stays the same.")
        elif p and not os.path.isfile(os.path.join(os.path.expanduser(p), "meta.json")):
            raise ValueError("This folder has no meta.json. Give the sentinel_utils package folder.")
        s["package"] = p
        _state.pop("su", None)
        _state.pop("reader", None)
    if export_dir is not None:
        d = os.path.expanduser(export_dir.strip())
        if d:
            os.makedirs(d, exist_ok=True)
            if not os.access(d, os.W_OK):
                raise ValueError(f"The app cannot write to {d}.")
        s["export_dir"] = d
    _write_json(SETTINGS_PATH, s)
    return config()


def pick_folder():
    if sys.platform != "darwin":
        return {"ok": False, "error": "The folder dialog works only on macOS. Type the folder path."}
    script = 'POSIX path of (choose folder with prompt "Save Sat Desk exports in this folder")'
    r = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True, timeout=600)
    path = r.stdout.strip()
    if r.returncode != 0 or not path:
        return {"ok": False, "cancelled": True}
    save_settings(export_dir=path.rstrip("/") or "/")
    return {"ok": True, "path": path.rstrip("/") or "/", "config": config()}


def search(bbox: list, start: str, end: str, cloud: float = 100):
    from concurrent.futures import ThreadPoolExecutor

    w, s, e, n = [float(v) for v in bbox]
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError(f"bad area {bbox}")
    if e - w > MAX_SEARCH_DEG or n - s > MAX_SEARCH_DEG:
        raise ValueError(f"The area is larger than {MAX_SEARCH_DEG:g} degrees. Zoom in to find dates.")
    if not (DAY.fullmatch(str(start)) and DAY.fullmatch(str(end))) or start > end:
        raise ValueError("start and end must be YYYY-MM-DD, and start must come before end")
    box = [round(v, 4) for v in (w, s, e, n)]
    t0 = time.time()
    units = [(tile, col, half) for tile in sorted(_tile_candidates(box)) for half in _halves(start, end)
             for col in COLLECTIONS if col == COLLECTIONS[0] or half < LEGACY_UNTIL]
    fetched = []
    with ThreadPoolExecutor(max(1, min(24, len(units)))) as ex:
        parts = list(ex.map(lambda u: _unit_scenes(*u, fetched=fetched), units))
    scenes = _merge_scenes([(COLLECTIONS.index(u[1]), items) for u, items in zip(units, parts)], box, start, end)
    return {"bbox": box, "start": start, "end": end, "cloud": float(cloud), "dates": _dates(scenes),
            "tiles": sorted({sc["tile"] for sc in scenes}), "fetched": len(fetched), "units": len(units),
            "seconds": round(time.time() - t0, 2)}


def export_start(spec: dict):
    spec = _check_spec(spec)
    s = _settings()
    if s["engine"] == "local":
        missing = engine_check()["missing"]
        if missing:
            raise ValueError(f"The local engine needs these Python packages on this Mac: {', '.join(missing)}. "
                             "Open the app again to install them.")
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    package = s["package"]
    spec.update(id=job_id, engine=s["engine"], package=package, export_dir=_export_dir(s))
    if not spec["package"]:
        raise ValueError("Set the sentinel_utils package in Setup first.")
    spec_path = os.path.join(JOBS_DIR, job_id + ".spec.json")
    _write_json(spec_path, spec)
    total = len(spec["dates"]) + 1
    _write_json(_job_path(job_id), {"id": job_id, "state": "queued", "step": "Start", "done": 0, "total": total,
                                    "engine": spec["engine"], "started": time.time(), "updated": time.time()})
    with open(os.path.join(JOBS_DIR, job_id + ".log"), "w") as log:
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--job", spec_path], cwd=HERE,
                         env={k: v for k, v in os.environ.items() if k not in TILE_GDAL}, stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return {"id": job_id, "total": total}


def export_status(id: str):
    if not JOB_ID.fullmatch(str(id)):
        raise ValueError("bad job id")
    st = _read_json(_job_path(id), None)
    if st is None:
        raise ValueError(f"unknown export {id}")
    if st["state"] in ("queued", "running") and time.time() - st.get("updated", st["started"]) > 30:
        pid = st.get("pid")
        if not pid or not _alive(pid):
            tail = ""
            try:
                with open(os.path.join(JOBS_DIR, id + ".log")) as f:
                    tail = f.read()[-1500:]
            except OSError:
                pass
            st.update(state="error", error="The export process stopped.", detail=tail)
    return st


def preview_compose(spec: dict):
    import numpy as np
    from PIL import Image
    from rasterio.crs import CRS
    from rasterio.transform import Affine

    spec = _check_spec(spec)
    folder = _preview_folder(spec)
    images = []
    for i in range(len(spec["dates"])):
        meta = _read_json(os.path.join(folder, f"panel_{i}.json"), None)
        if meta is None:
            raise ValueError("The preview is old. Make it again.")
        with Image.open(os.path.join(folder, f"panel_{i}.png")) as im:
            rgba = np.moveaxis(np.asarray(im.convert("RGBA")), -1, 0)
        images.append({"rgba": rgba, "date": meta["date"], "crs": CRS.from_wkt(meta["crs"]),
                       "transform": Affine(*meta["transform"]), "res": meta["res"]})
    return {"image": _compose_preview(spec, images, folder)}


def exports(limit: int = 20):
    items = [h for h in _read_json(HISTORY_PATH, []) if any(os.path.exists(f["path"]) for f in h.get("files", []))]
    return {"items": items[: int(limit)], "folder": _export_dir()}


def reveal(path: str):
    path = _known_file(path)
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)
    return {"ok": True}


def open_file(path: str):
    path = _known_file(path)
    subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], check=False)
    return {"ok": True}


def open_folder():
    d = _export_dir()
    os.makedirs(d, exist_ok=True)
    subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", d], check=False)
    return {"ok": True, "path": d}


def engine_check():
    if _state.get("deps_ok") == sys.executable:
        return {"deps_ok": True, "missing": []}
    code = ("import importlib, json, sys\nmissing = []\nfor name in sys.argv[1:]:\n    try:\n"
            "        importlib.import_module(name)\n    except Exception:\n        missing.append(name)\n"
            "print(json.dumps(missing))")
    r = subprocess.run([sys.executable, "-I", "-c", code, *LOCAL_DEPS], cwd=HERE, capture_output=True, text=True,
                       timeout=120)
    lines = [line for line in r.stdout.splitlines() if line.startswith("[")]
    missing = [LOCAL_DEPS[n] for n in json.loads(lines[-1])] if lines else list(LOCAL_DEPS.values())
    if not missing:
        _state["deps_ok"] = sys.executable
    return {"deps_ok": not missing, "missing": missing}


def tile(z: int, x: int, y: int, q: dict):
    import base64

    png = _make_tile(int(z), int(x), int(y), {str(k): str(v) for k, v in (q or {}).items()})
    return {"png": base64.b64encode(png).decode()}


OPS = {"ping": ping, "config": config, "account": account, "save_settings": save_settings, "pick_folder": pick_folder,
       "search": search, "export_start": export_start, "export_status": export_status, "exports": exports,
       "reveal": reveal, "open_file": open_file, "open_folder": open_folder, "preview_compose": preview_compose,
       "engine_check": engine_check, "tile": tile}


def _settings():
    s = {"package": DEFAULT_PACKAGE, "export_dir": "", "engine": "local"}
    s.update({k: v for k, v in _read_json(SETTINGS_PATH, {}).items() if k in s and isinstance(v, str) and v})
    if not s["package"].startswith("https://") and not os.path.isdir(os.path.expanduser(s["package"])):
        s["package"] = DEFAULT_PACKAGE
    if s["engine"] not in ENGINES:
        s["engine"] = "local"
    return s


def _local_env():
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


def _export_dir(s=None):
    return os.path.expanduser((s or _settings())["export_dir"] or DEFAULT_EXPORT_DIR)


def _package(src=None):
    src = src if src is not None else _settings()["package"]
    if not src:
        raise ValueError("Set the sentinel_utils package (a folder or a GitHub URL) in Setup.")
    hit = _state.get("su")
    if hit and hit[0] == src:
        return hit[1]
    fused = _sdk()
    su = fused.load(src if src.startswith("https://") else os.path.expanduser(src))
    _state["su"] = (src, su)
    return su


def _halves(start, end):
    out = []
    for year in range(max(int(start[:4]), 2015), int(end[:4]) + 1):
        for half, (a, b) in enumerate(((f"{year}-01-01", f"{year}-06-30"), (f"{year}-07-01", f"{year}-12-31"))):
            if a <= end and b >= start:
                out.append(f"{year}h{half + 1}")
    return out


def _half_range(half):
    year, h = half.split("h")
    return (f"{year}-01-01", f"{year}-06-30") if h == "1" else (f"{year}-07-01", f"{year}-12-31")


def _unit_scenes(tile, collection, half, fetched):
    key = (tile, collection, half)
    hit = _state.setdefault("scenes", {}).get(key)
    if hit is not None and _unit_fresh(hit, half):
        return hit["items"]
    path = os.path.join(SEARCH_DIR, collection, tile, half + ".json")
    hit = hit or _read_json(path, None)
    if hit is not None and _unit_fresh(hit, half):
        _state["scenes"][key] = hit
        return hit["items"]
    lo, hi = _half_range(half)
    items = {it["id"]: it for it in (hit or {}).get("items", [])}
    if hit is not None:
        lo = max(lo, (datetime.datetime.fromtimestamp(hit["at"], datetime.UTC) - datetime.timedelta(days=4)).strftime("%Y-%m-%d"))
    for it in _stac_items(tile, collection, lo, hi):
        items[it["id"]] = it
    fetched.append(key)
    hit = {"at": time.time(), "items": sorted(items.values(), key=lambda it: it["id"])}
    _state["scenes"][key] = hit
    _write_json(path, hit)
    return hit["items"]


def _unit_fresh(hit, half):
    return _fresh(hit["at"], _half_range(half)[1])


def _fresh(at, day):
    settled = datetime.datetime.fromisoformat(day).replace(tzinfo=datetime.UTC).timestamp() + SETTLE_S
    return at > settled or time.time() - at < SEARCH_TTL_S


def _stac_items(tile, collection, lo, hi, max_pages=20):
    import requests

    body = {"collections": [collection], "limit": 200, "datetime": f"{lo}T00:00:00Z/{hi}T23:59:59Z",
            "query": {"grid:code": {"eq": f"MGRS-{tile}"}}, "fields": STAC_FIELDS}
    url, out = STAC_API, []
    for _ in range(max_pages):
        r = requests.post(url, json=body, timeout=40)
        r.raise_for_status()
        js = r.json()
        for f in js.get("features", []):
            p = f.get("properties", {})
            geom = f.get("geometry") or {}
            ring = _outer_ring(geom)
            if not ring or not p.get("datetime"):
                continue
            lon = sum(x for x, _ in ring) / len(ring)
            t = datetime.datetime.fromisoformat(p["datetime"].replace("Z", "+00:00")) + datetime.timedelta(hours=lon / 15)
            out.append({"id": f["id"], "tile": tile, "date": t.strftime("%Y-%m-%d"), "cloud": _num(p.get("eo:cloud_cover")),
                        "platform": str(p.get("platform", "")).replace("sentinel-", "S").upper(),
                        "nodata": _num(p.get("s2:nodata_pixel_percentage")), "ring": ring})
        nxt = [link for link in js.get("links", []) if link.get("rel") == "next"]
        if not nxt:
            return out
        url, body = nxt[0]["href"], nxt[0].get("body", body)
    raise RuntimeError(f"too many scenes for {tile} {collection}")


def _outer_ring(geom):
    coords = geom.get("coordinates") or []
    if geom.get("type") == "MultiPolygon":
        coords = max(coords, key=lambda poly: len(poly[0]) if poly else 0, default=[])
    if not coords or not coords[0]:
        return []
    return [[round(float(x), 4), round(float(y), 4)] for x, y in coords[0]]


def _merge_scenes(parts, box, start, end):
    import shapely

    area = shapely.box(*box)
    best = {}
    for col_rank, items in parts:
        for it in items:
            if not (start <= it["date"] <= end):
                continue
            k = (it["tile"], it["date"], it["platform"])
            if k in best and best[k][0] <= col_rank:
                continue
            best[k] = (col_rank, it)
    out = []
    for _, it in best.values():
        geom = shapely.Polygon(it["ring"])
        if geom.is_valid and geom.intersects(area):
            out.append(dict(it, geom=geom))
    out.sort(key=lambda it: (it["date"], it["tile"], it["platform"]))
    return out


def _dates(scenes):
    import shapely

    out = []
    for date in sorted({sc["date"] for sc in scenes}):
        grp = [sc for sc in scenes if sc["date"] == date]
        clouds = [sc["cloud"] for sc in grp if sc["cloud"] is not None]
        foot = shapely.union_all([sc["geom"] for sc in grp]).simplify(0.001)
        out.append({"date": date, "scenes": len(grp),
                    "cloud": _num(sum(clouds) / len(clouds)) if clouds else None,
                    "cloud_min": _num(min(clouds)) if clouds else None, "cloud_max": _num(max(clouds)) if clouds else None,
                    "platforms": sorted({sc["platform"] for sc in grp}), "tiles": sorted({sc["tile"] for sc in grp}),
                    "rings": _rings(foot), "items": [{"tile": sc["tile"], "cloud": sc["cloud"], "rings": [sc["ring"]]} for sc in grp]})
    return out


def _tile_candidates(bounds):
    import pyproj

    w, s, e, n = bounds
    lo, hi = (min(max(int((lat + 80) // 8), 0), 19) for lat in (s - 1, n + 1))
    bands = MGRS_BANDS[lo:hi + 1]
    out = set()
    dz = 0.6 / max(0.2, math.cos(math.radians((s + n) / 2)))
    z0, z1 = int((w - dz + 180) // 6), int((e + dz + 180) // 6)
    for zone in {z % 60 + 1 for z in range(z0, z1 + 1)}:
        hemispheres = ([32600 + zone] if n + 1 >= 0 else []) + ([32700 + zone] if s - 1 < 0 else [])
        for epsg in hemispheres:
            x0, y0, x1, y1 = pyproj.Transformer.from_crs(4326, epsg, always_xy=True).transform_bounds(w, s, e, n, densify_pts=21)
            if not all(map(math.isfinite, (x0, y0, x1, y1))):
                continue
            for col in range(max(int((x0 - 1e4) // 1e5), 1), min(int((x1 + 1e4) // 1e5), 8) + 1):
                for row in range(int((y0 - 1e4) // 1e5), int((y1 + 1e4) // 1e5) + 1):
                    square = MGRS_COLUMNS[(zone - 1) % 3 * 8 + col - 1] + MGRS_ROWS[(row + 5 * (zone % 2 == 0)) % 20]
                    out.update(f"{zone:02d}{band}{square}" for band in bands)
    return out


def _rings(geom):
    import shapely

    rings = []
    for part in shapely.get_parts(geom):
        if part.geom_type == "Polygon" and not part.is_empty:
            rings.append([[round(x, 4), round(y, 4)] for x, y in part.exterior.coords])
        elif hasattr(part, "geoms"):
            rings.extend(_rings(part))
    return rings


def _check_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    area = [float(v) for v in spec.get("area", [])]
    if len(area) != 4 or not (-180 <= area[0] < area[2] <= 180 and -90 <= area[1] < area[3] <= 90):
        raise ValueError("area must be [west, south, east, north]")
    layer = str(spec.get("layer", ""))
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {list(LAYERS)}")
    dates = [str(d) for d in spec.get("dates", [])]
    if not 1 <= len(dates) <= 2 or not all(DAY.fullmatch(d) for d in dates):
        raise ValueError("dates must be one or two YYYY-MM-DD days")
    mode = spec.get("mode", "day")
    if mode not in ("day", "best"):
        raise ValueError("mode must be day or best")
    kind = spec.get("kind", "png")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    outlines = spec.get("outlines") or {}
    if not isinstance(outlines, dict):
        raise ValueError("outlines must be an object of date -> rings")
    out = {"area": area, "layer": layer, "dates": dates, "mode": mode, "kind": kind,
           "win": max(1, min(60, int(spec.get("win", 15)))), "cloud": max(0.0, min(100.0, float(spec.get("cloud", 100)))),
           "png_width": int(spec.get("png_width", 1600)), "title": str(spec.get("title", "")).strip()[:140],
           "place": str(spec.get("place", "")).strip()[:80], "date_label": bool(spec.get("date_label", True)),
           "place_label": bool(spec.get("place_label", True)), "scale_bar": bool(spec.get("scale_bar", False)),
           "north": bool(spec.get("north", False)), "legend": bool(spec.get("legend", False)),
           "map_labels": bool(spec.get("map_labels", False)), "preview": bool(spec.get("preview", False)),
           "outlines": {d: [[[float(x), float(y)] for x, y in ring] for ring in rings] for d, rings in outlines.items()
                        if d in dates and mode == "day"},
           "resolution": spec.get("resolution")}
    w, h = _size_m(area)
    if max(w, h) > MAX_BOX_KM * 1000 * 1.02:
        raise ValueError(f"The area is {w / 1000:.0f} x {h / 1000:.0f} km. It can be {MAX_BOX_KM} km on each side at most. "
                         "Draw a smaller box.")
    if out["png_width"] not in PNG_WIDTHS:
        raise ValueError(f"png_width must be one of {list(PNG_WIDTHS)}")
    if kind != "png":
        res = int(out["resolution"] or 10)
        if res not in RESOLUTIONS:
            raise ValueError(f"resolution must be one of {list(RESOLUTIONS)} m")
        if max(w, h) / res > MAX_EXPORT_PX:
            raise ValueError(f"At {res} m the image is {round(w / res)} x {round(h / res)} px, more than "
                             f"{MAX_EXPORT_PX} px. Choose a coarser resolution or draw a smaller box.")
        out["resolution"] = res
    return out


def _size_m(area):
    w, s, e, n = area
    lat = math.radians((s + n) / 2)
    return (e - w) * 111_320 * math.cos(lat), (n - s) * 110_540


def run_job(spec_path):
    spec = _read_json(spec_path, None)
    engine = spec.get("engine", "remote")
    where = "on this Mac" if engine == "local" else "on Fused"
    st = {"id": spec["id"], "state": "running", "pid": os.getpid(), "started": time.time(), "updated": time.time(),
          "done": 0, "total": len(spec["dates"]) + 1, "step": "Load the sentinel_utils library", "engine": engine}

    def step(msg, done=None):
        st.update(step=msg, updated=time.time())
        if done is not None:
            st["done"] = done
        _write_json(_job_path(spec["id"]), st)

    step(st["step"])
    try:
        t0 = time.time()
        if engine == "local":
            _local_env()
        su = _package(spec["package"])
        n = len(spec["dates"])
        step(f"Make {n} image{'s' if n > 1 else ''} {where}" + (" (both at the same time)" if n > 1 else ""), 0)
        images = _fetch_all(su, spec, step, engine, where)
        if spec.get("preview"):
            step("Make the preview", n)
            result = {"preview": _write_preview(spec, images)}
        else:
            step("Write the files", n)
            result = _write_outputs(spec, images)
        result.update(seconds=round(time.time() - t0, 1), engine=engine)
        st.update(state="done", result=result, done=st["total"], step="Done", finished=time.time(), updated=time.time())
        if not spec.get("preview"):
            _add_history(result)
    except Exception as e:
        st.update(state="error", error=_friendly(e, engine), detail=traceback.format_exc()[-3000:], finished=time.time(),
                  updated=time.time())
    _write_json(_job_path(spec["id"]), st)


def _fetch_all(su, spec, step, engine, where):
    from concurrent.futures import ProcessPoolExecutor

    plans = [_plan(su, spec, d) for d in spec["dates"]]
    pending = [r for p in plans for r in (p["main"], p["fill"]) if r is not None and _cached_path(r) is None]
    if len(pending) > 1:
        step(f"Make {len(pending)} images {where} at the same time", 0)
        with ProcessPoolExecutor(len(pending)) as ex:
            for req, data in zip(pending, ex.map(_run_remote, [su] * len(pending), pending, [engine] * len(pending))):
                _store(req, data)
    elif pending:
        _store(pending[0], _run_remote(su, pending[0], engine))
    images = []
    for p in plans:
        img = _read_tif(_cached(p["main"]))
        if p["fill"] is not None:
            img = _fill(img, _read_tif(_cached(p["fill"])))
        img["date"] = p["date"]
        images.append(img)
    return images


def _plan(su, spec, date):
    layer = LAYERS[spec["layer"]]
    if spec["kind"] == "png":
        grid = _png_grid(su, spec)
    else:
        grid = {"resolution": spec["resolution"]}
    common = dict(units="dn", output="geotiff", cloud_cover=spec["cloud"], mosaic_order="least_cloudy", **grid)
    if spec["mode"] == "day":
        main_req = su.process_request(spec["area"], date, layer["bands"], **common)
        return {"date": date, "main": main_req, "fill": None}
    d = datetime.date.fromisoformat(date)
    window = ((d - datetime.timedelta(days=spec["win"])).isoformat(), (d + datetime.timedelta(days=spec["win"])).isoformat())
    main_req = su.process_request(spec["area"], window, layer["bands"], mask="clear_snow", reducer="median", **common)
    fill_req = su.process_request(spec["area"], window, layer["bands"], reducer="median", **common)
    return {"date": date, "main": main_req, "fill": fill_req}


def _png_grid(su, spec):
    n = len(spec["dates"])
    width = spec["png_width"]
    panel = (width - round(width * 0.004) * (n - 1)) // n
    w10, h10 = su.bbox_to_dimensions(spec["area"], 10)
    scale = panel / max(w10, h10)
    if scale >= 1:
        return {"resolution": 10}
    return {"size": (max(1, round(w10 * scale)), max(1, round(h10 * scale)))}


def _run_remote(su, req, engine="remote"):
    import fused

    return su.from_payload(fused.run(su, request=json.dumps(req), engine=engine))


def _tile_reader():
    with _lock:
        if "tile_gate" not in _state:
            _local_env()
            for k, v in TILE_GDAL.items():
                os.environ.setdefault(k, v)
            _share_read_pool()
            for name in (*LOCAL_DEPS, "rio_tiler.io"):
                importlib.import_module(name)
            _state["tile_gate"] = threading.Semaphore(8)
        if "reader" not in _state:
            _state["reader"] = _package().module("su_reader")
        return _state["reader"]


def _share_read_pool():
    import concurrent.futures as cf

    real = cf.ThreadPoolExecutor
    shared = real(32, thread_name_prefix="s3-read")

    class Shared:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, *a, **k):
            return shared.map(*a, **k)

        def submit(self, *a, **k):
            return shared.submit(*a, **k)

        def shutdown(self, *a, **k):
            pass

    class Pool(real):
        def __new__(cls, *a, **k):
            if cls is Pool and a == (32,) and not k:
                return Shared()
            return super().__new__(cls)

    cf.ThreadPoolExecutor = Pool


def _make_tile(z, x, y, q):
    import numpy as np

    def arg(name, default=None):
        v = q.get(name, "").strip()
        return v if v else default

    date = arg("date")
    if not date or not DAY.fullmatch(date):
        raise ValueError("date must be YYYY-MM-DD")
    if not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise ValueError("the tile is outside the map")
    opts = {"days": int(arg("days", 30)), "what": arg("what", "true_color"), "method": arg("method", "best"),
            "cloud_cover": float(arg("max_cloud", 80)), "cloud_mask": arg("cloud_mask", "true").lower() in ("true", "1", "yes"),
            "cmap": arg("cmap"), "vmin": None if arg("vmin") is None else float(arg("vmin")),
            "vmax": None if arg("vmax") is None else float(arg("vmax")), "min_zoom": int(arg("min_zoom", 6))}
    if _settings()["engine"] == "remote":
        return _remote_tile(z, x, y, date, opts)
    reader = _tile_reader()
    try:
        with _state["tile_gate"]:
            rgba = reader.tile(z, x, y, date, **opts)
    except Exception as e:
        if "NoDataError" not in type(e).__name__:
            raise
        rgba = np.zeros((4, 256, 256), "uint8")
    return _png_bytes(rgba if rgba[3].any() else np.zeros((4, 256, 256), "uint8"))


def _remote_tile(z, x, y, date, opts):
    import base64

    package = _settings()["package"]
    if not package.startswith("https://"):
        raise ValueError("Map tiles on Fused need the sentinel_utils package as a GitHub URL. Set one in Setup or choose This Mac.")
    fused = _sdk()
    with _lock:
        udf = _state.get("tile_udf")
        if udf is None:
            udf = _state["tile_udf"] = fused.load(os.path.join(HERE, "tile_udf.py"))
    params = {k: v for k, v in opts.items() if v is not None}
    with _REMOTE_TILE_GATE:
        out = fused.run(udf, tz=z, tx=x, ty=y, date=date, package=package, engine="remote", **params)
    return base64.b64decode(out)


def _request_path(req):
    return os.path.join(REQUEST_DIR, _key("req", req) + ".tif")


def _cached_path(req):
    path = _request_path(req)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return path if _fresh(mtime, max(DAY.findall(json.dumps(req["date"])))) else None


def _cached(req):
    try:
        with open(_request_path(req), "rb") as f:
            return f.read()
    except OSError:
        return None


def _store(req, data):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"sentinel_utils sent {type(data).__name__}, not GeoTIFF bytes")
    _write_atomic(_request_path(req), bytes(data))


def _read_tif(data):
    import numpy as np
    from rasterio.io import MemoryFile

    with MemoryFile(data) as mem, mem.open() as ds:
        dn = ds.read()
        return {"dn": dn, "refl": np.where(dn > 0, dn.astype("float32") * 1e-4, np.nan), "transform": ds.transform,
                "crs": ds.crs, "res": abs(ds.transform.a)}


def _fill(img, other):
    import numpy as np

    if other["dn"].shape != img["dn"].shape:
        return img
    holes = ~np.all(np.isfinite(img["refl"]), axis=0)
    dn = np.where(holes[None], other["dn"], img["dn"])
    return dict(img, dn=dn, refl=np.where(dn > 0, dn.astype("float32") * 1e-4, np.nan))


def _index(refl):
    import numpy as np

    a, b = np.clip(refl[0], 0, None), np.clip(refl[1], 0, None)
    with np.errstate(invalid="ignore", divide="ignore"):
        v = (a - b) / (a + b)
    return np.where(np.isfinite(v), v, np.nan).astype("float32")


def _render(img, layer):
    import matplotlib
    import numpy as np

    refl = img["refl"]
    valid = np.all(np.isfinite(refl), axis=0)
    if layer["kind"] == "rgb":
        lo, hi, gamma = layer["stretch"]
        x = np.clip((refl - lo) / (hi - lo), 0, 1) ** (1.0 / gamma)
        rgb = np.nan_to_num(x * 255).astype("uint8")
    else:
        lo, hi = layer["range"]
        x = np.clip((_index(refl) - lo) / (hi - lo), 0, 1)
        rgb = np.moveaxis((matplotlib.colormaps[layer["cmap"]](np.nan_to_num(x))[..., :3] * 255).astype("uint8"), -1, 0)
    return np.concatenate([rgb, np.where(valid, 255, 0).astype("uint8")[None]])


def _write_outputs(spec, images):
    layer = LAYERS[spec["layer"]]
    folder = spec["export_dir"]
    os.makedirs(folder, exist_ok=True)
    files = []
    if spec["kind"] == "png":
        png = _compose(spec, images, layer)
        path = _unique(folder, _stem(spec, [i["date"] for i in images]) + ".png")
        with open(path, "wb") as f:
            f.write(png)
        files.append(_file_entry(path, "Newsroom image (PNG)", _preview_png(png)))
    else:
        for img in images:
            stem = _stem(spec, [img["date"]])
            rgba = _render(img, layer)
            if spec["kind"] == "tif_visual":
                path = _unique(folder, stem + "_visual.tif")
                _write_tif(path, rgba, img, colorinterp="rgba")
                label = "GeoTIFF as shown (RGB)"
            elif layer["kind"] == "index":
                path = _unique(folder, stem + f"_{layer['id']}.tif")
                _write_tif(path, _index(img["refl"])[None], img, names=[layer["id"]])
                label = f"GeoTIFF of {layer['id']} values"
            else:
                path = _unique(folder, stem + "_bands.tif")
                _write_tif(path, img["dn"], img, names=layer["bands"])
                label = "GeoTIFF of band values (reflectance x 10000)"
            files.append(_file_entry(path, label, _preview_array(rgba)))
    return {"files": files, "folder": folder, "caption": _caption(spec, layer), "credit": _credit(spec["dates"]),
            "layer": layer["name"], "dates": spec["dates"], "kind": spec["kind"], "at": time.time()}


def _write_tif(path, arr, img, *, colorinterp=None, names=None):
    import numpy as np
    import rasterio
    from rasterio.enums import ColorInterp, Resampling

    float_data = arr.dtype.kind == "f"
    profile = {"driver": "GTiff", "width": arr.shape[2], "height": arr.shape[1], "count": arr.shape[0],
               "dtype": arr.dtype.name, "crs": img["crs"], "transform": img["transform"], "compress": "deflate",
               "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER"}
    if float_data:
        profile.update(nodata=float("nan"), predictor=3)
    elif colorinterp is None:
        profile.update(nodata=0, predictor=2)
    tmp = path + ".part"
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(np.ascontiguousarray(arr))
        if colorinterp == "rgba":
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        for i, name in enumerate(names or [], start=1):
            dst.set_band_description(i, name)
        factors = [f for f in (2, 4, 8, 16, 32) if min(arr.shape[1:]) // f >= 256]
        if factors:
            dst.build_overviews(factors, Resampling.average)
    os.replace(tmp, path)


def _compose(spec, images, layer, as_image=False):
    import io

    import numpy as np
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    width = spec["png_width"]
    pad = round(width * 0.03)
    gap = round(width * 0.004)
    n = len(images)
    panel_w = (width - gap * (n - 1)) // n
    h0, w0 = _shape(images[0])
    panel_h = round(panel_w * h0 / w0)
    f_title = _font(round(width * 0.024), bold=True)
    f_chip = _font(round(width * 0.0135), bold=True)
    f_small = _font(round(width * 0.0105))
    m = round(pad * 0.4)
    canvas = Image.new("RGB", (panel_w * n + gap * (n - 1), panel_h), NODATA_FILL)
    for i, img in enumerate(images):
        x0 = i * (panel_w + gap)
        arr = img["rgba"] if "rgba" in img else _render(img, layer)
        rgba = Image.fromarray(np.moveaxis(arr, 0, -1), "RGBA")
        method = Image.LANCZOS if rgba.width >= panel_w else Image.BICUBIC
        rgba = rgba.resize((panel_w, panel_h), method)
        canvas.paste(rgba, (x0, 0), rgba)
        if spec["map_labels"]:
            _map_labels(canvas, img, (x0, 0, panel_w, panel_h))
        for ring in spec["outlines"].get(img["date"], []):
            _outline(canvas, ring, img, (x0, 0, panel_w, panel_h), max(2, round(width * 0.0015)))
        y = m
        if i == 0 and spec["title"]:
            y = _chip(canvas, (x0 + m, y), spec["title"], f_title, max_w=panel_w * n - 2 * m) + round(m * 0.5)
        parts = []
        if n == 2:
            parts.append("BEFORE" if i == 0 else "AFTER")
        if spec["place_label"] and spec["place"]:
            parts.append(spec["place"])
        if spec["date_label"]:
            parts.append(_date_label(spec, img["date"]))
        if parts:
            _chip(canvas, (x0 + m, y), "  ·  ".join(parts), f_chip)
        if spec["scale_bar"] and i == 0:
            _scale_bar(canvas, (x0 + m, panel_h - m), img["res"] * w0 / panel_w, panel_w, f_chip)
        if spec["north"] and i == n - 1:
            _north(canvas, (x0 + panel_w - m, m), round(width * 0.018), f_chip)
    right = canvas.width - m
    bottom = _chip(canvas, (right, panel_h - m), _credit(spec["dates"]), f_small, anchor="rb") - round(m * 0.5)
    if spec["legend"] and layer["kind"] == "index":
        _legend(canvas, (right, bottom), layer, round(width * 0.2), round(width * 0.009), f_small)
    if as_image:
        return canvas
    meta = PngInfo()
    if spec["title"]:
        meta.add_text("Title", spec["title"])
    meta.add_text("Description", _caption(spec, layer))
    meta.add_text("Copyright", _credit(spec["dates"]))
    meta.add_text("Source", "Sentinel-2 L2A")
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", pnginfo=meta, optimize=True)
    return buf.getvalue()


def _shape(img):
    return (img["rgba"] if "rgba" in img else img["refl"]).shape[1:]


def _map_labels(canvas, img, panel):
    import numpy as np
    from PIL import Image
    from rasterio.transform import Affine, array_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    x0, y0, pw, ph = panel
    h, w = _shape(img)
    dst_transform = img["transform"] * Affine.scale(w / pw, h / ph)
    left, bottom, right, top = array_bounds(ph, pw, dst_transform)
    west, south, east, north = transform_bounds(img["crs"], "EPSG:4326", left, bottom, right, top)
    m_per_px = img["res"] * w / pw
    lat = math.radians((south + north) / 2)
    z = int(max(2, min(16, round(math.log2(2 * WEB_MERCATOR_HALF / 256 * math.cos(lat) / m_per_px)))))
    mosaic, transform = _label_mosaic((west, south, east, north), z)
    if mosaic is None:
        return
    dst = np.zeros((4, ph, pw), "uint8")
    reproject(mosaic, dst, src_transform=transform, src_crs="EPSG:3857", dst_transform=dst_transform, dst_crs=img["crs"],
              resampling=Resampling.bilinear)
    labels = Image.fromarray(np.moveaxis(dst, 0, -1), "RGBA")
    canvas.paste(labels, (x0, y0), labels)


def _label_mosaic(bounds, z):
    import io
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    from PIL import Image
    from rasterio.transform import Affine

    def tile_xy(lon, lat, zz):
        n = 2 ** zz
        x = int((lon + 180) / 360 * n)
        r = math.radians(max(-85.0511, min(85.0511, lat)))
        y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
        return min(max(x, 0), n - 1), min(max(y, 0), n - 1)

    while True:
        xa, ya = tile_xy(bounds[0], bounds[3], z)
        xb, yb = tile_xy(bounds[2], bounds[1], z)
        if (xb - xa + 1) * (yb - ya + 1) <= 120 or z <= 2:
            break
        z -= 1

    def fetch(xy):
        x, y = xy
        path = os.path.join(LABEL_DIR, str(z), str(x), f"{y}.png")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            try:
                req = urllib.request.Request(LABEL_TILES.format(z=z, x=x, y=y), headers={"User-Agent": "SatDesk/" + APP_VERSION})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
            except Exception:
                return xy, None
            _write_atomic(path, data)
        try:
            return xy, np.moveaxis(np.asarray(Image.open(io.BytesIO(data)).convert("RGBA")), -1, 0)
        except Exception:
            return xy, None

    cells = [(x, y) for x in range(xa, xb + 1) for y in range(ya, yb + 1)]
    mosaic = np.zeros((4, (yb - ya + 1) * 256, (xb - xa + 1) * 256), "uint8")
    got = 0
    with ThreadPoolExecutor(16) as ex:
        for (x, y), arr in ex.map(fetch, cells):
            if arr is not None and arr.shape == (4, 256, 256):
                mosaic[:, (y - ya) * 256:(y - ya + 1) * 256, (x - xa) * 256:(x - xa + 1) * 256] = arr
                got += 1
    if not got:
        return None, None
    size = 2 * WEB_MERCATOR_HALF / 2 ** z
    transform = Affine(size / 256, 0, -WEB_MERCATOR_HALF + xa * size, 0, -size / 256, WEB_MERCATOR_HALF - ya * size)
    return mosaic, transform


def _preview_folder(spec):
    key = _key("preview", spec["area"], spec["layer"], spec["dates"], spec["mode"], spec["win"], spec["cloud"],
               spec["png_width"])
    return os.path.join(LIVE_PREVIEW_DIR, key)


def _looks_key(spec):
    return _key(spec["title"], spec["place"], spec["date_label"], spec["place_label"], spec["map_labels"],
                spec["scale_bar"], spec["north"], spec["legend"], spec["outlines"])


def _write_preview(spec, images):
    layer = LAYERS[spec["layer"]]
    folder = _preview_folder(spec)
    panels = []
    for i, img in enumerate(images):
        rgba = _render(img, layer)
        img["rgba"] = rgba
        _write_atomic(os.path.join(folder, f"panel_{i}.png"), _png_bytes(rgba))
        _write_json(os.path.join(folder, f"panel_{i}.json"), {"date": img["date"], "crs": img["crs"].to_wkt(),
                                                              "transform": list(img["transform"])[:6], "res": img["res"]})
        map_path = os.path.join(folder, f"map_{i}.png")
        _write_atomic(map_path, _png_bytes(_map_image(rgba, img, spec["area"])))
        panels.append({"date": img["date"], "image": os.path.relpath(map_path, HERE), "bounds": spec["area"]})
    return {"image": _compose_preview(spec, images, folder), "panels": panels}


def _compose_preview(spec, images, folder):
    path = os.path.join(folder, f"look_{_looks_key(spec)}.jpg")
    if not os.path.exists(path):
        import io

        canvas = _compose(spec, images, LAYERS[spec["layer"]], as_image=True)
        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, format="JPEG", quality=88)
        _write_atomic(path, buf.getvalue())
    return os.path.relpath(path, HERE)


def _map_image(rgba, img, area):
    import numpy as np
    from pyproj import Transformer
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject

    to_merc = Transformer.from_crs(4326, 3857, always_xy=True)
    x0, y0 = to_merc.transform(area[0], area[1])
    x1, y1 = to_merc.transform(area[2], area[3])
    w = rgba.shape[2]
    h = max(1, round(w * (y1 - y0) / (x1 - x0)))
    dst = np.zeros((4, h, w), "uint8")
    reproject(np.ascontiguousarray(rgba), dst, src_transform=img["transform"], src_crs=img["crs"],
              dst_transform=from_bounds(x0, y0, x1, y1, w, h), dst_crs="EPSG:3857", resampling=Resampling.bilinear)
    return dst


def _png_bytes(rgba):
    import io

    import numpy as np
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.moveaxis(rgba, 0, -1), "RGBA").save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def _outline(canvas, ring, img, panel, width):
    import pyproj
    from PIL import ImageDraw

    x0, y0, pw, ph = panel
    h, w = _shape(img)
    to_img = pyproj.Transformer.from_crs(4326, img["crs"], always_xy=True)
    inv = ~img["transform"]
    pts = []
    for lon, lat in ring:
        col, row = inv * to_img.transform(lon, lat)
        pts.append((x0 + col * pw / w, y0 + row * ph / h))
    if len(pts) < 2:
        return
    draw = ImageDraw.Draw(canvas)
    dash, space = width * 5, width * 3
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        if not length:
            continue
        ux, uy = (bx - ax) / length, (by - ay) / length
        t = 0.0
        while t < length:
            e = min(t + dash, length)
            seg = ((ax + ux * t, ay + uy * t), (ax + ux * e, ay + uy * e))
            draw.line(seg, fill=(0, 0, 0), width=width + 2)
            draw.line(seg, fill=(255, 255, 255), width=width)
            t += dash + space


def _legend(canvas, xy, layer, bar_w, bar_h, font):
    import numpy as np
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(canvas)
    lo, hi = layer["range"]
    left, right = f"{layer.get('low', '')} {lo:g}", f"{hi:g} {layer.get('high', '')}"
    lh = probe.textbbox((0, 0), "Ag", font=font)[3]
    px, py = round(lh * 0.7), round(lh * 0.5)
    box_w, box_h = bar_w + 2 * px, lh * 2 + bar_h + 3 * py
    x, y = xy[0] - box_w, xy[1] - box_h
    box = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(box)
    bd.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=round(lh * 0.5), fill=(0, 0, 0, 150))
    bd.text((px, py), layer["name"], font=font, fill=(255, 255, 255, 255))
    stops = [tuple(int(c[k:k + 2], 16) for k in (1, 3, 5)) for c in layer["gradient"]]
    xs = np.linspace(0, len(stops) - 1, bar_w)
    lo_i = np.floor(xs).astype(int).clip(0, len(stops) - 2)
    t = (xs - lo_i)[:, None]
    cols = (np.array(stops)[lo_i] * (1 - t) + np.array(stops)[lo_i + 1] * t).astype("uint8")
    box.paste(Image.fromarray(np.repeat(cols[None], bar_h, axis=0), "RGB"), (px, py + lh + round(py * 0.5)))
    ty = py + lh + bar_h + py
    bd.text((px, ty), left, font=font, fill=(230, 230, 230, 255))
    rw = bd.textbbox((0, 0), right, font=font)[2]
    bd.text((px + bar_w - rw, ty), right, font=font, fill=(230, 230, 230, 255))
    canvas.paste(box, (round(x), round(y)), box)


def _chip(canvas, xy, text, font, max_w=None, anchor="lt"):
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(canvas)
    lines = _wrap(probe, text, font, max_w) if max_w else [text]
    lh = probe.textbbox((0, 0), "Ag", font=font)[3]
    tw = max(probe.textbbox((0, 0), line, font=font)[2] for line in lines)
    px, py = round(lh * 0.6), round(lh * 0.35)
    w, h = tw + 2 * px, lh * len(lines) + round(lh * 0.25) * (len(lines) - 1) + 2 * py
    box = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(box)
    bd.rounded_rectangle((0, 0, w - 1, h - 1), radius=round(lh * 0.45), fill=(0, 0, 0, 150))
    for i, line in enumerate(lines):
        bd.text((px, py + i * (lh + round(lh * 0.25))), line, font=font, fill=(255, 255, 255, 255))
    x, y = xy
    if "r" in anchor:
        x -= w
    if "b" in anchor:
        y -= h
    canvas.paste(box, (round(x), round(y)), box)
    return round(y) + h if "b" not in anchor else round(y)


def _wrap(draw, text, font, max_w):
    if not text:
        return []
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textbbox((0, 0), trial, font=font)[2] > max_w:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines[:3]


def _scale_bar(canvas, xy, m_per_px, panel_w, font):
    from PIL import ImageDraw

    target = panel_w * 0.22 * m_per_px
    exp = 10 ** math.floor(math.log10(target))
    nice = max(v * exp for v in (1, 2, 5) if v * exp <= target)
    bar_px = round(nice / m_per_px)
    label = f"{nice / 1000:g} km" if nice >= 1000 else f"{nice:g} m"
    draw = ImageDraw.Draw(canvas)
    x, y = xy
    h = max(4, round(panel_w * 0.006))
    l, t, r, b = draw.textbbox((0, 0), label, font=font)
    draw.text((x + 1, y - h - (b - t) - 8 + 1), label, font=font, fill=(0, 0, 0))
    draw.text((x, y - h - (b - t) - 8), label, font=font, fill=(255, 255, 255))
    draw.rectangle((x - 1, y - h - 1, x + bar_px + 1, y + 1), fill=(0, 0, 0))
    draw.rectangle((x, y - h, x + bar_px // 2, y), fill=(255, 255, 255))
    draw.rectangle((x + bar_px // 2, y - h, x + bar_px, y), fill=(120, 120, 120))


def _north(canvas, xy, size, font):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)
    x, y = xy
    l, t, r, b = draw.textbbox((0, 0), "N", font=font)
    cx = x - size // 2
    draw.text((cx - (r - l) // 2 + 1, y + 1), "N", font=font, fill=(0, 0, 0))
    draw.text((cx - (r - l) // 2, y), "N", font=font, fill=(255, 255, 255))
    top = y + (b - t) + round(size * 0.35)
    pts = [(cx, top), (cx + size // 2, top + size), (cx, top + round(size * 0.72)), (cx - size // 2, top + size)]
    draw.polygon(pts, fill=(255, 255, 255), outline=(0, 0, 0))


def _font(size, bold=False):
    from PIL import ImageFont

    names = (("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "arialbd.ttf") if bold else
             ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "arial.ttf"))
    for path in names + ("/System/Library/Fonts/Helvetica.ttc",):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _date_label(spec, date):
    d = datetime.date.fromisoformat(date)
    text = f"{d.day} {d.strftime('%b %Y')}"
    return text if spec["mode"] == "day" else f"{text} (±{spec['win']} days)"


def _credit(dates):
    years = sorted({d[:4] for d in dates})
    return "Contains modified Copernicus Sentinel data " + ", ".join(years)


def _caption(spec, layer):
    place = spec["place"] or "the area"
    days = [datetime.date.fromisoformat(d) for d in spec["dates"]]
    labels = [f"{d.day} {d.strftime('%B %Y')}" for d in days]
    when = f"before ({labels[0]}) and after ({labels[1]})" if len(labels) == 2 else f"on {labels[0]}"
    method = "" if spec["mode"] == "day" else f" Clearest pixels within {spec['win']} days of each date."
    return f"Sentinel-2 satellite image of {place} {when}, {layer['name']}.{method} {_credit(spec['dates'])}."


def _stem(spec, dates):
    place = re.sub(r"[^a-z0-9]+", "-", (spec["place"] or "sat-desk").lower()).strip("-")[:40] or "sat-desk"
    return f"{place}_{spec['layer'].lower()}_{'_vs_'.join(dates)}"


def _unique(folder, name):
    base, ext = os.path.splitext(name)
    path = os.path.join(folder, name)
    k = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}-{k}{ext}")
        k += 1
    return path


def _file_entry(path, label, preview):
    return {"path": path, "name": os.path.basename(path), "label": label, "bytes": os.path.getsize(path),
            "preview": os.path.relpath(preview, HERE) if preview else None}


def _preview_png(png_bytes):
    import io

    from PIL import Image

    return _save_preview(Image.open(io.BytesIO(png_bytes)))


def _preview_array(rgba):
    import numpy as np
    from PIL import Image

    return _save_preview(Image.fromarray(np.moveaxis(rgba, 0, -1), "RGBA"))


def _save_preview(im):
    import io

    im.thumbnail((720, 720))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    path = os.path.join(PREVIEW_DIR, uuid.uuid4().hex[:16] + ".png")
    _write_atomic(path, buf.getvalue())
    return path


def _add_history(result):
    items = _read_json(HISTORY_PATH, [])
    items.insert(0, result)
    _write_json(HISTORY_PATH, items[:100])


def _known_file(path):
    path = os.path.abspath(os.path.expanduser(str(path)))
    known = {f["path"] for h in _read_json(HISTORY_PATH, []) for f in h.get("files", [])}
    if path not in known or not os.path.exists(path):
        raise ValueError("This file is not in the export list.")
    return path


def _friendly(e, engine="remote"):
    text = f"{type(e).__name__}: {e}"
    local = engine == "local"
    if "NoDataError" in text or "no Sentinel-2 scene" in text:
        return "No Sentinel-2 image for this area and date. Pick a date with a scene on the timeline, or use best pixels."
    if local and isinstance(e, ModuleNotFoundError):
        return f"A Python package for the local engine is missing on this Mac ({e.name}). Open the app again to install it."
    if local and isinstance(e, MemoryError):
        return "This Mac does not have sufficient memory for this area. Draw a smaller box or choose a coarser resolution."
    if not local and re.search(r"401|403|[Uu]nauthori[sz]ed|credential|not logged in|log in|login", text):
        return "Fused did not accept the sign-in. Sign in to Fused in Setup, then try again."
    if re.search(r"[Tt]imed? ?out|ReadTimeout", text):
        if local:
            return "The data took too long to come to this Mac. Draw a smaller box or choose a coarser resolution."
        return "Fused took too long. Draw a smaller box or choose a coarser resolution."
    if local and re.search(r"ConnectionError|Name or service not known|nodename nor servname|Network is unreachable", text):
        return "This Mac cannot get the Sentinel-2 data. Make sure that this Mac is connected to the internet."
    return _short(e)


def _short(e):
    return f"{type(e).__name__}: {str(e)[:400]}"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 1)


def _alive(pid):
    try:
        pid = int(pid)
        return os.waitpid(pid, os.WNOHANG)[0] == 0
    except ChildProcessError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    except (OSError, ValueError):
        return False


def _job_path(job_id):
    return os.path.join(JOBS_DIR, job_id + ".json")


def _key(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _write_json(path, obj):
    _write_atomic(path, json.dumps(obj, default=str).encode())


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--job":
    run_job(sys.argv[2])
elif __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--op":
    try:
        print(json.dumps({"result": OPS[sys.argv[2]](**json.loads(sys.argv[3]))}, default=str))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
