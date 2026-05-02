"""Open questions board — renders hypothesis_proposals as a browseable wiki page.

Cron: daily 6am Central (APScheduler wiring is the PI's job per WIKI_V1_PLAN.md).

Idle gate: skips if no row in hypothesis_proposals has been added or updated
since the last successful run.  Tracks the high-water mark via the most recent
output_ledger row for job_name='open_questions_index'.

Model: NONE — pure SQLite read + template render.  Zero LLM calls.

Algorithm:
  1. Connect to data/agent.db via agent.scheduler.DB_PATH.
  2. Discover hypothesis_proposals schema at runtime via PRAGMA table_info.
  3. SELECT proposals WHERE status IN ('proposed', 'adopted') if status column
     exists; otherwise SELECT all rows ordered by id DESC (most recent first).
  4. Group by status if the column exists; otherwise render as a single list.
  5. Render knowledge/questions/index.md.
  6. Write via website_git.stage_files() if available; otherwise write directly.
  7. Log one row to output_ledger and one row to cost_tracking.

Directory: creates knowledge/questions/ if it doesn't exist.

Manual run:
    FORCE_RUN=1 PYTHONPATH=/path/to/00-Lab-Agent \
      python -m agent.jobs.open_questions_index
"""
from __future__ import annotations

import os
import sqlite3
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
_OUTPUT_DIR = Path(_WEBSITE_REPO) / "knowledge" / "questions"
_OUTPUT_FILE = _OUTPUT_DIR / "index.md"

_JOB_NAME = "open_questions_index"

# Statuses we surface; anything else (e.g. 'rejected', 'archived') is excluded.
_TARGET_STATUSES = ("proposed", "adopted")

# Human-readable labels for each status bucket
_STATUS_LABELS = {
    "proposed": "Proposed",
    "adopted": "Adopted",
}


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------

def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for *table*, or empty set if table missing."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Idle-gate helper
# ---------------------------------------------------------------------------

def _get_last_run_iso() -> str | None:
    """Return created_at of the most recent output_ledger row for this job."""
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


def _get_max_proposal_ts(conn: sqlite3.Connection, cols: set[str]) -> str | None:
    """Return the most recent timestamp across hypothesis_proposals, or None."""
    # Prefer proposed_iso; fall back to rowid surrogate via MAX(id) cast
    if "proposed_iso" in cols:
        try:
            row = conn.execute(
                "SELECT MAX(proposed_iso) FROM hypothesis_proposals"
            ).fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            pass
    try:
        row = conn.execute("SELECT MAX(id) FROM hypothesis_proposals").fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _format_entry(row: dict, cols: set[str], index: int) -> list[str]:
    """Render one hypothesis_proposals row as a numbered-list item block."""
    hyp = row.get("hypothesis_md") or row.get("hypothesis") or "(no text)"

    # Metadata line
    meta_parts: list[str] = []
    if "proposed_iso" in cols and row.get("proposed_iso"):
        meta_parts.append(f"proposed: {row['proposed_iso'][:10]}")
    if "project_id" in cols and row.get("project_id"):
        meta_parts.append(f"project: `{row['project_id']}`")
    if "novelty_score" in cols and row.get("novelty_score") is not None:
        meta_parts.append(f"novelty: {row['novelty_score']:.1f}")
    if "feasibility_score" in cols and row.get("feasibility_score") is not None:
        meta_parts.append(f"feasibility: {row['feasibility_score']:.1f}")
    if "cited_paper_dois" in cols and row.get("cited_paper_dois"):
        doi_list = row["cited_paper_dois"].strip()
        if doi_list:
            meta_parts.append(f"based on: {doi_list[:80]}")

    lines = [f"{index}. {hyp}"]
    if meta_parts:
        lines.append(f"   _{'; '.join(meta_parts)}_")
    lines.append("")
    return lines


def _render_index(
    grouped: dict[str, list[dict]],
    cols: set[str],
    now_iso: str,
) -> str:
    """Render the full open-questions index page."""
    lines: list[str] = [
        "---",
        "layout: default",
        'title: "Open research questions"',
        "permalink: /knowledge/questions/",
        f"last_updated: {now_iso}",
        "---",
        "",
        "# Open research questions",
        "",
        "Hypotheses the lab thinks are worth testing. Each is generated either "
        "from the literature synthesis overnight job or by Heath directly.",
        "",
    ]

    if not grouped:
        lines.append("No open questions currently queued.")
        lines.append("")
        return "\n".join(lines)

    # Render in preferred order: adopted first, then proposed
    order = [s for s in ("adopted", "proposed") if s in grouped]
    # Any remaining statuses (in case of ungrouped / status=None bucket)
    order += [s for s in sorted(grouped.keys()) if s not in order]

    for status_key in order:
        proposals = grouped[status_key]
        label = _STATUS_LABELS.get(status_key, status_key.title())
        lines += [f"## {label}", ""]
        for i, row in enumerate(proposals, start=1):
            lines += _format_entry(row, cols, i)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _run() -> str:
    """Main logic, separated from the @tracked wrapper."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    cols = _get_columns(conn, "hypothesis_proposals")
    if not cols:
        conn.close()
        return "noop: hypothesis_proposals table not found"

    # Idle gate
    last_run_iso = _get_last_run_iso()
    max_ts = _get_max_proposal_ts(conn, cols)
    if last_run_iso and max_ts and max_ts <= last_run_iso:
        conn.close()
        return f"noop: no hypothesis_proposals updated since last run ({max_ts} <= {last_run_iso})"

    # Fetch proposals
    conn.row_factory = sqlite3.Row
    if "status" in cols:
        placeholders = ",".join("?" * len(_TARGET_STATUSES))
        rows = conn.execute(
            f"SELECT * FROM hypothesis_proposals WHERE status IN ({placeholders}) "
            f"ORDER BY id DESC",
            _TARGET_STATUSES,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM hypothesis_proposals ORDER BY id DESC"
        ).fetchall()

    conn.close()

    proposals = [dict(r) for r in rows]

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    if not proposals:
        # Empty-input guard: do NOT publish a blank page to the live website.
        # Pre-fix behavior wrote an empty rendered page when hypothesis_proposals
        # had zero matching rows; reviewers / readers saw a noop on the public
        # site. Better to leave the previously-published page in place (if any)
        # and surface the noop in job_runs / briefings.
        return "noop: hypothesis_proposals has zero matching rows; skipping publish"

    # Group by status
    grouped: dict[str, list[dict]] = {}
    if "status" in cols:
        for row in proposals:
            s = row.get("status") or "proposed"
            grouped.setdefault(s, []).append(row)
    else:
        grouped["all"] = proposals

    rendered = _render_index(grouped, cols, now_iso)
    _write_output(rendered)

    total = len(proposals)
    return (
        f"open_questions_index: rendered {total} proposal(s) across "
        f"{len(grouped)} status bucket(s)"
    )


def _write_output(content: str) -> None:
    """Write the rendered content, preferring website_git over direct write."""
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rel_path = str(_OUTPUT_FILE.relative_to(Path(_WEBSITE_REPO)))

    written_via_git = False
    try:
        from agent.jobs.website_git import stage_files  # noqa: PLC0415
        stage_files({rel_path: content})
        written_via_git = True
    except Exception:
        pass

    if not written_via_git:
        _OUTPUT_FILE.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job() -> str:
    """Daily 6am Central: render hypothesis_proposals as open-questions board."""
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

    summary = _run()

    # Audit logs
    status = "noop" if summary.startswith("noop") else "ok"
    record_output(
        kind="wiki_surface",
        job_name=_JOB_NAME,
        model="none",
        project_id=None,
        content_md=summary,
        tokens_in=0,
        tokens_out=0,
        provenance={"status": status},
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
    import sys
    os.environ["FORCE_RUN"] = "1"
    result = job()
    print(result)
    sys.exit(0 if "error" not in str(result).lower() else 1)
