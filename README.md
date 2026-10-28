# Sat Desk

Sentinel-2 images for reporters. A local fused-render app: the page shows a map and a timeline, and Fused does the
heavy work. The app gets back only the result.

## Before you open it

- **fused-render** on a Mac with macOS 14 or later (Apple Silicon), or macOS 15 or later (Intel). The Python packages
  install from ready-made files only for these versions. The folder picker and "Show in Finder" use macOS; the rest
  also works on Linux.
- **An internet connection.** The map, the date search and the satellite data all come from the internet.
- **First open:** fused-render installs the Python packages in `pyproject.toml` (with odc-stac and rio-tiler). This
  takes a few minutes one time.
- **No Fused account by default.** The app opens with "This Mac": your Mac makes the images, and the app needs no
  sign-in. Select "Fused" at the top to make the images on Fused; then the app asks you to sign in.
- **Your own data stays on your machine:** settings, export history and caches are in `.fused/`. Exports go to
  `.fused/data/exports/` until you choose a folder in Export. Do not copy `.fused/` or `.venv/` when you share the
  folder.
- **Shared services the app uses:** the map tiles of the Fused engine come from a shared Fused endpoint (set in
  `DEFAULT_TILE_BASE` in `worker.py`; no sign-in needed). The image code is `sentinel_utils` from the public
  `fusedio/udfs` repository, at a pinned commit (`DEFAULT_PACKAGE`). The base map and labels are from Esri.

## What you can do

- **Browse.** Find a place. The timeline shows each date with a Sentinel-2 image of the view. The bar height shows how
  much of the view the image covers, and the colour shows the cloud. Click a date, or use the arrow keys. Max cloud
  is a number box: scroll on it, use the arrow keys, or type. For Best pixels, the window (± days) is next to the date
  in the timeline, and the map shows the median of the clear pixels, so the image has no seams. Images show from map
  zoom 8. When the map is zoomed out, the dates are
  for the centre 100 km of the view.
  - **One day:** the image of one satellite pass. Clouds stay.
  - **Best pixels:** the clearest pixels within a window of days around the date.
- **Compare.** Before and after on a swipe map. When you turn it on, the first click on the timeline sets Before and the
  next click sets After. Later, select the Before or After marker, then click a date.
- **Mosaic card** (bottom left of the map): the eye button (or the M key) shows or hides the Sentinel-2 mosaic. The card
  shows what the mosaic is made of: the date range, the number of scenes and the Sentinel-2 tile IDs (for Before and
  After in Compare). Hover the eye to see how many map tiles came from Fused.
- **Layers.** True colour, false colour, SWIR, agriculture, urban, NDVI, NDMI, MNDWI, NBR, NDSI. Each layer has a short
  "how to read it" text and a legend.
- **Export** (form: Format, Output, Area, Preview, Title, Place, Width or Resolution, On image, Save to):
  - **Area:** the white frame on the map shows what goes in the file. "Map view" is the frame inside the view, "Draw
    box" is a box that you draw. The area is 100 km on each side at most.
  - **Preview:** makes the image on Fused and shows it in the panel (click to enlarge). The map also shows it inside
    the frame, so a large area has an overview when the map is zoomed out. When you change the title or what is on the
    image, the preview changes in about 1–3 s. The export after a preview uses the same Fused data, so it is fast.
  - **Newsroom image (PNG):** the image fills the file. Nothing goes on it by default. Tick what you want: date, place,
    map labels (names of towns), scene outline (One day only), scale bar, north arrow, legend (index layers). A title
    goes on only if you type one. BEFORE / AFTER labels and the small Copernicus credit are always on. Below Width you
    see the pixel size of the file and how many metres one pixel is. The app also writes a caption that you can copy.
  - **GeoTIFF, as on the map:** RGBA image with map position.
  - **GeoTIFF, data values:** band values (reflectance x 10000), or the index values for an index layer.
- **History.** All exports, with Open, Show in Finder and Copy caption.

## How it works

| Part | Where it runs |
|---|---|
| Map tiles | Fused engine: `tile_udf.py` runs `su_reader.tile` from the pinned `sentinel_utils` package on your own Fused account (`fused.run(..., engine="remote")`, one call for each tile, at most 6 at a time), map zoom 8 or closer. Fused keeps each tile for 7 days. The page keeps every tile it has seen in the browser Cache Storage (up to 1500 tiles), shows zoom-9 tiles first while the sharp tiles load, and fetches one ring of tiles around the view after the map is idle. This Mac engine: the `tile` op in the warm worker (see Engine). |
| Date search | Earth Search STAC, straight from the warm worker on this Mac, with the fields extension (about 50 KB a call). Results are cached by Sentinel-2 tile, collection and half year in `.fused/cache/v1/search/`, so a pan only fetches tiles the app has not seen. The current half year is refreshed after 1 hour with an incremental call. First search about 1.5–2.5 s, then 20 ms. |
| Exports | `process_request` jobs of `sentinel_utils` on the engine that you select (see Engine). One job for each date, all at the same time in separate processes. |
| Rendering of exports | On this Mac, with the same colours as the map (`layers.json`). |

"Best pixels" exports use the median of the clear pixels (SCL classes 4, 5, 6, 11). Where no clear pixel exists, the
app fills the gap with the median of all pixels.

## Engine

Sat Desk has two engines. Both use the same `sentinel_utils` code and the same requests. Only the engine changes:
`fused.run(su, request=..., engine="remote")` or `engine="local"`. Set it with the `engine` setting (`"local"`, the
default, or `"remote"`).

| | Fused (`remote`) | This Mac (`local`) |
|---|---|---|
| Map tiles | `tile_udf.py` on Fused, called by the worker for each tile | The `tile` op in the fused-render warm worker, called with `fused.daemon.run`; it calls `su_reader.tile` |
| Exports and previews | Fused workers | Processes on this Mac, one for each image, so the images use all the cores |
| Fused sign-in | Necessary | Not necessary |
| Good for | Large areas and many images | Small areas, and work without a Fused account |

In both engines the Sentinel-2 data comes from the public AWS bucket and the Earth Search STAC. With the local engine,
this Mac reads the data from the internet itself, so the speed depends on your connection. With "This Mac" the app
calls the Fused server only to get the `sentinel_utils` code (no sign-in).

What the local engine uses:

- **Python packages:** `odc-stac`, `pystac`, `mercantile` and `rio-tiler` (in `pyproject.toml`). fused-render installs
  them when it builds the app environment. `engine_check` tells you if one is missing.
- **The `sentinel_utils` package:** the same as the Fused engine: `fused.load(<package>)` of the pinned GitHub URL
  (`DEFAULT_PACKAGE` in `worker.py`), or of a local folder if the `package` setting is a folder. For a GitHub URL the
  Fused SDK gets the code from the Fused server (no sign-in needed), so the first load needs the network.
- **Map tiles:** the page asks the fused-render warm worker (`worker.py`) for each tile. There is no separate server.
  The worker does not keep tiles on disk; the browser keeps the tiles it has seen. The browser opens at most 6
  connections to fused-render, so the page sends at most 6 worker calls at the same time. App calls go first, and map
  tiles wait in a queue. The page removes a tile from the queue when the map does not need it any more (pan, zoom,
  new date, other engine). The worker makes at most 8 tiles at the same time. It keeps
  one pool of S3 read threads, so the connections stay open from tile to tile, and it reads a slightly smaller
  overview for map tiles only (`GDAL_OVERVIEW_OVERSAMPLING_THRESHOLD=1.35`). For map tiles it also reads the byte
  ranges of a file one after the other (`GDAL_HTTP_MULTIRANGE=SERIAL`), so tiles that load at the same time share the
  data that they download. Exports read the full 10 m data, without these two settings.

First-time setup for "This Mac": open the app one time so that fused-render installs the packages. This Mac is the
default engine.

Typical times (Los Angeles area, home connection): one new map tile 3 to 5 s, a full view of new tiles about
10 to 17 s. A one-date PNG export of a 32 x 28 km area about 12 s, a before and
after preview with best pixels (4 images) about 55 s.

## Account

The Fused engine needs a Fused sign-in. The This Mac engine does not. The app uses the Fused sign-in of fused-render (`~/.fused/credentials`). With the Fused engine, if you are not
signed in when the app opens, it asks you to sign in. "Sign in to Fused" starts the fused-render sign-in
(`POST /api/canvases/login`) and opens the Fused page in your browser; the app waits until the sign-in is done. The
account button, Preview and Export open the same prompt. The
`sentinel_utils` source is set in `worker.py` (`DEFAULT_PACKAGE`); `.fused/data/settings.json` can override it. No
shared canvas or share token is used.

## Files

- `index.html`: the page (MapLibre is in `vendor/`).
- `worker.py`: search, settings, account and export jobs (`python worker.py --job <spec>` runs one export).
- `tile_udf.py`: the UDF behind map tiles on the Fused engine. It loads the pinned `sentinel_utils` package and returns one PNG tile as base64.
- `layers.json`: layers, colour ramps, legend texts and the example stories. The page and the worker both read it.
