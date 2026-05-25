#!/usr/bin/env python3
"""Generate a DJI WPML survey mission KMZ from a place name or bounding box.

Produces a lawnmower-pattern waypoint mission ready to load into the Skyrover
(or any DJI Fly / MSDK V5) app. Camera fires at every waypoint.

Output is a .kmz file containing wpmz/template.kml + wpmz/waylines.wpml.
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# Skyrover X1 defaults (DJI Mini 4 Pro clone, wide lens)
DEFAULT_HFOV    = 82.1   # degrees horizontal
DEFAULT_VFOV    = 61.9   # degrees vertical (4:3 photo mode)
DEFAULT_ALT     = 80     # metres AGL
DEFAULT_OVERLAP = 80     # percent, both front and side
DEFAULT_SPEED   = 8.0    # m/s cruise


# ---------------------------------------------------------------------------
# Geocoding  (same logic as run_touchterrain.py)
# ---------------------------------------------------------------------------

def _point_bbox(lat: float, lon: float, pad: float) -> tuple[float, float, float, float]:
    """Tiny bbox centred on a point — used when Nominatim returns a bare pin."""
    half = 0.005 + pad   # ~500 m radius before any extra padding
    half_lon = half / max(math.cos(math.radians(lat)), 0.01)
    return lat - half, lon - half_lon, lat + half, lon + half_lon


def geocode(
    place: str, padding: float = 0.0
) -> tuple[float, float, float, float, str]:
    """Return (south, west, north, east, display_label) for a place name.

    Tries Nominatim first, falls back to US Census Geocoder.
    """
    MIN_EXTENT = 0.01

    query = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
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

    if results:
        hit = results[0]
        bb = hit["boundingbox"]   # [south, north, west, east]
        s = float(bb[0]) - padding
        n = float(bb[1]) + padding
        w = float(bb[2]) - padding
        e = float(bb[3]) + padding
        if (n - s) < MIN_EXTENT or (e - w) < MIN_EXTENT:
            s, w, n, e = _point_bbox(float(hit["lat"]), float(hit["lon"]), padding)
        return s, w, n, e, hit["display_name"]

    # US Census fallback
    cq = urllib.parse.urlencode({
        "address": place, "benchmark": "Public_AR_Current", "format": "json",
    })
    creq = urllib.request.Request(
        f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{cq}",
        headers={"User-Agent": "terraprint/1.0"},
    )
    try:
        with urllib.request.urlopen(creq, timeout=15) as r:
            data = json.loads(r.read())
        matches = data.get("result", {}).get("addressMatches", [])
    except Exception:
        matches = []

    if matches:
        coords = matches[0]["coordinates"]
        lat, lon = coords["y"], coords["x"]
        s, w, n, e = _point_bbox(lat, lon, padding)
        return s, w, n, e, matches[0]["matchedAddress"]

    print(f"ERROR: No results found for '{place}'", file=sys.stderr)
    sys.exit(1)


def parse_bbox(s: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("BBOX must be lat1,lon1,lat2,lon2")
    try:
        lat1, lon1, lat2, lon2 = map(float, parts)
        return min(lat1, lat2), min(lon1, lon2), max(lat1, lat2), max(lon1, lon2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def slugify(name: str) -> str:
    name = name.split(",")[0].strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "_", name)
    return name


# ---------------------------------------------------------------------------
# Grid planning
# ---------------------------------------------------------------------------

def camera_footprint(altitude_m: float, hfov_deg: float, vfov_deg: float) -> tuple[float, float]:
    """Return (width_m, height_m) ground footprint at altitude."""
    w = 2 * altitude_m * math.tan(math.radians(hfov_deg / 2))
    h = 2 * altitude_m * math.tan(math.radians(vfov_deg / 2))
    return w, h


def lawnmower_grid(
    south: float, west: float, north: float, east: float,
    altitude_m: float,
    front_overlap: float,
    side_overlap: float,
    hfov_deg: float,
    vfov_deg: float,
) -> list[tuple[float, float]]:
    """Return (lat, lon) waypoints as a N-S lawnmower grid covering the bbox."""
    center_lat = (south + north) / 2
    m_per_lat = 111_320.0
    m_per_lon = 111_320.0 * math.cos(math.radians(center_lat))

    fp_w, fp_h = camera_footprint(altitude_m, hfov_deg, vfov_deg)

    track_spacing_deg = fp_w * (1 - side_overlap / 100) / m_per_lon
    along_spacing_deg = fp_h * (1 - front_overlap / 100) / m_per_lat

    # Start/end half a footprint inside the bbox edges so every edge photo lands inside
    x0 = west  + (fp_w / 2) / m_per_lon
    x1 = east  - (fp_w / 2) / m_per_lon
    y0 = south + (fp_h / 2) / m_per_lat
    y1 = north - (fp_h / 2) / m_per_lat

    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)

    n_tracks = max(1, round((x1 - x0) / track_spacing_deg) + 1)
    n_points = max(2, round((y1 - y0) / along_spacing_deg) + 1)

    track_lons = [x0 + i * (x1 - x0) / max(n_tracks - 1, 1) for i in range(n_tracks)]
    track_lats = [y0 + i * (y1 - y0) / max(n_points - 1, 1) for i in range(n_points)]

    waypoints: list[tuple[float, float]] = []
    for i, lon in enumerate(track_lons):
        lats = track_lats if i % 2 == 0 else list(reversed(track_lats))
        for lat in lats:
            waypoints.append((lat, lon))

    return waypoints


# ---------------------------------------------------------------------------
# WPML XML generation
# ---------------------------------------------------------------------------

def _waylines_wpml(waypoints: list[tuple[float, float]], altitude_m: float, speed_ms: float) -> str:
    now_ms = int(time.time() * 1000)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">',
        '<Document>',
        f'  <wpml:author>terraprint</wpml:author>',
        f'  <wpml:createTime>{now_ms}</wpml:createTime>',
        f'  <wpml:updateTime>{now_ms}</wpml:updateTime>',
        '  <wpml:missionConfig>',
        '    <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>',
        '    <wpml:finishAction>goHome</wpml:finishAction>',
        '    <wpml:exitOnRCLost>goContinue</wpml:exitOnRCLost>',
        '    <wpml:takeOffSecurityHeight>20</wpml:takeOffSecurityHeight>',
        f'   <wpml:globalTransitionalSpeed>{speed_ms:.1f}</wpml:globalTransitionalSpeed>',
        '  </wpml:missionConfig>',
        '  <Folder>',
        '    <wpml:templateId>0</wpml:templateId>',
        '    <wpml:waylineId>0</wpml:waylineId>',
        f'   <wpml:autoFlightSpeed>{speed_ms:.1f}</wpml:autoFlightSpeed>',
        '    <wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode>',
    ]

    for idx, (lat, lon) in enumerate(waypoints):
        action_group_id = idx
        lines += [
            '    <Placemark>',
            f'      <Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point>',
            f'      <wpml:index>{idx}</wpml:index>',
            f'      <wpml:executeHeight>{altitude_m:.1f}</wpml:executeHeight>',
            f'      <wpml:waypointSpeed>{speed_ms:.1f}</wpml:waypointSpeed>',
            '      <wpml:waypointHeadingParam>',
            '        <wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode>',
            '      </wpml:waypointHeadingParam>',
            '      <wpml:waypointTurnParam>',
            '        <wpml:waypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:waypointTurnMode>',
            '        <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>',
            '      </wpml:waypointTurnParam>',
            f'      <wpml:actionGroup>',
            f'        <wpml:actionGroupId>{action_group_id}</wpml:actionGroupId>',
            f'        <wpml:actionGroupStartIndex>{idx}</wpml:actionGroupStartIndex>',
            f'        <wpml:actionGroupEndIndex>{idx}</wpml:actionGroupEndIndex>',
            '        <wpml:actionGroupMode>sequence</wpml:actionGroupMode>',
            '        <wpml:actionTrigger>',
            '          <wpml:actionTriggerType>reachPoint</wpml:actionTriggerType>',
            '        </wpml:actionTrigger>',
            '        <wpml:action>',
            '          <wpml:actionId>0</wpml:actionId>',
            '          <wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>',
            '          <wpml:actionActuatorFuncParam>',
            '            <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>',
            '          </wpml:actionActuatorFuncParam>',
            '        </wpml:action>',
            '      </wpml:actionGroup>',
            '    </Placemark>',
        ]

    lines += [
        '  </Folder>',
        '</Document>',
        '</kml>',
    ]
    return "\n".join(lines)


def _template_kml(waypoints: list[tuple[float, float]], altitude_m: float, speed_ms: float) -> str:
    now_ms = int(time.time() * 1000)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">',
        '<Document>',
        f'  <wpml:author>terraprint</wpml:author>',
        f'  <wpml:createTime>{now_ms}</wpml:createTime>',
        f'  <wpml:updateTime>{now_ms}</wpml:updateTime>',
        '  <wpml:missionConfig>',
        '    <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>',
        '    <wpml:finishAction>goHome</wpml:finishAction>',
        '    <wpml:exitOnRCLost>goContinue</wpml:exitOnRCLost>',
        '    <wpml:takeOffSecurityHeight>20</wpml:takeOffSecurityHeight>',
        f'   <wpml:globalTransitionalSpeed>{speed_ms:.1f}</wpml:globalTransitionalSpeed>',
        '  </wpml:missionConfig>',
        '  <Folder>',
        '    <wpml:templateType>waypoint</wpml:templateType>',
        '    <wpml:templateId>0</wpml:templateId>',
        f'   <wpml:autoFlightSpeed>{speed_ms:.1f}</wpml:autoFlightSpeed>',
        '    <wpml:waylineCoordinateSysParam>',
        '      <wpml:coordinateMode>WGS84</wpml:coordinateMode>',
        '      <wpml:heightMode>relativeToStartPoint</wpml:heightMode>',
        '    </wpml:waylineCoordinateSysParam>',
        '    <wpml:payloadParam>',
        '      <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>',
        '      <wpml:meteringMode>average</wpml:meteringMode>',
        '      <wpml:dewarpingEnable>0</wpml:dewarpingEnable>',
        '      <wpml:returnMode>singleReturnFirst</wpml:returnMode>',
        '      <wpml:samplingRate>240000</wpml:samplingRate>',
        '      <wpml:scanningMode>nonRepetitive</wpml:scanningMode>',
        '    </wpml:payloadParam>',
    ]

    for idx, (lat, lon) in enumerate(waypoints):
        lines += [
            '    <Placemark>',
            f'      <Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point>',
            f'      <wpml:index>{idx}</wpml:index>',
            f'      <wpml:executeHeight>{altitude_m:.1f}</wpml:executeHeight>',
            f'      <wpml:waypointSpeed>{speed_ms:.1f}</wpml:waypointSpeed>',
            '      <wpml:waypointHeadingParam>',
            '        <wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode>',
            '      </wpml:waypointHeadingParam>',
            '      <wpml:waypointTurnParam>',
            '        <wpml:waypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:waypointTurnMode>',
            '        <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>',
            '      </wpml:waypointTurnParam>',
            '    </Placemark>',
        ]

    lines += [
        '  </Folder>',
        '</Document>',
        '</kml>',
    ]
    return "\n".join(lines)


def make_kmz(
    waypoints: list[tuple[float, float]],
    altitude_m: float,
    speed_ms: float,
    out_path: Path,
) -> None:
    template = _template_kml(waypoints, altitude_m, speed_ms)
    waylines = _waylines_wpml(waypoints, altitude_m, speed_ms)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wpmz/template.kml", template)
        zf.writestr("wpmz/waylines.wpml", waylines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate a DJI WPML lawnmower survey KMZ for the Skyrover X1."
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--place", metavar="NAME",
                        help="Place name geocoded via Nominatim/OpenStreetMap.")
    source.add_argument("--bbox", metavar="lat1,lon1,lat2,lon2",
                        help="Bounding box corners (any order, will be normalised).")
    p.add_argument("--place-padding", type=float, default=0.0, metavar="DEG",
                   help="Extra degrees to pad around a --place bbox (default: 0).")
    p.add_argument("--altitude", type=float, default=DEFAULT_ALT, metavar="M",
                   help=f"Flight altitude in metres AGL (default: {DEFAULT_ALT}).")
    p.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP, metavar="PCT",
                   help=f"Front and side overlap percent (default: {DEFAULT_OVERLAP}).")
    p.add_argument("--front-overlap", type=float, default=None, metavar="PCT",
                   help="Front overlap percent (overrides --overlap).")
    p.add_argument("--side-overlap", type=float, default=None, metavar="PCT",
                   help="Side overlap percent (overrides --overlap).")
    p.add_argument("--speed", type=float, default=DEFAULT_SPEED, metavar="M/S",
                   help=f"Cruise speed in m/s (default: {DEFAULT_SPEED}).")
    p.add_argument("--hfov", type=float, default=DEFAULT_HFOV, metavar="DEG",
                   help=f"Camera horizontal FOV in degrees (default: {DEFAULT_HFOV}, X1 wide lens).")
    p.add_argument("--vfov", type=float, default=DEFAULT_VFOV, metavar="DEG",
                   help=f"Camera vertical FOV in degrees (default: {DEFAULT_VFOV}, 4:3 photo mode).")
    p.add_argument("--out", required=True, metavar="DIR",
                   help="Output directory for the .kmz file.")
    p.add_argument("--name", default=None, metavar="NAME",
                   help="Output filename stem (default: derived from place/bbox).")
    return p


def main() -> None:
    args = build_parser().parse_args()

    front_overlap = args.front_overlap if args.front_overlap is not None else args.overlap
    side_overlap  = args.side_overlap  if args.side_overlap  is not None else args.overlap

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.place:
        south, west, north, east, label = geocode(args.place, args.place_padding)
        name = args.name or slugify(args.place)
        print(f"Place      : {label}")
    else:
        south, west, north, east = parse_bbox(args.bbox)
        label = f"{south:.4f},{west:.4f} → {north:.4f},{east:.4f}"
        name = args.name or "survey"
        print(f"Bbox       : {label}")

    center_lat = (south + north) / 2
    area_w_m = (east - west) * 111_320 * math.cos(math.radians(center_lat))
    area_h_m = (north - south) * 111_320
    fp_w, fp_h = camera_footprint(args.altitude, args.hfov, args.vfov)

    waypoints = lawnmower_grid(
        south, west, north, east,
        args.altitude, front_overlap, side_overlap,
        args.hfov, args.vfov,
    )

    # DJI Fly / MSDK V5 hard limit is 65,535 waypoints.
    # Practically, 500+ waypoints means multiple batteries — warn early.
    MAX_WAYPOINTS = 65_535
    if len(waypoints) > MAX_WAYPOINTS:
        print(f"ERROR: {len(waypoints):,} waypoints exceeds the DJI Fly limit of {MAX_WAYPOINTS:,}.", file=sys.stderr)
        print("The area is too large for a single waypoint mission.", file=sys.stderr)
        print("Use a smaller bbox or a specific property address rather than a city/region name.", file=sys.stderr)
        sys.exit(1)

    if len(waypoints) > 500:
        batteries = math.ceil(len(waypoints) / 500)
        print(f"WARNING: {len(waypoints):,} waypoints — plan for ~{batteries} battery swaps.\n")

    out_path = out_dir / f"{name}.kmz"
    make_kmz(waypoints, args.altitude, args.speed, out_path)

    track_spacing_m = fp_w * (1 - side_overlap / 100)
    along_spacing_m = fp_h * (1 - front_overlap / 100)
    est_minutes = len(waypoints) * (along_spacing_m / args.speed) / 60

    print(f"Area       : {area_w_m:.0f} m × {area_h_m:.0f} m")
    print(f"Altitude   : {args.altitude:.0f} m AGL")
    print(f"Footprint  : {fp_w:.0f} m × {fp_h:.0f} m per photo")
    print(f"Track spac : {track_spacing_m:.0f} m  ({side_overlap:.0f}% side overlap)")
    print(f"Along spac : {along_spacing_m:.0f} m  ({front_overlap:.0f}% front overlap)")
    print(f"Waypoints  : {len(waypoints)}")
    print(f"Est. time  : ~{est_minutes:.0f} min at {args.speed:.0f} m/s")
    print()
    print(f"Output     : {out_path}")
    print()
    print("Load this KMZ into your Skyrover app → Waypoints → Import.")


if __name__ == "__main__":
    main()
