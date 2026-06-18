#!/usr/bin/env python3
"""One-time script to merge phone-imported missions that are passes of the same flight.

Usage:
    uv run python scripts/merge_phone_passes.py           # dry run
    uv run python scripts/merge_phone_passes.py --execute # apply changes
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from web.db import PASS_SUFFIXES, _base_name

DB_PATH = Path(__file__).parent.parent / "data" / "missions.db"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    rows = con.execute("""
        SELECT m.id AS mid, m.name, m.created_at,
               mp.id AS pid, mp.phone_mission_id, mp.waypoints
        FROM missions m
        JOIN mission_passes mp ON mp.mission_id = m.id
        WHERE m.polygon = '[]'
          AND mp.phone_mission_id IS NOT NULL
          AND m.status = 'on_phone'
        ORDER BY m.name
    """).fetchall()

    if not rows:
        print("No phone-imported missions found — nothing to do.")
        return

    # Group by base name
    groups: dict[str, list] = {}
    for r in rows:
        base, suffix = _base_name(r["name"])
        groups.setdefault(base, []).append({
            "mission_id": r["mid"], "pass_id": r["pid"],
            "name": r["name"], "suffix": suffix,
            "phone_mission_id": r["phone_mission_id"],
        })

    multi   = {k: v for k, v in groups.items() if len(v) > 1}
    singles = {k: v for k, v in groups.items() if len(v) == 1}

    print(f"\nFound {len(rows)} phone-imported missions across {len(groups)} base names.")
    print(f"  {len(multi)} group(s) with multiple passes → will merge")
    print(f"  {len(singles)} single mission(s)            → unchanged\n")

    if not multi:
        print("Nothing to merge.")
        return

    for base, passes in sorted(multi.items()):
        canonical = passes[0]["mission_id"]
        # 2+ passes from the same base name = photogrammetry; 1 pass = survey
        mode = "photogrammetry" if len(passes) > 1 else "survey"
        print(f"  '{base}'  [{mode}]  {len(passes)} passes:")
        for p in passes:
            tag = " ← keep" if p["mission_id"] == canonical else "   merge+delete"
            print(f"    {p['suffix']:10s}  {p['mission_id'][:8]}…  {tag}")
        print()

    if not args.execute:
        print("Dry run — no changes made. Re-run with --execute to apply.")
        return

    now = time.time()
    missions_deleted = 0
    for base, passes in multi.items():
        canonical = passes[0]["mission_id"]
        # 2+ passes from the same base name = photogrammetry; 1 pass = survey
        mode = "photogrammetry" if len(passes) > 1 else "survey"
        con.execute(
            "UPDATE missions SET name=?, mode=?, updated_at=? WHERE id=?",
            (base, mode, now, canonical),
        )
        for p in passes:
            con.execute("UPDATE mission_passes SET pass_name=? WHERE id=?", (p["suffix"], p["pass_id"]))
            if p["mission_id"] != canonical:
                con.execute("UPDATE mission_passes SET mission_id=? WHERE id=?", (canonical, p["pass_id"]))
                con.execute("DELETE FROM missions WHERE id=?", (p["mission_id"],))
                missions_deleted += 1

    con.commit()
    con.close()
    print(f"Done — merged {missions_deleted} missions into {len(multi)} group(s).")


if __name__ == "__main__":
    main()
