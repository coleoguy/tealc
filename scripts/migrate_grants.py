"""One-shot migration: move grant-type rows out of research_projects into the
new `grants` table.

Run once, after the scheduler's `grants` DDL has been applied.  Idempotent —
re-running is a no-op because it checks for existence before inserting and
only deletes research_projects rows that have been successfully copied.

Usage:
    ~/.lab-agent-venv/bin/python scripts/migrate_grants.py           # dry-run
    ~/.lab-agent-venv/bin/python scripts/migrate_grants.py --apply   # write
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..")))
from agent.scheduler import DB_PATH  # noqa: E402


def main(apply: bool) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    # Source: research_projects rows with project_type='grant'
    rows = conn.execute(
        "SELECT * FROM research_projects WHERE project_type='grant'"
    ).fetchall()

    if not rows:
        print("[migrate_grants] no rows to migrate.")
        conn.close()
        return 0

    print(f"[migrate_grants] found {len(rows)} rows with project_type='grant'")
    now = datetime.now(timezone.utc).isoformat()

    moved = 0
    skipped_existing = 0
    for r in rows:
        old_id = r["id"]
        # New ID: g_<numeric-tail>; preserves provenance (p_124 -> g_124)
        if old_id and "_" in old_id:
            tail = old_id.split("_", 1)[1]
            new_id = f"g_{tail}"
        else:
            new_id = f"g_{old_id}"

        existing = conn.execute(
            "SELECT 1 FROM grants WHERE id=?", (new_id,)
        ).fetchone()
        if existing:
            print(f"  [skip] {old_id} → {new_id} already present in grants table")
            skipped_existing += 1
            continue

        print(f"  [copy] {old_id} → {new_id}: {r['name']!r}")
        if apply:
            conn.execute("""
                INSERT INTO grants (
                    id, name, agency, program, status, deadline_iso, amount_usd,
                    pi_role, drive_folder_path, linked_artifact_id, linked_goal_ids,
                    current_hypothesis, next_action, notes, last_touched_by,
                    last_touched_iso, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                new_id,
                r["name"],
                r["agency"],
                r["program"],
                r["grant_status"],   # research_projects.grant_status → grants.status
                None,                 # deadline_iso — not currently tracked in research_projects
                None,                 # amount_usd
                None,                 # pi_role
                r["data_dir"],        # preserve the existing data_dir as drive_folder_path
                r["linked_artifact_id"],
                r["linked_goal_ids"],
                r["current_hypothesis"],
                r["next_action"],
                r["notes"],
                r["last_touched_by"],
                r["last_touched_iso"],
                now,
            ))
        moved += 1

    if apply and moved > 0:
        # Only delete rows we actually copied
        ids_to_delete = []
        for r in rows:
            old_id = r["id"]
            tail = old_id.split("_", 1)[1] if old_id and "_" in old_id else old_id
            new_id = f"g_{tail}"
            existed_before = conn.execute(
                "SELECT 1 FROM grants WHERE id=?", (new_id,)
            ).fetchone()
            if existed_before:
                ids_to_delete.append(old_id)

        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            conn.execute(
                f"DELETE FROM research_projects WHERE id IN ({placeholders})",
                ids_to_delete,
            )
        conn.commit()
        print(f"[migrate_grants] deleted {len(ids_to_delete)} source rows from research_projects")

    # Summary
    total_grants = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
    remaining_grant_type = conn.execute(
        "SELECT COUNT(*) FROM research_projects WHERE project_type='grant'"
    ).fetchone()[0]
    conn.close()

    mode = "APPLY" if apply else "DRY-RUN"
    print(
        f"[migrate_grants] {mode}: moved={moved} skipped_existing={skipped_existing} "
        f"grants_total={total_grants} research_projects_with_grant_type={remaining_grant_type}"
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Write the migration (default dry-run).")
    args = p.parse_args()
    sys.exit(main(args.apply))
