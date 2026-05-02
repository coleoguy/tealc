"""project_sessions.py — per-project filesystem continuity artifacts.

Implements Anthropic's multi-session harness pattern:
  ~/Library/Application Support/tealc/memories/projects/<project_id>/
    progress.md       — running session log
    feature_list.json — deliverable checklist (optional)
    init.sh           — optional startup script (never auto-executed)

Public API (NOT @tool decorated — wrap in tools.py):
    start_project_session(project_id)  -> dict
    end_project_session(project_id, ...) -> dict
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths — same pattern as agent/scheduler.py
# ---------------------------------------------------------------------------
import os

try:
    from agent.scheduler import DB_PATH
except ImportError:
    _DEFAULT_DB_DIR = os.path.expanduser("~/Library/Application Support/tealc")
    DB_PATH = os.environ.get(
        "TEALC_DB_PATH",
        os.path.join(_DEFAULT_DB_DIR, "agent.db"),
    )

_MEMORIES_ROOT = Path(
    os.environ.get(
        "TEALC_MEMORIES_ROOT",
        os.path.expanduser("~/Library/Application Support/tealc/memories/projects"),
    )
)

# Only allow alphanumeric, underscore, and hyphen — no path traversal.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_dir(project_id: str) -> Path:
    """Return the storage directory for *project_id*.

    Raises ValueError if project_id contains unsafe characters.
    """
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"project_id {project_id!r} contains invalid characters. "
            "Only A-Z a-z 0-9 _ - are allowed."
        )
    return _MEMORIES_ROOT / project_id


def _progress_template(project_name: str) -> str:
    """Return the initial markdown template for a new progress.md."""
    return f"# {project_name}\n\n## Sessions\n\n_New sessions append below._\n"


def _fetch_project_row(project_id: str) -> dict[str, Any] | None:
    """Return the research_projects row for *project_id*, or None if not found."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        row = conn.execute(
            "SELECT id, name, status, current_hypothesis, next_action, last_touched_iso "
            "FROM research_projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "project_id": row[0],
        "name": row[1],
        "status": row[2],
        "current_hypothesis": row[3],
        "next_action": row[4],
        "last_touched_iso": row[5],
    }


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def start_project_session(project_id: str) -> dict:
    """Load (or initialise) the continuity artifacts for *project_id*.

    Returns a dict with:
        project_id, name, status, current_hypothesis, next_action,
        last_touched_iso, progress_md (raw string), feature_list (list),
        session_dir (str path), init_sh_path (str | None)

    On unknown project_id returns {"error": "...", "project_id": project_id}.
    On partial filesystem failure returns {"error": "...", ...} with whatever
    fields could be populated.
    """
    # --- validate project_id -------------------------------------------------
    try:
        sdir = _session_dir(project_id)
    except ValueError as exc:
        return {"error": str(exc), "project_id": project_id}

    # --- fetch DB row --------------------------------------------------------
    try:
        row = _fetch_project_row(project_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"DB error: {exc}", "project_id": project_id}

    if row is None:
        return {
            "error": f"No research_projects row found for project_id={project_id!r}",
            "project_id": project_id,
        }

    result: dict[str, Any] = dict(row)  # project_id, name, status, …
    result["session_dir"] = str(sdir)

    # --- ensure storage dir --------------------------------------------------
    try:
        sdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result["error"] = f"Could not create session dir {sdir}: {exc}"
        result["progress_md"] = ""
        result["feature_list"] = []
        result["init_sh_path"] = None
        return result

    # --- progress.md ---------------------------------------------------------
    progress_path = sdir / "progress.md"
    try:
        if progress_path.exists():
            progress_md = progress_path.read_text(encoding="utf-8")
        else:
            progress_md = _progress_template(row["name"])
            progress_path.write_text(progress_md, encoding="utf-8")
    except OSError as exc:
        result["error"] = f"progress.md error: {exc}"
        progress_md = ""
    result["progress_md"] = progress_md

    # --- feature_list.json ---------------------------------------------------
    feature_list_path = sdir / "feature_list.json"
    try:
        if feature_list_path.exists():
            raw = feature_list_path.read_text(encoding="utf-8")
            feature_list = json.loads(raw)
            if not isinstance(feature_list, list):
                feature_list = [feature_list]
        else:
            feature_list = []
    except (OSError, json.JSONDecodeError) as exc:
        feature_list = []
        result.setdefault("error", f"feature_list.json error: {exc}")
    result["feature_list"] = feature_list

    # --- init.sh (presence check only — never auto-execute) -----------------
    init_sh = sdir / "init.sh"
    result["init_sh_path"] = str(init_sh) if init_sh.exists() else None

    return result


def end_project_session(
    project_id: str,
    completed_md: str = "",
    remaining_md: str = "",
    notes_md: str = "",
) -> dict:
    """Append a timestamped section to progress.md and update the DB row.

    Appended format:
        ## YYYY-MM-DD HH:MM (session end)
        **Completed:** <completed_md>
        **Remaining:** <remaining_md>
        **Notes:** <notes_md>

    Updates research_projects.last_touched_iso and last_touched_by='Tealc'.

    Returns {"ok": True, "project_id": ..., "progress_md_path": ...,
             "appended_chars": N}
    or {"ok": False, "error": "...", "project_id": ...} on failure.
    """
    # --- validate project_id -------------------------------------------------
    try:
        sdir = _session_dir(project_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "project_id": project_id}

    # --- build the section to append ----------------------------------------
    now_utc = datetime.now(tz=timezone.utc)
    timestamp = now_utc.strftime("%Y-%m-%d %H:%M")
    iso_ts = now_utc.isoformat()

    section = (
        f"\n## {timestamp} (session end)\n"
        f"**Completed:** {completed_md or '—'}\n"
        f"**Remaining:** {remaining_md or '—'}\n"
        f"**Notes:** {notes_md or '—'}\n"
    )

    # --- ensure dir + write progress.md -------------------------------------
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        progress_path = sdir / "progress.md"

        if progress_path.exists():
            existing = progress_path.read_text(encoding="utf-8")
        else:
            # Fetch name for template; fall back gracefully
            try:
                row = _fetch_project_row(project_id)
                pname = row["name"] if row else project_id
            except Exception:  # noqa: BLE001
                pname = project_id
            existing = _progress_template(pname)

        updated = existing.rstrip("\n") + "\n" + section
        progress_path.write_text(updated, encoding="utf-8")
        appended_chars = len(section)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"Could not write progress.md: {exc}",
            "project_id": project_id,
        }

    # --- update DB -----------------------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE research_projects "
            "SET last_touched_iso = ?, last_touched_by = ? "
            "WHERE id = ?",
            (iso_ts, "Tealc", project_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        # Don't fail the whole call — progress.md was already written.
        return {
            "ok": True,
            "project_id": project_id,
            "progress_md_path": str(progress_path),
            "appended_chars": appended_chars,
            "db_warning": f"progress.md written but DB update failed: {exc}",
        }

    return {
        "ok": True,
        "project_id": project_id,
        "progress_md_path": str(progress_path),
        "appended_chars": appended_chars,
    }
