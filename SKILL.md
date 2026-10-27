---
name: sat-desk
description: Find the Sentinel-2 satellite dates for a place, and export a newsroom PNG (with title, scale bar and credit) or a GeoTIFF, for one date or before and after.
approve: []
---
# Sat Desk

Search runs on this Mac (Earth Search). Exports send `process_request` jobs of `sentinel_utils` to the engine in the
`engine` setting: `"remote"` (default) runs them on Fused with the signed-in Fused account, `"local"` runs them on
this Mac with `fused.run(..., engine="local")` and needs no Fused sign-in. The app writes only the result. Settings are in
`.fused/data/settings.json`, exports go to `.fused/data/exports/` (or the folder in the settings), and caches are
in `.fused/cache/v1/`.

All calls go through one file, `worker.py`, with `op` naming the action and `args` a JSON string. The page runs it
as a warm worker.

## worker.py
Runs one operation. `search` reads the network and writes cache files only. `export_start` starts a background
export that writes files to the export folder. `save_settings` and `pick_folder` write `.fused/data/settings.json`.
`tile` makes one map tile, on this Mac or on Fused (see `engine`).
- Args: `op` (str, default "ping"; one of "ping", "config", "account", "search", "export_start", "export_status",
  "exports", "save_settings", "pick_folder", "reveal", "open_file", "open_folder", "preview_compose",
  "engine_check", "tile"), `args` (str, JSON object, default "{}")
- `config` → `{"settings": {"package", "export_dir", "engine"}, "engine": "remote" | "local", "local":
  {"deps_ok", "missing"}, "export_dir", ...}`
- `save_settings` args: `engine` ("remote" or "local"), `package`, `export_dir` (all optional) → the
  same as `config`.
- `engine_check` → `{"deps_ok", "missing": [pip names]}`. It imports odc-stac, mercantile, rio-tiler and
  pystac in a separate Python process.
- `tile` args: `z`, `x`, `y` (int), `q` (object of str: `date`, `days`, `what`, `method`, `max_cloud`, `cloud_mask`,
  `min_zoom`, `cmap`, `vmin`, `vmax`) → `{"png": base64}`. A 256 x 256 PNG,
  transparent where there is no image. With engine `"local"` it runs `su_reader.tile` in the warm worker; with
  `"remote"` it runs `tile_udf.py` on the signed-in Fused account with the pinned package (a GitHub URL) and keeps
  nothing on disk.
- `search` args: `bbox` ([w, s, e, n], at most 1.2° x 1.2°), `start`, `end` (str "YYYY-MM-DD"), `cloud` (float, max scene
  cloud %, default 100) → `{"dates": [{"date", "scenes", "cloud", "cloud_min", "cloud_max", "platforms", "tiles", "rings",
  "items": [{"tile", "cloud", "rings"}]}], "bbox", "tiles", "fetched", "units", "seconds"}`. Rings are full scene
  footprints (not clipped). Results come from Earth Search and are cached per Sentinel-2 tile and half year.
- `export_start` args: `spec` (object): `area` ([w, s, e, n]), `layer` (an id from layers.json), `dates` (one or two
  "YYYY-MM-DD"), `mode` ("day" = one pass, or "best" = median of clear pixels within `win` days), `win` (int, default 15),
  `cloud` (max scene cloud %), `kind` ("png", "tif_visual" or "tif_data"), `resolution` (m, for GeoTIFF: 10, 20, 40,
  80 or 160), `png_width` (1200, 1600 or 2400), `title`, `place` (str, both optional), `date_label`, `place_label`
  (bool, default true), `scale_bar`, `north`, `legend`, `map_labels` (bool, default false), `outlines` (object: date →
  list of lon/lat rings, drawn as a dashed scene outline; `mode` "day" only), `preview` (bool: make a preview, not a
  file). The area is at most 100 km on each side. → `{"id": str, "total": int}`. A preview result is
  `{"preview": {"image", "panels": [{"date", "image", "bounds"}]}}`: `image` is the composed JPEG, each panel image is
  a Web Mercator PNG for `bounds` (paths relative to the app folder).
- `preview_compose` args: `spec` (the same spec as the preview, with other title or On image options) → `{"image"}`.
  It uses the stored preview data and does not call Fused.
- `export_status` args: `id` (str) → `{"state": "queued" | "running" | "done" | "error", "step", "done", "total",
  "engine", "result": {"files": [{"path", "name", "label", "bytes", "preview"}], "caption", "credit", "folder",
  "engine"}, "error"}`. The engine is the `engine` setting at `export_start`.
- `exports` args: `limit` (int, default 20) → `{"items": [result, ...], "folder": str}`
- `account` args: `refresh` (bool) → `{"logged_in": bool, "email", "handle"}`. It reads the Fused sign-in that fused-render
  keeps in `~/.fused/credentials`; it writes nothing.
- Example: `{"action":"py","app":"sat-desk","file":"worker.py","args":{"op":"search","args":"{\"bbox\":[-118.62,34.02,-118.47,34.10],\"start\":\"2024-12-01\",\"end\":\"2025-02-15\"}"}}`
- Example: `{"action":"py","app":"sat-desk","file":"worker.py","args":{"op":"export_start","args":"{\"spec\":{\"area\":[-118.60,34.03,-118.49,34.09],\"layer\":\"swir\",\"dates\":[\"2025-02-11\"],\"mode\":\"day\",\"kind\":\"png\",\"title\":\"Palisades burn scar\",\"place\":\"Pacific Palisades\"}}"}}`
