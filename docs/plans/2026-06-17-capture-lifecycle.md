# Capture/Flight Lifecycle Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Associate drone photo captures with missions, track them through the ODM/terrain pipeline, and clear them (mark done + optionally delete from Skyrover on phone).

**Architecture:** Four new `web/db.py` functions wire up the existing `flights` table. Five new `web/app.py` endpoints expose them plus a `GET /api/raw-dirs` directory scanner. The frontend expands each mission item in the sidebar to reveal its flights with a three-step pipeline status bar, a "Log Flight" dropdown, and a "Clear" button that optionally removes the KMZ from Skyrover via USB.

**Tech Stack:** Python 3.11, SQLite (`web/db.py` `_db()` context manager), FastAPI, pymobiledevice3 (AFC for phone delete), Leaflet SPA (vanilla JS, no build step)

---

## Schema reference

```sql
-- already exists — no migration needed
CREATE TABLE flights (
    id             TEXT PRIMARY KEY,
    mission_id     TEXT NOT NULL REFERENCES missions(id),
    flight_date    REAL,
    photo_count    INTEGER,
    raw_dir        TEXT,
    odm_status     TEXT DEFAULT 'pending',   -- pending | running | done | failed
    terrain_status TEXT DEFAULT 'pending'
);
```

`_derive_status` already promotes a mission to `flown / processing / processed` once a `flights` row exists — no changes needed there.

---

## Task 1: DB functions — log_flight, list_flights, update_flight_status, mark_flight_done

**Files:**
- Modify: `web/db.py` (append after `sync_from_phone`)
- Test: `tests/test_db.py` (append new tests)

### Step 1: Write failing tests

Append to `tests/test_db.py`:

```python
# ── flights ───────────────────────────────────────────────────────────────────

def test_log_flight_returns_id():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    fid = db.log_flight(mid, "00_raw/test_flight", 120)
    assert len(fid) == 36


def test_log_flight_sets_mission_flown():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.log_flight(mid, "00_raw/test_flight", 120)
    rows = db.list_missions()
    assert rows[0]["status"] == "flown"


def test_list_flights_returns_rows():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    db.log_flight(mid, "00_raw/f1", 50)
    db.log_flight(mid, "00_raw/f2", 80)
    flights = db.list_flights(mid)
    assert len(flights) == 2
    assert {f["raw_dir"] for f in flights} == {"00_raw/f1", "00_raw/f2"}


def test_list_flights_keys():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    fid = db.log_flight(mid, "00_raw/f1", 42)
    flights = db.list_flights(mid)
    f = flights[0]
    assert f["id"] == fid
    assert f["photo_count"] == 42
    assert f["odm_status"] == "pending"
    assert f["terrain_status"] == "pending"


def test_update_flight_odm_status():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    fid = db.log_flight(mid, "00_raw/f1", 42)
    db.update_flight_status(fid, odm_status="running")
    flights = db.list_flights(mid)
    assert flights[0]["odm_status"] == "running"
    assert db.list_missions()[0]["status"] == "processing"


def test_update_flight_terrain_status():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    fid = db.log_flight(mid, "00_raw/f1", 42)
    db.update_flight_status(fid, odm_status="done", terrain_status="done")
    assert db.list_missions()[0]["status"] == "processed"


def test_mark_flight_done():
    mid = db.save_mission("BJR", POLY, 80, 80, None, 8, "survey", _one_pass())
    fid = db.log_flight(mid, "00_raw/f1", 42)
    db.mark_flight_done(fid)
    flights = db.list_flights(mid)
    assert flights[0]["odm_status"] == "done"
    assert flights[0]["terrain_status"] == "done"
    assert db.list_missions()[0]["status"] == "processed"
```

### Step 2: Run — expect FAIL

```bash
cd /home/irjudson/Projects/terraprint && uv run pytest tests/test_db.py -k "flight" -v
```

Expected: `AttributeError: module 'web.db' has no attribute 'log_flight'`

### Step 3: Implement in `web/db.py`

Append after `sync_from_phone`:

```python
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
```

### Step 4: Run tests — expect all green

```bash
uv run pytest tests/test_db.py -v
```

Expected: 22 passed (14 existing + 8 new).

### Step 5: Commit

```bash
git add web/db.py tests/test_db.py
git commit -m "feat: add flight lifecycle DB functions (log, list, update_status, mark_done)"
```

---

## Task 2: API endpoints — flights CRUD + raw-dirs scanner

**Files:**
- Modify: `web/app.py`

### Step 1: Add `DATA_ROOT` constant and import new DB functions

Near the top of `web/app.py`, after the existing `from web.db import (...)` block, add to that import:

```python
from web.db import (
    init_db,
    save_mission as _db_save_mission,
    record_push as _db_record_push,
    list_missions as _db_list_missions,
    get_mission_waypoints as _db_get_mission_waypoints,
    sync_from_phone as _db_sync_from_phone,
    log_flight as _db_log_flight,
    list_flights as _db_list_flights,
    update_flight_status as _db_update_flight_status,
    mark_flight_done as _db_mark_flight_done,
)
```

Also add this constant after the imports (before `app = FastAPI(...)`):

```python
DATA_ROOT = Path(__file__).parent.parent / "data"
```

### Step 2: Add Pydantic models

After the existing `SaveMissionRequest` model, add:

```python
class LogFlightRequest(BaseModel):
    raw_dir: str
    photo_count: int
    flight_date: Optional[float] = None


class FlightStatusRequest(BaseModel):
    odm_status: Optional[str] = None
    terrain_status: Optional[str] = None
```

### Step 3: Add five new endpoints

Add these after `POST /api/missions/{mission_id}/sync-phone`:

```python
@app.get("/api/raw-dirs")
async def list_raw_dirs():
    raw_root = DATA_ROOT / "00_raw"
    if not raw_root.exists():
        return []
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng"}
    result = []
    for d in sorted(raw_root.iterdir()):
        if d.is_dir():
            count = sum(1 for f in d.iterdir() if f.suffix.lower() in IMAGE_EXTS)
            result.append({"dir": d.name, "photo_count": count})
    return result


@app.get("/api/missions/{mission_id}/flights")
async def get_flights(mission_id: str):
    return _db_list_flights(mission_id)


@app.post("/api/missions/{mission_id}/flights")
async def log_flight(mission_id: str, req: LogFlightRequest):
    flight_id = _db_log_flight(mission_id, req.raw_dir, req.photo_count, req.flight_date)
    return {"flight_id": flight_id}


@app.patch("/api/flights/{flight_id}")
async def update_flight(flight_id: str, req: FlightStatusRequest):
    _db_update_flight_status(flight_id, req.odm_status, req.terrain_status)
    return {"ok": True}


@app.post("/api/flights/{flight_id}/clear")
async def clear_flight(flight_id: str, delete_from_phone: bool = False):
    _db_mark_flight_done(flight_id)

    if not delete_from_phone:
        return {"ok": True, "phone_deleted": 0}

    # Find the mission and its active phone passes
    with __import__("web.db", fromlist=["_db"]).db._db() as con:
        row = con.execute("SELECT mission_id FROM flights WHERE id=?", (flight_id,)).fetchone()
        if not row:
            return {"ok": True, "phone_deleted": 0}
        mid = row["mission_id"]
        passes = con.execute(
            """SELECT id, phone_mission_id FROM mission_passes
               WHERE mission_id=? AND phone_mission_id IS NOT NULL
               AND deleted_from_phone_at IS NULL""",
            (mid,),
        ).fetchall()

    if not passes:
        return {"ok": True, "phone_deleted": 0}

    try:
        from skyrover_ios_bridge import MISSION_DB, MISSION_ROOT, _afc_session, _get_lockdown, _read, _write
        import tempfile, sqlite3 as _sq

        try:
            lockdown = await asyncio.wait_for(_get_lockdown(), timeout=8.0)
        except asyncio.TimeoutError:
            raise HTTPException(503, "iPhone not connected.")

        now = time.time()
        phone_deleted = 0

        async with _afc_session(lockdown) as afc:
            db_bytes = await _read(afc, MISSION_DB)
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
                f.write(db_bytes)
                tmp = f.name

            try:
                pcon = _sq.connect(tmp)
                for p in passes:
                    pmid = p["phone_mission_id"]
                    # Mark deleted in phone DB
                    pcon.execute(
                        "UPDATE kmzTable SET deleteTime=? WHERE missionId=?", (now, pmid)
                    )
                    # Remove KMZ file from AFC
                    kmz_path = f"{MISSION_ROOT}/{pmid}/{pmid}.kmz"
                    try:
                        await afc.rm(kmz_path, force=True)
                    except Exception:
                        pass
                    phone_deleted += 1
                pcon.commit()
                pcon.close()
                with open(tmp, "rb") as f:
                    await _write(afc, MISSION_DB, f.read())
            finally:
                Path(tmp).unlink(missing_ok=True)

        # Update local DB
        now_local = time.time()
        with __import__("web.db", fromlist=["_db"]).db._db() as con:
            for p in passes:
                con.execute(
                    "UPDATE mission_passes SET deleted_from_phone_at=? WHERE id=?",
                    (now_local, p["id"]),
                )

        return {"ok": True, "phone_deleted": phone_deleted}

    except HTTPException:
        raise
    except SystemExit:
        raise HTTPException(503, "iPhone not connected — check USB cable and unlock the phone.")
    except Exception as exc:
        raise HTTPException(500, str(exc))
```

**Note on the `__import__` pattern above:** That's a workaround placeholder — the actual import of `_db` from `web.db` should use the already-imported `_db` context manager. Fix it to use:

```python
from web.db import _db as _open_db
```

Add `_db as _open_db` to the existing `from web.db import (...)` block, then replace `__import__(...)._db()` with `_open_db()` in the clear endpoint.

### Step 4: Verify routes exist

```bash
cd /home/irjudson/Projects/terraprint && uv run python -c "
from web.app import app
for r in app.routes:
    if hasattr(r, 'path') and 'flight' in r.path.lower():
        print(r.methods, r.path)
"
```

Expected output includes:
```
{'GET'} /api/raw-dirs
{'GET'} /api/missions/{mission_id}/flights
{'POST'} /api/missions/{mission_id}/flights
{'PATCH'} /api/flights/{flight_id}
{'POST'} /api/flights/{flight_id}/clear
```

### Step 5: Smoke-test with curl

```bash
# Should return [] or list of raw dirs
curl -s http://localhost:8001/api/raw-dirs | python3 -m json.tool

# Flights for a non-existent mission returns []
curl -s http://localhost:8001/api/missions/no-such-id/flights
```

### Step 6: Commit

```bash
git add web/app.py
git commit -m "feat: add flights API endpoints and raw-dirs scanner"
```

---

## Task 3: Frontend — flights sub-panel in missions sidebar

**Files:**
- Modify: `web/static/index.html`

This is all JS/CSS additions. Read the full file before editing. The missions panel JS is in the final `<script>` block around line 710+.

### Step 1: Add CSS for flights sub-panel

Inside `<style>`, append before `</style>`:

```css
/* ── flights sub-panel ── */
.mission-flights {
  display: none; border-top: 1px solid rgba(255,255,255,.06);
  background: rgba(0,0,0,.15); padding: 8px 10px;
}
.mission-flights.open { display: block; }
.flight-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,.04);
  font-size: 11px;
}
.flight-row:last-child { border-bottom: none; }
.flight-info { flex: 1; min-width: 0; }
.flight-dir  { font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.flight-meta { color: var(--muted); }
.pipeline-bar {
  display: flex; gap: 3px; align-items: center; margin-top: 3px;
}
.pipeline-step {
  padding: 1px 5px; border-radius: 3px; font-size: 9px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .04em;
}
.step-pending    { background: #333; color: #666; }
.step-running    { background: #4a3a1a; color: #f5c57a; }
.step-done       { background: #1a3a1a; color: #4caf50; }
.step-failed     { background: #3a1a1a; color: #f55; }
.flight-actions  { display: flex; gap: 4px; flex-shrink: 0; }
.flight-btn {
  padding: 2px 7px; border: none; border-radius: 3px;
  font-size: 10px; font-weight: 600; cursor: pointer;
  background: var(--card); color: var(--muted);
}
.flight-btn:hover { background: var(--accent); color: #fff; }
.flight-btn.danger:hover { background: #c0392b; }
.log-flight-row {
  padding: 6px 0 2px; display: flex; gap: 6px; align-items: center;
}
.log-flight-row select {
  flex: 1; background: var(--card); border: none; border-radius: 4px;
  color: var(--text); padding: 4px 6px; font-size: 11px; outline: none;
}
.log-flight-row select:disabled { opacity: .4; }
#btn-log-flight-confirm {
  padding: 4px 10px; border: none; border-radius: 4px;
  background: var(--accent); color: #fff; font-size: 11px;
  font-weight: 600; cursor: pointer;
}
#btn-log-flight-confirm:disabled { opacity: .4; cursor: not-allowed; }
```

### Step 2: Replace `visualizeMission` and `loadMissions` with expanded versions

The current `loadMissions` renders static `.mission-item` divs. Replace the entire Missions panel JS block (from `// ── Missions panel` to the end of the sync button listener) with the version below.

**Find** the comment `// ── Missions panel ──` and replace everything from there to (and including) the closing `});` of the sync button listener with:

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

function pipelineStep(status, label) {
  return `<span class="pipeline-step step-${status}">${label} ${status === 'done' ? '✓' : status === 'running' ? '…' : status === 'failed' ? '✗' : '·'}</span>`;
}

function renderFlight(f, missionId, hasPhonePass) {
  const div = document.createElement('div');
  div.className = 'flight-row';
  div.dataset.id = f.id;
  div.innerHTML = `
    <div class="flight-info">
      <div class="flight-dir">${f.raw_dir}</div>
      <div class="flight-meta">${f.photo_count} photos</div>
      <div class="pipeline-bar">
        ${pipelineStep('done', 'RAW')}
        ${pipelineStep(f.odm_status, 'ODM')}
        ${pipelineStep(f.terrain_status, 'STL')}
      </div>
    </div>
    <div class="flight-actions">
      <button class="flight-btn danger btn-clear-flight"
        data-id="${f.id}" data-has-phone="${hasPhonePass}"
        title="Mark done">Clear</button>
    </div>
  `;
  div.querySelector('.btn-clear-flight').addEventListener('click', e => {
    e.stopPropagation();
    clearFlight(f.id, missionId, hasPhonePass);
  });
  return div;
}

async function loadFlights(missionId, container, hasPhonePass) {
  container.innerHTML = '<div style="padding:4px 0;font-size:11px;color:var(--muted);">Loading…</div>';
  try {
    const res = await fetch(`/api/missions/${missionId}/flights`);
    const flights = await res.json();
    container.innerHTML = '';
    flights.forEach(f => container.appendChild(renderFlight(f, missionId, hasPhonePass)));

    // Log flight row
    const logRow = document.createElement('div');
    logRow.className = 'log-flight-row';
    logRow.innerHTML = `
      <select id="raw-dir-select-${missionId}"><option value="">Loading…</option></select>
      <button id="btn-log-flight-confirm" data-mission="${missionId}" disabled>+ Log</button>
    `;
    container.appendChild(logRow);

    const sel = logRow.querySelector('select');
    const btn = logRow.querySelector('button');

    fetch('/api/raw-dirs').then(r => r.json()).then(dirs => {
      sel.innerHTML = '<option value="">— pick raw dir —</option>';
      dirs.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.dir;
        opt.textContent = `${d.dir} (${d.photo_count} photos)`;
        opt.dataset.count = d.photo_count;
        sel.appendChild(opt);
      });
      if (!dirs.length) sel.innerHTML = '<option value="">No raw dirs found</option>';
    }).catch(() => { sel.innerHTML = '<option value="">Failed to load</option>'; });

    sel.addEventListener('change', () => { btn.disabled = !sel.value; });
    btn.addEventListener('click', async () => {
      const dir = sel.value;
      if (!dir) return;
      const count = parseInt(sel.selectedOptions[0]?.dataset.count || '0');
      btn.disabled = true;
      btn.textContent = '…';
      try {
        await fetch(`/api/missions/${missionId}/flights`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ raw_dir: dir, photo_count: count }),
        });
        await loadFlights(missionId, container, hasPhonePass);
        loadMissions();
        toast('Flight logged', 'ok');
      } catch { toast('Failed to log flight', 'err'); }
      finally { btn.disabled = false; btn.textContent = '+ Log'; }
    });
  } catch {
    container.innerHTML = '<div style="padding:4px 0;font-size:11px;color:var(--muted);">Failed to load flights.</div>';
  }
}

async function clearFlight(flightId, missionId, hasPhonePass) {
  let deleteFromPhone = false;
  if (hasPhonePass) {
    deleteFromPhone = confirm('Also remove mission from Skyrover on phone?');
  } else {
    if (!confirm('Mark this flight as done?')) return;
  }
  try {
    const url = `/api/flights/${flightId}/clear${deleteFromPhone ? '?delete_from_phone=true' : ''}`;
    const res = await fetch(url, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || 'Clear failed', 'err'); return; }
    const msg = deleteFromPhone ? `Done · ${data.phone_deleted} removed from phone` : 'Marked done';
    toast(msg, 'ok');
    // Refresh flights panel and mission list
    const container = document.querySelector(`.mission-flights[data-mission="${missionId}"]`);
    if (container) await loadFlights(missionId, container, hasPhonePass);
    loadMissions();
  } catch { toast('Clear failed', 'err'); }
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
      const wrapper = document.createElement('div');

      const item = document.createElement('div');
      item.className = 'mission-item' + (m.id === selectedMissionId ? ' selected' : '');
      item.dataset.id = m.id;
      const d = new Date((m.created_at || 0) * 1000);
      item.innerHTML = `
        <div class="mission-item-name">
          ${m.name}
          <span class="mission-status status-${m.status}">${m.status.replace('_', ' ')}</span>
        </div>
        <div class="mission-item-meta">${m.mode} · ${m.altitude || '?'}m · ${d.toLocaleDateString()}</div>
      `;

      const flightsPanel = document.createElement('div');
      flightsPanel.className = 'mission-flights';
      flightsPanel.dataset.mission = m.id;

      const hasPhonePass = m.passes_on_phone > 0;

      item.addEventListener('click', () => {
        const isOpen = flightsPanel.classList.toggle('open');
        if (isOpen) loadFlights(m.id, flightsPanel, hasPhonePass);
        visualizeMission(m.id, item);
      });

      wrapper.appendChild(item);
      wrapper.appendChild(flightsPanel);
      list.appendChild(wrapper);
    });
  } catch {
    list.innerHTML = '<div style="padding:14px;font-size:12px;color:var(--muted);">Failed to load.</div>';
  }
}

async function visualizeMission(missionId, el) {
  document.querySelectorAll('.mission-item.selected').forEach(d => d.classList.remove('selected'));
  clearMissionLayers();
  if (selectedMissionId === missionId) { selectedMissionId = null; return; }
  selectedMissionId = missionId;
  el.classList.add('selected');
  try {
    const res = await fetch(`/api/missions/${missionId}/waypoints`);
    const passes = await res.json();
    passes.forEach(p => {
      if (!p.waypoints.length) return;
      const color = p.on_phone ? STATUS_COLORS.on_phone : STATUS_COLORS.planned;
      const lls = p.waypoints.map(wp => [wp[0], wp[1]]);
      missionLayers.push(L.polyline(lls, { color, weight: 1.5, opacity: .8 }).addTo(map));
      missionLayers.push(L.circleMarker(lls[0], { radius: 5, color, fillColor: color, fillOpacity: 1 }).addTo(map));
    });
    if (missionLayers.length) map.fitBounds(L.featureGroup(missionLayers).getBounds(), { padding: [40, 40] });
  } catch { toast('Failed to load waypoints', 'err'); }
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
    const res = await fetch('/api/missions/sync/sync-phone', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || 'Sync failed', 'err'); return; }
    toast(`Sync done · ${data.matched} matched · ${data.newly_deleted} removed · ${data.imported ?? 0} imported`, 'ok');
    loadMissions();
  } catch {
    toast('Sync failed — phone plugged in?', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '📱 Sync';
  }
});
```

### Step 3: Verify with grep

```bash
grep -c "mission-flights" web/static/index.html
grep -c "clearFlight" web/static/index.html
grep -c "loadFlights" web/static/index.html
```

All should return ≥ 1.

### Step 4: Commit

```bash
git add web/static/index.html
git commit -m "feat: add flights sub-panel with pipeline status, Log Flight, and Clear"
```

---

## Task 4: Rebuild Docker + end-to-end browser verification

**Files:** None changed — build and test only.

### Step 1: Rebuild container

```bash
cd /home/irjudson/Projects/terraprint && docker compose build web && docker compose up -d web
```

### Step 2: Verify API with curl

```bash
# raw-dirs endpoint
curl -s http://localhost:8001/api/raw-dirs | python3 -m json.tool

# full test: insert a mission directly, log a flight, check status
sqlite3 data/missions.db "
INSERT INTO missions (id, name, polygon, altitude, overlap, speed, mode, status, created_at, updated_at)
VALUES ('smoke-1', 'Smoke Test', '[]', 80, 80, 8, 'survey', 'planned', strftime('%s','now'), strftime('%s','now'));
"
curl -s -X POST http://localhost:8001/api/missions/smoke-1/flights \
  -H 'Content-Type: application/json' \
  -d '{"raw_dir":"smoke_raw","photo_count":99}' | python3 -m json.tool

curl -s http://localhost:8001/api/missions/smoke-1/flights | python3 -m json.tool

# Cleanup
sqlite3 data/missions.db "DELETE FROM flights WHERE mission_id='smoke-1'; DELETE FROM missions WHERE id='smoke-1';"
```

Expected: flight logged, photo_count=99, odm_status='pending'.

### Step 3: Run all tests

```bash
uv run pytest tests/ -v
```

Expected: 32 passed (14 existing DB + 8 new flight DB + 10 existing survey).

Wait — existing test count is 14 db + 18 survey = 32. New tests add 8, giving 40 total. Adjust expected accordingly.

### Step 4: Browser smoke test with Playwright

```bash
python3 - <<'EOF'
from playwright.sync_api import sync_playwright
import os, sqlite3, time

# seed a mission
con = sqlite3.connect('data/missions.db')
con.execute("INSERT OR IGNORE INTO missions (id,name,polygon,altitude,overlap,speed,mode,status,created_at,updated_at) VALUES ('ui-test','UI Test Mission','[]',80,80,8,'survey','planned',?,?)", (time.time(), time.time()))
con.commit(); con.close()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width":1280,"height":800})
    page.goto("http://localhost:8001")
    page.wait_for_load_state("networkidle")

    page.locator("#btn-missions").click()
    page.wait_for_timeout(600)

    items = page.locator(".mission-item")
    print(f"Mission items: {items.count()}")
    assert items.count() >= 1

    # Click to expand flights panel
    items.first.click()
    page.wait_for_timeout(800)
    flights_panel = page.locator(".mission-flights.open")
    print(f"Flights panel open: {flights_panel.count() >= 1}")

    page.screenshot(path="/tmp/flights-panel.png")
    print("Screenshot: /tmp/flights-panel.png")
    browser.close()

# cleanup
con = sqlite3.connect('data/missions.db')
con.execute("DELETE FROM missions WHERE id='ui-test'")
con.commit(); con.close()
print("Done")
EOF
```

Expected: "Mission items: N", "Flights panel open: True", screenshot shows flights sub-panel.

### Step 5: Final commit if any fixups needed

```bash
git add -A && git commit -m "fix: post-verification fixups for capture lifecycle"
```

---

## What this does NOT do yet

- Trigger ODM/terrain pipeline from the UI (would need `make odm FLIGHT=<name>` integration — next task)
- Import photos from phone/SD automatically
- Show individual photo thumbnails
