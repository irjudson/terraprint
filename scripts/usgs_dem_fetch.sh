#!/usr/bin/env bash
# Manual USGS DEM download helper — use this if you don't want Earth Engine.
set -euo pipefail

USGS_DIR="$(dirname "$0")/../data/usgs"
MERGED="$USGS_DIR/usgs_merged.tif"

echo ""
echo "=== USGS 1m DEM Manual Download Helper ==="
echo ""
echo "Step 1: Open the USGS National Map Lidar Explorer:"
echo "  https://apps.nationalmap.gov/lidar-explorer"
echo ""
echo "Step 2: Pan/zoom to your area of interest."
echo "  - Click 'Availability' to see lidar coverage."
echo "  - Draw a polygon around your AOI."
echo "  - In the left panel, filter to '1 meter DEM' under 'Elevation Products (3DEP)'."
echo ""
echo "Step 3: Add tiles to cart, then click 'Generate wget Script'."
echo "  - Save the generated script as: data/usgs/download.sh"
echo ""
echo "Step 4: Run the download script:"
echo "  bash data/usgs/download.sh"
echo ""
echo "Step 5: Come back here — this script will merge downloaded tiles."
echo ""

TIF_COUNT=$(find "$USGS_DIR" -maxdepth 1 -name "*.tif" ! -name "usgs_merged.tif" | wc -l)

if [ "$TIF_COUNT" -eq 0 ]; then
    echo "No .tif files found in data/usgs/ yet."
    echo "Complete steps 1-4 above, then re-run this script."
    exit 0
fi

echo "Found $TIF_COUNT .tif file(s) in data/usgs/. Merging..."

if command -v gdalbuildvrt &> /dev/null && command -v gdal_translate &> /dev/null; then
    TMPVRT=$(mktemp --suffix=.vrt)
    gdalbuildvrt "$TMPVRT" "$USGS_DIR"/*.tif
    gdal_translate -of GTiff -co COMPRESS=DEFLATE "$TMPVRT" "$MERGED"
    rm -f "$TMPVRT"
    echo "Merged DEM written to: $MERGED"
    echo ""
    echo "Use it with:"
    echo "  make terrain DEM=data/usgs/usgs_merged.tif"
else
    echo "gdalbuildvrt/gdal_translate not found on the host."
    echo "Run inside the container instead:"
    echo ""
    echo "  docker compose run --rm processor bash -c \\"
    echo "    'gdalbuildvrt /tmp/merged.vrt /data/usgs/*.tif && \\"
    echo "     gdal_translate -of GTiff -co COMPRESS=DEFLATE /tmp/merged.vrt /data/usgs/usgs_merged.tif'"
    echo ""
    echo "Then:"
    echo "  make terrain DEM=data/usgs/usgs_merged.tif"
fi
