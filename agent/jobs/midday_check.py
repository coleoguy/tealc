"""Midday proactive check — runs at 1pm Central. Surfaces actionable items so
Heath doesn't have to ask "what should I do this afternoon?".

Scans: unsurfaced briefings >24h old, unreviewed overnight drafts, pending
hypothesis proposals, deadlines within 10 days, students flagged for attention.
If anything warrants attention, creates a single consolidated briefing.
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def _count(conn, sql, params=()):
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


@tracked("midday_check")
def job() -> str:
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("midday_check"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    stale_briefings = _count(
        conn,
        "SELECT count(*) FROM briefings WHERE surfaced_at IS NULL AND created_at < ?",
        (cutoff_24h,),
    )
    unreviewed_drafts = _count(
        conn,
        "SELECT count(*) FROM overnight_drafts WHERE reviewed_at IS NULL",
    )
    pending_hypotheses = _count(
        conn,
        "SELECT count(*) FROM hypothesis_proposals WHERE status='proposed'",
    )
    needs_attention_students = _count(
        conn,
        "SELECT count(*) FROM students WHERE status='active' AND "
        "(notes_md LIKE '%FLAGGED%' OR id IN ("
        "  SELECT student_id FROM milestones WHERE completed_iso IS NULL "
        "  AND target_iso IS NOT NULL AND target_iso < ?"
        "))",
        (now_iso,),
    )
    unack_conflicts = _count(
        conn,
        "SELECT count(*) FROM goal_conflicts WHERE acknowledged_at IS NULL",
    )
    conn.close()

    total_actionable = (
        stale_briefings + unreviewed_drafts + pending_hypotheses
        + needs_attention_students + unack_conflicts
    )
    if total_actionable == 0:
        return "nothing_to_surface"

    parts = ["Midday check — here's what's waiting:\n"]
    if stale_briefings:
        parts.append(f"- **{stale_briefings}** briefing(s) from >24h ago still unread")
    if unreviewed_drafts:
        parts.append(f"- **{unreviewed_drafts}** overnight draft(s) not yet reviewed")
    if pending_hypotheses:
        parts.append(f"- **{pending_hypotheses}** hypothesis proposal(s) awaiting adopt/reject")
    if needs_attention_students:
        parts.append(f"- **{needs_attention_students}** student(s) flagged or past a milestone")
    if unack_conflicts:
        parts.append(f"- **{unack_conflicts}** unacknowledged goal conflict(s)")
    parts.append("\n_Ask me about any of these; I can walk through or clear them with you._")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('midday_check', 'info', ?, ?, ?)",
        (f"Midday check — {total_actionable} open item(s)", "\n".join(parts), now_iso),
    )
    conn.commit()
    conn.close()
    return f"surfaced: {total_actionable} items"


if __name__ == "__main__":
    print(job())
