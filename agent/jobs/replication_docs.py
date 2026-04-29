"""Replication docs — weekly codebase snapshot; rewrites REPLICATION.md auto-sections.

Recommended schedule:
    CronTrigger(day_of_week="sun", hour=3, minute=0, timezone="America/Chicago")   # Sundays 3am

Run manually to test:
    python -m agent.jobs.replication_docs
"""
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_REPLICATION_MD = os.path.join(_PROJECT_ROOT, "REPLICATION.md")
_TOOLS_PY = os.path.join(_PROJECT_ROOT, "agent", "tools.py")
_JOBS_DIR = os.path.join(_PROJECT_ROOT, "agent", "jobs")
_SCHEDULER_PY = os.path.join(_PROJECT_ROOT, "agent", "scheduler.py")

_SKELETON = """\
# Tealc — Replication Guide

This document shows how to replicate Tealc from scratch.

<!-- AUTO-GENERATED-START:snapshot -->
<!-- AUTO-GENERATED-END:snapshot -->

## Architecture overview
(to be filled by the documentation pass)

<!-- AUTO-GENERATED-START:history -->
<!-- AUTO-GENERATED-END:history -->

## Setup-from-scratch
(to be filled by the documentation pass)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replication_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at     TEXT NOT NULL,
            tool_count      INTEGER NOT NULL,
            job_count       INTEGER NOT NULL,
            table_count     INTEGER NOT NULL,
            schema_version  TEXT NOT NULL,
            diff_from_previous TEXT
        )
    """)
    conn.commit()


def _count_tools() -> int:
    """Count @tool decorators in agent/tools.py."""
    try:
        with open(_TOOLS_PY) as f:
            content = f.read()
        return len(re.findall(r"^@tool\b", content, re.MULTILINE))
    except Exception:
        return 0


def _count_jobs() -> int:
    """Count .py files in agent/jobs/ excluding __init__.py."""
    try:
        files = [
            f for f in os.listdir(_JOBS_DIR)
            if f.endswith(".py") and f != "__init__.py"
        ]
        return len(files)
    except Exception:
        return 0


def _count_tables(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _schema_version() -> str:
    try:
        content = open(_SCHEDULER_PY, "rb").read()
        return hashlib.sha256(content).hexdigest()[:12]
    except Exception:
        return "unknown"


def _ascii_bar(value: int, max_val: int, width: int = 20) -> str:
    if max_val == 0:
        return ""
    filled = round(value / max_val * width)
    return "#" * filled + "-" * (width - filled)


def _build_history_block(snapshots: list) -> str:
    """Build ASCII bar chart for last 8 weekly snapshots.
    snapshots: list of (snapshot_at, tool_count, job_count, table_count) newest-first.
    """
    if not snapshots:
        return "_No historical snapshots yet._\n"

    max_tools = max(s[1] for s in snapshots) or 1
    max_jobs = max(s[2] for s in snapshots) or 1
    max_tables = max(s[3] for s in snapshots) or 1

    lines = ["```", f"{'Date':<12}  {'Tools':>5}  {'Jobs':>5}  {'Tables':>6}  Chart"]
    lines.append("-" * 70)
    for snapshot_at, tc, jc, tbl_c in reversed(snapshots):
        date_str = snapshot_at[:10] if snapshot_at else "?"
        bar_t = _ascii_bar(tc, max_tools, 10)
        bar_j = _ascii_bar(jc, max_jobs, 10)
        bar_tbl = _ascii_bar(tbl_c, max_tables, 10)
        lines.append(
            f"{date_str:<12}  {tc:>5}  {jc:>5}  {tbl_c:>6}  "
            f"T:{bar_t} J:{bar_j} DB:{bar_tbl}"
        )
    lines.append("```")
    return "\n".join(lines) + "\n"


def _rewrite_section(content: str, tag: str, new_body: str) -> str:
    """Replace text between AUTO-GENERATED markers (inserting markers if absent)."""
    start_marker = f"<!-- AUTO-GENERATED-START:{tag} -->"
    end_marker = f"<!-- AUTO-GENERATED-END:{tag} -->"

    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        # Re-insert both markers with new body at end of file
        content = content.rstrip() + f"\n\n{start_marker}\n{new_body}{end_marker}\n"
        return content

    # Replace text between markers, keeping markers in place
    before = content[:start_idx + len(start_marker)]
    after = content[end_idx:]
    return before + "\n" + new_body + after


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("replication_docs")
def job() -> str:
    now = datetime.now(timezone.utc)
    snapshot_at = now.isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)

    # 1. Measure the codebase
    tool_count = _count_tools()
    job_count = _count_jobs()
    table_count = _count_tables(conn)
    schema_ver = _schema_version()

    # 2. Compare to previous snapshot
    prev = conn.execute(
        "SELECT tool_count, job_count, table_count, schema_version "
        "FROM replication_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if prev:
        pt, pj, ptbl, psv = prev
        dt = tool_count - pt
        dj = job_count - pj
        dtbl = table_count - ptbl
        diff_from_previous = (
            f"+{dt} tools" if dt >= 0 else f"{dt} tools"
        ) + ", " + (
            f"+{dj} jobs" if dj >= 0 else f"{dj} jobs"
        ) + ", " + (
            f"+{dtbl} tables" if dtbl >= 0 else f"{dtbl} tables"
        ) + f", schema hash {psv[:8]}→{schema_ver[:8]}"
    else:
        diff_from_previous = "initial snapshot"

    # 3. INSERT snapshot
    conn.execute(
        "INSERT INTO replication_snapshots"
        "(snapshot_at, tool_count, job_count, table_count, schema_version, diff_from_previous) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (snapshot_at, tool_count, job_count, table_count, schema_ver, diff_from_previous),
    )
    conn.commit()

    # Fetch last 8 snapshots for history chart
    history_rows = conn.execute(
        "SELECT snapshot_at, tool_count, job_count, table_count "
        "FROM replication_snapshots ORDER BY id DESC LIMIT 8"
    ).fetchall()
    conn.close()

    # 4. Build snapshot block text
    # Format timestamp in local-ish display
    ts_display = now.strftime("%Y-%m-%d %H:%M UTC")
    snapshot_block = (
        f"_Last snapshot: {ts_display}_\n"
        f"_Schema version: `{schema_ver}`_\n\n"
        f"| Component | Count |\n"
        f"|---|---|\n"
        f"| Tools in `agent/tools.py` | {tool_count} |\n"
        f"| Jobs in `agent/jobs/` | {job_count} |\n"
        f"| SQLite tables in `data/agent.db` | {table_count} |\n\n"
        f"Diff from previous: {diff_from_previous}\n"
    )

    history_block = _build_history_block(history_rows)

    # 4. Read or create REPLICATION.md
    if not os.path.exists(_REPLICATION_MD):
        content = _SKELETON
    else:
        with open(_REPLICATION_MD) as f:
            content = f.read()

    # Rewrite both sections
    content = _rewrite_section(content, "snapshot", snapshot_block)
    content = _rewrite_section(content, "history", history_block)

    with open(_REPLICATION_MD, "w") as f:
        f.write(content)

    return (
        f"snapshot: tools={tool_count} jobs={job_count} tables={table_count} "
        f"schema={schema_ver}; diff={diff_from_previous}"
    )


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
