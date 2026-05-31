"""sync_lab_team.py — reconcile the website's team.json into the students table.

The lab website's `data/team.json` is the source of truth for current lab
members.  This job reads that file and:

  1. Inserts new members not yet in students.
  2. Updates short_name/role/joined_iso/email when team.json says so.
  3. Re-activates anyone previously alumnified who is back in team.json
     (status='active').
  4. Soft-marks departures: rows whose full_name no longer appears in
     team.json get status='alumni'.  Never hard-deletes — historical
     project leadership references stay valid.

Heath Blackmon is skipped — he's the hardcoded option in the dashboard's
lead picker, so giving him a students row would duplicate him in the menu.

Schedule: nightly at 4:15am Central (registered in scheduler.py).
Run on demand: `python -m agent.jobs.sync_lab_team --verbose`
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Optional

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.jobs.website_git import website_repo_path  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_JOB_NAME = "sync_lab_team"
_SKIP_NAMES = {"Heath Blackmon"}  # hardcoded in the dashboard lead picker


def _team_json_path() -> str:
    return os.path.join(website_repo_path(), "data", "team.json")


def _short_name(full: str) -> str:
    return full.split()[0] if full else ""


def _joined_iso(year_joined) -> Optional[str]:
    """Convert a year (int or string) to an ISO date string. None if unparseable."""
    if year_joined is None:
        return None
    try:
        y = int(str(year_joined).strip())
        return f"{y}-01-01"
    except (ValueError, TypeError):
        return None


def _sync(conn: sqlite3.Connection) -> dict:
    """Reconcile students with team.json. Returns dict of change lists + counts."""
    with open(_team_json_path()) as f:
        team = json.load(f)
    members = team.get("members", []) or []

    canonical: dict[str, dict] = {}
    for m in members:
        name = (m.get("name") or "").strip()
        role = (m.get("role") or "").strip()
        if not name or not role:
            continue
        if name in _SKIP_NAMES:
            continue
        canonical[name] = {
            "full_name": name,
            "short_name": _short_name(name),
            "role": role,
            "joined_iso": _joined_iso(m.get("year_joined")),
            "email": (m.get("email") or "").strip() or None,
        }

    inserted: list[str] = []
    updated: list[str] = []
    alumnified: list[str] = []
    unchanged = 0

    conn.row_factory = sqlite3.Row
    existing = {
        r["full_name"]: r
        for r in conn.execute(
            "SELECT id, full_name, short_name, role, joined_iso, status, email FROM students"
        )
    }

    for name, want in canonical.items():
        cur = existing.get(name)
        if cur is None:
            conn.execute(
                "INSERT INTO students (full_name, short_name, role, joined_iso, status, email) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (
                    want["full_name"],
                    want["short_name"],
                    want["role"],
                    want["joined_iso"],
                    want["email"],
                ),
            )
            inserted.append(name)
            continue

        changes: dict[str, object] = {}
        if (cur["short_name"] or "") != want["short_name"]:
            changes["short_name"] = want["short_name"]
        if (cur["role"] or "") != want["role"]:
            changes["role"] = want["role"]
        if (cur["joined_iso"] or None) != want["joined_iso"]:
            changes["joined_iso"] = want["joined_iso"]
        if (cur["email"] or None) != want["email"]:
            changes["email"] = want["email"]
        if (cur["status"] or "") != "active":
            changes["status"] = "active"

        if changes:
            set_clause = ", ".join(f"{k}=?" for k in changes)
            conn.execute(
                f"UPDATE students SET {set_clause} WHERE id=?",
                list(changes.values()) + [cur["id"]],
            )
            updated.append(name)
        else:
            unchanged += 1

    # Soft-mark departures
    for name, cur in existing.items():
        if name in canonical:
            continue
        if (cur["status"] or "") == "alumni":
            continue
        conn.execute("UPDATE students SET status='alumni' WHERE id=?", (cur["id"],))
        alumnified.append(name)

    conn.commit()
    return {
        "members_in_team_json": len(canonical),
        "inserted": inserted,
        "updated": updated,
        "alumnified": alumnified,
        "unchanged": unchanged,
    }


@tracked(_JOB_NAME)
def job(verbose: bool = False) -> str:
    """Run one reconciliation pass. Idempotent."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        result = _sync(conn)
    finally:
        conn.close()

    summary = (
        f"sync_lab_team: members={result['members_in_team_json']} "
        f"inserted={len(result['inserted'])} "
        f"updated={len(result['updated'])} "
        f"alumnified={len(result['alumnified'])} "
        f"unchanged={result['unchanged']}"
    )
    if verbose:
        print(summary)
        if result["inserted"]:
            print("  inserted:", ", ".join(result["inserted"]))
        if result["updated"]:
            print("  updated:", ", ".join(result["updated"]))
        if result["alumnified"]:
            print("  alumnified:", ", ".join(result["alumnified"]))
    return summary


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="sync_lab_team CLI")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    os.environ.setdefault("FORCE_RUN", "1")
    print(job(verbose=args.verbose))
    sys.exit(0)
