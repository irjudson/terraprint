#!/usr/bin/env python3
"""Convert a bounding box or local GeoTIFF into printable STL tiles via TouchTerrain."""

import argparse
import json
import logging
import os
import shutil
import sys
import warnings
import zipfile
from pathlib import Path

# Suppress deprecation/future warnings from ee and GDAL before any imports fire them.
warnings.filterwarnings("ignore")
# Suppress the TouchTerrain service-account EE init warning (expected — we use OAuth2).
logging.disable(logging.WARNING)


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        raw = json.load(f)
    # Strip inline-comment keys (underscore-prefixed) that TouchTerrain rejects
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def parse_bbox(bbox_str: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in bbox_str.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"BBOX must be 'lat1,lon1,lat2,lon2', got: {bbox_str!r}"
        )
    try:
        return tuple(float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"BBOX values must be floats: {exc}") from exc


def cache_key(bllat: float, bllon: float, trlat: float, trlon: float, dem_source: str) -> str:
    src = dem_source.replace("/", "_")
    return f"{src}_{bllat:.6f}_{bllon:.6f}_{trlat:.6f}_{trlon:.6f}.tif"


def repair_stl(path: Path) -> str:
    import trimesh
    mesh = trimesh.load(str(path), force="mesh")
    before = mesh.is_watertight
    mesh.merge_vertices()
    mesh.remove_degenerate_faces()
    mesh.remove_duplicate_faces()
    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fill_holes(mesh)
    mesh.export(str(path))
    after = mesh.is_watertight
    if not before and after:
        return "repaired → watertight"
    elif not before and not after:
        return "repaired (non-manifold edges remain)"
    return "already watertight"


def slugify(name: str) -> str:
    import re
    # Use the first meaningful part before a comma (e.g. "Bozeman" from "Bozeman, Montana")
    name = name.split(",")[0].strip()
    name = name.lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s]+", "_", name)
    return name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run TouchTerrain to produce printable STL terrain tiles."
    )

    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--bbox",
        metavar="lat1,lon1,lat2,lon2",
        help="Bounding box (bottom-left and top-right corners). Fetches from Earth Engine.",
    )
    source.add_argument(
        "--dem",
        metavar="PATH",
        help="Path to a local GeoTIFF DEM inside the container.",
    )
    source.add_argument(
        "--place",
        metavar="NAME",
        help="Place name or geographic feature to look up via Nominatim (OpenStreetMap).",
    )
    p.add_argument(
        "--place-padding",
        type=float,
        default=0.0,
        metavar="DEGREES",
        help="Extra margin in degrees to add around a --place bounding box (default: 0).",
    )

    p.add_argument("--out", required=True, metavar="DIR", help="Output directory for STL files.")
    p.add_argument("--tile-width", type=float, default=None, help="Tile width in mm.")
    p.add_argument("--ntiles-x", type=int, default=None, help="Number of tiles in X direction.")
    p.add_argument("--ntiles-y", type=int, default=None, help="Number of tiles in Y direction.")
    p.add_argument("--base-thickness", type=float, default=None, help="Base thickness in mm.")
    p.add_argument("--z-scale", type=float, default=None, help="Vertical exaggeration factor.")
    p.add_argument("--printres", type=float, default=None, help="Horizontal mesh resolution in mm.")
    p.add_argument(
        "--dem-source",
        default="USGS/3DEP/10m",
        help=(
            "Earth Engine asset name (default: USGS/3DEP/10m). Ignored when --dem is set. "
            "Valid sources: USGS/3DEP/10m, USGS/SRTMGL1_003, USGS/GMTED2010, "
            "NOAA/NGDC/ETOPO1, JAXA/ALOS/AW3D30/V2_2, NRCan/CDEM, "
            "AU/GA/AUSTRALIA_5M_DEM, USGS/GTOPO30, CPOM/CryoSat2/ANTARCTICA_DEM, "
            "MERIT/DEM/v1_0_3"
        ),
    )
    p.add_argument(
        "--config",
        default="configs/touchterrain_default.json",
        help="Path to base JSON config file.",
    )
    p.add_argument(
        "--printer",
        default=None,
        metavar="PATH",
        help="Printer profile JSON (e.g. configs/printers/kobra3max.json). "
             "Overrides base config; CLI flags override both.",
    )
    p.add_argument(
        "--cache-dir",
        default="/data/cache",
        metavar="DIR",
        help="Directory for cached GeoTIFFs (default: /data/cache).",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the GeoTIFF cache and always fetch from Earth Engine.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Layer configs: base → printer profile → CLI flags
    tt_args = load_config(args.config)
    if args.printer:
        tt_args.update(load_config(args.printer))

    # Apply CLI overrides on top
    if args.tile_width is not None:
        tt_args["tilewidth"] = args.tile_width
    if args.ntiles_x is not None:
        tt_args["ntilesx"] = args.ntiles_x
    if args.ntiles_y is not None:
        tt_args["ntilesy"] = args.ntiles_y
    if args.base_thickness is not None:
        tt_args["basethick"] = args.base_thickness
    if args.z_scale is not None:
        tt_args["zscale"] = args.z_scale
    if args.printres is not None:
        tt_args["printres"] = args.printres

    # --- Resolve --place to a bbox via Nominatim ---
    if args.place:
        import urllib.request
        import urllib.parse

        query = urllib.parse.urlencode({"q": args.place, "format": "json", "limit": 1})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{query}",
            headers={"User-Agent": "terraprint/1.0 (github.com/terraprint)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                results = json.loads(r.read())
        except Exception as exc:
            print(f"ERROR: Nominatim lookup failed: {exc}", file=sys.stderr)
            sys.exit(1)

        if not results:
            print(f"ERROR: No results found for '{args.place}'", file=sys.stderr)
            print("Try a more specific name, e.g. 'Bozeman, Montana' or 'Beartooth Mountains, MT'.", file=sys.stderr)
            sys.exit(1)

        hit = results[0]
        # boundingbox: [south_lat, north_lat, west_lon, east_lon]
        bb = hit["boundingbox"]
        pad = args.place_padding
        south = float(bb[0]) - pad
        north = float(bb[1]) + pad
        west  = float(bb[2]) - pad
        east  = float(bb[3]) + pad
        args.bbox = f"{south},{west},{north},{east}"
        print(f"Place      : {hit['display_name']}")
        if pad:
            print(f"Padding    : ±{pad}°")

    # --- Determine data source (EE fetch or local DEM) ---
    using_ee = False

    if args.bbox:
        bllat, bllon, trlat, trlon = parse_bbox(args.bbox)

        # Check GeoTIFF cache before hitting Earth Engine
        cached_tif = cache_dir / cache_key(bllat, bllon, trlat, trlon, args.dem_source)
        if not args.no_cache and cached_tif.exists():
            print(f"Cache hit  : {cached_tif.name}")
            print(f"Source     : local cache (no EE call)")
            tt_args["importedDEM"] = str(cached_tif)
        else:
            if args.no_cache:
                print(f"Source     : Earth Engine — {args.dem_source}  (cache disabled)")
            else:
                print(f"Source     : Earth Engine — {args.dem_source}  (no cache)")
            tt_args["bllat"] = bllat
            tt_args["bllon"] = bllon
            tt_args["trlat"] = trlat
            tt_args["trlon"] = trlon
            tt_args["DEM_name"] = args.dem_source
            using_ee = True

        print(f"Bbox       : ({bllat}, {bllon}) → ({trlat}, {trlon})")
    else:
        dem_path = Path(args.dem)
        if not dem_path.exists():
            print(f"ERROR: DEM file not found: {args.dem}", file=sys.stderr)
            sys.exit(1)
        tt_args["importedDEM"] = str(dem_path)
        print(f"Source     : local DEM — {args.dem}")

    tile_w = tt_args.get("tilewidth", 250)
    ntx = tt_args.get("ntilesx", 1)
    nty = tt_args.get("ntilesy", 1)
    print(f"Tile       : {tile_w} mm × {ntx}×{nty} tiles")
    print(f"Z-scale    : {tt_args.get('zscale', 1.0)}")
    print(f"Print res  : {tt_args.get('printres', 0.4)} mm")
    print()

    from touchterrain.common import TouchTerrainEarthEngine as TT

    logging.disable(logging.NOTSET)

    if using_ee:
        import ee
        try:
            ee.Initialize()
        except Exception as exc:
            print(f"ERROR: Could not initialize Earth Engine: {exc}", file=sys.stderr)
            print("Make sure you have run ./ee_auth.sh and authenticated successfully.", file=sys.stderr)
            sys.exit(1)

    tmp_dir = out_dir / "_work"
    tmp_dir.mkdir(exist_ok=True)
    tt_args["temp_folder"] = str(tmp_dir)
    tt_args["zip_file_name"] = "tiles"

    print("Running TouchTerrain…")
    sys.stdout.flush()

    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)
    try:
        totalsize, full_zip = TT.get_zipped_tiles(**tt_args)
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)

    # Extract STLs; also cache the GeoTIFF for future runs
    place_slug = slugify(args.place) if args.place else None
    stl_files = []
    stl_index = 0
    with zipfile.ZipFile(full_zip) as zf:
        for name in zf.namelist():
            low = name.lower()
            if low.endswith((".stl", ".stlb")):
                orig = Path(name)
                if place_slug:
                    suffix = orig.suffix
                    stl_index += 1
                    tile_num = f"_{stl_index}" if stl_index > 1 else ""
                    dest = out_dir / f"{place_slug}{tile_num}{suffix}"
                else:
                    dest = out_dir / orig.name
                with zf.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                stl_files.append(dest)
            elif low.endswith(".tif") and using_ee and not args.no_cache:
                # Cache the fetched GeoTIFF so subsequent runs skip EE
                with zf.open(name) as src, open(cached_tif, "wb") as dst:
                    dst.write(src.read())

    shutil.rmtree(tmp_dir, ignore_errors=True)

    if using_ee and not args.no_cache and cached_tif.exists():
        size_mb = cached_tif.stat().st_size / 1024 / 1024
        print(f"Cached DEM : {cached_tif.name}  ({size_mb:.1f} MB)")

    print("Repairing mesh(es)…")
    for stl in stl_files:
        status = repair_stl(stl)
        print(f"  {stl.name}  [{status}]")

    print()
    print(f"Done — {len(stl_files)} STL file(s) written to {out_dir}:")
    for stl in sorted(stl_files):
        size_mb = stl.stat().st_size / 1024 / 1024
        print(f"  {stl.name}  ({size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
