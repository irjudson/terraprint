# terraprint roadmap

Each MVP produces something tangible and testable on its own. Don't skip ahead — the staging exists so each piece can be verified before the next builds on it.

## MVP 0 — terrain only ✓ (complete)

Lat/lon bbox, place name, or local GeoTIFF in → tiled STL files out.

- Containerized TouchTerrain with Earth Engine integration
- Place-name geocoding via Nominatim (OpenStreetMap)
- GeoTIFF cache (skips Earth Engine API on repeat runs)
- Layered config: base JSON → printer profile → CLI flags
- Automatic mesh repair (trimesh; reports watertight status)
- Printer profiles (`configs/printers/`) for build-plate-optimal tile sizing
- `terrain.sh` convenience wrapper; `make terrain` for scripted use

Validated: Docker plumbing, Earth Engine auth, TouchTerrain integration, printer sizing, watertight STL output.

## MVP 1 — drone photogrammetry

Add an `odm` service to docker-compose.yml using `opendronemap/odm:latest`.
Add a flights/ directory layout (`data/00_raw/<flight>/`,
`data/01_odm/<flight>/`, etc.) and a `make odm FLIGHT=<n>` target. The
existing `make terrain` learns to point at the ODM-produced DSM by default
when FLIGHT is set.

Suggested ODM flags:
`--feature-quality high --pc-quality high --pc-las --dsm --dtm
 --orthophoto-resolution 2 --dem-resolution 5 --smrf-threshold 0.5
 --use-3dmesh --mesh-octree-depth 12 --texturing-keep-unseen-faces
 --auto-boundary`

Done when: `make odm FLIGHT=test && make terrain FLIGHT=test` produces a
higher-resolution terrain STL than the Earth Engine path.

## MVP 2 — building extraction

Add `scripts/extract_buildings.py` and `make buildings FLIGHT=<n>`. Extend
the processor Dockerfile with `scikit-image>=0.22 shapely>=2.0 scipy>=1.11
pyproj>=3.6 manifold3d`.

Algorithm: DSM minus DTM → threshold by minimum height → connected
components → filter by area → height-field mesh per component → export
STL per building.

Skip magnet pockets in this MVP. Boxy buildings are fine.

Done when: per-building STLs land in `data/03_buildings/<flight>/` and
roughly match the structures actually on the property.

## MVP 3 — magnet pockets + parametric pieces

Add magnet pocket subtraction to extract_buildings.py (6.2mm × 2.2mm
neodymium discs, 4 corners, 4mm inset, catch boolean failures gracefully).
Add `pieces/parametric_building.scad` for hypothetical or future buildings
that don't exist in the photogrammetry yet.

Done when: extracted building STLs have magnet pockets, and
`openscad -o piece.stl -D 'length_m=12' pieces/parametric_building.scad`
produces a snap-fit custom building.

## MVP 4 — layered config + flight scaffolding

Refactor `.env` into project-wide defaults plus `flights/<flight>.env` for
per-flight overrides. Makefile loads `.env` then `flights/$(FLIGHT).env`
on top, so flight values override project defaults. Add
`make new-flight FLIGHT=<n>` that creates the raw photo directory and
clones `flights/example.env`.

Done when: multiple flights at different scales coexist without editing
any script.

## MVP 5 — print queue + slicer integration

Add `data/04_print_ready/<flight>/` staging directory and `make stage`
target that collects all STLs there. Optional `make slice` target wrapping
OrcaSlicer's CLI to produce gcode in `data/05_gcode/<flight>/` ready for
SD card or network upload.

## MVP 6 — optional WebODM GUI

Add `docker-compose.webodm.yml` as a separate stack for users who prefer
the browser GUI for parameter exploration. Doesn't replace the CLI flow.
