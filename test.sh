#!/usr/bin/env bash
# Acceptance test — verifies the full MVP 0 install is working.
set -euo pipefail

PASS=0
FAIL=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "--- $* ---"; }

step "1. setup.sh (build image + smoke test)"
./setup.sh

step "2. make check"
make check && ok "compose config and image OK" || fail "make check"

step "3. copy .env.example → .env"
if [[ -f .env ]]; then
    ok ".env already exists"
else
    cp .env.example .env && ok "copied .env.example to .env"
fi

step "4. earthengine-api version"
VERSION=$(docker compose run --rm processor python -c "import ee; print(ee.__version__)" 2>&1)
if [[ $VERSION =~ ^[0-9] ]]; then
    ok "earthengine-api $VERSION"
else
    fail "could not import ee (got: $VERSION)"
fi

step "5. make terrain with no args (should print a helpful error)"
OUTPUT=$(make terrain 2>&1 || true)
if echo "$OUTPUT" | grep -q "ERROR: You must provide BBOX, DEM, or PLACE"; then
    ok "error message correct"
    echo "$OUTPUT" | grep -A20 "ERROR:"
else
    fail "unexpected output from 'make terrain'"
    echo "$OUTPUT"
fi

step "6. post-processing: repair, scale bar, and label on existing STL"
FIXTURE_HOST=$(ls data/terrain/*.STL data/terrain/*.stl 2>/dev/null | head -1)
if [[ -z "$FIXTURE_HOST" ]]; then
    fail "no STL fixture found in data/terrain/ — run a terrain generation first"
else
    echo "  fixture: $FIXTURE_HOST"
    # Convert host-relative path to container path (./data → /data)
    FIXTURE_CTR="/data/terrain/$(basename "$FIXTURE_HOST")"

    RESULT=$(docker compose run --rm processor python3 - <<PYEOF 2>&1
import sys, shutil
sys.path.insert(0, "scripts")
from pathlib import Path
from run_touchterrain import repair_stl, add_scale_bar, add_label

src = Path("$FIXTURE_CTR")
out = Path("/data/terrain/_test_postprocess.STL")
shutil.copy(src, out)

import trimesh
m_src = trimesh.load(str(src), force="mesh")

repair_status = repair_stl(out)
m_repaired = trimesh.load(str(out), force="mesh")
repair_faces = len(m_repaired.faces)

scale_status  = add_scale_bar(out, 9500, 375, 2.0)
add_label(out, "Virginia City, MT", 45.2967, -111.9378, 2.0)

m_out = trimesh.load(str(out), force="mesh")

import numpy as np
south_protrusion = m_src.bounds[0][1] - m_out.bounds[0][1]

# Verify the platform exists: check that there are output vertices in the SE
# corner area that are taller than the original terrain in the same area.
se_x0 = m_src.bounds[1][0] - 100   # last 100 mm on east side
se_y1 = m_src.bounds[0][1] + 100   # first 100 mm on south side
def corner_z_max(m):
    v = m.vertices
    mask = (v[:,0] >= se_x0) & (v[:,1] <= se_y1)
    return float(v[mask, 2].max()) if mask.any() else 0.0

label_z_rise = corner_z_max(m_out) - corner_z_max(m_src)

print(f"repair={repair_status}")
print(f"scale={scale_status}")
print(f"south_protrusion={south_protrusion:.3f}")
print(f"label_z_rise={label_z_rise:.3f}")
print(f"out_faces={len(m_out.faces)}")
print(f"repair_faces={repair_faces}")
PYEOF
    )

    echo "$RESULT" | grep -v "^$"

    if echo "$RESULT" | grep -q "repair="; then
        ok "repair_stl ran"
    else
        fail "repair_stl did not run"
    fi

    if echo "$RESULT" | grep -q "scale=scale bar:"; then
        ok "add_scale_bar produced a bar"
    else
        fail "add_scale_bar failed"
    fi

    SOUTH=$(echo "$RESULT" | grep "^south_protrusion=" | cut -d= -f2)
    if [[ -n "$SOUTH" ]] && awk "BEGIN{exit !($SOUTH+0 > 0)}"; then
        ok "scale bar protrudes south by ${SOUTH} mm"
    else
        fail "scale bar did not protrude (south_protrusion=${SOUTH:-empty})"
    fi

    LABEL_ZR=$(echo "$RESULT" | grep "^label_z_rise=" | cut -d= -f2)
    if [[ -n "$LABEL_ZR" ]] && awk "BEGIN{exit !($LABEL_ZR+0 > 0)}"; then
        ok "label platform raised SE corner z by ${LABEL_ZR} mm"
    else
        fail "label platform did not raise SE corner (label_z_rise=${LABEL_ZR:-empty})"
    fi

    OUT_FACES=$(echo "$RESULT" | grep "^out_faces=" | cut -d= -f2)
    REPAIR_FACES=$(echo "$RESULT" | grep "^repair_faces=" | cut -d= -f2)
    if [[ -n "$OUT_FACES" && -n "$REPAIR_FACES" ]] && (( OUT_FACES > REPAIR_FACES )); then
        ok "geometry additions increased face count ($REPAIR_FACES → $OUT_FACES)"
    else
        fail "geometry additions did not increase face count ($REPAIR_FACES → $OUT_FACES)"
    fi

    rm -f data/terrain/_test_postprocess.STL
fi

echo
echo "==============================="
echo "Results: $PASS passed, $FAIL failed"
echo "==============================="
[[ $FAIL -eq 0 ]]
