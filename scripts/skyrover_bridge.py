#!/usr/bin/env python3
"""
skyrover_bridge.py — inject a terraprint KMZ survey mission into the Skyrover app.

Usage:
  python skyrover_bridge.py probe             # discover mission storage on connected device
  python skyrover_bridge.py push <file.kmz>   # push a KMZ mission to the device
  python skyrover_bridge.py list              # list missions already on the device
  python skyrover_bridge.py watch             # tail logcat and capture kmzFilePath log lines

Requires:
  - Android device connected via USB with USB Debugging enabled
  - adb in PATH (install via `sudo apt install adb` or Android Platform Tools)
  - Skyrover app installed and opened at least once (so its directories exist)

How it works:
  The Skyrover app (com.sky.dronemaster) is a DJI Fly clone built on DJI's UAV SDK.
  Its native WPMZ library (libwpmz_jni.so) reads KMZ files from a path passed in from
  the Java layer.  This tool discovers that path by probing the device filesystem and
  watching logcat for 'kmzFilePath' log lines, then writes our KMZ there and broadcasts
  a media-scan intent so the app notices the new file.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_PKG = "com.sky.dronemaster"

# Candidate paths in order of likelihood (based on DJI Fly / UAV SDK conventions).
# The bridge probes each one and uses whichever exists on the device.
CANDIDATE_PATHS = [
    f"/sdcard/Android/data/{APP_PKG}/files/waypoint",
    f"/sdcard/Android/data/{APP_PKG}/files/mission",
    f"/sdcard/Android/data/{APP_PKG}/files/wpmz",
    f"/sdcard/Android/data/{APP_PKG}/files/route",
    f"/sdcard/Android/data/{APP_PKG}/files",
    f"/sdcard/DJI/waypoint",
    f"/sdcard/DJI/mission",
]

# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def _adb(*args, check=True, capture=True) -> subprocess.CompletedProcess:
    cmd = ["adb", *args]
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )


def adb_shell(cmd: str, check=False) -> str:
    r = _adb("shell", cmd, check=check)
    return r.stdout.strip()


def require_adb() -> None:
    if not shutil.which("adb"):
        print("ERROR: adb not found in PATH.")
        print("Install Android Platform Tools:")
        print("  Ubuntu/Debian: sudo apt install adb")
        print("  macOS:         brew install android-platform-tools")
        print("  Windows:       https://developer.android.com/studio/releases/platform-tools")
        sys.exit(1)


def require_device() -> str:
    """Return the device serial, or exit with instructions."""
    result = _adb("devices", check=False)
    lines = [l for l in result.stdout.splitlines()[1:] if l.strip() and "device" in l]
    if not lines:
        print("ERROR: No Android device found.")
        print()
        print("On your phone:")
        print("  Settings → About Phone → tap Build Number 7 times")
        print("  Settings → Developer Options → USB Debugging → ON")
        print("  Connect via USB and accept the 'Allow USB Debugging' prompt")
        sys.exit(1)
    if len(lines) > 1:
        print(f"Multiple devices found; using first: {lines[0].split()[0]}")
    return lines[0].split()[0]


def require_app() -> None:
    installed = adb_shell(f"pm list packages {APP_PKG}")
    if APP_PKG not in installed:
        print(f"ERROR: {APP_PKG} is not installed on the device.")
        print("Make sure the Skyrover app is installed and has been opened at least once.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

def find_mission_path() -> str | None:
    """Try each candidate path; return the first that exists on the device."""
    for path in CANDIDATE_PATHS:
        out = adb_shell(f"ls '{path}' 2>/dev/null && echo EXISTS")
        if "EXISTS" in out:
            return path
    return None


def probe_filesystem() -> dict:
    """Walk the app's external storage tree and return what's there."""
    root = f"/sdcard/Android/data/{APP_PKG}"
    out = adb_shell(f"find '{root}' -maxdepth 4 2>/dev/null")
    entries = [l for l in out.splitlines() if l.strip()]

    kmz_files = [e for e in entries if e.endswith(".kmz")]
    db_files  = [e for e in entries if e.endswith(".db") or e.endswith(".sqlite")]
    dirs      = [e for e in entries if not Path(e).suffix]

    return {"root": root, "kmz": kmz_files, "db": db_files, "dirs": dirs, "all": entries}


def probe_internal_db() -> list[str]:
    """Try to list internal databases via run-as (works on debug builds / some devices)."""
    out = adb_shell(f"run-as {APP_PKG} ls databases/ 2>/dev/null")
    return [l for l in out.splitlines() if l.strip() and not l.startswith("ls:")]


def sniff_db_schema(db_name: str) -> str:
    """Pull a DB from internal storage, return its table list."""
    remote = f"/data/data/{APP_PKG}/databases/{db_name}"
    local  = f"/tmp/{db_name}"
    try:
        _adb("shell", f"run-as {APP_PKG} cat databases/{db_name} > /tmp/pulled_{db_name}", check=False)
        r = subprocess.run(
            ["adb", "pull", f"/data/data/{APP_PKG}/databases/{db_name}", local],
            capture_output=True, text=True, check=False
        )
        if r.returncode != 0:
            return "(could not pull — may require root)"
        result = subprocess.run(
            ["sqlite3", local, ".tables"],
            capture_output=True, text=True, check=False
        )
        return result.stdout.strip() or "(empty)"
    except Exception as e:
        return f"({e})"


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def push_kmz(kmz_path: Path, mission_dir: str) -> None:
    """Push a KMZ file to the device and broadcast a media scan."""
    remote = f"{mission_dir}/{kmz_path.name}"
    print(f"Pushing {kmz_path.name} → {remote}")

    # Ensure the directory exists
    adb_shell(f"mkdir -p '{mission_dir}'")

    result = _adb("push", str(kmz_path), remote, check=False)
    if result.returncode != 0:
        print(f"ERROR: adb push failed:\n{result.stderr}")
        sys.exit(1)
    print(f"  Pushed ({kmz_path.stat().st_size // 1024} KB)")

    # Broadcast media scan so Android and the app notice the new file
    adb_shell(
        f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
        f"-d 'file://{remote}'"
    )
    print(f"  Media scan broadcast sent")

    # Also try sending a file-open intent directly to the app
    adb_shell(
        f"am start -a android.intent.action.VIEW "
        f"-d 'file://{remote}' "
        f"-t 'application/zip' "
        f"--activity-single-top 2>/dev/null"
    )

    print()
    print(f"Done. Open the Skyrover app → Waypoints → your mission should appear.")
    print(f"If it doesn't, try: Waypoints → menu → Import → navigate to {remote}")


def list_missions(mission_dir: str) -> None:
    out = adb_shell(f"find '{mission_dir}' -name '*.kmz' 2>/dev/null")
    files = [l for l in out.splitlines() if l.strip()]
    if not files:
        print(f"No .kmz files found under {mission_dir}")
        return
    for f in files:
        size = adb_shell(f"stat -c '%s' '{f}' 2>/dev/null")
        name = Path(f).name
        kb = int(size) // 1024 if size.isdigit() else "?"
        print(f"  {name}  ({kb} KB)  —  {f}")


# ---------------------------------------------------------------------------
# Logcat watcher
# ---------------------------------------------------------------------------

def watch_logcat(timeout: int = 60) -> None:
    """Watch logcat for kmzFilePath messages — reveals the exact path the app uses."""
    print(f"Watching logcat for kmzFilePath messages ({timeout}s)...")
    print("Open the Skyrover app and start a waypoint mission to trigger the log.")
    print("Ctrl+C to stop.\n")

    adb_shell("logcat -c")  # clear buffer
    proc = subprocess.Popen(
        ["adb", "logcat", "-s", "*:V"],
        stdout=subprocess.PIPE,
        text=True,
    )
    start = time.time()
    hits = []
    try:
        for line in proc.stdout:
            if time.time() - start > timeout:
                break
            if any(k in line for k in ["kmzFilePath", "wpmz", "wayline", "WaylineMission", "WPMZ"]):
                print(f"  HIT: {line.rstrip()}")
                hits.append(line.rstrip())
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()

    if hits:
        print(f"\nCaptured {len(hits)} relevant log lines.")
        print("Look for 'kmzFilePath : /path/to/file.kmz' to find the mission directory.")
    else:
        print("\nNo WPMZ log lines captured in this session.")
        print("Make sure to open the app and trigger a waypoint mission while watching.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_probe(args) -> None:
    require_adb()
    require_device()
    require_app()

    print(f"Probing device for {APP_PKG} storage...\n")

    fs = probe_filesystem()
    print(f"External storage tree ({fs['root']}):")
    if fs["all"]:
        for e in sorted(set(fs["dirs"]))[:20]:
            print(f"  {e}")
        if len(fs["dirs"]) > 20:
            print(f"  ... ({len(fs['dirs'])} dirs total)")
    else:
        print("  (empty or inaccessible)")

    print()
    if fs["kmz"]:
        print("Existing KMZ files:")
        for k in fs["kmz"]:
            print(f"  {k}")
    else:
        print("No .kmz files found in external storage yet.")

    print()
    mission_path = find_mission_path()
    if mission_path:
        print(f"Mission directory found:  {mission_path}")
    else:
        print("No mission directory found yet.")
        print("Candidates tried:")
        for c in CANDIDATE_PATHS:
            print(f"  {c}")
        print()
        print("The app may create this directory the first time you open waypoints.")
        print("Try: open Skyrover app → waypoint mode → plan a mission → save it,")
        print("then re-run: python scripts/skyrover_bridge.py probe")

    print()
    internal_dbs = probe_internal_db()
    if internal_dbs:
        print(f"Internal databases (run-as accessible):")
        for db in internal_dbs:
            tables = sniff_db_schema(db)
            print(f"  {db}  →  tables: {tables}")
    else:
        print("Internal databases: not accessible via run-as (normal for release builds)")
        print("A rooted device would reveal the mission DB schema.")


def cmd_push(args) -> None:
    require_adb()
    require_device()
    require_app()

    kmz = Path(args.kmz)
    if not kmz.exists():
        print(f"ERROR: File not found: {kmz}")
        sys.exit(1)
    if kmz.suffix.lower() != ".kmz":
        print(f"WARNING: File doesn't end in .kmz — proceeding anyway")

    mission_dir = args.path or find_mission_path()
    if not mission_dir:
        print("ERROR: Could not find mission directory on device.")
        print("Run 'python scripts/skyrover_bridge.py probe' first,")
        print("or specify the path with --path /sdcard/Android/data/com.sky.dronemaster/files/waypoint")
        sys.exit(1)

    push_kmz(kmz, mission_dir)


def cmd_list(args) -> None:
    require_adb()
    require_device()
    require_app()

    mission_dir = args.path or find_mission_path()
    if not mission_dir:
        print("No mission directory found. Run probe first.")
        sys.exit(1)

    print(f"Missions in {mission_dir}:")
    list_missions(mission_dir)


def cmd_watch(args) -> None:
    require_adb()
    require_device()
    watch_logcat(timeout=args.timeout)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bridge: push terraprint KMZ survey missions to the Skyrover app via ADB."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="Discover mission storage on the connected device")

    push_p = sub.add_parser("push", help="Push a KMZ mission to the device")
    push_p.add_argument("kmz", help="Path to the .kmz file to push")
    push_p.add_argument("--path", default=None,
                        metavar="DIR",
                        help="Override mission directory on device")

    list_p = sub.add_parser("list", help="List missions on the device")
    list_p.add_argument("--path", default=None, metavar="DIR")

    watch_p = sub.add_parser("watch", help="Watch logcat for kmzFilePath log lines")
    watch_p.add_argument("--timeout", type=int, default=120,
                         help="Seconds to watch (default: 120)")

    return p


def main() -> None:
    args = build_parser().parse_args()
    {
        "probe": cmd_probe,
        "push":  cmd_push,
        "list":  cmd_list,
        "watch": cmd_watch,
    }[args.command](args)


if __name__ == "__main__":
    main()
