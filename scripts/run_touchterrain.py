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

# Native ground resolution (metres/pixel) for each supported EE DEM asset.
DEM_RES_M: dict[str, int] = {
    "USGS/3DEP/1m":            1,
    "AU/GA/AUSTRALIA_5M_DEM":  5,
    "USGS/3DEP/10m":          10,
    "NRCan/CDEM":             20,
    "JAXA/ALOS/AW3D30/V2_2": 30,
    "USGS/SRTMGL1_003":       30,
    "MERIT/DEM/v1_0_3":       90,
    "USGS/GMTED2010":        232,
    "USGS/GTOPO30":          927,
    "NOAA/NGDC/ETOPO1":     1852,
}

# Primary elevation band name for each EE asset (used for min/max stats queries).
DEM_BAND: dict[str, str] = {
    "USGS/3DEP/1m":            "elevation",
    "AU/GA/AUSTRALIA_5M_DEM":  "elevation",
    "USGS/3DEP/10m":           "elevation",
    "NRCan/CDEM":              "elevation",
    "JAXA/ALOS/AW3D30/V2_2":  "AVE_DSM",
    "USGS/SRTMGL1_003":        "elevation",
    "MERIT/DEM/v1_0_3":        "dem",
    "USGS/GMTED2010":          "be75",
    "USGS/GTOPO30":            "elevation",
    "NOAA/NGDC/ETOPO1":        "bedrock",
}


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


def _make_text_mesh(text: str, font_height_mm: float, depth_mm: float):
    """Extrude a string into a 3-D mesh lying in the XY plane, extruded in +Z."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    from shapely.geometry import Polygon
    import trimesh

    fp = FontProperties(family="monospace")
    tp = TextPath((0, 0), text, size=font_height_mm, prop=fp)

    raw = []
    for verts in tp.to_polygons():
        if len(verts) < 3:
            continue
        try:
            p = Polygon(verts)
            if p.is_valid and p.area > 0.05:
                raw.append(p)
        except Exception:
            continue

    if not raw:
        return None

    # Sort largest-first; smaller polygons contained within larger ones are holes.
    raw.sort(key=lambda p: p.area, reverse=True)
    is_hole = [False] * len(raw)
    for i in range(len(raw)):
        for j in range(i + 1, len(raw)):
            if not is_hole[j] and raw[i].contains(raw[j]):
                is_hole[j] = True

    used_as_hole: set[int] = set()
    meshes = []
    for i, outer in enumerate(raw):
        if is_hole[i]:
            continue
        holes = [
            list(raw[j].exterior.coords)
            for j in range(len(raw))
            if j != i and is_hole[j] and outer.contains(raw[j]) and j not in used_as_hole
        ]
        used_as_hole.update(
            j for j in range(len(raw))
            if j != i and is_hole[j] and outer.contains(raw[j])
        )
        try:
            poly = Polygon(list(outer.exterior.coords), holes)
            if not poly.is_valid:
                poly = poly.buffer(0)
            meshes.append(trimesh.creation.extrude_polygon(poly, depth_mm))
        except Exception:
            continue

    return trimesh.util.concatenate(meshes) if meshes else None


def add_label(
    path: Path,
    search_term: str,
    center_lat: float,
    center_lon: float,
    base_thick_mm: float,
) -> None:
    """Place name and coordinates on a flat-topped platform in the SE corner.

    A solid rectangular platform is raised just above the local terrain height
    in that corner so its flat top is always visible from above.  Text is
    extruded upward (+Z) from the platform top and reads naturally when
    looking down at the tile with north at the top.
    """
    import trimesh

    FONT_H    = 4.5   # mm character height
    TEXT_RISE = 1.0   # mm text protrudes above platform surface
    PAD       = 3.5   # mm padding inside platform around text
    LINE_SEP  = 7.0   # mm between line baselines
    MARGIN    = 5.0   # mm inset from tile east/south edges
    MAX_CHARS = 50

    lat_h = "N" if center_lat >= 0 else "S"
    lon_h = "E" if center_lon >= 0 else "W"
    lines = [
        search_term[:MAX_CHARS],
        f"{lat_h}{abs(center_lat):.4f}  {lon_h}{abs(center_lon):.4f}",
    ]

    mesh = trimesh.load(str(path), force="mesh")
    x_max = mesh.bounds[1][0]
    y_min = mesh.bounds[0][1]

    # Build text meshes; track widest line for platform sizing.
    text_info = []   # (mesh, text_width, line_index)
    max_tw = 0.0
    for i, line in enumerate(lines):
        tm = _make_text_mesh(line, FONT_H, TEXT_RISE)
        if tm is None:
            continue
        tw = tm.bounds[1][0] - tm.bounds[0][0]
        max_tw = max(max_tw, tw)
        text_info.append((tm, tw, i))

    if not text_info:
        return

    n = len(text_info)
    plat_w = max_tw + 2 * PAD                          # X dimension
    plat_d = (n - 1) * LINE_SEP + FONT_H + 2 * PAD    # Y dimension

    # Platform footprint in SE corner
    plat_x0 = x_max - MARGIN - plat_w
    plat_x1 = x_max - MARGIN
    plat_y0 = y_min + MARGIN
    plat_y1 = y_min + MARGIN + plat_d

    # Sample the full SE quadrant (platform footprint + 100 mm halo in each
    # direction) so that the platform top clears all terrain that would visually
    # surround it — not just the tight footprint directly below it.
    verts = mesh.vertices
    se_halo = 100.0
    se_mask = (
        (verts[:, 0] >= plat_x0 - se_halo) &
        (verts[:, 1] <= plat_y1 + se_halo)
    )
    local_z_max = float(verts[se_mask, 2].max()) if se_mask.any() else base_thick_mm
    plat_z = local_z_max + 1.0   # 1 mm clearance above all SE-corner terrain

    # Solid platform from z=0 to plat_z.
    platform = trimesh.creation.box([plat_w, plat_d, plat_z])
    platform.apply_translation([
        (plat_x0 + plat_x1) / 2,
        (plat_y0 + plat_y1) / 2,
        plat_z / 2,
    ])

    # Text on platform top, extruded upward — readable from above with north at top.
    # Line 0 (place name) at larger y (north = visually on top); line 1 (coords) below.
    parts = [mesh, platform]
    for tm, tw, i in text_info:
        tx = plat_x0 + PAD + (max_tw - tw) / 2   # centre each line horizontally
        ty = plat_y0 + PAD + (n - 1 - i) * LINE_SEP
        tm.apply_translation([tx, ty, plat_z])
        parts.append(tm)

    trimesh.util.concatenate(parts).export(str(path))


def add_place_marker(path: Path, base_thick_mm: float) -> Path:
    """Output a cone marker STL centred on the searched-for place (tile origin).

    With tile_centered=true (the default), the geographic centre of the tile is
    always at (0, 0) in mesh space — exactly where the searched place sits.
    The returned file can be imported alongside the terrain STL in BambuStudio,
    Orca Slicer, or PrusaSlicer as a second body and assigned a different colour.
    """
    import trimesh

    CONE_R = 4.0    # mm  base radius
    CONE_H = 10.0   # mm  height (visually prominent, still fine to print)

    mesh = trimesh.load(str(path), force="mesh")
    verts = mesh.vertices

    # Sample terrain within 15 mm of tile centre to find the surface z there.
    near = (verts[:, 0] ** 2 + verts[:, 1] ** 2) < 15.0 ** 2
    z_base = float(verts[near, 2].max()) if near.any() else base_thick_mm

    cone = trimesh.creation.cone(radius=CONE_R, height=CONE_H)
    cone.apply_translation([0.0, 0.0, z_base])

    marker_path = path.with_name(path.stem + "_marker" + path.suffix)
    cone.export(str(marker_path))
    return marker_path


def add_scale_bar(
    path: Path,
    geo_width_m: float,
    tile_width_mm: float,
    base_thick_mm: float,
) -> str:
    """Embed a raised scale bar along the south edge of the tile base."""
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    bounds = mesh.bounds
    x_min = bounds[0][0]
    y_min = bounds[0][1]

    m_per_mm = geo_width_m / tile_width_mm

    # Pick the largest round distance whose bar fits between 10 % and 50 % of tile width.
    bar_mm = bar_m = None
    for candidate_m in [100, 200, 500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000]:
        mm = candidate_m / m_per_mm
        if tile_width_mm * 0.10 <= mm <= tile_width_mm * 0.50:
            bar_mm, bar_m = mm, candidate_m
            break
    if bar_mm is None:
        bar_m    = m_per_mm * tile_width_mm * 0.25
        bar_mm   = tile_width_mm * 0.25

    # Bar protrudes outward from the south side wall, just above the base.
    # This keeps it clear of the terrain surface regardless of edge elevation.
    BAR_PROTRUDE = 1.5   # mm outward from south face
    BAR_H        = 1.5   # mm tall (Z)
    TICK_PROTRUDE = 2.5  # mm — end ticks protrude more than bar body
    TICK_W        = 1.5  # mm — X width of each end tick
    MARGIN        = 8.0  # mm — inset from west edge

    bar_x0    = x_min + MARGIN
    bar_z_bot = base_thick_mm          # sits right above the base top surface
    bar_z_ctr = bar_z_bot + BAR_H / 2

    parts = [mesh]

    # Main bar body
    body = trimesh.creation.box([bar_mm, BAR_PROTRUDE, BAR_H])
    body.apply_translation([bar_x0 + bar_mm / 2, y_min - BAR_PROTRUDE / 2, bar_z_ctr])
    parts.append(body)

    # End ticks (slightly taller and deeper than the bar)
    for x_off in [0.0, bar_mm]:
        tick = trimesh.creation.box([TICK_W, TICK_PROTRUDE, BAR_H + 1.0])
        tick.apply_translation([bar_x0 + x_off, y_min - TICK_PROTRUDE / 2, bar_z_bot + (BAR_H + 1.0) / 2])
        parts.append(tick)

    trimesh.util.concatenate(parts).export(str(path))

    label = f"{bar_m / 1000:.0f} km" if bar_m >= 1000 else f"{bar_m:.0f} m"
    return f"scale bar: {label} = {bar_mm:.1f} mm"


def repair_stl(path: Path) -> str:
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    before = mesh.is_watertight

    # Primary: pymeshfix fills holes and resolves non-manifold edges.
    try:
        import pymeshfix
        mf = pymeshfix.MeshFix(mesh.vertices, mesh.faces)
        mf.repair()
        mesh = trimesh.Trimesh(vertices=mf.points, faces=mf.faces, process=False)
        engine = "pymeshfix"
    except Exception:
        # Fallback: trimesh repair chain.
        mesh.merge_vertices()
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fix_winding(mesh)
        trimesh.repair.fill_holes(mesh)
        engine = "trimesh"

    mesh.export(str(path))
    after = mesh.is_watertight

    if not before and after:
        return f"repaired → watertight ({engine})"
    elif not before and not after:
        return f"repaired ({engine}, non-manifold edges remain)"
    return "already watertight"


def slugify(name: str) -> str:
    import re
    # Use the first meaningful part before a comma (e.g. "Bozeman" from "Bozeman, Montana")
    name = name.split(",")[0].strip()
    name = name.lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s]+", "_", name)
    return name


def _elev_range_from_tif(path: Path) -> tuple[float, float] | tuple[None, None]:
    """Return (min_elev, max_elev) in metres from a local GeoTIFF."""
    try:
        import numpy as np
        import rasterio
        with rasterio.open(str(path)) as r:
            data = r.read(1, masked=True)
            return float(np.nanmin(data)), float(np.nanmax(data))
    except Exception:
        return None, None


def _elev_range_from_ee(
    bllat: float, bllon: float, trlat: float, trlon: float, dem_source: str
) -> tuple[float, float] | tuple[None, None]:
    """Query EE for min/max elevation in bbox without downloading the full DEM."""
    try:
        import ee
        band   = DEM_BAND.get(dem_source, "elevation")
        region = ee.Geometry.Rectangle([bllon, bllat, trlon, trlat])
        stats  = (
            ee.Image(dem_source)
            .select(band)
            .reduceRegion(
                reducer=ee.Reducer.minMax(),
                geometry=region,
                scale=100,
                bestEffort=True,
                maxPixels=int(1e9),
            )
            .getInfo()
        )
        lo, hi = stats.get(f"{band}_min"), stats.get(f"{band}_max")
        return (float(lo), float(hi)) if lo is not None and hi is not None else (None, None)
    except Exception:
        return None, None


def _auto_zscale(
    relief_m: float,
    printres_mm: float,
    dem_res_m: float,
    geo_width_m: float,
    tile_mm: float,
    mode: str,
) -> tuple[float, str]:
    """Compute zscale and a human-readable description.

    mode='auto'  — choose the smallest nice value that gives ~15 mm of terrain relief.
    mode='true'  — compute geographic true scale (zscale may be < 1.0).
    """
    # At zscale=1.0 TouchTerrain sets z_mm = elev_m * printres_mm / dem_res_m.
    height_at_1 = relief_m * printres_mm / dem_res_m

    if mode == "true":
        # True scale: z_mm / relief_m  ==  tile_mm / geo_width_m
        # → zscale = tile_mm * dem_res_m / (geo_width_m * printres_mm)
        true_z = tile_mm * dem_res_m / (geo_width_m * printres_mm)
        true_h = height_at_1 * true_z
        suffix = f"  ⚠ only {true_h:.1f} mm tall" if true_h < 2.0 else f"  ({true_h:.0f} mm relief)"
        return round(true_z, 4), f"true scale{suffix}"

    # Auto: target ~15 mm of relief.
    TARGET = 15.0
    if height_at_1 >= TARGET * 0.8:
        return 1.0, f"auto → 1.0  ({height_at_1:.0f} mm relief at true scale)"

    raw  = TARGET / max(height_at_1, 0.001)
    nice = min([1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0],
               key=lambda v: abs(v - raw))
    return nice, f"auto → {nice}  ({height_at_1 * nice:.0f} mm relief)"


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
    p.add_argument(
        "--z-scale",
        type=str,
        default=None,
        metavar="SCALE",
        help=(
            "Vertical exaggeration: a number (e.g. 2.0), 'auto' (pick the smallest nice value "
            "that gives ~15 mm of terrain relief), or 'true' (geographic true scale, z may be "
            "<1.0). Default: 'auto'."
        ),
    )
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
    p.add_argument(
        "--place-marker",
        action="store_true",
        default=False,
        help=(
            "Output a separate <name>_marker.STL cone at the searched-for place location. "
            "Import it alongside the terrain in your slicer and assign a second colour for "
            "multi-colour printing."
        ),
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
    # z-scale mode: resolved after bbox/DEM and elevation range are known.
    zscale_mode: str | None = None
    zscale_desc: str = ""
    if args.z_scale is None:
        zscale_mode = "auto"
    elif args.z_scale.lower() in ("auto", "true"):
        zscale_mode = args.z_scale.lower()
    else:
        try:
            tt_args["zscale"] = float(args.z_scale)
        except ValueError:
            print(f"ERROR: --z-scale must be a number, 'auto', or 'true'", file=sys.stderr)
            sys.exit(1)
    if args.printres is not None:
        tt_args["printres"] = args.printres

    # --- Resolve --place to a bbox via Nominatim, with Census Geocoder fallback ---
    if args.place:
        import math
        import urllib.parse
        import urllib.request

        def _point_bbox(lat: float, lon: float, pad: float) -> tuple[float, float, float, float]:
            """Compute a bbox that gives 1:1 DEM-to-printer resolution, centred on lat/lon.

            1:1 means one DEM pixel → one print pixel (printres mm).
            bbox_side = (tilewidth / printres) * dem_res_m
            --place-padding adds extra degrees beyond that on each edge.
            """
            dem_res = DEM_RES_M.get(args.dem_source, 10)
            samples  = tt_args.get("tilewidth", 250) / tt_args.get("printres", 0.4)
            half_m   = samples * dem_res / 2
            half_lat = half_m / 111_320 + pad
            half_lon = half_m / (111_320 * math.cos(math.radians(lat))) + pad
            return lat - half_lat, lon - half_lon, lat + half_lat, lon + half_lon

        def _bbox_km(s, w, n, e, lat):
            ns = (n - s) * 111.32
            ew = (e - w) * 111.32 * math.cos(math.radians(lat))
            return ns, ew

        # Threshold below which a Nominatim result is treated as a point (not an area).
        MIN_EXTENT = 0.01  # degrees

        # 1. Try Nominatim (works for named places, landmarks, and OSM-indexed addresses)
        query = urllib.parse.urlencode({"q": args.place, "format": "json", "limit": 1})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{query}",
            headers={"User-Agent": "terraprint/1.0 (github.com/terraprint)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                nominatim_results = json.loads(r.read())
        except Exception as exc:
            print(f"ERROR: Nominatim lookup failed: {exc}", file=sys.stderr)
            sys.exit(1)

        pad = args.place_padding
        south = north = west = east = None
        source_label = None

        if nominatim_results:
            hit = nominatim_results[0]
            bb = hit["boundingbox"]  # [south, north, west, east]
            s = float(bb[0]) - pad
            n = float(bb[1]) + pad
            w = float(bb[2]) - pad
            e = float(bb[3]) + pad
            if (n - s) < MIN_EXTENT or (e - w) < MIN_EXTENT:
                s, w, n, e = _point_bbox(float(hit["lat"]), float(hit["lon"]), pad)
                ns_km, ew_km = _bbox_km(s, w, n, e, float(hit["lat"]))
                source_label = f"{hit['display_name']} ({ns_km:.1f} km × {ew_km:.1f} km at 1:1 scale)"
            else:
                source_label = hit["display_name"]
                if pad:
                    source_label += f" (±{pad}°)"
            south, north, west, east = s, n, w, e

        # 2. Fall back to US Census Geocoder for addresses not in OSM
        if south is None:
            census_query = urllib.parse.urlencode({
                "address": args.place,
                "benchmark": "Public_AR_Current",
                "format": "json",
            })
            census_req = urllib.request.Request(
                f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{census_query}",
                headers={"User-Agent": "terraprint/1.0"},
            )
            try:
                with urllib.request.urlopen(census_req, timeout=15) as r:
                    census_data = json.loads(r.read())
                matches = census_data.get("result", {}).get("addressMatches", [])
            except Exception:
                matches = []

            if matches:
                coords = matches[0]["coordinates"]
                lat, lon = coords["y"], coords["x"]
                matched = matches[0]["matchedAddress"]
                south, west, north, east = _point_bbox(lat, lon, pad)
                ns_km, ew_km = _bbox_km(south, west, north, east, lat)
                source_label = f"{matched} ({ns_km:.1f} km × {ew_km:.1f} km at 1:1 scale, US Census)"

        if south is None:
            print(f"ERROR: No results found for '{args.place}'", file=sys.stderr)
            print("Try a more specific name, e.g. 'Bozeman, Montana' or 'Beartooth Mountains, MT'.", file=sys.stderr)
            sys.exit(1)

        print(f"Place      : {source_label}")
        args.bbox = f"{south},{west},{north},{east}"

    # --- Determine data source (EE fetch or local DEM) ---
    using_ee = False

    geo_width_m: float | None = None  # set below when a bbox is available
    center_lat:  float | None = None
    center_lon:  float | None = None

    if args.bbox:
        import math as _math
        bllat, bllon, trlat, trlon = parse_bbox(args.bbox)
        center_lat = (bllat + trlat) / 2
        center_lon = (bllon + trlon) / 2
        geo_width_m = (trlon - bllon) * 111_320 * _math.cos(_math.radians(center_lat))

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
    printres = tt_args.get("printres", 0.4)

    from touchterrain.common import TouchTerrainEarthEngine as TT

    logging.disable(logging.NOTSET)

    ee_initialized = False
    if using_ee:
        import ee
        try:
            ee.Initialize()
            ee_initialized = True
        except Exception as exc:
            print(f"ERROR: Could not initialize Earth Engine: {exc}", file=sys.stderr)
            print("Make sure you have run ./ee_auth.sh and authenticated successfully.", file=sys.stderr)
            sys.exit(1)

    # Resolve auto/true z-scale now that we know the bbox and data source.
    if zscale_mode is not None and geo_width_m is not None:
        dem_res = DEM_RES_M.get(args.dem_source, 10)
        lo, hi = None, None
        # Try cached TIF first (free), then EE stats (requires auth but avoids full download).
        if "importedDEM" in tt_args:
            lo, hi = _elev_range_from_tif(Path(tt_args["importedDEM"]))
        if lo is None and ee_initialized:
            print("Querying elevation range from Earth Engine…")
            lo, hi = _elev_range_from_ee(bllat, bllon, trlat, trlon, args.dem_source)
        if lo is not None and hi is not None:
            relief_m = hi - lo
            zval, zscale_desc = _auto_zscale(relief_m, printres, dem_res, geo_width_m, tile_w, zscale_mode)
            tt_args["zscale"] = zval
        else:
            zscale_desc = f"{zscale_mode} (elevation data unavailable — using 1.0)"
            tt_args.setdefault("zscale", 1.0)
    elif zscale_mode is not None:
        # --dem path without geo_width_m: try to read range from TIF
        if "importedDEM" in tt_args:
            lo, hi = _elev_range_from_tif(Path(tt_args["importedDEM"]))
            if lo is not None:
                import math as _math2
                # Estimate geo_width from DEM extent via rasterio
                try:
                    import rasterio
                    with rasterio.open(tt_args["importedDEM"]) as r:
                        bounds = r.bounds
                        clat = (bounds.bottom + bounds.top) / 2
                        gw = (bounds.right - bounds.left) * 111_320 * _math2.cos(_math2.radians(clat))
                    dem_res = DEM_RES_M.get(args.dem_source, 10)
                    zval, zscale_desc = _auto_zscale(hi - lo, printres, dem_res, gw, tile_w, zscale_mode)
                    tt_args["zscale"] = zval
                except Exception:
                    zscale_desc = f"{zscale_mode} (could not read DEM extent — using 1.0)"
                    tt_args.setdefault("zscale", 1.0)
            else:
                zscale_desc = f"{zscale_mode} (could not read elevation range — using 1.0)"
                tt_args.setdefault("zscale", 1.0)
        else:
            zscale_desc = f"{zscale_mode} (no DEM — using 1.0)"
            tt_args.setdefault("zscale", 1.0)

    zscale_display = f"{tt_args.get('zscale', 1.0)}"
    if zscale_desc:
        zscale_display += f"  [{zscale_desc}]"

    print(f"Tile       : {tile_w} mm × {ntx}×{nty} tiles")
    print(f"Z-scale    : {zscale_display}")
    print(f"Print res  : {printres} mm")
    print()

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

    ntx      = tt_args.get("ntilesx", 1)
    base_thick = tt_args.get("basethick", 2.0)

    print("Repairing mesh(es)…")
    for i, stl in enumerate(stl_files):
        status = repair_stl(stl)
        scale_note = ""
        if geo_width_m is not None:
            tile_geo_w = geo_width_m / ntx
            scale_note = "  [" + add_scale_bar(stl, tile_geo_w, tile_w, base_thick) + "]"
        label_note = ""
        if center_lat is not None and args.place:
            add_label(stl, args.place, center_lat, center_lon, base_thick)
            label_note = "  [label added]"
            if args.place_marker and len(stl_files) == 1:
                marker_path = add_place_marker(stl, base_thick)
                label_note += f"  [marker: {marker_path.name}]"
        print(f"  {stl.name}  [{status}]{scale_note}{label_note}")

    print()
    print(f"Done — {len(stl_files)} STL file(s) written to {out_dir}:")
    for stl in sorted(stl_files):
        size_mb = stl.stat().st_size / 1024 / 1024
        print(f"  {stl.name}  ({size_mb:.0f} MB)")
        marker = stl.with_name(stl.stem + "_marker" + stl.suffix)
        if marker.exists():
            size_kb = marker.stat().st_size / 1024
            print(f"  {marker.name}  ({size_kb:.0f} KB)  ← import alongside terrain in slicer, assign 2nd colour")


if __name__ == "__main__":
    main()
