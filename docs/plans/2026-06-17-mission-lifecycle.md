# Mission Lifecycle System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a persistent SQLite mission database and a lifecycle-aware Missions panel to the Terraprint web app so missions can be tracked from planning through flight and reconstruction, with two-way sync against the phone.

**Architecture:** A new `web/db.py` module owns the SQLite schema and all CRUD. `web/app.py` grows five new endpoints (list, save, waypoints, sync-phone, mark-flown). The single-page frontend gets a collapsible Missions sidebar that lists missions by lifecycle stage and draws selected mission paths on the map. Phone sync pulls the phone's `wpmz.sqlite3` via USB and reconciles it against our local DB by matching the UUID stored when we pushed.

**Tech Stack:** Python 3.11, SQLite (stdlib `sqlite3`), FastAPI, Leaflet.js, pymobiledevice3 (already installed)

---

## Schema reference (read before touching any task)

```sql
-- one row per planned area
CREATE TABLE missions (
    id          TEXT PRIMARY KEY,      -- UUID4
    name        TEXT NOT NULL,
    polygon     TEXT NOT NULL,         -- JSON [[lat,lon],…]
    altitude    REAL,
    overlap     REAL,
    front_overlap REAL,
    speed       REAL,
    mode        TEXT DEFAULT 'survey', -- 'survey' | 'photogrammetry'
    status      TEXT DEFAULT 'planned',-- planned | on_phone | flown | processing | processed
    created_at  REAL,
    updated_at  REAL
);

-- one row per KMZ pass (1 for survey, 5 for photogrammetry)
CREATE TABLE mission_passes (
    id                    TEXT PRIMARY KEY,  -- UUID4
    mission_id            TEXT NOT NULL REFERENCES missions(id),
    pass_name             TEXT,              -- nadir|north|east|south|west|survey
    kmz_path              TEXT,              -- local absolute path
    phone_mission_id      TEXT,              -- UUID on phone (wpmz missionId)
    waypoints             TEXT,              -- JSON [[lat,lon],…]
    pushed_at             REAL,
    deleted_from_phone_at REAL
);

-- one row per flight event
CREATE TABLE flights (
    id             TEXT PRIMARY KEY,
    mission_id     TEXT NOT NULL REFERENCES missions(id),
    flight_date    REAL,
    photo_count    INTEGER,
    raw_dir        TEXT,
    odm_status     TEXT DEFAULT 'pending',  -- pending|running|done|failed
    terrain_status TEXT DEFAULT 'pending'
);
```

Lifecycle status derivation (computed in Python, not stored redundantly):
- `planned`    — no passes have a `phone_mission_id`
- `on_phone`   — at least one pass has a `phone_mission_id` and no `deleted_from_phone_at`
- `flown`      — a `flights` row exists for this mission
- `processing` — flight exists and `odm_status = 'running'`
- `processed`  — flight exists and `terrain_status = 'done'`

---

## Task 1: Create `web/db.py` — schema + CRUD

**Files:**
- Create: `web/db.py`

### Step 1: Write the file

```python
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


def _derive_status(passes: list[sqlite3.Row], flights: list[sqlite3.Row]) -> str:
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
    passes: list[dict],  # [{"pass_name": str, "kmz_path": str, "waypoints": list}]
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


def list_missions() -> list[dict]:
    """Return all missions with derived status and pass summary."""
    con = _connect()
    missions = con.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
    result = []
    for m in missions:
        passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (m["id"],)).fetchall()
        flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (m["id"],)).fetchall()
        status  = _derive_status(passes, flights)
        result.append({
            "id":         m["id"],
            "name":       m["name"],
            "mode":       m["mode"],
            "status":     status,
            "altitude":   m["altitude"],
            "created_at": m["created_at"],
            "pass_count": len(passes),
            "passes_on_phone": sum(1 for p in passes if p["phone_mission_id"] and not p["deleted_from_phone_at"]),
        })
    con.close()
    return result


def get_mission_waypoints(mission_id: str) -> list[dict]:
    """Return passes with their waypoints for map rendering."""
    con = _connect()
    passes = con.execute(
        "SELECT pass_name, waypoints, phone_mission_id FROM mission_passes WHERE mission_id=?",
        (mission_id,),
    ).fetchall()
    con.close()
    return [
        {"pass_name": p["pass_name"],
         "waypoints": json.loads(p["waypoints"]) if p["waypoints"] else [],
         "on_phone":  bool(p["phone_mission_id"])}
        for p in passes
    ]


def sync_from_phone(phone_rows: list[dict]) -> dict:
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

    # Mark passes whose phone mission was deleted
    our_passes = con.execute(
        "SELECT id, phone_mission_id FROM mission_passes WHERE phone_mission_id IS NOT NULL"
    ).fetchall()
    for p in our_passes:
        pmid = p["phone_mission_id"]
        if pmid in phone_ids:
            matched += 1
        elif pmid in deleted_ids and not con.execute(
            "SELECT 1 FROM mission_passes WHERE id=? AND deleted_from_phone_at IS NOT NULL", (p["id"],)
        ).fetchone():
            con.execute(
                "UPDATE mission_passes SET deleted_from_phone_at=? WHERE id=?", (now, p["id"])
            )
            newly_deleted += 1

    # Re-derive and persist status for affected missions
    affected = {p["id"] for p in our_passes}
    for pass_id in affected:
        row = con.execute("SELECT mission_id FROM mission_passes WHERE id=?", (pass_id,)).fetchone()
        if row:
            mid = row["mission_id"]
            passes  = con.execute("SELECT * FROM mission_passes WHERE mission_id=?", (mid,)).fetchall()
            flights = con.execute("SELECT * FROM flights WHERE mission_id=?", (mid,)).fetchall()
            status  = _derive_status(passes, flights)
            con.execute("UPDATE missions SET status=?, updated_at=? WHERE id=?", (status, now, mid))

    con.commit()
    con.close()
    return {"matched": matched, "newly_deleted": newly_deleted}
```

### Step 2: No test yet — move to Task 2.

---

## Task 2: Test `web/db.py`

**Files:**
- Create: `tests/test_db.py`

### Step 1: Write the tests

```python
"""Tests for web/db.py — mission lifecycle database."""

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
    """init_db is idempotent and creates all three tables."""
    db.init_db()  # second call should not raise
    con = db._connect()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"missions", "mission_passes", "flights"} <= tables
    con.close()


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
    passes = [{"pass_name": n, "kmz_path": "/tmp/x.kmz", "waypoints": WAYPOINTS}
              for n in ("nadir", "north", "east", "south", "west")]
    mid = db.save_mission("BJR5", POLY, 80, 80, None, 8, "photogrammetry", passes)
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
    # Simulate deletion then re-push
    con = db._connect()
    con.execute("UPDATE mission_passes SET deleted_from_phone_at=? WHERE mission_id=?",
                (time.time(), mid))
    con.commit(); con.close()
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


# ── sync_from_phone ───────────────────────────────────────────────────────────

def test_sync_marks_deleted_passes():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID")
    result = db.sync_from_phone([{"missionId": "PHONE-UUID", "deleteTime": 1234567.0}])
    assert result["newly_deleted"] == 1
    rows = db.list_missions()
    assert rows[0]["passes_on_phone"] == 0


def test_sync_matched_count():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.record_push(mid, "survey", "PHONE-UUID")
    result = db.sync_from_phone([{"missionId": "PHONE-UUID", "deleteTime": -1}])
    assert result["matched"] == 1
    assert result["newly_deleted"] == 0


def test_sync_unknown_phone_missions_ignored():
    """Phone missions we never pushed are not inserted."""
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    result = db.sync_from_phone([{"missionId": "UNKNOWN-UUID", "deleteTime": -1}])
    rows = db.list_missions()
    assert rows[0]["status"] == "planned"
```

### Step 2: Run the tests — expect FAIL (module missing)

```bash
cd /home/irjudson/Projects/terraprint && uv run pytest tests/test_db.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` because `web/db.py` doesn't exist yet.

### Step 3: Create `web/db.py` (copy from Task 1)

### Step 4: Run tests again

```bash
uv run pytest tests/test_db.py -v
```

Expected: all green.

### Step 5: Commit

```bash
git add web/db.py tests/test_db.py
git commit -m "feat: add mission lifecycle SQLite database (web/db.py)"
```

---

## Task 3: Wire DB into `web/app.py` — startup + save on generate

**Files:**
- Modify: `web/app.py`

### Step 1: Add `init_db` call at startup

At the top of `web/app.py`, after the imports, add:

```python
from web.db import init_db, save_mission, record_push, list_missions, get_mission_waypoints, sync_from_phone as _sync_from_phone
```

After `app = FastAPI(...)`, add:

```python
@app.on_event("startup")
async def _startup():
    init_db()
```

### Step 2: Add `POST /api/missions` endpoint

This saves a generated mission to the DB. Add after the existing `/api/generate` endpoint:

```python
class SaveMissionRequest(BaseModel):
    job_id: str
    name: str

@app.post("/api/missions")
async def save_mission_endpoint(req: SaveMissionRequest):
    job = _jobs.get(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found — re-generate first.")

    if job["mode"] == "photogrammetry":
        passes = [
            {"pass_name": p["pass"], "kmz_path": p.get("kmz_path"), "waypoints": p["waypoints"]}
            for p in job["passes"]
        ]
    else:
        passes = [{"pass_name": "survey", "kmz_path": job.get("kmz_path"), "waypoints": job["waypoints"]}]

    # Pull params from job (stored during generate)
    params = job.get("params", {})
    mission_id = save_mission(
        name=req.name,
        polygon=job.get("polygon", []),
        altitude=params.get("altitude", 80),
        overlap=params.get("overlap", 80),
        front_overlap=params.get("front_overlap"),
        speed=params.get("speed", 8),
        mode=job["mode"],
        passes=passes,
    )
    return {"mission_id": mission_id}
```

### Step 3: Store params + polygon in `_jobs` during `/api/generate`

In the existing `generate()` handler, update the two `_jobs[job_id] = {...}` assignments to also include `polygon` and `params`:

**For photogrammetry mode** (around line 145), change:
```python
_jobs[job_id] = {"mode": "photogrammetry", "name": req.name,
                 "passes": [dict(p, waypoints=waypoints) for p in passes]}
```
to:
```python
_jobs[job_id] = {
    "mode": "photogrammetry", "name": req.name,
    "passes": [dict(p, waypoints=waypoints) for p in passes],
    "polygon": req.polygon,
    "params": {"altitude": req.altitude, "overlap": req.overlap,
               "front_overlap": req.front_overlap, "speed": req.speed},
}
```

**For survey mode** (around line 163), change:
```python
_jobs[job_id] = {"mode": "survey", "kmz_path": str(tmp_path),
                 "name": req.name, "waypoints": waypoints}
```
to:
```python
_jobs[job_id] = {
    "mode": "survey", "kmz_path": str(tmp_path),
    "name": req.name, "waypoints": waypoints,
    "polygon": req.polygon,
    "params": {"altitude": req.altitude, "overlap": req.overlap,
               "front_overlap": req.front_overlap, "speed": req.speed},
}
```

### Step 4: Update `POST /api/push/{job_id}` to call `record_push`

In the existing `push()` handler, after the line `mission_ids.append(mission_id)`, add:

```python
# job["mission_id"] is set by the frontend after calling POST /api/missions
if "mission_id" in job:
    pass_name = item_name.split("(")[-1].rstrip(")") if "(" in item_name else "survey"
    record_push(job["mission_id"], pass_name, mission_id)
```

And store the `mission_id` on the job when the frontend calls `POST /api/missions`:
```python
# At end of save_mission_endpoint, before return:
if req.job_id in _jobs:
    _jobs[req.job_id]["mission_id"] = mission_id
```

### Step 5: Restart and verify no errors

```bash
uv run uvicorn web.app:app --host 0.0.0.0 --port 8001 --reload
```

Expected: starts cleanly, `data/missions.db` created.

### Step 6: Commit

```bash
git add web/app.py
git commit -m "feat: wire mission DB into app startup, generate, and push endpoints"
```

---

## Task 4: Add `GET /api/missions`, `GET /api/missions/{id}/waypoints`, `POST /api/missions/{id}/sync-phone`

**Files:**
- Modify: `web/app.py`

### Step 1: Add the three endpoints

Add these after the `save_mission_endpoint`:

```python
@app.get("/api/missions")
async def list_missions_endpoint():
    return list_missions()


@app.get("/api/missions/{mission_id}/waypoints")
async def mission_waypoints(mission_id: str):
    passes = get_mission_waypoints(mission_id)
    if not passes:
        raise HTTPException(404, "Mission not found.")
    return passes


@app.post("/api/missions/{mission_id}/sync-phone")
async def sync_phone(mission_id: str):
    """Pull phone mission DB and reconcile against our records."""
    try:
        from skyrover_ios_bridge import MISSION_DB, _afc_session, _get_lockdown, _read
        import tempfile, sqlite3 as _sq

        try:
            lockdown = await asyncio.wait_for(_get_lockdown(), timeout=8.0)
        except asyncio.TimeoutError:
            raise HTTPException(503, "No iPhone found.")

        async with _afc_session(lockdown) as afc:
            db_bytes = await _read(afc, MISSION_DB)

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(db_bytes)
            tmp = f.name

        con = _sq.connect(tmp)
        rows = [
            {"missionId": r[0], "name": r[1], "deleteTime": r[2]}
            for r in con.execute("SELECT missionId, name, deleteTime FROM kmzTable").fetchall()
        ]
        con.close()
        Path(tmp).unlink(missing_ok=True)

        counts = _sync_from_phone(rows)
        return {"ok": True, **counts, "phone_total": len(rows)}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))
```

### Step 2: Verify endpoints with curl

```bash
# Should return [] initially
curl -s http://localhost:8001/api/missions | python3 -m json.tool
```

Expected: `[]`

### Step 3: Commit

```bash
git add web/app.py
git commit -m "feat: add list, waypoints, and sync-phone API endpoints"
```

---

## Task 5: Frontend — Missions sidebar panel

**Files:**
- Modify: `web/static/index.html`

This is the largest task. Work section by section.

### Step 1: Add CSS for the sidebar

Add inside the `<style>` block, before `</style>`:

```css
/* ── missions sidebar ── */
#missions-panel {
  position: fixed; left: 68px; top: 12px; bottom: 12px;
  z-index: 1000; width: 280px;
  background: var(--surface); border-radius: var(--radius);
  box-shadow: var(--shadow); display: none; flex-direction: column;
  overflow: hidden;
}
#missions-panel.open { display: flex; }
#missions-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,.06);
  flex-shrink: 0;
}
#missions-header h3 { font-size: 13px; font-weight: 600; margin: 0; }
#btn-sync-phone {
  padding: 4px 10px; border: none; border-radius: 4px;
  background: var(--card); color: var(--text); font-size: 11px;
  cursor: pointer; white-space: nowrap;
}
#btn-sync-phone:hover { background: var(--accent); }
#missions-list {
  flex: 1; overflow-y: auto; padding: 8px 0;
}
.mission-item {
  padding: 8px 14px; cursor: pointer;
  border-bottom: 1px solid rgba(255,255,255,.03);
  transition: background .1s;
}
.mission-item:hover { background: rgba(255,255,255,.04); }
.mission-item.selected { background: var(--card); }
.mission-item-name { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
.mission-item-meta { font-size: 11px; color: var(--muted); }
.mission-status {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; margin-left: 6px;
}
.status-planned    { background: #333; color: #aaa; }
.status-on_phone   { background: #1a4a80; color: #7ab4f5; }
.status-flown      { background: #1a4a1a; color: #7af57a; }
.status-processing { background: #4a3a1a; color: #f5c57a; }
.status-processed  { background: #2a4a2a; color: #4caf50; }
```

### Step 2: Add sidebar HTML

Inside `<body>`, after the `#settings-panel` div, add:

```html
<!-- Missions sidebar -->
<div id="missions-panel">
  <div id="missions-header">
    <h3>Missions</h3>
    <button id="btn-sync-phone" title="Sync with phone">📱 Sync</button>
  </div>
  <div id="missions-list">
    <div style="padding:14px;font-size:12px;color:var(--muted);">Loading…</div>
  </div>
</div>
```

### Step 3: Add a toolbar button for the missions panel

In the `#toolbar` div, after `#btn-settings`, add:

```html
<button class="tool-btn" id="btn-missions" title="Missions">📋</button>
```

### Step 4: Add JavaScript

At the end of the `<script>` block, before `</script>`, add:

```javascript
// ── Missions panel ──────────────────────────────────────────────────────────

const STATUS_COLORS = {
  planned:    '#888888',
  on_phone:   '#4a90d9',
  flown:      '#4caf50',
  processing: '#f5a623',
  processed:  '#27ae60',
};

let missionLayers = [];
let selectedMissionId = null;

function clearMissionLayers() {
  missionLayers.forEach(l => map.removeLayer(l));
  missionLayers = [];
}

async function loadMissions() {
  const list = document.getElementById('missions-list');
  try {
    const res = await fetch('/api/missions');
    const missions = await res.json();
    if (!missions.length) {
      list.innerHTML = '<div style="padding:14px;font-size:12px;color:var(--muted);">No missions yet.<br>Draw a polygon and generate one.</div>';
      return;
    }
    list.innerHTML = '';
    missions.forEach(m => {
      const div = document.createElement('div');
      div.className = 'mission-item' + (m.id === selectedMissionId ? ' selected' : '');
      div.dataset.id = m.id;
      const d = new Date(m.created_at * 1000);
      const dateStr = d.toLocaleDateString();
      div.innerHTML = `
        <div class="mission-item-name">
          ${m.name}
          <span class="mission-status status-${m.status}">${m.status.replace('_',' ')}</span>
        </div>
        <div class="mission-item-meta">${m.mode} · ${m.altitude}m · ${dateStr}</div>
      `;
      div.addEventListener('click', () => visualizeMission(m.id, div));
      list.appendChild(div);
    });
  } catch (e) {
    list.innerHTML = '<div style="padding:14px;font-size:12px;color:var(--muted);">Failed to load.</div>';
  }
}

async function visualizeMission(missionId, el) {
  // Deselect previous
  document.querySelectorAll('.mission-item.selected').forEach(d => d.classList.remove('selected'));
  clearMissionLayers();

  if (selectedMissionId === missionId) {
    selectedMissionId = null;
    return; // toggle off
  }
  selectedMissionId = missionId;
  el.classList.add('selected');

  try {
    const res = await fetch(`/api/missions/${missionId}/waypoints`);
    const passes = await res.json();

    passes.forEach(p => {
      if (!p.waypoints.length) return;
      const color = p.on_phone ? STATUS_COLORS.on_phone : STATUS_COLORS.planned;
      const lls = p.waypoints.map(wp => [wp[0], wp[1]]);
      const line = L.polyline(lls, { color, weight: 1.5, opacity: .8 }).addTo(map);
      missionLayers.push(line);
      // Start dot
      missionLayers.push(
        L.circleMarker(lls[0], { radius: 5, color, fillColor: color, fillOpacity: 1 }).addTo(map)
      );
    });

    if (missionLayers.length) {
      const group = L.featureGroup(missionLayers);
      map.fitBounds(group.getBounds(), { padding: [40, 40] });
    }
  } catch (e) {
    toast('Failed to load waypoints', 'err');
  }
}

document.getElementById('btn-missions').addEventListener('click', () => {
  const panel = document.getElementById('missions-panel');
  const isOpen = panel.classList.toggle('open');
  document.getElementById('btn-missions').classList.toggle('active', isOpen);
  if (isOpen) loadMissions();
});

document.getElementById('btn-sync-phone').addEventListener('click', async () => {
  const btn = document.getElementById('btn-sync-phone');
  btn.disabled = true;
  btn.textContent = 'Syncing…';
  try {
    // Sync globally (we pass a sentinel mission_id; backend reads all phone missions)
    const res = await fetch('/api/missions/sync/sync-phone', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || 'Sync failed', 'err'); return; }
    toast(`Sync done · ${data.matched} matched · ${data.newly_deleted} removed`, 'ok');
    loadMissions();
  } catch (e) {
    toast('Sync failed — phone plugged in?', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '📱 Sync';
  }
});

// After a successful push, save to DB then refresh missions list
// Patch the existing push button handler to also POST /api/missions
const origPushBtn = document.getElementById('btn-push');
const origPushHandler = origPushBtn.onclick;
document.getElementById('btn-push').addEventListener('click', async function patchedPush() {
  // The main push listener runs first; we hook the save step after it completes.
  // We use a small delay to let the push response resolve before saving.
  // (A cleaner approach would be a shared promise, but this avoids rewriting the push handler.)
});
// NOTE: The save-to-DB step is handled server-side via record_push() in /api/push.
// The frontend needs to call POST /api/missions before pushing so the server knows the mission_id.
// Add this to the existing push button click handler (find the handler and wrap it):

// Replace the existing push button listener with a version that saves first:
// Find: document.getElementById('btn-push').addEventListener('click', async () => {
// Change the handler body to save mission first, then push:
/*
  MANUAL EDIT REQUIRED in the existing push listener:
  At the start of the push click handler, BEFORE the fetch('/api/push/...'), add:

  // Save mission to DB if not already saved
  if (currentJobId && !window._savedMissionId) {
    const nameEl = document.getElementById('mission-name');
    const saveRes = await fetch('/api/missions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ job_id: currentJobId, name: nameEl.value.trim() || 'Mission' }),
    });
    if (saveRes.ok) {
      const saved = await saveRes.json();
      window._savedMissionId = saved.mission_id;
    }
  }

  Also reset window._savedMissionId = null in the clearPolygon() / btn-clear handler.
  And after a successful push response, call loadMissions() if the panel is open.
*/
```

### Step 5: Apply the manual edits to the push handler

In `web/static/index.html`, find the push button handler (around line 611):

```javascript
document.getElementById('btn-push').addEventListener('click', async () => {
  if (!currentJobId) return;
  const btn = document.getElementById('btn-push');
  btn.disabled = true;
  btn.textContent = 'Pushing…';
  toast('Pushing to phone…', 'info');

  try {
    const res = await fetch(`/api/push/${currentJobId}`, { method: 'POST' });
```

Replace with:

```javascript
document.getElementById('btn-push').addEventListener('click', async () => {
  if (!currentJobId) return;
  const btn = document.getElementById('btn-push');
  btn.disabled = true;
  btn.textContent = 'Pushing…';
  toast('Pushing to phone…', 'info');

  try {
    // Save to mission DB before pushing (idempotent if already saved)
    if (!window._savedMissionId) {
      const nameEl = document.getElementById('mission-name');
      const saveRes = await fetch('/api/missions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: currentJobId, name: nameEl.value.trim() || 'Mission' }),
      });
      if (saveRes.ok) {
        const saved = await saveRes.json();
        window._savedMissionId = saved.mission_id;
      }
    }

    const res = await fetch(`/api/push/${currentJobId}`, { method: 'POST' });
```

And in the success branch (`toast(\`✓ ...`)), after the toast call add:
```javascript
    if (document.getElementById('missions-panel').classList.contains('open')) loadMissions();
    window._savedMissionId = null;
```

And in the `btn-clear` handler, add:
```javascript
    window._savedMissionId = null;
```

### Step 6: Add a global sync endpoint (mission_id = "sync")

In `web/app.py`, the sync endpoint is `POST /api/missions/{mission_id}/sync-phone`. The frontend uses `sync` as the sentinel. No change needed — FastAPI will route `/api/missions/sync/sync-phone` to the handler with `mission_id="sync"`, which is fine since we ignore `mission_id` in the global sync.

### Step 7: Restart and smoke-test in browser

```bash
# Restart the server
docker compose restart web
# Or if running dev:
uv run uvicorn web.app:app --host 0.0.0.0 --port 8001 --reload
```

Open http://localhost:8001, click 📋, verify the panel opens and shows "No missions yet."

### Step 8: Commit

```bash
git add web/static/index.html web/app.py
git commit -m "feat: add Missions sidebar panel with map visualization and phone sync"
```

---

## Task 6: End-to-end smoke test

**Goal:** Verify the full flow works manually. No automated test needed for the phone path (requires hardware).

### Step 1: Generate a mission

1. Open http://localhost:8001
2. Draw a polygon over the ranch area
3. Click **Generate Preview** — waypoints appear on map
4. Click 📋 — Missions panel opens, shows "No missions yet"
5. Click **📱 Push to Phone** — triggers save-to-DB then push
6. Missions panel refreshes — mission appears with status `on_phone`

### Step 2: Click the mission in the sidebar

Mission waypoints draw on map in blue.

### Step 3: Sync phone

Click **📱 Sync** — returns match count, no errors.

### Step 4: Verify DB content

```bash
sqlite3 data/missions.db "SELECT name, status FROM missions;"
sqlite3 data/missions.db "SELECT pass_name, phone_mission_id, pushed_at FROM mission_passes;"
```

Expected: one row each, `phone_mission_id` populated after push.

### Step 5: Run all tests

```bash
uv run pytest tests/ -v
```

Expected: all green.

### Step 6: Final commit

```bash
git add -A
git commit -m "feat: complete mission lifecycle MVP — DB, API, sidebar, phone sync"
```

---

## What this does NOT do yet (future tasks)

- Import photos from drone → `data/00_raw/<flight>/` (requires DJI transfer)
- Mark a mission as `flown` (needs a "Log Flight" button + `POST /api/missions/{id}/flight`)
- Trigger ODM processing from the UI
- Show flight records and processing status in the sidebar

These are MVP 1 items in ROADMAP.md and can follow as a separate plan.
