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

app = FastAPI(title="Terraprint Mission Planner")

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
        _jobs[job_id] = {"mode": "photogrammetry", "name": req.name, "passes": passes}
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
    _jobs[job_id] = {"mode": "survey", "kmz_path": str(tmp_path),
                     "name": req.name, "waypoints": waypoints}
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
