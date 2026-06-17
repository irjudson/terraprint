import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import web.db as db


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_missions.db")
    db.init_db()


POLY = [[45.73, -111.48], [45.73, -111.47], [45.74, -111.47], [45.74, -111.48]]
WAYPOINTS = [[45.731, -111.479], [45.732, -111.475], [45.733, -111.471]]


def _one_pass(name="survey"):
    return [{"pass_name": name, "kmz_path": "/tmp/test.kmz", "waypoints": WAYPOINTS}]


# ── init_db ───────────────────────────────────────────────────────────────────

def test_init_db_creates_tables(tmp_path, monkeypatch):
    db.init_db()  # second call should not raise
    with db._db() as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"missions", "mission_passes", "flights"} <= tables


# ── save_mission ──────────────────────────────────────────────────────────────

def test_save_mission_returns_id():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    assert len(mid) == 36  # UUID4


def test_save_mission_persists_data():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    rows = db.list_missions()
    assert len(rows) == 1
    assert rows[0]["id"] == mid
    assert rows[0]["name"] == "BJR"
    assert rows[0]["status"] == "planned"


def test_save_mission_photogrammetry_five_passes():
    passes = [
        {"pass_name": n, "kmz_path": "/tmp/x.kmz", "waypoints": WAYPOINTS}
        for n in ("nadir", "north", "east", "south", "west")
    ]
    db.save_mission("BJR5", POLY, 80, 80, None, 8, "photogrammetry", passes)
    rows = db.list_missions()
    assert rows[0]["pass_count"] == 5


# ── record_push ───────────────────────────────────────────────────────────────

def test_record_push_updates_status():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID-1234")
    rows = db.list_missions()
    assert rows[0]["status"] == "on_phone"
    assert rows[0]["passes_on_phone"] == 1


def test_record_push_clears_deleted_flag():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID-1234")
    with db._db() as con:
        con.execute(
            "UPDATE mission_passes SET deleted_from_phone_at=? WHERE mission_id=?",
            (time.time(), mid),
        )
    db.record_push(mid, "survey", "PHONE-UUID-5678")
    rows = db.list_missions()
    assert rows[0]["passes_on_phone"] == 1


# ── get_mission_waypoints ─────────────────────────────────────────────────────

def test_get_mission_waypoints_returns_passes():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    passes = db.get_mission_waypoints(mid)
    assert len(passes) == 1
    assert passes[0]["pass_name"] == "survey"
    assert passes[0]["waypoints"] == WAYPOINTS
    assert passes[0]["on_phone"] is False


def test_get_mission_waypoints_on_phone_flag():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID")
    passes = db.get_mission_waypoints(mid)
    assert passes[0]["on_phone"] is True


def test_get_mission_waypoints_deleted_not_on_phone():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID")
    with db._db() as con:
        con.execute(
            "UPDATE mission_passes SET deleted_from_phone_at=? WHERE mission_id=?",
            (time.time(), mid),
        )
    passes = db.get_mission_waypoints(mid)
    assert passes[0]["on_phone"] is False


# ── sync_from_phone ───────────────────────────────────────────────────────────

def test_sync_marks_deleted_passes():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID")
    result = db.sync_from_phone([{"missionId": "PHONE-UUID", "deleteTime": 1234567.0, "waypoints": []}])
    assert result["newly_deleted"] == 1
    rows = db.list_missions()
    assert rows[0]["passes_on_phone"] == 0


def test_sync_matched_count():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID")
    result = db.sync_from_phone([{"missionId": "PHONE-UUID", "deleteTime": -1, "waypoints": WAYPOINTS}])
    assert result["matched"] == 1
    assert result["newly_deleted"] == 0
    assert result["imported"] == 0


def test_sync_imports_unknown_phone_missions():
    result = db.sync_from_phone([
        {"missionId": "NEW-PHONE-UUID", "name": "Phone Mission", "deleteTime": -1, "waypoints": WAYPOINTS}
    ])
    assert result["imported"] == 1
    rows = db.list_missions()
    assert len(rows) == 1
    assert rows[0]["name"] == "Phone Mission"
    assert rows[0]["status"] == "on_phone"
    assert rows[0]["passes_on_phone"] == 1


def test_sync_imports_waypoints_correctly():
    db.sync_from_phone([
        {"missionId": "WP-UUID", "name": "WP Mission", "deleteTime": -1, "waypoints": WAYPOINTS}
    ])
    rows = db.list_missions()
    passes = db.get_mission_waypoints(rows[0]["id"])
    assert passes[0]["waypoints"] == WAYPOINTS
    assert passes[0]["on_phone"] is True


def test_sync_does_not_reimport_known_missions():
    db.sync_from_phone([
        {"missionId": "KNOWN-UUID", "name": "Mission", "deleteTime": -1, "waypoints": WAYPOINTS}
    ])
    result = db.sync_from_phone([
        {"missionId": "KNOWN-UUID", "name": "Mission", "deleteTime": -1, "waypoints": WAYPOINTS}
    ])
    assert result["imported"] == 0
    assert result["matched"] == 1
    assert len(db.list_missions()) == 1
