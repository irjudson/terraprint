FROM python:3.11-slim

ARG HOST_UID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    python3-dev \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Pin the Python GDAL bindings to the system GDAL version so osgeo.gdal resolves
RUN pip install --no-cache-dir "GDAL==$(gdal-config --version)"

RUN pip install --no-cache-dir \
    "rasterio>=1.3" \
    "numpy>=1.24" \
    "Pillow>=10.0" \
    "tqdm>=4.66" \
    "earthengine-api>=0.1.380" \
    "trimesh>=4.0" \
    "git+https://github.com/ChHarding/TouchTerrain_for_CAGEO.git"

RUN useradd -m -u ${HOST_UID} -s /bin/bash tp \
    && mkdir -p /home/tp/.config/matplotlib \
    && chown -R tp:tp /home/tp/.config

ENV MPLCONFIGDIR=/home/tp/.config/matplotlib

WORKDIR /workspace
USER tp

CMD ["sh", "-c", "echo 'Run via: docker compose run --rm processor python scripts/<script>'"]
