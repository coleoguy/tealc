"""Projects mirror — renders each active research_projects row as a wiki page.

Cron: daily 4am Central (APScheduler wiring is the PI's job per WIKI_V1_PLAN.md).

Idle gate: skips if the maximum last_touched_iso across all research_projects rows
hasn't changed since this job's last successful run.  Tracks the high-water mark
via the most recent output_ledger row for job_name='projects_mirror'.

Model: NONE — pure SQLite read + template render.  Zero LLM calls.

Algorithm:
  1. Connect to data/agent.db via agent.scheduler.DB_PATH.
  2. Discover the research_projects schema at runtime via PRAGMA table_info and
     only reference columns that actually exist.
  3. SELECT active projects (WHERE status='active' if status column exists; else all).
  4. For each row, render knowledge/projects/<project_id>.md.
  5. Also render knowledge/projects/index.md with a summary table.
  6. Query literature_notes WHERE project_id=? to list linked papers in each
     project page (skip silently if the join returns nothing).
  7. Write via website_git.stage_files() if available; otherwise write directly.
  8. Log one row to output_ledger and one row to cost_tracking.

Schema discovery: every field access is guarded by the runtime-discovered column
set, so the job is forward- and backward-compatible with schema migrations.

Directory: creates knowledge/projects/ if it doesn't exist.

Manual run:
    FORCE_RUN=1 PYTHONPATH=/path/to/00-Lab-Agent \
      python -m agent.jobs.projects_mirror

Dry-run (prints what would be written, writes nothing):
    FORCE_RUN=1 DRY_RUN=1 PYTHONPATH=/path/to/00-Lab-Agent \
      python -m agent.jobs.projects_mirror
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_WEBSITE_REPO = os.environ.get(
    "TEALC_WEBSITE_REPO",
    os.path.expanduser("~/Desktop/GitHub/lab-pages"),
)
_OUTPUT_DIR = Path(_WEBSITE_REPO) / "knowledge" / "projects"
_INDEX_FILE = _OUTPUT_DIR / "index.md"

_JOB_NAME = "projects_mirror"


# ---------------------------------------------------------------------------
# Schema discovery helpers
# ---------------------------------------------------------------------------

def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for *table*, or empty set if table missing."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    except Exception:
        return set()


def _row_to_dict(row: sqlite3.Row, columns: set[str]) -> dict:
    """Convert a sqlite3.Row to a plain dict, only including known columns."""
    return {k: row[k] for k in columns if k in row.keys()}


def _g(d: dict, *keys: str, default: str = "") -> str:
    """Get the first key from d that exists and is non-empty, else default."""
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return default


# ---------------------------------------------------------------------------
# Idle-gate helpers
# ---------------------------------------------------------------------------

def _get_last_run_iso() -> str | None:
    """Return the created_at timestamp of the most recent output_ledger row for
    this job, or None if never run."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT created_at FROM output_ledger WHERE job_name=? "
            "ORDER BY created_at DESC LIMIT 1",
            (_JOB_NAME,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_max_project_touched(conn: sqlite3.Connection, proj_cols: set[str]) -> str | None:
    """Return MAX(last_touched_iso) across all research_projects rows, or None."""
    col = "last_touched_iso" if "last_touched_iso" in proj_cols else None
    if col is None:
        col = "synced_at" if "synced_at" in proj_cols else None
    if col is None:
        return None
    try:
        row = conn.execute(f"SELECT MAX({col}) FROM research_projects").fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Literature-notes helpers
# ---------------------------------------------------------------------------

def _get_linked_papers(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Return literature_notes rows linked to project_id, if the column exists."""
    lit_cols = _get_columns(conn, "literature_notes")
    if "project_id" not in lit_cols:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM literature_notes WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.row_factory = None


def _paper_slug_from_doi(doi: str | None) -> str | None:
    """Convert a DOI to the wiki-slug format used in knowledge/papers/."""
    if not doi:
        return None
    return doi.replace("/", "_").replace(".", "_")


# ---------------------------------------------------------------------------
# Template renderers
# ---------------------------------------------------------------------------

def _render_project_page(proj: dict, proj_cols: set[str], papers: list[dict]) -> str:
    """Render the full markdown content for one project page."""
    project_id = _g(proj, "id")
    title = _g(proj, "name", "title")
    if not title:
        title = project_id.replace("_", " ").title()

    status = _g(proj, "status", default="unknown")
    current_hypothesis = _g(proj, "current_hypothesis")
    next_action = _g(proj, "next_action")
    keywords = _g(proj, "keywords")
    linked_artifact_id = _g(proj, "linked_artifact_id")
    last_updated = _g(proj, "last_touched_iso", "synced_at")
    description = _g(proj, "description")

    # Frontmatter
    fm_lines = [
        "---",
        "layout: default",
        f'title: "Active project · {title}"',
        f"project_id: {project_id}",
        f"status: {status}",
        f"permalink: /knowledge/projects/{project_id}/",
    ]
    if current_hypothesis:
        # Truncate long values to keep frontmatter readable
        hyp_short = current_hypothesis[:120].replace('"', "'")
        fm_lines.append(f'current_hypothesis: "{hyp_short}"')
    if next_action:
        na_short = next_action[:120].replace('"', "'")
        fm_lines.append(f'next_action: "{na_short}"')
    if keywords:
        fm_lines.append(f'keywords: "{keywords}"')
    if linked_artifact_id:
        fm_lines.append(f"linked_artifact_id: {linked_artifact_id}")
    if last_updated:
        fm_lines.append(f"last_updated: {last_updated}")
    fm_lines.append("---")

    body_lines: list[str] = [
        "",
        f"# {title}",
        "",
    ]

    if description:
        body_lines += [description, ""]

    # --- Tealc-owned project-status region ---
    body_lines += [
        "<!-- tealc:project-status-start -->",
        "",
        f"**Status:** {status}",
        "",
    ]
    if current_hypothesis:
        body_lines += [
            "**Current hypothesis:**",
            "",
            current_hypothesis,
            "",
        ]
    if next_action:
        body_lines += [
            "**Next action:**",
            "",
            next_action,
            "",
        ]
    if linked_artifact_id:
        body_lines += [f"**Linked artifact:** {linked_artifact_id}", ""]
    body_lines.append("<!-- tealc:project-status-end -->")
    body_lines.append("")

    # --- Literature region ---
    body_lines.append("<!-- tealc:project-lit-start -->")
    body_lines.append("")
    if papers:
        body_lines += ["## Linked literature", ""]
        for paper in papers:
            p_title = paper.get("title") or "Untitled"
            doi = paper.get("doi")
            slug = _paper_slug_from_doi(doi)
            if slug:
                body_lines.append(f"- [{p_title}](/knowledge/papers/{slug}/)")
            else:
                body_lines.append(f"- {p_title}")
        body_lines.append("")
    body_lines.append("<!-- tealc:project-lit-end -->")
    body_lines.append("")

    # --- User notes region ---
    body_lines += [
        "<!-- user-start -->",
        "",
        "<!-- user-end -->",
        "",
    ]

    return "\n".join(fm_lines + body_lines)


def _render_index_page(projects: list[dict], now_iso: str) -> str:
    """Render the projects/index.md summary table."""
    lines: list[str] = [
        "---",
        "layout: default",
        'title: "Research projects"',
        "permalink: /knowledge/projects/",
        f"last_updated: {now_iso}",
        "---",
        "",
        "# Research projects",
        "",
        "Live mirror of the lab's active research projects from Tealc's operational DB.",
        "",
    ]

    if not projects:
        lines.append("No active projects to display.")
        lines.append("")
        return "\n".join(lines)

    # Table header
    lines += [
        "| Project | Status | Current hypothesis | Last updated |",
        "|---|---|---|---|",
    ]
    for proj in sorted(projects, key=lambda p: _g(p, "name", "id")):
        project_id = _g(proj, "id")
        title = _g(proj, "name", default=project_id.replace("_", " ").title())
        status = _g(proj, "status", default="—")
        hyp = _g(proj, "current_hypothesis")
        hyp_short = textwrap.shorten(hyp, width=80, placeholder="…") if hyp else "—"
        last_upd = _g(proj, "last_touched_iso", "synced_at", default="—")
        link = f"[{title}](/knowledge/projects/{project_id}/)"
        lines.append(f"| {link} | {status} | {hyp_short} | {last_upd} |")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _run(dry_run: bool = False) -> str:
    """Main logic, separated from the @tracked wrapper for testability."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # Discover schema
    proj_cols = _get_columns(conn, "research_projects")
    if not proj_cols:
        conn.close()
        return "noop: research_projects table not found"

    # Idle gate: skip if nothing has changed since last run
    last_run_iso = _get_last_run_iso()
    max_touched = _get_max_project_touched(conn, proj_cols)
    if last_run_iso and max_touched and max_touched <= last_run_iso:
        conn.close()
        return f"noop: no projects updated since last run ({max_touched} <= {last_run_iso})"

    # Fetch rows — only paper projects get a wiki page.  Grants moved to their
    # own `grants` table on 2026-04-24; databases + teaching remain in
    # research_projects but are excluded here (they'll get their own surface).
    # project_type IS NULL is allowed so legacy rows still render until the
    # Drive-sync audit promotes them to 'paper' (or the user reclassifies).
    conn.row_factory = sqlite3.Row
    has_type = "project_type" in proj_cols
    has_status = "status" in proj_cols

    where = []
    if has_type:
        where.append("(project_type='paper' OR project_type IS NULL)")
    if has_status:
        where.append("status='active'")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM research_projects{clause} ORDER BY id"
    ).fetchall()

    if not rows and has_status and has_type:
        # Fall back: any paper project (regardless of status)
        rows = conn.execute(
            "SELECT * FROM research_projects "
            "WHERE project_type='paper' OR project_type IS NULL "
            "ORDER BY id"
        ).fetchall()
    elif not rows:
        rows = conn.execute("SELECT * FROM research_projects ORDER BY id").fetchall()

    projects = [dict(r) for r in rows]

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    files_to_write: dict[str, str] = {}

    if not projects:
        # Write a minimal index page only
        files_to_write["knowledge/projects/index.md"] = _render_index_page([], now_iso)
        conn.close()
        _write_files(files_to_write, dry_run)
        return "noop: research_projects has zero rows; wrote empty index"

    # Render per-project pages
    expected_ids: set[str] = set()
    for proj in projects:
        project_id = proj.get("id") or ""
        if not project_id:
            continue
        expected_ids.add(project_id)
        conn.row_factory = sqlite3.Row  # re-set after dict fetches
        papers = _get_linked_papers(conn, project_id)
        conn.row_factory = None
        content = _render_project_page(proj, proj_cols, papers)
        files_to_write[f"knowledge/projects/{project_id}.md"] = content

    # Render index
    files_to_write["knowledge/projects/index.md"] = _render_index_page(projects, now_iso)

    conn.close()

    # Identify stale pages — anything in knowledge/projects/ that is NOT in the
    # expected set and is not the index itself.  This cleans up pages for
    # projects whose type changed (e.g. 'paper' → 'grant' on 2026-04-24),
    # whose status flipped to 'dropped', or whose rows were deleted.
    stale_to_delete: list[str] = []
    projects_dir = Path(_WEBSITE_REPO) / "knowledge" / "projects"
    if projects_dir.is_dir():
        for md in projects_dir.glob("*.md"):
            if md.name == "index.md":
                continue
            pid = md.stem
            if pid not in expected_ids:
                stale_to_delete.append(str(md.relative_to(Path(_WEBSITE_REPO))))

    _write_files(files_to_write, dry_run)
    _delete_stale_pages(stale_to_delete, dry_run)

    stale_msg = f", pruned {len(stale_to_delete)} stale" if stale_to_delete else ""
    return (
        f"{'[DRY-RUN] ' if dry_run else ''}projects_mirror: "
        f"rendered {len(projects)} project(s) + index{stale_msg}; "
        f"{len(files_to_write)} file(s) {'would be written' if dry_run else 'written'}"
    )


def _delete_stale_pages(rel_paths: list[str], dry_run: bool) -> None:
    """Remove pages for projects no longer in the rendered set.  Always a
    no-op for `index.md` (protected by the caller)."""
    if not rel_paths:
        return
    if dry_run:
        for p in rel_paths:
            print(f"Would delete stale page: {p}")
        return
    for rel in rel_paths:
        dest = Path(_WEBSITE_REPO) / rel
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[projects_mirror] failed to delete {rel}: {exc}")


def _write_files(files: dict[str, str], dry_run: bool) -> None:
    """Write files via website_git or directly; or just print in dry-run mode."""
    if dry_run:
        for rel, content in files.items():
            print(f"\n{'='*60}")
            print(f"Would write: {rel}")
            print(f"{'='*60}")
            print(content[:800] + (" [...]" if len(content) > 800 else ""))
        return

    # Ensure output dir exists (safe even if writing via git)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Try website_git first
    written_via_git = False
    try:
        from agent.jobs.website_git import stage_files  # noqa: PLC0415
        stage_files(files)
        written_via_git = True
    except Exception:
        pass

    if not written_via_git:
        for rel, content in files.items():
            dest = Path(_WEBSITE_REPO) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job() -> str:
    """Daily 4am Central: mirror research_projects to wiki pages."""
    # Control-tab gate
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle(_JOB_NAME):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # Working-hours guard
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and not os.environ.get("FORCE_RUN"):
        return f"skipped: working-hours guard (hour={hour} CT; set FORCE_RUN=1 to bypass)"

    dry_run = bool(os.environ.get("DRY_RUN"))
    summary = _run(dry_run=dry_run)

    # Audit logs (always written, even in dry-run)
    record_output(
        kind="wiki_surface",
        job_name=_JOB_NAME,
        model="none",
        project_id=None,
        content_md=summary,
        tokens_in=0,
        tokens_out=0,
        provenance={"status": "ok" if "noop" not in summary else "noop", "dry_run": dry_run},
    )
    record_call(
        job_name=_JOB_NAME,
        model="none",
        usage={"input_tokens": 0, "output_tokens": 0},
    )

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Projects mirror job")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without actually writing files.",
    )
    args = parser.parse_args()

    import sys
    os.environ["FORCE_RUN"] = "1"
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    result = job()
    print(result)
    sys.exit(0 if "error" not in str(result).lower() else 1)
