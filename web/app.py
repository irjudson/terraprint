"""Terraprint Mission Planner — FastAPI backend."""

import asyncio
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from shapely.geometry import Point, Polygon as SPolygon

# ── import survey helpers from scripts/ ──────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from generate_survey import (
    DEFAULT_ALT, DEFAULT_HFOV, DEFAULT_OVERLAP, DEFAULT_SPEED, DEFAULT_VFOV,
    PHOTOGRAMMETRY_PASSES,
    camera_footprint, lawnmower_grid, make_kmz, make_photogrammetry_missions,
)

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
    delete_mission as _db_delete_mission,
    _db as _open_db,
)

DATA_ROOT = Path(__file__).parent.parent / "data"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng"}

app = FastAPI(title="Terraprint Mission Planner")


@app.on_event("startup")
async def _startup():
    init_db()


# In-process job store (single-user local app; no persistence needed)
_jobs: dict[str, dict] = {}


# ── models ────────────────────────────────────────────────────────────────────

class SurveyRequest(BaseModel):
    polygon: list[list[float]]          # [[lat, lon], ...]
    name: str = "survey"
    altitude: float = DEFAULT_ALT
    overlap: float = DEFAULT_OVERLAP    # percent, both axes
    front_overlap: Optional[float] = None
    side_overlap: Optional[float] = None
    speed: float = DEFAULT_SPEED
    gimbal_pitch: float = -90.0         # degrees: -90=nadir, -45=oblique
    mission_mode: str = "survey"        # "survey" or "photogrammetry"


# ── helpers ───────────────────────────────────────────────────────────────────

def _clip_grid_to_polygon(
    polygon_latlons: list[tuple[float, float]],
    altitude: float,
    front_ov: float,
    side_ov: float,
) -> list[tuple[float, float]]:
    lats = [p[0] for p in polygon_latlons]
    lons = [p[1] for p in polygon_latlons]
    south, north = min(lats), max(lats)
    west,  east  = min(lons), max(lons)

    grid = lawnmower_grid(
        south, west, north, east, altitude,
        front_ov, side_ov, DEFAULT_HFOV, DEFAULT_VFOV,
    )

    spoly = SPolygon([(lon, lat) for lat, lon in polygon_latlons])

    # Buffer by half a track spacing so edge photos are included
    fp_w, _ = camera_footprint(altitude, DEFAULT_HFOV, DEFAULT_VFOV)
    track_m  = fp_w * (1 - side_ov / 100)
    center_lat = (south + north) / 2
    deg_buf  = (track_m * 0.5) / (111_320 * math.cos(math.radians(center_lat)))
    buffered = spoly.buffer(deg_buf)

    return [(lat, lon) for lat, lon in grid if buffered.contains(Point(lon, lat))]


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(df/2)**2 + math.cos(f1)*math.cos(f2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return {"mapbox_token": os.getenv("MAPBOX_TOKEN", "")}


@app.get("/api/geocode")
async def geocode(q: str):
    import urllib.parse, urllib.request
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": q, "format": "json", "limit": 5,
                                     "polygon_geojson": 1}))
    req = urllib.request.Request(url, headers={"User-Agent": "terraprint/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as exc:
        raise HTTPException(502, f"Geocode failed: {exc}")


@app.post("/api/generate")
async def generate(req: SurveyRequest):
    poly = [(p[0], p[1]) for p in req.polygon]
    if len(poly) < 3:
        raise HTTPException(400, "Polygon must have at least 3 points.")

    front_ov = req.front_overlap if req.front_overlap is not None else req.overlap
    side_ov  = req.side_overlap  if req.side_overlap  is not None else req.overlap

    waypoints = _clip_grid_to_polygon(poly, req.altitude, front_ov, side_ov)

    if not waypoints:
        raise HTTPException(400, "No waypoints generated — area too small or overlap too high.")
    if len(waypoints) > 65_535:
        raise HTTPException(400, f"{len(waypoints):,} waypoints exceeds the 65,535 limit. "
                                  "Reduce area, lower overlap, or increase altitude.")

    # Shared stats
    fp_w, fp_h = camera_footprint(req.altitude, DEFAULT_HFOV, DEFAULT_VFOV)
    along_m = fp_h * (1 - front_ov / 100)
    est_sec_per_pass = int(len(waypoints) * along_m / req.speed)
    dist_m = sum(
        _haversine_m(waypoints[i][0], waypoints[i][1],
                     waypoints[i+1][0], waypoints[i+1][1])
        for i in range(len(waypoints) - 1)
    )
    IMAGE_W_PX = 4032
    gsd_cm = round((fp_w / IMAGE_W_PX) * 100, 1)
    job_id = str(uuid.uuid4())

    if req.mission_mode == "photogrammetry":
        tmp_dir = Path(tempfile.mkdtemp(prefix="tp_photo_"))
        passes = make_photogrammetry_missions(
            waypoints, req.altitude, req.speed, tmp_dir, "mission",
            oblique_pitch=req.gimbal_pitch if req.gimbal_pitch != -90.0 else -45.0,
        )
        _jobs[job_id] = {
            "mode": "photogrammetry",
            "name": req.name,
            "passes": [dict(p, waypoints=waypoints) for p in passes],
            "polygon": req.polygon,
            "params": {
                "altitude": req.altitude,
                "overlap": req.overlap,
                "front_overlap": req.front_overlap,
                "speed": req.speed,
            },
        }
        n_passes = len(passes)
        return {
            "job_id":         job_id,
            "mode":           "photogrammetry",
            "passes":         [{"pass": p["pass"], "waypoints": waypoints,
                                "gimbal": p["gimbal"], "color": p["color"]} for p in passes],
            "waypoint_count": len(waypoints) * n_passes,
            "distance_m":     round(dist_m * n_passes),
            "est_sec":        est_sec_per_pass * n_passes,
            "gsd_cm":         gsd_cm,
        }

    # Single-pass survey mode
    tmp = tempfile.NamedTemporaryFile(suffix=".kmz", delete=False, prefix="tp_")
    tmp_path = Path(tmp.name)
    tmp.close()
    make_kmz(waypoints, req.altitude, req.speed, tmp_path, req.gimbal_pitch)
    _jobs[job_id] = {
        "mode": "survey",
        "kmz_path": str(tmp_path),
        "name": req.name,
        "waypoints": waypoints,
        "polygon": req.polygon,
        "params": {
            "altitude": req.altitude,
            "overlap": req.overlap,
            "front_overlap": req.front_overlap,
            "speed": req.speed,
        },
    }
    return {
        "job_id":         job_id,
        "mode":           "survey",
        "passes":         [{"pass": "nadir", "waypoints": waypoints,
                            "gimbal": req.gimbal_pitch, "color": "#4caf50"}],
        "waypoint_count": len(waypoints),
        "distance_m":     round(dist_m),
        "est_sec":        est_sec_per_pass,
        "gsd_cm":         gsd_cm,
        "gimbal_pitch":   req.gimbal_pitch,
    }


class SaveMissionRequest(BaseModel):
    job_id: str
    name: str


class LogFlightRequest(BaseModel):
    raw_dir: str
    photo_count: int
    flight_date: Optional[float] = None


class FlightStatusRequest(BaseModel):
    odm_status: Optional[str] = None
    terrain_status: Optional[str] = None


@app.post("/api/missions")
async def save_mission_endpoint(req: SaveMissionRequest):
    job = _jobs.get(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found — re-generate first.")

    if job["mode"] == "photogrammetry":
        passes = [
            {
                "pass_name": p["pass"],
                "kmz_path":  p.get("kmz_path"),
                "waypoints": p["waypoints"],
            }
            for p in job["passes"]
        ]
    else:
        passes = [
            {
                "pass_name": "survey",
                "kmz_path":  job.get("kmz_path"),
                "waypoints": job["waypoints"],
            }
        ]

    params = job.get("params", {})
    mission_id = _db_save_mission(
        name=req.name,
        polygon=job.get("polygon", []),
        altitude=params.get("altitude", 80),
        overlap=params.get("overlap", 80),
        front_overlap=params.get("front_overlap"),
        speed=params.get("speed", 8),
        mode=job["mode"],
        passes=passes,
    )
    _jobs[req.job_id]["mission_id"] = mission_id
    return {"mission_id": mission_id}


@app.get("/api/missions")
async def list_missions_endpoint():
    return _db_list_missions()


@app.get("/api/missions/{mission_id}/waypoints")
async def mission_waypoints(mission_id: str):
    passes = _db_get_mission_waypoints(mission_id)
    if not passes:
        raise HTTPException(404, "Mission not found.")
    return passes


@app.post("/api/missions/{mission_id}/sync-phone")
async def sync_phone(mission_id: str):  # mission_id unused — global sync
    try:
        from skyrover_ios_bridge import MISSION_DB, _afc_session, _get_lockdown, _read

        try:
            lockdown = await asyncio.wait_for(_get_lockdown(), timeout=8.0)
        except asyncio.TimeoutError:
            raise HTTPException(503, "No iPhone found.")

        async with _afc_session(lockdown) as afc:
            db_bytes = await _read(afc, MISSION_DB)

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(db_bytes)
            tmp = f.name

        try:
            con = sqlite3.connect(tmp)
            rows = []
            for r in con.execute(
                "SELECT missionId, name, deleteTime, allPointLocations FROM kmzTable"
            ).fetchall():
                raw_wps = r[3]
                try:
                    pts = json.loads(raw_wps) if raw_wps else []
                    waypoints = [[p["latitude"], p["longitude"]] for p in pts]
                except Exception:
                    waypoints = []
                rows.append({
                    "missionId":  r[0],
                    "name":       r[1],
                    "deleteTime": r[2],
                    "waypoints":  waypoints,
                })
            con.close()
        finally:
            Path(tmp).unlink(missing_ok=True)

        counts = _db_sync_from_phone(rows)
        return {"ok": True, **counts, "phone_total": len(rows)}

    except HTTPException:
        raise
    except SystemExit:
        raise HTTPException(503, "iPhone not connected — check USB cable and unlock the phone.")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/raw-dirs")
async def list_raw_dirs():
    raw_root = DATA_ROOT / "00_raw"
    if not raw_root.exists():
        return []
    result = []
    for d in sorted(raw_root.iterdir()):
        if d.is_dir():
            count = sum(1 for f in d.iterdir() if f.suffix.lower() in IMAGE_EXTS)
            result.append({"dir": d.name, "photo_count": count})
    return result


@app.get("/api/missions/{mission_id}/flights")
async def get_flights(mission_id: str):
    with _open_db() as con:
        exists = con.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Mission not found")
    return _db_list_flights(mission_id)


@app.post("/api/missions/{mission_id}/flights")
async def log_flight(mission_id: str, req: LogFlightRequest):
    with _open_db() as con:
        exists = con.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Mission not found")
    flight_id = _db_log_flight(mission_id, req.raw_dir, req.photo_count, req.flight_date)
    return {"flight_id": flight_id}


@app.patch("/api/flights/{flight_id}")
async def update_flight(flight_id: str, req: FlightStatusRequest):
    with _open_db() as con:
        exists = con.execute("SELECT 1 FROM flights WHERE id=?", (flight_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Flight not found")
    _db_update_flight_status(flight_id, req.odm_status, req.terrain_status)
    return {"ok": True}


@app.post("/api/flights/{flight_id}/clear")
async def clear_flight(flight_id: str, delete_from_phone: bool = False):
    if not delete_from_phone:
        _db_mark_flight_done(flight_id)
        return {"ok": True, "phone_deleted": 0}

    # Find active phone passes BEFORE marking done
    with _open_db() as con:
        row = con.execute("SELECT mission_id FROM flights WHERE id=?", (flight_id,)).fetchone()
        if not row:
            _db_mark_flight_done(flight_id)
            return {"ok": True, "phone_deleted": 0}
        mid = row["mission_id"]
        passes = con.execute(
            """SELECT id, phone_mission_id FROM mission_passes
               WHERE mission_id=? AND phone_mission_id IS NOT NULL
               AND deleted_from_phone_at IS NULL""",
            (mid,),
        ).fetchall()

    if not passes:
        _db_mark_flight_done(flight_id)
        return {"ok": True, "phone_deleted": 0}

    try:
        from skyrover_ios_bridge import MISSION_DB, MISSION_ROOT, _afc_session, _get_lockdown, _read, _write

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
                import sqlite3 as _sq
                pcon = _sq.connect(tmp)
                try:
                    for p in passes:
                        pmid = p["phone_mission_id"]
                        pcon.execute(
                            "UPDATE kmzTable SET deleteTime=? WHERE missionId=?", (now, pmid)
                        )
                        kmz_path = f"{MISSION_ROOT}/{pmid}/{pmid}.kmz"
                        try:
                            await afc.rm(kmz_path, force=True)
                        except Exception:
                            pass
                        phone_deleted += 1
                    pcon.commit()
                finally:
                    pcon.close()
                with open(tmp, "rb") as f:
                    await _write(afc, MISSION_DB, f.read())
            finally:
                Path(tmp).unlink(missing_ok=True)

        # Phone operations succeeded — NOW mark done locally
        _db_mark_flight_done(flight_id)
        now_local = time.time()
        with _open_db() as con:
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


@app.delete("/api/missions/{mission_id}")
async def delete_mission(mission_id: str, delete_from_phone: bool = False):
    if not delete_from_phone:
        existed = _db_delete_mission(mission_id)
        if not existed:
            raise HTTPException(404, "Mission not found")
        return {"ok": True, "phone_deleted": 0}

    # Gather phone passes before deleting
    with _open_db() as con:
        exists = con.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Mission not found")
        passes = con.execute(
            """SELECT id, phone_mission_id FROM mission_passes
               WHERE mission_id=? AND phone_mission_id IS NOT NULL
               AND deleted_from_phone_at IS NULL""",
            (mission_id,),
        ).fetchall()

    phone_deleted = 0
    if passes:
        try:
            from skyrover_ios_bridge import MISSION_DB, MISSION_ROOT, _afc_session, _get_lockdown, _read, _write

            try:
                lockdown = await asyncio.wait_for(_get_lockdown(), timeout=8.0)
            except asyncio.TimeoutError:
                raise HTTPException(503, "iPhone not connected.")

            now = time.time()
            async with _afc_session(lockdown) as afc:
                db_bytes = await _read(afc, MISSION_DB)
                with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
                    f.write(db_bytes)
                    tmp = f.name
                try:
                    import sqlite3 as _sq
                    pcon = _sq.connect(tmp)
                    try:
                        for p in passes:
                            pmid = p["phone_mission_id"]
                            pcon.execute(
                                "UPDATE kmzTable SET deleteTime=? WHERE missionId=?", (now, pmid)
                            )
                            kmz_path = f"{MISSION_ROOT}/{pmid}/{pmid}.kmz"
                            try:
                                await afc.rm(kmz_path, force=True)
                            except Exception:
                                pass
                            phone_deleted += 1
                        pcon.commit()
                    finally:
                        pcon.close()
                    with open(tmp, "rb") as f:
                        await _write(afc, MISSION_DB, f.read())
                finally:
                    Path(tmp).unlink(missing_ok=True)
        except HTTPException:
            raise
        except SystemExit:
            raise HTTPException(503, "iPhone not connected — check USB cable and unlock the phone.")
        except Exception as exc:
            raise HTTPException(500, str(exc))

    _db_delete_mission(mission_id)
    return {"ok": True, "phone_deleted": phone_deleted}


def _db_insert_mission(con: sqlite3.Connection, container_uuid: str,
                       mission_id: str, name: str, waypoints: list, kmz_bytes: bytes,
                       mission_root: str) -> str:
    """Insert one mission row and return the absolute KMZ path (for use by the caller)."""
    lats  = [p[0] for p in waypoints]
    lons  = [p[1] for p in waypoints]
    dist  = sum(
        _haversine_m(waypoints[i][0], waypoints[i][1],
                     waypoints[i+1][0], waypoints[i+1][1])
        for i in range(len(waypoints) - 1)
    )
    abs_path = (f"/var/mobile/Containers/Data/Application/{container_uuid}"
                f"/Documents/wayline_mission/{mission_id}/{mission_id}.kmz")
    all_locs = json.dumps(
        [{"latitude": lat, "longitude": lon} for lat, lon in waypoints],
        separators=(",", ":")
    )
    now = time.time()
    con.execute("""
        INSERT OR REPLACE INTO kmzTable
          (missionId, filePath, name, author, createTime, updateTime,
           coverImagePath, waypointImageNames, poiImageNames,
           waypointCount, mileage, waylineLatitude, waylineLongitude,
           locationDes, duration, allPointLocations, deleteTime, lastSyncTime)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        mission_id, abs_path, name, "UAV", now, now,
        f"{mission_id}.jpg", ",".join(f"WP_{i}" for i in range(len(waypoints))), "",
        len(waypoints), dist,
        sum(lats) / len(lats), sum(lons) / len(lons),
        name, int(dist / 8), all_locs, -1, -1.0,
    ))
    return abs_path


@app.post("/api/push/{job_id}")
async def push(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found. Re-generate the mission first.")

    name = job["name"]

    # Build list of (display_name, kmz_path, waypoints) for each pass to push
    if job.get("mode") == "photogrammetry":
        push_items = [
            (f"{name} ({p['pass']})", Path(p["kmz_path"]), p["waypoints"])
            for p in job["passes"]
        ]
    else:
        kmz_path = Path(job["kmz_path"])
        if not kmz_path.exists():
            raise HTTPException(410, "KMZ file expired. Re-generate the mission.")
        push_items = [(name, kmz_path, job["waypoints"])]

    try:
        from skyrover_ios_bridge import (
            MISSION_DB, MISSION_ROOT,
            _afc_session, _get_lockdown, _mkdir, _read, _write,
        )

        try:
            lockdown = await asyncio.wait_for(_get_lockdown(), timeout=8.0)
        except asyncio.TimeoutError:
            raise HTTPException(503, "No iPhone found — check USB cable and unlock the phone.")

        async with _afc_session(lockdown) as afc:
            # Read the DB once and work on a local copy
            db_bytes = await _read(afc, MISSION_DB)
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
                f.write(db_bytes)
                db_tmp = Path(f.name)

            con = sqlite3.connect(str(db_tmp))
            row = con.execute("SELECT filePath FROM kmzTable LIMIT 1").fetchone()
            if not row:
                con.close()
                raise HTTPException(400, "No existing missions on device — save one in-app first.")
            parts = row[0].split("/")
            container_uuid = parts[parts.index("Application") + 1]

            mission_ids = []
            for item_name, kmz_path, waypoints in push_items:
                if not kmz_path.exists():
                    continue
                mission_id = str(uuid.uuid4()).upper()
                _db_insert_mission(con, container_uuid, mission_id, item_name, waypoints,
                                   kmz_path.read_bytes(), MISSION_ROOT)
                dest_dir = f"{MISSION_ROOT}/{mission_id}"
                await _mkdir(afc, dest_dir)
                await _write(afc, f"{dest_dir}/{mission_id}.kmz", kmz_path.read_bytes())
                mission_ids.append(mission_id)
                if "mission_id" in job:
                    pass_label = item_name.split("(")[-1].rstrip(")") if "(" in item_name else "survey"
                    _db_record_push(job["mission_id"], pass_label, mission_id)

            con.commit()
            con.close()
            await _write(afc, MISSION_DB, db_tmp.read_bytes())
            db_tmp.unlink()

        n = len(mission_ids)
        label = f"{n} passes" if n > 1 else name
        return {"success": True, "mission_ids": mission_ids, "name": label, "count": n}

    except HTTPException:
        raise
    except SystemExit:
        raise HTTPException(503, "iPhone not connected — check USB cable and unlock the phone.")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, str(exc))


# ── CLI entry point ──────────────────────────────────────────────────────────

def serve():
    import uvicorn
    uvicorn.run(
        "web.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8001)),
        reload=False,
    )


# ── static files + SPA ────────────────────────────────────────────────────────

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
