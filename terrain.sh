#!/usr/bin/env bash
# Generate terrain STLs from a bounding box, local GeoTIFF, or place name.
#
# Usage:
#   ./terrain.sh --bbox "lat1,lon1,lat2,lon2" [options]
#   ./terrain.sh --dem path/to/file.tif       [options]
#   ./terrain.sh --place "City or Feature"    [options]
#
# Options:
#   --printer FILE         Printer profile JSON (e.g. configs/printers/kobra3max.json)
#   --tile-width MM        Tile width in mm (overrides printer profile)
#   --z-scale N            Vertical exaggeration (default: 1.0)
#   --ntiles-x N           Number of tiles in X direction
#   --ntiles-y N           Number of tiles in Y direction
#   --dem-source ASSET     Earth Engine asset (default: USGS/3DEP/10m)
#   --out DIR              Output directory (default: data/terrain)
#   --no-cache             Always fetch from Earth Engine, skip GeoTIFF cache
#   --place-padding DEG    Extra margin in degrees around a --place bbox (default: 0)
#   --help
set -euo pipefail

BBOX=""
DEM=""
PLACE=""
PLACE_PADDING=""
PRINTER=""
TILE_WIDTH=""
Z_SCALE=""
NTILES_X=""
NTILES_Y=""
DEM_SOURCE=""
OUT=""
NO_CACHE=""

usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bbox)           BBOX="$2";           shift 2 ;;
        --dem)            DEM="$2";            shift 2 ;;
        --place)          PLACE="$2";          shift 2 ;;
        --place-padding)  PLACE_PADDING="$2";  shift 2 ;;
        --printer)        PRINTER="$2";        shift 2 ;;
        --tile-width)     TILE_WIDTH="$2";     shift 2 ;;
        --z-scale)        Z_SCALE="$2";        shift 2 ;;
        --ntiles-x)       NTILES_X="$2";       shift 2 ;;
        --ntiles-y)       NTILES_Y="$2";       shift 2 ;;
        --dem-source)     DEM_SOURCE="$2";     shift 2 ;;
        --out)            OUT="$2";            shift 2 ;;
        --no-cache)       NO_CACHE="1";        shift ;;
        --help|-h)        usage 0 ;;
        *) echo "Unknown option: $1"; usage 1 ;;
    esac
done

SOURCE_COUNT=0
[[ -n "$BBOX" ]]  && SOURCE_COUNT=$((SOURCE_COUNT + 1))
[[ -n "$DEM" ]]   && SOURCE_COUNT=$((SOURCE_COUNT + 1))
[[ -n "$PLACE" ]] && SOURCE_COUNT=$((SOURCE_COUNT + 1))

if [[ "$SOURCE_COUNT" -eq 0 ]]; then
    echo "Error: supply one of --bbox, --dem, or --place."
    echo
    echo "Examples:"
    echo '  ./terrain.sh --place "Bozeman, Montana"'
    echo '  ./terrain.sh --place "Beartooth Mountains" --place-padding 0.1'
    echo '  ./terrain.sh --bbox "44.50,-108.25,44.69,-107.97"'
    echo "  ./terrain.sh --dem data/usgs/my_dem.tif"
    exit 1
fi

if [[ "$SOURCE_COUNT" -gt 1 ]]; then
    echo "Error: --bbox, --dem, and --place are mutually exclusive."
    exit 1
fi

MAKE_ARGS=()
[[ -n "$BBOX" ]]          && MAKE_ARGS+=("BBOX=$BBOX")
[[ -n "$DEM" ]]           && MAKE_ARGS+=("DEM=$DEM")
[[ -n "$PLACE" ]]         && MAKE_ARGS+=("PLACE=$PLACE")
[[ -n "$PLACE_PADDING" ]] && MAKE_ARGS+=("PLACE_PADDING=$PLACE_PADDING")
[[ -n "$PRINTER" ]]       && MAKE_ARGS+=("PRINTER=$PRINTER")
[[ -n "$TILE_WIDTH" ]]    && MAKE_ARGS+=("TILE_WIDTH_MM=$TILE_WIDTH")
[[ -n "$Z_SCALE" ]]       && MAKE_ARGS+=("Z_SCALE=$Z_SCALE")
[[ -n "$NTILES_X" ]]      && MAKE_ARGS+=("NTILES_X=$NTILES_X")
[[ -n "$NTILES_Y" ]]      && MAKE_ARGS+=("NTILES_Y=$NTILES_Y")
[[ -n "$DEM_SOURCE" ]]    && MAKE_ARGS+=("DEM_SOURCE=$DEM_SOURCE")
[[ -n "$OUT" ]]           && MAKE_ARGS+=("OUT=$OUT")
[[ -n "$NO_CACHE" ]]      && MAKE_ARGS+=("NO_CACHE=1")

echo "Running: make terrain ${MAKE_ARGS[*]}"
echo
make terrain "${MAKE_ARGS[@]}"
