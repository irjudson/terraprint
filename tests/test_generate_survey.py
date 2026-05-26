"""Tests for generate_survey.py — mission grid and WPML generation."""

import math
import pathlib
import tempfile
import zipfile

import pytest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

from generate_survey import (
    PHOTOGRAMMETRY_PASSES,
    camera_footprint,
    lawnmower_grid,
    make_kmz,
    make_photogrammetry_missions,
)

# Tiny bbox used across tests (approx 1 km x 1 km in Montana)
SOUTH, WEST, NORTH, EAST = 45.730, -111.480, 45.740, -111.470


# ---------------------------------------------------------------------------
# camera_footprint
# ---------------------------------------------------------------------------

def test_footprint_nadir_80m():
    w, h = camera_footprint(80, 82.1, 61.9)
    # At 80 m AGL with these FOVs expect ~130 m wide, ~90 m tall
    assert 100 < w < 180
    assert 60 < h < 130


def test_footprint_scales_with_altitude():
    w1, h1 = camera_footprint(40, 82.1, 61.9)
    w2, h2 = camera_footprint(80, 82.1, 61.9)
    assert abs(w2 / w1 - 2.0) < 0.01
    assert abs(h2 / h1 - 2.0) < 0.01


# ---------------------------------------------------------------------------
# lawnmower_grid
# ---------------------------------------------------------------------------

def test_grid_returns_waypoints():
    wpts = lawnmower_grid(SOUTH, WEST, NORTH, EAST, 80, 80, 80, 82.1, 61.9)
    assert len(wpts) > 0


def test_grid_waypoints_within_bbox():
    wpts = lawnmower_grid(SOUTH, WEST, NORTH, EAST, 80, 80, 80, 82.1, 61.9)
    for lat, lon in wpts:
        assert SOUTH - 0.01 <= lat <= NORTH + 0.01
        assert WEST  - 0.01 <= lon <= EAST  + 0.01


def test_grid_more_overlap_means_more_waypoints():
    lo = lawnmower_grid(SOUTH, WEST, NORTH, EAST, 80, 70, 70, 82.1, 61.9)
    hi = lawnmower_grid(SOUTH, WEST, NORTH, EAST, 80, 90, 90, 82.1, 61.9)
    assert len(hi) > len(lo)


def test_grid_alternating_track_direction():
    wpts = lawnmower_grid(SOUTH, WEST, NORTH, EAST, 80, 80, 80, 82.1, 61.9)
    # Group by track (same longitude cluster)
    lons = [lon for _, lon in wpts]
    # First lon and second lon should differ (new track)
    unique_lons = list(dict.fromkeys(lons))  # preserve order, deduplicate
    if len(unique_lons) >= 2:
        # First track: increasing lat; second track: decreasing lat
        first_lon = unique_lons[0]
        second_lon = unique_lons[1]
        track1 = [lat for lat, lon in wpts if abs(lon - first_lon) < 1e-8]
        track2 = [lat for lat, lon in wpts if abs(lon - second_lon) < 1e-8]
        if len(track1) >= 2 and len(track2) >= 2:
            assert track1[0] < track1[-1], "track 1 should go south→north"
            assert track2[0] > track2[-1], "track 2 should go north→south (lawnmower reverse)"


# ---------------------------------------------------------------------------
# make_kmz — single pass
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_waypoints():
    return lawnmower_grid(SOUTH, WEST, NORTH, EAST, 80, 80, 80, 82.1, 61.9)


def test_make_kmz_creates_valid_zip(small_waypoints, tmp_path):
    out = tmp_path / "test.kmz"
    make_kmz(small_waypoints, 80, 8, out)
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "wpmz/template.kml" in names
    assert "wpmz/waylines.wpml" in names


def test_make_kmz_nadir_has_gimbal_rotate(small_waypoints, tmp_path):
    out = tmp_path / "test.kmz"
    make_kmz(small_waypoints, 80, 8, out, gimbal_pitch=-90.0)
    with zipfile.ZipFile(out) as zf:
        wpml = zf.read("wpmz/waylines.wpml").decode()
    assert "gimbalRotate" in wpml
    assert "-90.0" in wpml


def test_make_kmz_oblique_encodes_pitch(small_waypoints, tmp_path):
    out = tmp_path / "test.kmz"
    make_kmz(small_waypoints, 80, 8, out, gimbal_pitch=-45.0)
    with zipfile.ZipFile(out) as zf:
        wpml = zf.read("wpmz/waylines.wpml").decode()
    assert "-45.0" in wpml
    assert "gimbalRotate" in wpml


def test_make_kmz_takephoto_is_action1(small_waypoints, tmp_path):
    out = tmp_path / "test.kmz"
    make_kmz(small_waypoints, 80, 8, out)
    with zipfile.ZipFile(out) as zf:
        wpml = zf.read("wpmz/waylines.wpml").decode()
    # gimbalRotate is action 0, takePhoto is action 1
    assert "<wpml:actionId>0</wpml:actionId>" in wpml
    assert "<wpml:actionId>1</wpml:actionId>" in wpml
    # takePhoto should follow gimbalRotate
    gimbal_pos = wpml.index("gimbalRotate")
    photo_pos  = wpml.index("takePhoto")
    assert gimbal_pos < photo_pos


def test_make_kmz_followwayline_heading(small_waypoints, tmp_path):
    out = tmp_path / "test.kmz"
    make_kmz(small_waypoints, 80, 8, out, heading_mode="followWayline")
    with zipfile.ZipFile(out) as zf:
        wpml = zf.read("wpmz/waylines.wpml").decode()
    assert "followWayline" in wpml
    assert "fixed" not in wpml


def test_make_kmz_fixed_heading_encodes_bearing(small_waypoints, tmp_path):
    for bearing in (0, 90, 180, 270):
        out = tmp_path / f"test_{bearing}.kmz"
        make_kmz(small_waypoints, 80, 8, out, heading_mode="fixed", fixed_heading=bearing)
        with zipfile.ZipFile(out) as zf:
            wpml = zf.read("wpmz/waylines.wpml").decode()
        assert "fixed" in wpml
        assert f"<wpml:waypointHeadingAngle>{bearing}</wpml:waypointHeadingAngle>" in wpml


# ---------------------------------------------------------------------------
# make_photogrammetry_missions
# ---------------------------------------------------------------------------

def test_photogrammetry_produces_five_kmzs(small_waypoints, tmp_path):
    results = make_photogrammetry_missions(small_waypoints, 80, 8, tmp_path, "mission")
    assert len(results) == 5
    for r in results:
        assert pathlib.Path(r["kmz_path"]).exists()


def test_photogrammetry_pass_names(small_waypoints, tmp_path):
    results = make_photogrammetry_missions(small_waypoints, 80, 8, tmp_path, "mission")
    names = [r["pass"] for r in results]
    assert names == ["nadir", "north", "east", "south", "west"]


def test_photogrammetry_nadir_is_90(small_waypoints, tmp_path):
    results = make_photogrammetry_missions(small_waypoints, 80, 8, tmp_path, "mission")
    nadir = next(r for r in results if r["pass"] == "nadir")
    assert nadir["gimbal"] == -90.0
    with zipfile.ZipFile(nadir["kmz_path"]) as zf:
        wpml = zf.read("wpmz/waylines.wpml").decode()
    assert "-90.0" in wpml
    assert "followWayline" in wpml


def test_photogrammetry_oblique_passes_use_fixed_heading(small_waypoints, tmp_path):
    results = make_photogrammetry_missions(small_waypoints, 80, 8, tmp_path, "mission",
                                           oblique_pitch=-45.0)
    headings = {"north": 0, "east": 90, "south": 180, "west": 270}
    for r in results:
        if r["pass"] == "nadir":
            continue
        with zipfile.ZipFile(r["kmz_path"]) as zf:
            wpml = zf.read("wpmz/waylines.wpml").decode()
        assert "fixed" in wpml, f"{r['pass']} should have fixed heading"
        assert f"<wpml:waypointHeadingAngle>{headings[r['pass']]}</wpml:waypointHeadingAngle>" in wpml
        assert "-45.0" in wpml


def test_photogrammetry_oblique_pitch_respected(small_waypoints, tmp_path):
    results = make_photogrammetry_missions(small_waypoints, 80, 8, tmp_path, "mission",
                                           oblique_pitch=-30.0)
    for r in results:
        if r["pass"] == "nadir":
            continue
        assert r["gimbal"] == -30.0
        with zipfile.ZipFile(r["kmz_path"]) as zf:
            wpml = zf.read("wpmz/waylines.wpml").decode()
        assert "-30.0" in wpml


def test_photogrammetry_passes_constant_has_colors():
    assert len(PHOTOGRAMMETRY_PASSES) == 5
    for p in PHOTOGRAMMETRY_PASSES:
        assert "color" in p
        assert p["color"].startswith("#")
