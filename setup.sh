#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "=== terraprint setup ==="
echo ""

# Verify docker is available
if ! command -v docker &> /dev/null; then
    echo "ERROR: docker is not installed or not in PATH."
    echo "Install Docker Desktop or Docker Engine: https://docs.docker.com/get-docker/"
    exit 1
fi

# Verify docker compose (v2) is available
if ! docker compose version &> /dev/null; then
    echo "ERROR: 'docker compose' (v2) is not available."
    echo "Update Docker Desktop, or install the compose plugin:"
    echo "  https://docs.docker.com/compose/install/"
    exit 1
fi

echo "Docker:  $(docker --version)"
echo "Compose: $(docker compose version)"
echo ""

# Write HOST_UID into .env so docker-compose passes the right UID into the image
# build. This ensures files written by the container user are owned by the host
# user and bind-mounted volumes are readable/writable by both.
REAL_UID=$(id -u)

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example"
fi

if grep -q "^HOST_UID=" .env; then
    sed -i "s/^HOST_UID=.*/HOST_UID=${REAL_UID}/" .env
else
    echo "HOST_UID=${REAL_UID}" >> .env
fi
echo "Set HOST_UID=${REAL_UID} in .env"
echo ""

# Export for this session so compose picks it up immediately
export HOST_UID="${REAL_UID}"

# Build the processor image
echo "Building processor image (this may take a few minutes on first run)..."
docker compose build processor
echo ""

# Create data directories
mkdir -p data/usgs data/terrain
echo "Created data/usgs/ and data/terrain/"
echo ""

# Smoke test
echo "Running smoke test..."
docker compose run --rm processor python -c \
    "import rasterio, ee; from touchterrain.common import TouchTerrainEarthEngine; print('terraprint MVP 0 OK')"
echo ""

echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Register a free Earth Engine account (usually instant):"
echo "       https://code.earthengine.google.com/"
echo "  2. ./ee_auth.sh"
echo "  3. Generate your first terrain:"
echo "       ./terrain.sh --place \"Bozeman, Montana\""
echo "       ./terrain.sh --bbox \"44.50,-108.25,44.69,-107.97\""
echo ""
echo "Or skip Earth Engine and use a local GeoTIFF:"
echo "  make terrain DEM=data/usgs/your_dem.tif"
echo "  (see scripts/usgs_dem_fetch.sh for download instructions)"
echo ""
