"""Mission lifecycle database — SQLite via stdlib sqlite3."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "missions.db"

PASS_SUFFIXES = {"nadir", "north", "east", "south", "west", "oblique", "grid"}


def _base_name(name: str) -> tuple[str, str]:
    """Split 'BJR nadir' → ('BJR', 'nadir'). No suffix → (name, 'survey')."""
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in PASS_SUFFIXES:
        return parts[0].strip(), parts[1].lower()
    return name.strip(), "survey"


@contextmanager
def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.execute("""
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
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS mission_passes (
                id                    TEXT PRIMARY KEY,
                mission_id            TEXT NOT NULL REFERENCES missions(id),
                pass_name             TEXT,
                kmz_path              TEXT,
                phone_mission_id      TEXT,
                waypoints             TEXT,
                pushed_at             REAL,
                deleted_from_phone_at REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS flights (
                id             TEXT PRIMARY KEY,
                mission_id     TEXT NOT NULL REFERENCES missions(id),
                flight_date    REAL,
                photo_count    INTEGER,
                raw_dir        TEXT,
                odm_status     TEXT DEFAULT 'pending',
                terrain_status TEXT DEFAULT 'pending'
            )
        """)


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
    front_overlap: float | None,
    speed: float,
    mode: str,
    passes: list,
) -> str:
    """Persist a generated mission. Returns new mission id."""
    mission_id = str(uuid.uuid4())
    now = time.time()
    with _db() as con:
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
    return mission_id


def record_push(mission_id: str, pass_name: str, phone_mission_id: str) -> None:
    """Mark a pass as pushed to phone."""
    now = time.time()
    with _db() as con:
        con.execute(
            """UPDATE mission_passes SET phone_mission_id=?, pushed_at=?, deleted_from_phone_at=NULL
               WHERE mission_id=? AND pass_name=?""",
            (phone_mission_id, now, mission_id, pass_name),
        )
        passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (mission_id,)).fetchall()
        flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (mission_id,)).fetchall()
        status  = _derive_status(passes, flights)
        con.execute("UPDATE missions SET status=?, updated_at=? WHERE id=?", (status, now, mission_id))


def list_missions() -> list:
    """Return all missions with derived status and pass summary."""
    with _db() as con:
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
    return result


def get_mission_waypoints(mission_id: str) -> list:
    """Return passes with their waypoints for map rendering."""
    with _db() as con:
        passes = con.execute(
            "SELECT pass_name, waypoints, phone_mission_id, deleted_from_phone_at FROM mission_passes WHERE mission_id=?",
            (mission_id,),
        ).fetchall()
    return [
        {
            "pass_name": p["pass_name"],
            "waypoints": json.loads(p["waypoints"]) if p["waypoints"] else [],
            "on_phone":  bool(p["phone_mission_id"]) and not p["deleted_from_phone_at"],
        }
        for p in passes
    ]


def sync_from_phone(phone_rows: list) -> dict:
    """
    Reconcile our DB against what's on the phone.
    phone_rows: [{"missionId": str, "name": str, "deleteTime": float, "waypoints": [[lat,lon],...]}]
    Returns counts: {matched, newly_deleted, imported}
    """
    active_rows = [r for r in phone_rows if r.get("deleteTime", -1) == -1]
    phone_ids   = {r["missionId"] for r in active_rows}
    deleted_ids = {r["missionId"] for r in phone_rows if r.get("deleteTime", -1) != -1}
    by_id       = {r["missionId"]: r for r in phone_rows}

    now = time.time()
    matched = 0
    newly_deleted = 0
    imported = 0

    with _db() as con:
        our_passes = con.execute(
            "SELECT id, phone_mission_id, mission_id FROM mission_passes WHERE phone_mission_id IS NOT NULL"
        ).fetchall()
        known_phone_ids = {p["phone_mission_id"] for p in our_passes}

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

        # Import phone missions that aren't in our DB yet — group by base name
        groups: dict[str, list] = {}
        for phone_id in phone_ids - known_phone_ids:
            row = by_id[phone_id]
            base, suffix = _base_name(row.get("name", ""))
            groups.setdefault(base, []).append(
                {"phone_id": phone_id, "suffix": suffix, "row": row}
            )

        for base, passes in groups.items():
            mission_id = str(uuid.uuid4())
            # 2+ passes from the same base name = photogrammetry; 1 pass = survey
            mode = "photogrammetry" if len(passes) > 1 else "survey"
            con.execute(
                """INSERT INTO missions
                   (id, name, polygon, mode, status, created_at, updated_at)
                   VALUES (?,?,'[]',?,'on_phone',?,?)""",
                (mission_id, base, mode, now, now),
            )
            for p in passes:
                con.execute(
                    """INSERT INTO mission_passes
                       (id, mission_id, pass_name, phone_mission_id, waypoints, pushed_at)
                       VALUES (?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), mission_id, p["suffix"], p["phone_id"],
                     json.dumps(p["row"].get("waypoints", [])), now),
                )
                imported += 1

    return {"matched": matched, "newly_deleted": newly_deleted, "imported": imported}


def log_flight(mission_id: str, raw_dir: str, photo_count: int,
               flight_date: float | None = None) -> str:
    """Create a flight record for a mission. Returns new flight id."""
    flight_id = str(uuid.uuid4())
    now = time.time()
    with _db() as con:
        con.execute(
            """INSERT INTO flights (id, mission_id, flight_date, photo_count, raw_dir)
               VALUES (?,?,?,?,?)""",
            (flight_id, mission_id, flight_date or now, photo_count, raw_dir),
        )
        passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (mission_id,)).fetchall()
        flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (mission_id,)).fetchall()
        status  = _derive_status(passes, flights)
        con.execute("UPDATE missions SET status=?, updated_at=? WHERE id=?", (status, now, mission_id))
    return flight_id


def list_flights(mission_id: str) -> list:
    """Return all flights for a mission ordered by flight_date."""
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM flights WHERE mission_id=? ORDER BY flight_date DESC",
            (mission_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_flight_status(flight_id: str, odm_status: str | None = None,
                         terrain_status: str | None = None) -> None:
    """Patch odm_status and/or terrain_status; re-derives parent mission status."""
    now = time.time()
    with _db() as con:
        if odm_status is not None:
            con.execute("UPDATE flights SET odm_status=? WHERE id=?", (odm_status, flight_id))
        if terrain_status is not None:
            con.execute("UPDATE flights SET terrain_status=? WHERE id=?", (terrain_status, flight_id))
        row = con.execute("SELECT mission_id FROM flights WHERE id=?", (flight_id,)).fetchone()
        if row:
            mid = row["mission_id"]
            passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (mid,)).fetchall()
            flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (mid,)).fetchall()
            status  = _derive_status(passes, flights)
            con.execute("UPDATE missions SET status=?, updated_at=? WHERE id=?", (status, now, mid))


def mark_flight_done(flight_id: str) -> None:
    """Mark both pipeline stages done and promote mission to processed."""
    update_flight_status(flight_id, odm_status="done", terrain_status="done")
