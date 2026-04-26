-include .env

DATA_ROOT     ?= ./data
PRINTER       ?=
DEM_SOURCE    ?= USGS/3DEP/10m
BBOX          ?=
DEM           ?=
PLACE         ?=
PLACE_PADDING ?=
TILE_WIDTH_MM ?=
Z_SCALE       ?=
NTILES_X      ?=
NTILES_Y      ?=
NO_CACHE      ?=
OUT           ?= $(DATA_ROOT)/terrain

.PHONY: help check build auth terrain cache-clear clean

help:
	@echo ""
	@echo "terraprint MVP 0 — terrain → STL pipeline"
	@echo ""
	@echo "Active configuration:"
	@echo "  DEM_SOURCE    = $(DEM_SOURCE)"
	@echo "  PRINTER       = $(if $(PRINTER),$(PRINTER),(none — using touchterrain_default.json))"
	@echo "  TILE_WIDTH_MM = $(if $(TILE_WIDTH_MM),$(TILE_WIDTH_MM),(from config))"
	@echo "  Z_SCALE       = $(if $(Z_SCALE),$(Z_SCALE),(from config))"
	@echo "  BBOX          = $(if $(BBOX),$(BBOX),(not set))"
	@echo "  DEM           = $(if $(DEM),$(DEM),(not set))"
	@echo "  OUT           = $(OUT)"
	@echo ""
	@echo "Targets:"
	@echo "  make build                           Build the processor Docker image"
	@echo "  make check                           Verify compose config and image"
	@echo "  make auth                            Run one-time Earth Engine authentication"
	@echo "  make terrain BBOX=\"lat1,lon1,lat2,lon2\"           Fetch from Earth Engine"
	@echo "  make terrain BBOX=... PRINTER=configs/printers/kobra3max.json"
	@echo "  make terrain DEM=path/to/file.tif                  Use a local geotiff"
	@echo "  make terrain PLACE=\"Bozeman, Montana\"             Geocode a place name"
	@echo "  make terrain PLACE=\"Beartooth Mountains\" PLACE_PADDING=0.1"
	@echo "  make cache-clear                     Delete cached GeoTIFFs"
	@echo "  make clean                           Remove output STLs"
	@echo ""
	@echo "See ROADMAP.md for upcoming features."
	@echo ""

check:
	@echo "Checking compose configuration..."
	docker compose config -q
	@echo "Checking processor image..."
	docker image inspect terraprint/processor:latest > /dev/null 2>&1 \
		|| (echo "ERROR: Image not found. Run: make build" && exit 1)
	@echo "All checks passed."

build:
	docker compose build processor

auth:
	./ee_auth.sh

terrain:
	@if [ -z "$(BBOX)" ] && [ -z "$(DEM)" ] && [ -z "$(PLACE)" ]; then \
		echo ""; \
		echo "ERROR: You must provide BBOX, DEM, or PLACE."; \
		echo ""; \
		echo "  Geocode a place name:"; \
		echo "    make terrain PLACE=\"Bozeman, Montana\""; \
		echo "    make terrain PLACE=\"Beartooth Mountains\" PLACE_PADDING=0.1"; \
		echo "  Fetch from Earth Engine:"; \
		echo "    make terrain BBOX=\"lat1,lon1,lat2,lon2\""; \
		echo "  With printer profile:"; \
		echo "    make terrain BBOX=\"...\" PRINTER=configs/printers/kobra3max.json"; \
		echo "  Use a local GeoTIFF:"; \
		echo "    make terrain DEM=path/to/file.tif"; \
		echo ""; \
		echo "  Run ./setup.sh for first-time setup, then ./ee_auth.sh to authenticate."; \
		echo ""; \
		exit 1; \
	fi
	@SOURCES=0; \
	[ -n "$(BBOX)" ]  && SOURCES=$$((SOURCES+1)); \
	[ -n "$(DEM)" ]   && SOURCES=$$((SOURCES+1)); \
	[ -n "$(PLACE)" ] && SOURCES=$$((SOURCES+1)); \
	if [ "$$SOURCES" -gt 1 ]; then \
		echo "ERROR: BBOX, DEM, and PLACE are mutually exclusive."; \
		exit 1; \
	fi
	@mkdir -p $(OUT) $(DATA_ROOT)/cache
	@SCRIPT_ARGS="--out /data/terrain --dem-source $(DEM_SOURCE) --config configs/touchterrain_default.json"; \
	[ -n "$(BBOX)" ]          && SCRIPT_ARGS="$$SCRIPT_ARGS --bbox \"$(BBOX)\""; \
	[ -n "$(DEM)" ]           && { DEM_REL=$$(realpath --relative-to=$(DATA_ROOT) $(DEM) 2>/dev/null || echo "$(DEM)"); SCRIPT_ARGS="$$SCRIPT_ARGS --dem /data/$$DEM_REL"; }; \
	[ -n "$(PLACE)" ]         && SCRIPT_ARGS="$$SCRIPT_ARGS --place \"$(PLACE)\""; \
	[ -n "$(PLACE_PADDING)" ] && SCRIPT_ARGS="$$SCRIPT_ARGS --place-padding $(PLACE_PADDING)"; \
	[ -n "$(PRINTER)" ]       && SCRIPT_ARGS="$$SCRIPT_ARGS --printer $(PRINTER)"; \
	[ -n "$(TILE_WIDTH_MM)" ] && SCRIPT_ARGS="$$SCRIPT_ARGS --tile-width $(TILE_WIDTH_MM)"; \
	[ -n "$(Z_SCALE)" ]       && SCRIPT_ARGS="$$SCRIPT_ARGS --z-scale $(Z_SCALE)"; \
	[ -n "$(NTILES_X)" ]      && SCRIPT_ARGS="$$SCRIPT_ARGS --ntiles-x $(NTILES_X)"; \
	[ -n "$(NTILES_Y)" ]      && SCRIPT_ARGS="$$SCRIPT_ARGS --ntiles-y $(NTILES_Y)"; \
	[ -n "$(NO_CACHE)" ]      && SCRIPT_ARGS="$$SCRIPT_ARGS --no-cache"; \
	docker compose run --rm processor sh -c "python scripts/run_touchterrain.py $$SCRIPT_ARGS"

cache-clear:
	@echo "Clearing GeoTIFF cache in $(DATA_ROOT)/cache/ ..."
	@find $(DATA_ROOT)/cache -name "*.tif" -delete 2>/dev/null || true
	@echo "Done."

clean:
	@echo "Removing $(OUT)..."
	rm -rf $(OUT)
	@echo "Done."
