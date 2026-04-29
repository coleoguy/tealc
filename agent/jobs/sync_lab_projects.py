"""sync_lab_projects.py — mirror the shared-Drive "Projects" folder into the
research_projects table.  The folder tree at

    ~/Library/CloudStorage/GoogleDrive-*/Shared drives/Blackmon Lab/Projects

is the source of truth for active lab paper projects.  Each direct-child
subfolder of Projects/ is one project.  Subfolders under `2024 abandoned?`
and `2025 abandoned?` are dropped projects.  Admin entries (`Example`,
`Open Dissertation Projects`, `tealc`, and any top-level *file*) are skipped.

Per run:
  1. Enumerate the Drive tree.
  2. Match Drive subfolders to existing research_projects rows by normalized
     name (trim, lowercase, collapse punctuation).  Update `lab_drive_path`
     on matches and promote `project_type` from NULL → 'paper'.
  3. Insert rows for Drive subfolders with no DB match (project_type='paper').
  4. Flag research_projects rows with project_type='paper' OR NULL that have
     no matching Drive subfolder — audit signal for Heath.
  5. Write a briefing with the full diff + audit.

The job is safe to run on-demand (try `run_scheduled_job sync_lab_projects now`)
or daily.  NEVER hard-deletes DB rows.  Soft-moves abandoned projects to
status='dropped' only when the Drive tree says so.

Recommended schedule: CronTrigger(hour=3, minute=30, timezone="America/Chicago")
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_NAME = "sync_lab_projects"

# Source of truth — the Blackmon Lab shared Drive, mounted via Google Drive
# for desktop.  This path is the authoritative list of paper projects.
PROJECTS_ROOT = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/"
    "Shared drives/Blackmon Lab/Projects"
)

# Subfolder names that are NOT themselves projects — admin/template folders.
# Top-level files at Projects/ root are skipped unconditionally (only dirs count).
_ADMIN_FOLDERS = {
    "Example",
    "Open Dissertation Projects",
    "tealc",  # this is the Tealc codebase folder, not a paper project
}

# Subfolders whose *children* are dropped (abandoned) projects.
_ABANDONED_PARENTS = {
    "2024 abandoned?",
    "2025 abandoned?",
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Collapse to a minimal key: lowercase, strip punctuation & whitespace."""
    if not name:
        return ""
    return _NORMALIZE_RE.sub("", name.lower()).strip()


def _list_direct_subdirs(path: str) -> list[str]:
    """Return names of direct-child directories in `path` (alphabetically).
    Skips files and symlinks-to-files."""
    if not os.path.isdir(path):
        return []
    out: list[str] = []
    for entry in sorted(os.listdir(path)):
        if entry.startswith("."):
            continue
        full = os.path.join(path, entry)
        if os.path.isdir(full):
            out.append(entry)
    return out


def _discover_drive_projects() -> tuple[list[dict], list[dict]]:
    """Return (active, dropped) lists of project dicts from the Drive tree.

    Each dict: {name, folder_path, normalized, source}
    source is 'active' (direct child of Projects/) or 'abandoned'
    (grandchild under an abandoned-year folder).
    """
    active: list[dict] = []
    dropped: list[dict] = []

    if not os.path.isdir(PROJECTS_ROOT):
        return active, dropped

    for entry in _list_direct_subdirs(PROJECTS_ROOT):
        if entry in _ADMIN_FOLDERS:
            continue
        full = os.path.join(PROJECTS_ROOT, entry)
        if entry in _ABANDONED_PARENTS:
            for child in _list_direct_subdirs(full):
                if child in _ADMIN_FOLDERS:
                    continue
                dropped.append({
                    "name": child,
                    "folder_path": os.path.join(full, child),
                    "normalized": _normalize(child),
                    "source": f"abandoned ({entry})",
                })
            continue
        active.append({
            "name": entry,
            "folder_path": full,
            "normalized": _normalize(entry),
            "source": "active",
        })

    return active, dropped


def _load_db_projects(conn: sqlite3.Connection) -> list[dict]:
    """Return every research_projects row as a dict."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, project_type, status, lab_drive_path, data_dir FROM research_projects"
    ).fetchall()
    return [dict(r) for r in rows]


class _IdAllocator:
    """Hand out unique `p_NNN` ids without round-tripping the DB each time.
    Works in both apply and dry-run mode — dry-run's reported ids match what
    apply would produce."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id FROM research_projects WHERE id LIKE 'p_%'"
        ).fetchall()
        max_n = 0
        for (rid,) in rows:
            try:
                n = int(str(rid).split("_", 1)[1])
                max_n = max(max_n, n)
            except (ValueError, IndexError):
                continue
        self._next = max_n + 1

    def next(self) -> str:
        pid = f"p_{self._next:03d}"
        self._next += 1
        return pid


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def _sync(conn: sqlite3.Connection, apply: bool) -> dict:
    """Perform the reconciliation.  Returns a dict of counts + itemized lists.

    `apply=False` runs in read-only mode — no rows change, just reports.
    """
    active_drive, dropped_drive = _discover_drive_projects()
    db_rows = _load_db_projects(conn)
    id_alloc = _IdAllocator(conn)

    # Build normalized-name index over DB paper/null rows.  Grants, databases,
    # teaching are excluded — those are separate categories.
    paper_candidates = [
        r for r in db_rows
        if (r["project_type"] or "") in ("paper", "")
    ]

    by_norm: dict[str, list[dict]] = {}
    for r in paper_candidates:
        key = _normalize(r["name"])
        by_norm.setdefault(key, []).append(r)

    now = datetime.now(timezone.utc).isoformat()

    # Result bins
    linked: list[dict] = []       # DB row matched Drive folder; lab_drive_path set
    promoted: list[dict] = []     # project_type was NULL → set to 'paper'
    inserted: list[dict] = []     # no DB match; inserted a new row
    marked_dropped: list[dict] = []  # Drive says abandoned; status → 'dropped'
    orphan_db: list[dict] = []    # DB paper row with no Drive folder
    ambiguous: list[dict] = []    # >1 DB row matched the same Drive folder

    # ---- Pass 1: match active Drive subfolders to DB rows -----------------
    for d in active_drive:
        candidates = by_norm.get(d["normalized"], [])
        if len(candidates) == 0:
            # Insert new
            new_id = id_alloc.next()
            if apply:
                conn.execute("""
                    INSERT INTO research_projects
                        (id, name, project_type, status, data_dir,
                         lab_drive_path, last_touched_by, last_touched_iso,
                         synced_at)
                    VALUES (?, ?, 'paper', 'active', ?, ?, 'sync_lab_projects', ?, ?)
                """, (new_id, d["name"], d["folder_path"], d["folder_path"], now, now))
            inserted.append({"id": new_id, "name": d["name"], "folder": d["folder_path"]})
        else:
            if len(candidates) > 1:
                ambiguous.append({
                    "drive_name": d["name"],
                    "candidates": [{"id": c["id"], "name": c["name"]} for c in candidates],
                })
            # Use the first candidate (pragmatic; ambiguous cases still surface)
            target = candidates[0]
            was_null = (target["project_type"] or "") == ""
            if apply:
                conn.execute("""
                    UPDATE research_projects
                    SET lab_drive_path=?,
                        project_type='paper',
                        last_touched_by='sync_lab_projects',
                        last_touched_iso=?,
                        synced_at=?
                    WHERE id=?
                """, (d["folder_path"], now, now, target["id"]))
                # Only set data_dir if currently empty; don't overwrite manual edits
                if not target.get("data_dir"):
                    conn.execute(
                        "UPDATE research_projects SET data_dir=? WHERE id=?",
                        (d["folder_path"], target["id"]),
                    )
            linked.append({"id": target["id"], "name": target["name"], "folder": d["folder_path"]})
            if was_null:
                promoted.append({"id": target["id"], "name": target["name"]})
        # Remove matched candidates so they don't reappear as orphans
        by_norm.pop(d["normalized"], None)

    # ---- Pass 2: abandoned subfolders → mark DB rows status='dropped' -----
    for d in dropped_drive:
        candidates = by_norm.get(d["normalized"], [])
        if candidates:
            target = candidates[0]
            if apply:
                conn.execute("""
                    UPDATE research_projects
                    SET status='dropped',
                        project_type=COALESCE(project_type, 'paper'),
                        lab_drive_path=?,
                        last_touched_by='sync_lab_projects',
                        last_touched_iso=?,
                        synced_at=?
                    WHERE id=?
                """, (d["folder_path"], now, now, target["id"]))
            marked_dropped.append({
                "id": target["id"],
                "name": target["name"],
                "source": d["source"],
            })
            by_norm.pop(d["normalized"], None)
        else:
            # Drive says abandoned but we don't have a row — insert as dropped
            new_id = id_alloc.next()
            if apply:
                conn.execute("""
                    INSERT INTO research_projects
                        (id, name, project_type, status, data_dir,
                         lab_drive_path, last_touched_by, last_touched_iso,
                         synced_at, notes)
                    VALUES (?, ?, 'paper', 'dropped', ?, ?, 'sync_lab_projects',
                            ?, ?, ?)
                """, (new_id, d["name"], d["folder_path"], d["folder_path"],
                      now, now, f"Discovered in {d['source']} during first sync."))
            marked_dropped.append({"id": new_id, "name": d["name"], "source": d["source"]})

    # ---- Pass 3: remaining unmatched paper/null DB rows = audit orphans ---
    for key, rows in by_norm.items():
        for r in rows:
            # Skip the test project; it's internal
            if r["name"].startswith("TEST"):
                continue
            orphan_db.append({
                "id": r["id"],
                "name": r["name"],
                "project_type": r["project_type"],
                "status": r.get("status"),
            })

    if apply:
        conn.commit()

    return {
        "drive_active_count": len(active_drive),
        "drive_abandoned_count": len(dropped_drive),
        "linked": linked,
        "promoted": promoted,
        "inserted": inserted,
        "marked_dropped": marked_dropped,
        "orphan_db": orphan_db,
        "ambiguous": ambiguous,
    }


# ---------------------------------------------------------------------------
# Audit reporting — writes a briefing row summarizing the run.
# ---------------------------------------------------------------------------

def _render_briefing(result: dict, apply: bool) -> tuple[str, str]:
    """Return (title, markdown_body) for the briefing row."""
    now_date = datetime.now(timezone.utc).date().isoformat()
    mode = "applied" if apply else "dry-run"
    title = (
        f"Lab projects sync — {now_date} ({mode}): "
        f"{result['drive_active_count']} active + "
        f"{result['drive_abandoned_count']} abandoned folders, "
        f"{len(result['orphan_db'])} DB orphans"
    )

    lines = [f"# Lab projects sync — {now_date}"]
    lines.append("")
    lines.append(
        f"**Mode:** {mode}.  "
        f"**Drive source:** `{PROJECTS_ROOT.replace(os.path.expanduser('~'), '~')}`."
    )
    lines.append("")
    lines.append(
        f"- Active Drive subfolders: **{result['drive_active_count']}**\n"
        f"- Abandoned subfolders (under `2024 abandoned?` / `2025 abandoned?`): "
        f"**{result['drive_abandoned_count']}**\n"
        f"- DB rows linked to a Drive folder this run: **{len(result['linked'])}**\n"
        f"- DB rows promoted (project_type: NULL → paper): **{len(result['promoted'])}**\n"
        f"- New DB rows inserted (Drive folder with no match): **{len(result['inserted'])}**\n"
        f"- DB rows marked `status='dropped'`: **{len(result['marked_dropped'])}**\n"
        f"- Ambiguous matches (multiple DB rows per folder): **{len(result['ambiguous'])}**\n"
        f"- **DB orphans requiring audit: {len(result['orphan_db'])}**"
    )
    lines.append("")

    if result["inserted"]:
        lines.append(f"## New projects ({len(result['inserted'])})")
        for it in result["inserted"]:
            lines.append(f"- `{it['id']}` — {it['name']}")
        lines.append("")

    if result["promoted"]:
        lines.append(f"## Promoted to project_type='paper' ({len(result['promoted'])})")
        for it in result["promoted"]:
            lines.append(f"- `{it['id']}` — {it['name']}")
        lines.append("")

    if result["marked_dropped"]:
        lines.append(f"## Marked dropped from abandoned folders ({len(result['marked_dropped'])})")
        for it in result["marked_dropped"]:
            lines.append(f"- `{it['id']}` — {it['name']} ({it['source']})")
        lines.append("")

    if result["ambiguous"]:
        lines.append(f"## Ambiguous (manual review needed) ({len(result['ambiguous'])})")
        for a in result["ambiguous"]:
            ids = ", ".join(c["id"] for c in a["candidates"])
            lines.append(f"- Drive folder {a['drive_name']!r} matches DB rows: {ids}")
        lines.append("")

    if result["orphan_db"]:
        lines.append(
            f"## DB orphans — paper/null rows with no Drive folder "
            f"({len(result['orphan_db'])})"
        )
        lines.append(
            "_These research_projects rows have no matching subfolder in the "
            "lab Drive Projects tree.  Options: (a) rename the DB row to match "
            "a Drive folder, (b) create the Drive folder, (c) mark the row "
            "`status='dropped'`, (d) change `project_type` (e.g. 'database', "
            "'teaching') if it's not actually a paper project._"
        )
        lines.append("")
        for o in result["orphan_db"]:
            pt = o["project_type"] or "NULL"
            st = o["status"] or "?"
            lines.append(f"- `{o['id']}` — {o['name']}  [project_type={pt}, status={st}]")
        lines.append("")

    return title, "\n".join(lines)


def _write_briefing(title: str, body: str, urgency: str, metadata: dict) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO briefings
               (kind, urgency, title, content_md, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "lab_projects_sync",
            urgency,
            title,
            body,
            json.dumps(metadata),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job(apply: Optional[bool] = None, verbose: bool = False) -> str:
    """Reconcile the shared-Drive Projects folder with research_projects.

    Args:
        apply: True to write changes, False to dry-run, None to read config
               (defaults to True after first deploy — see tealc_config.json).
        verbose: if True, print the full briefing body.
    """
    if not os.path.isdir(PROJECTS_ROOT):
        return f"skipped: Projects folder not found at {PROJECTS_ROOT!r}"

    if apply is None:
        # Config default: apply unless user explicitly turned off
        try:
            with open(os.path.join(_PROJECT_ROOT, "data", "tealc_config.json")) as fh:
                cfg = json.load(fh)
            apply = not bool(
                cfg.get("jobs", {}).get(_JOB_NAME, {}).get("dry_run", False)
            )
        except Exception:
            apply = True

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        result = _sync(conn, apply=apply)
    finally:
        conn.close()

    title, body = _render_briefing(result, apply)
    metadata = {
        "mode": "applied" if apply else "dry-run",
        "counts": {k: len(v) if isinstance(v, list) else v
                   for k, v in result.items()},
        "drive_root": PROJECTS_ROOT,
    }
    urgency = "high" if result["orphan_db"] or result["ambiguous"] else "info"
    _write_briefing(title, body, urgency, metadata)

    if verbose:
        print(body)

    summary = (
        f"sync_lab_projects: active={result['drive_active_count']} "
        f"linked={len(result['linked'])} inserted={len(result['inserted'])} "
        f"promoted={len(result['promoted'])} dropped={len(result['marked_dropped'])} "
        f"orphans={len(result['orphan_db'])} ambiguous={len(result['ambiguous'])} "
        f"mode={'applied' if apply else 'dry-run'}"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="sync_lab_projects CLI")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply",   action="store_true", default=None,
                   help="Write DB changes.")
    g.add_argument("--dry-run", dest="dry_run", action="store_true",
                   default=False, help="Report only, no DB writes.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    apply = False if args.dry_run else (True if args.apply else None)
    os.environ.setdefault("FORCE_RUN", "1")
    print(job(apply=apply, verbose=args.verbose))
    sys.exit(0)
