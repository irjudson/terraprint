#!/usr/bin/env python3
"""
skyrover_ios_bridge.py — inject a terraprint KMZ survey mission into the Skyrover iOS app.

Usage:
  python skyrover_ios_bridge.py probe           # explore app container, find mission storage
  python skyrover_ios_bridge.py push <file.kmz> # push a KMZ mission to the device
  python skyrover_ios_bridge.py list            # list missions already on device
  python skyrover_ios_bridge.py watch           # tail syslog for kmzFilePath messages

Requirements:
  uv sync   (installs pymobiledevice3 into the project venv)

On your iPhone:
  - Trust this computer when prompted after connecting via USB
  - Skyrover app installed and opened at least once
  - Developer Mode ON: Settings → search "Developer" → Developer Mode → ON
"""

import argparse
import asyncio
import json
import math
import sqlite3
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

BUNDLE_ID = "com.sky.dronemaster"
MISSION_ROOT = "Documents/wayline_mission"
MISSION_DB   = f"{MISSION_ROOT}/mission_db/wpmz.sqlite3"

WPML_NS = "http://www.dji.com/wpmz/1.0.2"


# ---------------------------------------------------------------------------
# pymobiledevice3 helpers
# ---------------------------------------------------------------------------

def _require_pymobiledevice() -> None:
    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        print("ERROR: pymobiledevice3 is not installed.  Run: uv sync")
        sys.exit(1)


async def _get_lockdown():
    from pymobiledevice3.lockdown import create_using_usbmux
    try:
        return await create_using_usbmux()
    except Exception as exc:
        print(f"ERROR: Could not connect to iPhone: {exc}")
        print("Make sure the iPhone is connected, unlocked, and you tapped 'Trust'.")
        sys.exit(1)


@asynccontextmanager
async def _afc_session(lockdown):
    from pymobiledevice3.services.house_arrest import HouseArrestService
    setup_done = False
    try:
        async with HouseArrestService(lockdown, documents_only=True) as afc:
            await afc.send_command(BUNDLE_ID, "VendDocuments")
            setup_done = True
            yield afc
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        if setup_done:
            raise  # body exceptions propagate so callers can handle them
        print(f"ERROR: Could not open app container for {BUNDLE_ID}: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

async def _ls(afc, path: str) -> list[str]:
    try:
        return await afc.listdir(path)
    except Exception:
        return []


async def _exists(afc, path: str) -> bool:
    try:
        await afc.stat(path)
        return True
    except Exception:
        return False


async def _write(afc, path: str, data: bytes) -> None:
    await afc.set_file_contents(path, data)


async def _read(afc, path: str) -> bytes:
    return await afc.get_file_contents(path)


async def _mkdir(afc, path: str) -> None:
    parts = Path(path).parts
    for i in range(1, len(parts) + 1):
        p = str(Path(*parts[:i]))
        if not await _exists(afc, p):
            try:
                await afc.makedirs(p)
            except Exception:
                pass


async def _walk(afc, root: str, max_depth: int = 4) -> list[str]:
    results = []

    async def _recurse(path: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = await afc.listdir(path)
        except Exception:
            return
        for entry in entries:
            full = f"{path}/{entry}"
            results.append(full)
            await _recurse(full, depth + 1)

    await _recurse(root, 0)
    return results


async def find_kmz_files(afc) -> list[str]:
    return [e for e in await _walk(afc, "Documents") if e.lower().endswith(".kmz")]


# ---------------------------------------------------------------------------
# KMZ / mission DB helpers
# ---------------------------------------------------------------------------

def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_waypoints_from_kmz(kmz_path: Path) -> list[tuple[float, float]]:
    """Return list of (lat, lon) from the waylines.wpml inside the KMZ."""
    with zipfile.ZipFile(kmz_path) as z:
        wpml_name = next((n for n in z.namelist() if n.endswith("waylines.wpml")), None)
        if not wpml_name:
            return []
        data = z.read(wpml_name)
    root = ET.fromstring(data)
    KML_NS = "http://www.opengis.net/kml/2.2"
    points = []
    for coord_el in root.findall(f".//{{{KML_NS}}}coordinates"):
        # KML coordinates are "lon,lat,alt"
        for token in coord_el.text.strip().split():
            parts = token.split(",")
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                points.append((lat, lon))
    return points


def _extract_container_uuid(db_path: str) -> str | None:
    """Parse the container UUID from an existing DB filePath."""
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT filePath FROM kmzTable LIMIT 1").fetchone()
    con.close()
    if not row:
        return None
    # filePath looks like /var/mobile/Containers/Data/Application/<UUID>/Documents/...
    parts = row[0].split("/")
    try:
        idx = parts.index("Application")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


def _insert_mission(db_path: str, mission_id: str, container_uuid: str,
                    waypoints: list[tuple[float, float]], name: str, speed_ms: float = 8.0) -> None:
    if not waypoints:
        raise ValueError("No waypoints found in KMZ")

    lats = [p[0] for p in waypoints]
    lons = [p[1] for p in waypoints]
    center_lat = (min(lats) + max(lats)) / 2
    center_lon = (min(lons) + max(lons)) / 2

    mileage = sum(_haversine_m(waypoints[i][0], waypoints[i][1],
                               waypoints[i+1][0], waypoints[i+1][1])
                  for i in range(len(waypoints) - 1))
    duration = int(mileage / speed_ms)

    all_locs = json.dumps([{"latitude": lat, "longitude": lon} for lat, lon in waypoints],
                          separators=(',', ':'))
    wp_image_names = ",".join(f"WP_{i}" for i in range(len(waypoints)))

    abs_path = (f"/var/mobile/Containers/Data/Application/{container_uuid}"
                f"/Documents/wayline_mission/{mission_id}/{mission_id}.kmz")

    now = time.time()

    con = sqlite3.connect(db_path)
    con.execute("""
        INSERT OR REPLACE INTO kmzTable
          (missionId, filePath, name, author, createTime, updateTime,
           coverImagePath, waypointImageNames, poiImageNames,
           waypointCount, mileage, waylineLatitude, waylineLongitude,
           locationDes, duration, allPointLocations, deleteTime, lastSyncTime)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        mission_id, abs_path, name, "UAV", now, now,
        f"{mission_id}.jpg", wp_image_names, "",
        len(waypoints), mileage, center_lat, center_lon,
        name, duration, all_locs, -1, -1.0,
    ))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_probe(args) -> None:
    _require_pymobiledevice()
    lockdown = await _get_lockdown()

    print(f"Device : {lockdown.short_info}")
    print(f"App    : {BUNDLE_ID}")
    print()

    async with _afc_session(lockdown) as afc:
        print("Walking app container...")
        all_entries = await _walk(afc, "Documents", max_depth=5)

        dirs = [e for e in all_entries if not Path(e).suffix]
        kmzs = [e for e in all_entries if e.lower().endswith(".kmz")]
        dbs  = [e for e in all_entries if e.lower().endswith((".db", ".sqlite", ".sqlite3"))]

        print("\nTop-level:")
        for d in sorted(set(await _ls(afc, "Documents"))):
            print(f"  Documents/{d}")

        print(f"\nKMZ files:")
        if kmzs:
            for k in kmzs:
                try:
                    st = await afc.stat(k)
                    print(f"  {k}  ({int(st['st_size'])//1024} KB)")
                except Exception:
                    print(f"  {k}")
        else:
            print("  None found. Save a mission in-app first, then re-run probe.")

        print(f"\nDatabases:")
        for d in dbs:
            print(f"  {d}")

        print(f"\nMission root: {MISSION_ROOT}")


async def cmd_push(args) -> None:
    _require_pymobiledevice()
    lockdown = await _get_lockdown()

    kmz_path = Path(args.kmz)
    if not kmz_path.exists():
        print(f"ERROR: File not found: {kmz_path}")
        sys.exit(1)

    mission_name = args.name or kmz_path.stem
    mission_id   = str(uuid.uuid4()).upper()

    # Parse waypoints from KMZ
    print(f"Parsing {kmz_path.name}...")
    waypoints = _parse_waypoints_from_kmz(kmz_path)
    if not waypoints:
        print("ERROR: No waypoints found in KMZ. Is this a valid WPML mission file?")
        sys.exit(1)
    print(f"  {len(waypoints)} waypoints, center ~{sum(p[0] for p in waypoints)/len(waypoints):.4f}, "
          f"{sum(p[1] for p in waypoints)/len(waypoints):.4f}")

    async with _afc_session(lockdown) as afc:
        # Pull existing DB to get the container UUID
        print("Pulling mission database...")
        db_bytes = await _read(afc, MISSION_DB)

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(db_bytes)
            tmp_db = f.name

        container_uuid = _extract_container_uuid(tmp_db)
        if not container_uuid:
            print("ERROR: Could not determine container UUID from existing missions.")
            print("Save at least one mission in the Skyrover app first, then retry.")
            sys.exit(1)
        print(f"  Container: {container_uuid}")

        # Insert mission row
        _insert_mission(tmp_db, mission_id, container_uuid, waypoints, mission_name)

        # Write KMZ as <mission_id>.kmz in its own subfolder
        dest_dir = f"{MISSION_ROOT}/{mission_id}"
        dest_kmz = f"{dest_dir}/{mission_id}.kmz"
        await _mkdir(afc, dest_dir)

        print(f"Pushing KMZ → {dest_kmz}")
        await _write(afc, dest_kmz, kmz_path.read_bytes())

        # Push updated DB back
        print(f"Updating mission database...")
        with open(tmp_db, "rb") as f:
            await _write(afc, MISSION_DB, f.read())

        print(f"\nDone. Mission '{mission_name}' ({len(waypoints)} waypoints) pushed.")
        print("Open Skyrover → Waypoints — it should appear in the list.")


async def cmd_list(args) -> None:
    _require_pymobiledevice()
    lockdown = await _get_lockdown()

    async with _afc_session(lockdown) as afc:
        db_bytes = await _read(afc, MISSION_DB)
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(db_bytes)
            tmp_db = f.name

        con = sqlite3.connect(tmp_db)
        rows = con.execute(
            "SELECT name, waypointCount, mileage, duration FROM kmzTable WHERE deleteTime=-1"
        ).fetchall()
        con.close()

        if rows:
            print(f"{'Name':<40} {'Waypoints':>10} {'Distance':>12} {'Duration':>10}")
            print("-" * 76)
            for name, wpc, miles, dur in rows:
                print(f"{name:<40} {wpc:>10} {miles/1000:>11.2f}km {dur//60:>8}min")
        else:
            print("No missions found.")


async def cmd_watch(args) -> None:
    _require_pymobiledevice()
    lockdown = await _get_lockdown()

    print(f"Watching syslog for WPMZ/kmzFilePath messages ({args.timeout}s)...")
    print("Open Skyrover → Waypoints → load a mission to trigger the log.")
    print("Ctrl+C to stop.\n")

    try:
        from pymobiledevice3.services.syslog import SyslogService
    except ImportError:
        print("ERROR: SyslogService not available.")
        sys.exit(1)

    keywords = ["kmzFilePath", "wpmz", "wayline", "WaylineMission", "WPMZ", "libwpmz"]
    hits = []
    start = time.time()

    try:
        async with SyslogService(lockdown=lockdown) as syslog:
            async for line in syslog:
                if time.time() - start > args.timeout:
                    break
                msg = str(line)
                if any(k in msg for k in keywords):
                    print(f"  HIT: {msg.rstrip()}")
                    hits.append(msg.rstrip())
    except KeyboardInterrupt:
        pass

    if hits:
        print(f"\nCaptured {len(hits)} relevant log lines.")
    else:
        print("\nNo WPMZ log lines captured.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="iOS bridge: push terraprint KMZ missions into the Skyrover app via USB."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="Explore app container, locate mission storage")

    push_p = sub.add_parser("push", help="Push a KMZ mission to the iPhone")
    push_p.add_argument("kmz", help="Path to the .kmz file")
    push_p.add_argument("--name", default=None, help="Mission display name (default: filename stem)")
    push_p.add_argument("--path", default=None, metavar="CONTAINER_PATH",
                        help="Override mission root path inside the app container")

    sub.add_parser("list", help="List missions on device")

    watch_p = sub.add_parser("watch", help="Tail syslog for kmzFilePath log lines")
    watch_p.add_argument("--timeout", type=int, default=120)

    return p


def main() -> None:
    args = build_parser().parse_args()
    cmd = {
        "probe": cmd_probe,
        "push":  cmd_push,
        "list":  cmd_list,
        "watch": cmd_watch,
    }[args.command]
    asyncio.run(cmd(args))


if __name__ == "__main__":
    main()
