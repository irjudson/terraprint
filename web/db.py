"""Mission lifecycle database — SQLite via stdlib sqlite3."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "missions.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = _connect()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS missions (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            polygon       TEXT NOT NULL,
            altitude      REAL,
            overlap       REAL,
            front_overlap REAL,
            speed         REAL,
            mode          TEXT DEFAULT 'survey',
            status        TEXT DEFAULT 'planned',
            created_at    REAL,
            updated_at    REAL
        );
        CREATE TABLE IF NOT EXISTS mission_passes (
            id                    TEXT PRIMARY KEY,
            mission_id            TEXT NOT NULL REFERENCES missions(id),
            pass_name             TEXT,
            kmz_path              TEXT,
            phone_mission_id      TEXT,
            waypoints             TEXT,
            pushed_at             REAL,
            deleted_from_phone_at REAL
        );
        CREATE TABLE IF NOT EXISTS flights (
            id             TEXT PRIMARY KEY,
            mission_id     TEXT NOT NULL REFERENCES missions(id),
            flight_date    REAL,
            photo_count    INTEGER,
            raw_dir        TEXT,
            odm_status     TEXT DEFAULT 'pending',
            terrain_status TEXT DEFAULT 'pending'
        );
    """)
    con.commit()
    con.close()


def _derive_status(passes: list, flights: list) -> str:
    if flights:
        fl = flights[0]
        if fl["terrain_status"] == "done":
            return "processed"
        if fl["odm_status"] == "running":
            return "processing"
        return "flown"
    active = [p for p in passes if p["phone_mission_id"] and not p["deleted_from_phone_at"]]
    if active:
        return "on_phone"
    return "planned"


def save_mission(
    name: str,
    polygon: list,
    altitude: float,
    overlap: float,
    front_overlap,
    speed: float,
    mode: str,
    passes: list,
) -> str:
    """Persist a generated mission. Returns new mission id."""
    mission_id = str(uuid.uuid4())
    now = time.time()
    con = _connect()
    con.execute(
        """INSERT INTO missions
           (id, name, polygon, altitude, overlap, front_overlap, speed, mode, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,'planned',?,?)""",
        (mission_id, name, json.dumps(polygon), altitude, overlap, front_overlap, speed, mode, now, now),
    )
    for p in passes:
        con.execute(
            """INSERT INTO mission_passes
               (id, mission_id, pass_name, kmz_path, waypoints)
               VALUES (?,?,?,?,?)""",
            (str(uuid.uuid4()), mission_id, p["pass_name"],
             p.get("kmz_path"), json.dumps(p["waypoints"])),
        )
    con.commit()
    con.close()
    return mission_id


def record_push(mission_id: str, pass_name: str, phone_mission_id: str) -> None:
    """Mark a pass as pushed to phone."""
    con = _connect()
    now = time.time()
    con.execute(
        """UPDATE mission_passes SET phone_mission_id=?, pushed_at=?, deleted_from_phone_at=NULL
           WHERE mission_id=? AND pass_name=?""",
        (phone_mission_id, now, mission_id, pass_name),
    )
    con.execute("UPDATE missions SET status='on_phone', updated_at=? WHERE id=?", (now, mission_id))
    con.commit()
    con.close()


def list_missions() -> list:
    """Return all missions with derived status and pass summary."""
    con = _connect()
    missions = con.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
    result = []
    for m in missions:
        passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (m["id"],)).fetchall()
        flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (m["id"],)).fetchall()
        status  = _derive_status(passes, flights)
        result.append({
            "id":              m["id"],
            "name":            m["name"],
            "mode":            m["mode"],
            "status":          status,
            "altitude":        m["altitude"],
            "created_at":      m["created_at"],
            "pass_count":      len(passes),
            "passes_on_phone": sum(1 for p in passes if p["phone_mission_id"] and not p["deleted_from_phone_at"]),
        })
    con.close()
    return result


def get_mission_waypoints(mission_id: str) -> list:
    """Return passes with their waypoints for map rendering."""
    con = _connect()
    passes = con.execute(
        "SELECT pass_name, waypoints, phone_mission_id FROM mission_passes WHERE mission_id=?",
        (mission_id,),
    ).fetchall()
    con.close()
    return [
        {
            "pass_name": p["pass_name"],
            "waypoints": json.loads(p["waypoints"]) if p["waypoints"] else [],
            "on_phone":  bool(p["phone_mission_id"]),
        }
        for p in passes
    ]


def sync_from_phone(phone_rows: list) -> dict:
    """
    Reconcile our DB against what's actually on the phone.
    phone_rows: [{"missionId": str, "name": str, "deleteTime": float}, ...]
    Returns counts: {matched, newly_deleted}
    """
    phone_ids   = {r["missionId"] for r in phone_rows if r.get("deleteTime", -1) == -1}
    deleted_ids = {r["missionId"] for r in phone_rows if r.get("deleteTime", -1) != -1}

    con = _connect()
    now = time.time()
    matched = 0
    newly_deleted = 0

    our_passes = con.execute(
        "SELECT id, phone_mission_id, mission_id FROM mission_passes WHERE phone_mission_id IS NOT NULL"
    ).fetchall()

    affected_missions = set()
    for p in our_passes:
        pmid = p["phone_mission_id"]
        if pmid in phone_ids:
            matched += 1
        elif pmid in deleted_ids:
            already = con.execute(
                "SELECT 1 FROM mission_passes WHERE id=? AND deleted_from_phone_at IS NOT NULL",
                (p["id"],),
            ).fetchone()
            if not already:
                con.execute(
                    "UPDATE mission_passes SET deleted_from_phone_at=? WHERE id=?", (now, p["id"])
                )
                newly_deleted += 1
        affected_missions.add(p["mission_id"])

    for mid in affected_missions:
        passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (mid,)).fetchall()
        flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (mid,)).fetchall()
        status  = _derive_status(passes, flights)
        con.execute("UPDATE missions SET status=?, updated_at=? WHERE id=?", (status, now, mid))

    con.commit()
    con.close()
    return {"matched": matched, "newly_deleted": newly_deleted}
