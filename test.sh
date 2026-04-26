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

echo
echo "==============================="
echo "Results: $PASS passed, $FAIL failed"
echo "==============================="
[[ $FAIL -eq 0 ]]
