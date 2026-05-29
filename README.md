# terraprint

Terraprint is two tools in one:

1. **Mission Planner** — a browser-based PWA for planning drone survey flights. Draw a polygon on a satellite map, tune altitude/overlap/speed, preview the lawnmower path, and push the mission directly to a Skyrover X1 app on your iPhone over USB.

2. **Terrain STL pipeline** — turn geographic terrain data into 3D-printable STL tiles. Give it a place name, a lat/lon bounding box, or a GeoTIFF — it produces clean, watertight STL files sized for your printer's build plate. Everything runs in Docker; the only requirement on your machine is Docker and Compose.

---

## Mission Planner

### Quick start

```bash
git clone https://github.com/irjudson/terraprint.git
cd terraprint
cp .env.example .env
# Optional: add your Mapbox token to .env for better satellite imagery
uv sync
uv run uvicorn web.app:app --reload --host 0.0.0.0 --port 8001
# open http://localhost:8001
```

Or with Docker:

```bash
docker compose up web
# open http://localhost:8001
```

Or install as a CLI tool:

```bash
uv tool install .
terraprint          # starts the server on port 8001
```

### Features

- Draw a polygon or rectangle on a satellite map to define the survey area
- Search for any place by name (Nominatim geocoding)
- Tune altitude, overlap, front/side overlap independently, and drone speed
- Preview the lawnmower flight path with start/end markers
- Stats: waypoint count, distance, estimated flight time
- Push the generated KMZ mission directly to the **Skyrover iOS app** over USB
- Metric / imperial units toggle (persists across sessions)
- PWA — add to home screen on iOS/Android for a native-like experience

### Satellite imagery

By default the planner uses USGS National Map tiles. For higher-resolution imagery, add your free [Mapbox](https://mapbox.com) token:

**Via `.env`** (applies to all users of your instance):
```bash
MAPBOX_TOKEN=pk.eyJ1...
```

**Via the in-app settings** (⚙️ button, stored in browser `localStorage`):
Paste your token and click Save. This overrides the server default for your browser only.

### Pushing missions to Skyrover (iPhone)

Requirements:
- iPhone connected via USB, unlocked, and this computer trusted
- Skyrover app installed and opened at least once
- At least one waypoint mission saved in the app (so the DB exists)
- Developer Mode ON: Settings → search "Developer" → Developer Mode → ON

The Push button injects the KMZ directly into the app's SQLite mission database via `pymobiledevice3`. No Wi-Fi, no cloud, no app update needed.

---

## Terrain STL pipeline

## How it works

```
place name  ──or──  lat/lon bbox  ──or──  local GeoTIFF
                         │
                         ▼
           TouchTerrain (in Docker container)
                         │
                         ├── Earth Engine DEM fetch (bbox/place)
                         │      └── GeoTIFF cache (skips EE on repeat runs)
                         │
                         ├── mesh repair (trimesh — ensures watertight output)
                         │
                         └── tiled STL files ready for any slicer
```

## Requirements

- Docker Engine + Compose plugin (v2)
- A free [Google Earth Engine account](https://code.earthengine.google.com/) (for bbox/place input — not needed for local GeoTIFF)
- ~5 GB free disk (image ~2 GB, data varies by area)

## One-time setup

```bash
git clone https://github.com/irjudson/terraprint.git
cd terraprint
./setup.sh          # writes .env, builds Docker image, smoke-tests imports
./ee_auth.sh        # opens browser auth flow; saves credentials locally
make check          # verify everything is ready
```

`setup.sh` automatically detects your user ID and writes it to `.env` so files created inside the container are owned by you on the host.

## Generate terrain

**By place name** (geocoded via OpenStreetMap):

```bash
./terrain.sh --place "Bozeman, Montana"
./terrain.sh --place "Grand Teton, Wyoming"
./terrain.sh --place "Glacier National Park" --place-padding 0.05
```

**By bounding box** (bottom-left lat/lon, top-right lat/lon):

```bash
./terrain.sh --bbox "44.50,-108.25,44.69,-107.97"
```

**From a local GeoTIFF** (no Earth Engine required):

```bash
./terrain.sh --dem data/usgs/my_dem.tif
```

STLs land in `data/terrain/`. Place-name runs use the place as the filename (e.g. `bozeman.stl`).

## Printer profiles

Set your printer once in `.env`:

```bash
PRINTER=configs/printers/kobra3max.json
```

Or pass it per-run:

```bash
./terrain.sh --place "Beartooth Mountains" --printer configs/printers/kobra3max.json
```

Profiles live in `configs/printers/`. The included `kobra3max.json` is tuned for the Anycubic Kobra 3 Max (400×400mm bed, 0.4mm nozzle, 380mm tile width). Copy it to make your own.

## Common options

| Flag | Description |
|---|---|
| `--place NAME` | Geocode a place name to a bounding box |
| `--place-padding DEG` | Add margin in degrees around a place bbox |
| `--bbox lat1,lon1,lat2,lon2` | Explicit bounding box |
| `--dem PATH` | Local GeoTIFF instead of Earth Engine |
| `--printer PATH` | Printer profile JSON |
| `--z-scale N` | Vertical exaggeration (1.0 = true scale) |
| `--tile-width MM` | Tile width in mm (overrides printer profile) |
| `--ntiles-x N` | Split into N tiles across longitude |
| `--ntiles-y N` | Split into N tiles across latitude |
| `--dem-source ASSET` | Earth Engine DEM asset (default: `USGS/3DEP/10m`) |
| `--no-cache` | Always fetch from Earth Engine, skip local cache |

## Make targets

```bash
make build                              # build the processor Docker image
make check                              # verify compose config and image
make auth                               # run Earth Engine authentication
make terrain PLACE="Bozeman, Montana"   # generate terrain by place name
make terrain BBOX="lat1,lon1,lat2,lon2" # generate terrain by bounding box
make terrain DEM=path/to/file.tif       # generate terrain from local GeoTIFF
make cache-clear                        # delete cached GeoTIFFs
make clean                              # remove output STLs
make help                               # show all options
```

## DEM sources

| Asset | Resolution | Coverage |
|---|---|---|
| `USGS/3DEP/10m` | 10m | Continental US + territories (default) |
| `USGS/SRTMGL1_003` | ~30m | Near-global |
| `USGS/GMTED2010` | ~230m | Global |
| `NOAA/NGDC/ETOPO1` | ~1800m | Global (oceans + land) |
| `JAXA/ALOS/AW3D30/V2_2` | 30m | Near-global |
| `NRCan/CDEM` | 20m | Canada |
| `AU/GA/AUSTRALIA_5M_DEM` | 5m | Australia |
| `MERIT/DEM/v1_0_3` | ~90m | Global, hydrologically conditioned |

For international terrain, use `USGS/SRTMGL1_003` (~30m, near-global):

```bash
./terrain.sh --place "Mont Blanc" --dem-source USGS/SRTMGL1_003
```

## Local GeoTIFF (no Earth Engine)

Download 1m USGS DEMs from [The National Map](https://apps.nationalmap.gov/lidar-explorer), then merge tiles:

```bash
bash scripts/usgs_dem_fetch.sh
make terrain DEM=data/usgs/usgs_merged.tif
```

## Scale guidance (Kobra 3 Max, 380mm tile)

| Terrain type | Recommended z-scale | Notes |
|---|---|---|
| Rocky Mountains / high relief | 1.0–1.5 | True scale looks good |
| Rolling hills | 2.0–3.0 | |
| Plains / coastal | 3.0–5.0 | |

For the default 10m DEM, a ~9.5km bbox width maps one DEM cell to one model pixel at the 380mm tile width. Split larger areas with `--ntiles-x`/`--ntiles-y`.

## Caching

On the first bbox or place-name run, terraprint fetches a GeoTIFF from Earth Engine and caches it in `data/cache/`. Subsequent runs with the same bbox use the cached file and skip the Earth Engine API call entirely (~10s vs ~30s).

Clear the cache with `make cache-clear`.

## Configuration

Copy `.env.example` to `.env` and edit:

```bash
DATA_ROOT=./data
HOST_UID=1026           # auto-set by setup.sh — do not edit manually
PRINTER=configs/printers/kobra3max.json
# DEM_SOURCE=USGS/3DEP/10m
```

## File layout

```
terraprint/
├── Makefile
├── terrain.sh              # convenience wrapper around make terrain
├── setup.sh                # first-time setup
├── ee_auth.sh              # Earth Engine authentication
├── test.sh                 # acceptance test suite
├── pyproject.toml          # uv/pip project (terraprint CLI entry point)
├── .env.example
├── docker-compose.yml
├── docker/
│   ├── processor/
│   │   └── Dockerfile      # terrain STL pipeline
│   └── web/
│       └── Dockerfile      # mission planner web app
├── scripts/
│   ├── run_touchterrain.py    # terrain pipeline script
│   ├── generate_survey.py     # KMZ waypoint generation
│   ├── skyrover_ios_bridge.py # CLI iPhone push tool
│   └── usgs_dem_fetch.sh      # USGS DEM download helper
├── web/
│   ├── app.py              # FastAPI backend (mission planner)
│   └── static/
│       ├── index.html      # PWA frontend
│       ├── favicon.svg
│       ├── manifest.json
│       └── sw.js
├── configs/
│   ├── touchterrain_default.json
│   └── printers/
│       └── kobra3max.json  # Anycubic Kobra 3 Max profile
└── data/                   # gitignored — created by setup.sh
    ├── cache/              # cached GeoTIFFs from Earth Engine
    ├── terrain/            # STL output
    └── usgs/               # manually downloaded DEMs
```

## Why containers

GDAL is notoriously hard to install consistently — apt packages, pip wheels, conda, and system rasterio all disagree on library versions. A container pins every dependency (GDAL, rasterio, earthengine-api, TouchTerrain, trimesh) in one reproducible layer. Same result on any Linux box with Docker, no `apt install gdal-*`, no version drift.

## What's next

See [ROADMAP.md](ROADMAP.md) for upcoming stages: drone photogrammetry (MVP 1), building extraction (MVP 2), magnet pockets (MVP 3), and more.
