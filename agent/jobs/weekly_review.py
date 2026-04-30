"""Weekly self-review job — runs every Sunday at 7pm Central via APScheduler.

Reads what Tealc actually did over the past week (executive decisions, email triage,
briefings, intentions, job runs, grants) and writes a Sonnet-authored critique briefing
for the researcher's Monday morning.  This is the learning loop — the researcher uses it to tighten
behavioral rules in agent/graph.py.

Run manually to test:
    python -m agent.jobs.weekly_review
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# Load .env from project root (two levels up from agent/jobs/)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", ".env"))
load_dotenv(_ENV_PATH, override=True)

from anthropic import Anthropic  # noqa: E402 (after load_dotenv)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
NOTIFICATION_RATE_PATH = os.path.join(_DATA, "notification_rate.json")

# ---------------------------------------------------------------------------
# System prompt for Sonnet
# ---------------------------------------------------------------------------
REVIEW_SYSTEM_PROMPT = """\
You write the researcher's weekly Tealc self-review. the researcher has the goal of NAS membership \
and is drowning in responsibilities. Tealc just spent a week running scheduled jobs, \
classifying email, deciding what to do (in advisor mode for now). Your output is a \
briefing the researcher reads Monday morning.

Output structure (use these exact headers):
## What Tealc did this week
(One sentence per major activity — not a tool-call log, the meaningful work)
## What worked well
(2-3 bullets, specific. e.g., "Email triage correctly drafted reply to X — useful.")
## What was wasteful or wrong
(2-3 bullets. e.g., "Executive picked 'surface_briefing' 14 times this week despite no \
truly urgent briefings; the rule should require unsurfaced_count >= 3 OR an urgency=critical \
briefing before considering this action.")
## Concrete rule changes I recommend
(Bullet list of specific edits to `agent/graph.py` SYSTEM_PROMPT — actual sentences the researcher \
could paste in)
## Numbers
(Compact table: jobs run / errors, emails triaged by class, briefings created/surfaced, \
intentions added/completed/abandoned)
## Open questions for the researcher
(Things only the researcher can answer — e.g., "Do you want me to start auto-sending email drafts \
after you've approved 5 in a row?")

Be terse. Specific over vague. Don't pad. 600-800 words total."""


# ---------------------------------------------------------------------------
# Data-gathering helpers (each wrapped in try/except — never crash scheduler)
# ---------------------------------------------------------------------------

def _exec_decisions_stats(conn: sqlite3.Connection, week_start: str) -> dict:
    """Count executive decisions by action and pull 5 high-confidence non-nothing decisions."""
    try:
        action_counts = conn.execute(
            "SELECT action, COUNT(*) FROM executive_decisions "
            "WHERE decided_at > ? GROUP BY action ORDER BY COUNT(*) DESC",
            (week_start,),
        ).fetchall()
    except Exception as e:
        action_counts = []
        print(f"[weekly_review] exec action_counts error: {e}")

    try:
        top_decisions = conn.execute(
            "SELECT action, reasoning, confidence FROM executive_decisions "
            "WHERE decided_at > ? AND action != 'nothing' "
            "ORDER BY confidence DESC LIMIT 5",
            (week_start,),
        ).fetchall()
    except Exception as e:
        top_decisions = []
        print(f"[weekly_review] exec top_decisions error: {e}")

    return {
        "action_counts": [{"action": r[0], "count": r[1]} for r in action_counts],
        "top_non_nothing": [
            {"action": r[0], "reasoning": (r[1] or "")[:200], "confidence": r[2]}
            for r in top_decisions
        ],
    }


def _email_triage_stats(conn: sqlite3.Connection, week_start: str) -> dict:
    """Count email triage by classification, pull would_notify rows, and recent drafts."""
    try:
        class_counts = conn.execute(
            "SELECT classification, COUNT(*) FROM email_triage_decisions "
            "WHERE decided_at > ? GROUP BY classification ORDER BY COUNT(*) DESC",
            (week_start,),
        ).fetchall()
    except Exception as e:
        class_counts = []
        print(f"[weekly_review] triage class_counts error: {e}")

    try:
        would_notify = conn.execute(
            "SELECT from_email, subject, classification, reasoning, confidence "
            "FROM email_triage_decisions "
            "WHERE decided_at > ? AND would_notify=1 "
            "ORDER BY decided_at DESC LIMIT 25",
            (week_start,),
        ).fetchall()
    except Exception as e:
        would_notify = []
        print(f"[weekly_review] triage would_notify error: {e}")

    try:
        draft_rows = conn.execute(
            "SELECT COUNT(*), from_email, subject FROM email_triage_decisions "
            "WHERE decided_at > ? AND draft_id IS NOT NULL AND draft_id != '' "
            "GROUP BY from_email, subject "
            "ORDER BY COUNT(*) DESC LIMIT 3",
            (week_start,),
        ).fetchall()
        draft_count_row = conn.execute(
            "SELECT COUNT(*) FROM email_triage_decisions "
            "WHERE decided_at > ? AND draft_id IS NOT NULL AND draft_id != ''",
            (week_start,),
        ).fetchone()
        total_drafts = draft_count_row[0] if draft_count_row else 0
    except Exception as e:
        draft_rows = []
        total_drafts = 0
        print(f"[weekly_review] triage draft_rows error: {e}")

    return {
        "class_counts": [{"classification": r[0], "count": r[1]} for r in class_counts],
        "would_notify": [
            {
                "from_email": r[0],
                "subject": (r[1] or "")[:80],
                "classification": r[2],
                "reasoning": (r[3] or "")[:150],
                "confidence": r[4],
            }
            for r in would_notify
        ],
        "total_drafts": total_drafts,
        "recent_drafts": [
            {"from_email": r[1], "subject": (r[2] or "")[:80]}
            for r in draft_rows
        ],
    }


def _briefings_stats(conn: sqlite3.Connection, week_start: str) -> dict:
    """Count briefings by kind and surfaced vs unsurfaced."""
    try:
        kind_counts = conn.execute(
            "SELECT kind, COUNT(*) FROM briefings "
            "WHERE created_at > ? GROUP BY kind ORDER BY COUNT(*) DESC",
            (week_start,),
        ).fetchall()
    except Exception as e:
        kind_counts = []
        print(f"[weekly_review] briefings kind_counts error: {e}")

    try:
        surfaced = conn.execute(
            "SELECT COUNT(*) FROM briefings "
            "WHERE created_at > ? AND surfaced_at IS NOT NULL",
            (week_start,),
        ).fetchone()
        unsurfaced = conn.execute(
            "SELECT COUNT(*) FROM briefings "
            "WHERE created_at > ? AND surfaced_at IS NULL",
            (week_start,),
        ).fetchone()
    except Exception as e:
        surfaced = (0,)
        unsurfaced = (0,)
        print(f"[weekly_review] briefings surfaced error: {e}")

    return {
        "kind_counts": [{"kind": r[0], "count": r[1]} for r in kind_counts],
        "surfaced": surfaced[0] if surfaced else 0,
        "unsurfaced": unsurfaced[0] if unsurfaced else 0,
    }


def _intentions_stats(conn: sqlite3.Connection, week_start: str) -> dict:
    """Count intentions added, completed, abandoned, and still pending."""
    try:
        added = conn.execute(
            "SELECT COUNT(*) FROM intentions WHERE created_at > ?", (week_start,)
        ).fetchone()
        completed = conn.execute(
            "SELECT COUNT(*) FROM intentions WHERE completed_at > ?", (week_start,)
        ).fetchone()
        abandoned = conn.execute(
            "SELECT COUNT(*) FROM intentions "
            "WHERE status='abandoned' AND updated_at > ?",
            (week_start,),
        ).fetchone()
        still_pending = conn.execute(
            "SELECT COUNT(*) FROM intentions WHERE status='pending'"
        ).fetchone()
    except Exception as e:
        added = completed = abandoned = still_pending = (0,)
        print(f"[weekly_review] intentions stats error: {e}")

    return {
        "added": added[0] if added else 0,
        "completed": completed[0] if completed else 0,
        "abandoned": abandoned[0] if abandoned else 0,
        "still_pending": still_pending[0] if still_pending else 0,
    }


def _job_runs_stats(conn: sqlite3.Connection, week_start: str) -> dict:
    """Count successes/errors per job_name and pull error messages for failed runs."""
    try:
        counts = conn.execute(
            "SELECT job_name, status, COUNT(*) FROM job_runs "
            "WHERE started_at > ? GROUP BY job_name, status ORDER BY job_name",
            (week_start,),
        ).fetchall()
    except Exception as e:
        counts = []
        print(f"[weekly_review] job_runs counts error: {e}")

    try:
        errors = conn.execute(
            "SELECT job_name, started_at, error FROM job_runs "
            "WHERE started_at > ? AND status='error' "
            "ORDER BY started_at DESC LIMIT 10",
            (week_start,),
        ).fetchall()
    except Exception as e:
        errors = []
        print(f"[weekly_review] job_runs errors error: {e}")

    # Reorganise counts into {job_name: {status: count}}
    job_summary: dict = {}
    for job_name, status, count in counts:
        if job_name not in job_summary:
            job_summary[job_name] = {}
        job_summary[job_name][status] = count

    return {
        "job_summary": job_summary,
        "error_details": [
            {
                "job_name": r[0],
                "started_at": r[1],
                "error": (r[2] or "")[:300],
            }
            for r in errors
        ],
    }


def _students_needing_attention_count(conn: sqlite3.Connection) -> int:
    """Read current_context for students_needing_attention_count."""
    try:
        row = conn.execute(
            "SELECT students_needing_attention_count FROM current_context WHERE id=1"
        ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        print(f"[weekly_review] students_needing_attention error: {e}")
        return 0


def _grants_new_this_week(conn: sqlite3.Connection, week_start: str) -> int:
    """Count new grant opportunities added this week."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM grant_opportunities WHERE first_seen > ?",
            (week_start,),
        ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        print(f"[weekly_review] grants new error: {e}")
        return 0


def _notify_heath_calls(week_start: str) -> str:
    """Best-effort: check notification_rate.json for recent critical notifications.
    notify_heath has no DB table — this is deliberately best-effort."""
    try:
        if not os.path.exists(NOTIFICATION_RATE_PATH):
            return "notify_heath: no notification_rate.json found (best-effort metric unavailable)"
        with open(NOTIFICATION_RATE_PATH) as f:
            data = json.load(f)
        # notification_rate.json stores a rolling list of timestamps for rate-limiting
        timestamps = data.get("timestamps", [])
        recent = [t for t in timestamps if t >= week_start]
        return f"notify_heath: ~{len(recent)} critical notifications this week (from notification_rate.json rolling window)"
    except Exception as e:
        return f"notify_heath: best-effort read failed — {e}"


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("weekly_review")
def job() -> str:
    """Read the past week of Tealc activity and write a critique briefing for the researcher."""
    now_utc = datetime.now(timezone.utc)
    week_start = now_utc - timedelta(days=7)
    week_start_iso = week_start.isoformat()
    week_end_iso = now_utc.isoformat()
    week_start_date = week_start.strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        # --- Gather all stats ---
        exec_stats = _exec_decisions_stats(conn, week_start_iso)
        email_stats = _email_triage_stats(conn, week_start_iso)
        briefing_stats = _briefings_stats(conn, week_start_iso)
        intention_stats = _intentions_stats(conn, week_start_iso)
        job_stats = _job_runs_stats(conn, week_start_iso)
        students_count = _students_needing_attention_count(conn)
        new_grants = _grants_new_this_week(conn, week_start_iso)

        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        # Return a minimal summary so the tracker records success
        return f"weekly_review job failed at data-gather: {e}"

    notify_note = _notify_heath_calls(week_start_iso)

    # --- Compose input message for Sonnet ---
    input_msg = f"""WEEKLY TEALC SELF-REVIEW INPUT
Week: {week_start_date} → {week_end_iso[:10]}

=== EXECUTIVE DECISIONS (Haiku advisor loop) ===
Action counts this week:
{json.dumps(exec_stats['action_counts'], indent=2)}

5 most-confident non-nothing decisions (for inspection):
{json.dumps(exec_stats['top_non_nothing'], indent=2)}

=== EMAIL TRIAGE ===
Classification counts:
{json.dumps(email_stats['class_counts'], indent=2)}

Would-notify decisions (all, for sanity check):
{json.dumps(email_stats['would_notify'], indent=2)}

Drafts created: {email_stats['total_drafts']} total
Last 3 drafts: {json.dumps(email_stats['recent_drafts'], indent=2)}

=== BRIEFINGS ===
By kind: {json.dumps(briefing_stats['kind_counts'], indent=2)}
Surfaced: {briefing_stats['surfaced']} | Unsurfaced: {briefing_stats['unsurfaced']}

=== NOTIFICATIONS ===
{notify_note}
Note: notify_heath has no DB table; this metric is best-effort from notification_rate.json.

=== INTENTIONS ===
Added this week: {intention_stats['added']}
Completed this week: {intention_stats['completed']}
Abandoned this week: {intention_stats['abandoned']}
Still pending (all time): {intention_stats['still_pending']}

=== TOOL CALLS ===
Tool-call logging is not yet implemented in SQLite (aquarium writes go to JSON, not DB).
Skipping this metric — recommend adding as a future enhancement.

=== JOB RUNS ===
Successes/errors per job:
{json.dumps(job_stats['job_summary'], indent=2)}

Error details (up to 10):
{json.dumps(job_stats['error_details'], indent=2)}

=== STUDENTS NEEDING ATTENTION (current) ===
Count: {students_count}

=== GRANT OPPORTUNITIES ===
New this week: {new_grants}
"""

    # --- Call Sonnet 4.6 ---
    try:
        client = Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": input_msg}],
        )
        content_md: str = msg.content[0].text
    except Exception as e:
        content_md = f"[weekly_review] Sonnet call failed: {e}\n\nRaw stats:\n{input_msg}"

    # --- Insert briefing ---
    title = f"Tealc weekly self-review — week of {week_start_date}"
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('weekly_review', 'info', ?, ?, ?)",
            (title, content_md, now_utc.isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"weekly_review: briefing insert failed: {e}"

    # Compute summary counts
    total_jobs = sum(
        sum(v for v in counts.values())
        for counts in job_stats["job_summary"].values()
    )
    total_emails = sum(r["count"] for r in email_stats["class_counts"])
    total_briefings = sum(r["count"] for r in briefing_stats["kind_counts"])
    total_intentions = intention_stats["added"]

    return (
        f"reviewed: jobs={total_jobs} emails={total_emails} "
        f"briefings={total_briefings} intentions={total_intentions}"
    )


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
