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
FLIGHT        ?=

# When FLIGHT is set, default DEM and OUT to ODM outputs for that flight
ifdef FLIGHT
DEM ?= $(DATA_ROOT)/01_odm/$(FLIGHT)/odm_dem/dsm.tif
OUT ?= $(DATA_ROOT)/02_terrain/$(FLIGHT)
else
OUT ?= $(DATA_ROOT)/terrain
endif

ODM_FLAGS ?= --feature-quality high --pc-quality high --pc-las --dsm --dtm \
             --orthophoto-resolution 2 --dem-resolution 5 --smrf-threshold 0.5 \
             --use-3dmesh --mesh-octree-depth 12 --texturing-keep-unseen-faces \
             --auto-boundary

ALTITUDE      ?= 80
OVERLAP       ?= 80
SURVEY_SPEED  ?= 8
MISSIONS_DIR  ?= $(DATA_ROOT)/missions

.PHONY: help check build auth terrain survey new-flight odm probe-phone push-mission watch-phone web cache-clear clean

help:
	@echo ""
	@echo "terraprint — terrain → STL pipeline"
	@echo ""
	@echo "Active configuration:"
	@echo "  DEM_SOURCE    = $(DEM_SOURCE)"
	@echo "  PRINTER       = $(if $(PRINTER),$(PRINTER),(none — using touchterrain_default.json))"
	@echo "  TILE_WIDTH_MM = $(if $(TILE_WIDTH_MM),$(TILE_WIDTH_MM),(from config))"
	@echo "  Z_SCALE       = $(if $(Z_SCALE),$(Z_SCALE),(from config))"
	@echo "  FLIGHT        = $(if $(FLIGHT),$(FLIGHT),(not set))"
	@echo "  BBOX          = $(if $(BBOX),$(BBOX),(not set))"
	@echo "  DEM           = $(if $(DEM),$(DEM),(not set))"
	@echo "  OUT           = $(OUT)"
	@echo ""
	@echo "Drone survey planning:"
	@echo "  make survey PLACE=\"my field\"         Generate KMZ mission for Skyrover app"
	@echo "  make survey BBOX=\"lat1,lon1,lat2,lon2\" ALTITUDE=80 OVERLAP=80"
	@echo ""
	@echo "Mission planner PWA:"
	@echo "  make web                             Start map planner at http://localhost:8000"
	@echo ""
	@echo "iPhone mission push (requires USB + pymobiledevice3 via: uv sync):"
	@echo "  make probe-phone                     Explore app container, find mission storage"
	@echo "  make push-mission FLIGHT=<name>      Push data/missions/<name>.kmz to iPhone"
	@echo "  make watch-phone                     Tail syslog for kmzFilePath log lines"
	@echo ""
	@echo "Drone photogrammetry (MVP 1):"
	@echo "  make new-flight FLIGHT=<name>        Create flight directory structure"
	@echo "  make odm FLIGHT=<name>               Process drone photos → DSM with OpenDroneMap"
	@echo "  make terrain FLIGHT=<name>           Terrain STL from ODM DSM output"
	@echo ""
	@echo "Terrain from Earth Engine or local data:"
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
		echo "ERROR: You must provide BBOX, DEM, PLACE, or FLIGHT."; \
		echo ""; \
		echo "  From drone flight (MVP 1):"; \
		echo "    make terrain FLIGHT=<name>"; \
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
	@OUT_CTR=$$(echo "$(OUT)" | sed 's|^\./data|/data|'); \
	SCRIPT_ARGS="--out $$OUT_CTR --dem-source $(DEM_SOURCE) --config configs/touchterrain_default.json"; \
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

survey:
	@if [ -z "$(BBOX)" ] && [ -z "$(PLACE)" ]; then \
		echo ""; \
		echo "ERROR: You must provide PLACE or BBOX."; \
		echo "  make survey PLACE=\"my field\" ALTITUDE=80 OVERLAP=80"; \
		echo "  make survey BBOX=\"lat1,lon1,lat2,lon2\""; \
		echo ""; \
		exit 1; \
	fi
	@mkdir -p $(MISSIONS_DIR)
	@SCRIPT_ARGS="--out /data/missions --altitude $(ALTITUDE) --overlap $(OVERLAP) --speed $(SURVEY_SPEED)"; \
	[ -n "$(PLACE)" ]         && SCRIPT_ARGS="$$SCRIPT_ARGS --place \"$(PLACE)\""; \
	[ -n "$(BBOX)" ]          && SCRIPT_ARGS="$$SCRIPT_ARGS --bbox \"$(BBOX)\""; \
	[ -n "$(PLACE_PADDING)" ] && SCRIPT_ARGS="$$SCRIPT_ARGS --place-padding $(PLACE_PADDING)"; \
	docker compose run --rm processor sh -c "python scripts/generate_survey.py $$SCRIPT_ARGS"

new-flight:
	@if [ -z "$(FLIGHT)" ]; then \
		echo ""; \
		echo "ERROR: FLIGHT is required."; \
		echo "  make new-flight FLIGHT=myfield"; \
		echo ""; \
		exit 1; \
	fi
	@mkdir -p "$(DATA_ROOT)/00_raw/$(FLIGHT)"
	@mkdir -p "$(DATA_ROOT)/01_odm/$(FLIGHT)"
	@mkdir -p "$(DATA_ROOT)/02_terrain/$(FLIGHT)"
	@echo ""
	@echo "Flight '$(FLIGHT)' ready."
	@echo "  Drop photos into: $(DATA_ROOT)/00_raw/$(FLIGHT)/"
	@echo "  Then run:         make odm FLIGHT=$(FLIGHT)"
	@echo ""

odm:
	@if [ -z "$(FLIGHT)" ]; then \
		echo ""; \
		echo "ERROR: FLIGHT is required."; \
		echo "  make odm FLIGHT=myfield"; \
		echo ""; \
		exit 1; \
	fi
	@if [ ! -d "$(DATA_ROOT)/00_raw/$(FLIGHT)" ]; then \
		echo ""; \
		echo "ERROR: Raw photos directory not found: $(DATA_ROOT)/00_raw/$(FLIGHT)/"; \
		echo "  Run: make new-flight FLIGHT=$(FLIGHT)"; \
		echo ""; \
		exit 1; \
	fi
	@count=$$(ls "$(DATA_ROOT)/00_raw/$(FLIGHT)" 2>/dev/null | wc -l); \
	if [ "$$count" -eq 0 ]; then \
		echo ""; \
		echo "ERROR: No photos in $(DATA_ROOT)/00_raw/$(FLIGHT)/"; \
		echo "  Copy your drone photos there and retry."; \
		echo ""; \
		exit 1; \
	fi
	@mkdir -p "$(DATA_ROOT)/01_odm/$(FLIGHT)"
	@echo "Processing flight '$(FLIGHT)' with OpenDroneMap ($$count photos)..."
	FLIGHT=$(FLIGHT) docker compose run --rm odm $(ODM_FLAGS)

web:
	docker compose up -d web

web-build:
	docker compose build web

web-logs:
	docker compose logs -f web

web-dev:
	uv run uvicorn web.app:app --reload --host 0.0.0.0 --port ${PORT:-8001}

probe-phone:
	uv run python scripts/skyrover_ios_bridge.py probe

push-mission:
	@if [ -z "$(FLIGHT)" ]; then \
		echo ""; \
		echo "ERROR: FLIGHT is required."; \
		echo "  make push-mission FLIGHT=myfield"; \
		echo ""; \
		exit 1; \
	fi
	uv run python scripts/skyrover_ios_bridge.py push $(MISSIONS_DIR)/$(FLIGHT).kmz --name "$(FLIGHT)"

watch-phone:
	uv run python scripts/skyrover_ios_bridge.py watch

cache-clear:
	@echo "Clearing GeoTIFF cache in $(DATA_ROOT)/cache/ ..."
	@find $(DATA_ROOT)/cache -name "*.tif" -delete 2>/dev/null || true
	@echo "Done."

clean:
	@echo "Removing $(OUT)..."
	rm -rf $(OUT)
	@echo "Done."
