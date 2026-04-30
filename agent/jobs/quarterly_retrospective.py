"""Quarterly retrospective job — runs on the first Sunday of Jan/Apr/Jul/Oct at 8pm Central.

Reads 90 days of goal portfolio activity and produces a deep retrospective via Sonnet 4.6.
Writes to both briefings and quarterly_retrospectives tables.

Run manually to test:
    python -m agent.jobs.quarterly_retrospective
"""
import json
import os
import re
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
# System prompt for Sonnet
# ---------------------------------------------------------------------------
RETRO_SYSTEM_PROMPT = """\
You write the researcher's quarterly retrospective on their goal portfolio. They have the goal \
of NAS membership and is drowning in responsibilities. The past 90 days of activity are \
summarized below. Your output is a deep review Heath reads on a Sunday evening — it should \
change how he thinks about the next quarter.

Output structure (use these exact headers, in this order):
## Past quarter at a glance
(3-4 sentences. Most important arc of the quarter.)
## What advanced
(2-3 specific goals or milestones that moved meaningfully. Be concrete — name papers, drafts, students.)
## What stalled
(1-2 specific items that didn't move and shouldn't have stalled. Honest.)
## NAS-trajectory delta
(Citation delta, h-index delta, papers landed/submitted, invited talks. Numbers.)
## Goal portfolio recommendations
Subsections:
- **Goals to drop** (1-3 specific goals by id and name with one-sentence reason)
- **Goals to add** (0-2 specific proposals with name + time_horizon + why)
- **Goals to re-prioritize** (1-2 specific importance changes with reason)
## Suggested rule changes
(1-2 specific edits to graph.py SYSTEM_PROMPT — actual sentences Heath could paste in)
## Question for Heath
(1 hard question only Heath can answer — e.g., "You added 4 service-related goals this quarter. \
Is that intentional drift, or do you need help saying no?")

Length: 800-1200 words. Be specific over vague. Don't pad."""


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def _quarter_of(dt: datetime) -> tuple[int, int]:
    """Return (year, quarter_number) for a datetime."""
    return dt.year, (dt.month - 1) // 3 + 1


def _quarter_start(year: int, q: int) -> datetime:
    """Return the UTC midnight start of a quarter."""
    month = (q - 1) * 3 + 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Data-gathering helpers (each wrapped in try/except — never crash scheduler)
# ---------------------------------------------------------------------------

def _goals_current(conn: sqlite3.Connection) -> list[dict]:
    """All goals: id, name, importance, nas_relevance, status."""
    try:
        rows = conn.execute(
            "SELECT id, name, importance, nas_relevance, status "
            "FROM goals ORDER BY importance DESC"
        ).fetchall()
        return [
            {"id": r[0], "name": r[1], "importance": r[2],
             "nas_relevance": r[3], "status": r[4]}
            for r in rows
        ]
    except Exception as e:
        print(f"[quarterly_retrospective] goals_current error: {e}")
        return []


def _milestones_in_quarter(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict:
    """Count milestones with target_iso in the quarter, grouped by status."""
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM milestones_v2 "
            "WHERE target_iso >= ? AND target_iso <= ? "
            "GROUP BY status",
            (start_iso[:10], end_iso[:10]),
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        print(f"[quarterly_retrospective] milestones_in_quarter error: {e}")
        return {}


def _decisions_log_entries(conn: sqlite3.Connection, start_iso: str) -> list[dict]:
    """All decisions_log entries from the quarter."""
    try:
        rows = conn.execute(
            "SELECT decided_iso, decision, reasoning, linked_goal_id "
            "FROM decisions_log WHERE decided_iso >= ? ORDER BY decided_iso",
            (start_iso,),
        ).fetchall()
        return [
            {"decided_iso": r[0], "decision": (r[1] or "")[:200],
             "reasoning": (r[2] or "")[:150], "linked_goal_id": r[3]}
            for r in rows
        ]
    except Exception as e:
        print(f"[quarterly_retrospective] decisions_log error: {e}")
        return []


def _exec_decisions_sample(conn: sqlite3.Connection, start_iso: str) -> list[dict]:
    """Count by action; return the 5 highest-confidence non-'nothing' actions."""
    try:
        counts = conn.execute(
            "SELECT action, COUNT(*) as cnt FROM executive_decisions "
            "WHERE decided_at >= ? GROUP BY action ORDER BY cnt DESC",
            (start_iso,),
        ).fetchall()
        top5 = conn.execute(
            "SELECT action, reasoning, confidence FROM executive_decisions "
            "WHERE decided_at >= ? AND action != 'nothing' "
            "ORDER BY confidence DESC LIMIT 5",
            (start_iso,),
        ).fetchall()
        return {
            "action_counts": [{"action": r[0], "count": r[1]} for r in counts],
            "top_5_confident": [
                {"action": r[0], "reasoning": (r[1] or "")[:200], "confidence": r[2]}
                for r in top5
            ],
        }
    except Exception as e:
        print(f"[quarterly_retrospective] exec_decisions_sample error: {e}")
        return {"action_counts": [], "top_5_confident": []}


def _email_triage_sample(conn: sqlite3.Connection, start_iso: str) -> dict:
    """Count by classification; service_request decisions specifically."""
    try:
        class_counts = conn.execute(
            "SELECT classification, COUNT(*) FROM email_triage_decisions "
            "WHERE decided_at >= ? GROUP BY classification ORDER BY COUNT(*) DESC",
            (start_iso,),
        ).fetchall()
        service_rows = conn.execute(
            "SELECT from_email, subject, service_recommendation, reasoning "
            "FROM email_triage_decisions "
            "WHERE decided_at >= ? AND classification='service_request' "
            "ORDER BY decided_at DESC LIMIT 10",
            (start_iso,),
        ).fetchall()
        return {
            "class_counts": [{"classification": r[0], "count": r[1]} for r in class_counts],
            "service_requests": [
                {"from_email": r[0], "subject": (r[1] or "")[:80],
                 "recommendation": r[2], "reasoning": (r[3] or "")[:150]}
                for r in service_rows
            ],
        }
    except Exception as e:
        print(f"[quarterly_retrospective] email_triage_sample error: {e}")
        return {"class_counts": [], "service_requests": []}


def _nas_metrics_delta(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict:
    """First and last NAS metric snapshots in the quarter; return deltas."""
    try:
        first = conn.execute(
            "SELECT snapshot_iso, total_citations, h_index FROM nas_metrics "
            "WHERE snapshot_iso >= ? AND snapshot_iso <= ? "
            "ORDER BY snapshot_iso ASC LIMIT 1",
            (start_iso, end_iso),
        ).fetchone()
        last = conn.execute(
            "SELECT snapshot_iso, total_citations, h_index FROM nas_metrics "
            "WHERE snapshot_iso >= ? AND snapshot_iso <= ? "
            "ORDER BY snapshot_iso DESC LIMIT 1",
            (start_iso, end_iso),
        ).fetchone()
        if first and last:
            citation_delta = (last[1] or 0) - (first[1] or 0)
            h_delta = (last[2] or 0) - (first[2] or 0)
            return {
                "start_snapshot": first[0],
                "end_snapshot": last[0],
                "start_citations": first[1],
                "end_citations": last[1],
                "citation_delta": citation_delta,
                "start_h_index": first[2],
                "end_h_index": last[2],
                "h_index_delta": h_delta,
            }
        # No snapshots in quarter; grab the most recent available
        latest = conn.execute(
            "SELECT snapshot_iso, total_citations, h_index FROM nas_metrics "
            "ORDER BY snapshot_iso DESC LIMIT 1"
        ).fetchone()
        if latest:
            return {
                "start_snapshot": None,
                "end_snapshot": latest[0],
                "start_citations": None,
                "end_citations": latest[1],
                "citation_delta": None,
                "start_h_index": None,
                "end_h_index": latest[2],
                "h_index_delta": None,
                "note": "No snapshots in quarter window; showing most recent available.",
            }
        return {}
    except Exception as e:
        print(f"[quarterly_retrospective] nas_metrics_delta error: {e}")
        return {}


def _intentions_stats(conn: sqlite3.Connection, start_iso: str) -> dict:
    """Count intentions added, completed, abandoned, still pending in the quarter."""
    try:
        added = conn.execute(
            "SELECT COUNT(*) FROM intentions WHERE created_at >= ?", (start_iso,)
        ).fetchone()
        completed = conn.execute(
            "SELECT COUNT(*) FROM intentions WHERE completed_at >= ?", (start_iso,)
        ).fetchone()
        abandoned = conn.execute(
            "SELECT COUNT(*) FROM intentions "
            "WHERE status='abandoned' AND updated_at >= ?", (start_iso,)
        ).fetchone()
        still_pending = conn.execute(
            "SELECT COUNT(*) FROM intentions WHERE status='pending'"
        ).fetchone()
        return {
            "added": added[0] if added else 0,
            "completed": completed[0] if completed else 0,
            "abandoned": abandoned[0] if abandoned else 0,
            "still_pending": still_pending[0] if still_pending else 0,
        }
    except Exception as e:
        print(f"[quarterly_retrospective] intentions_stats error: {e}")
        return {"added": 0, "completed": 0, "abandoned": 0, "still_pending": 0}


def _briefings_by_kind(conn: sqlite3.Connection, start_iso: str) -> list[dict]:
    """Count briefings by kind in the quarter."""
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM briefings "
            "WHERE created_at >= ? GROUP BY kind ORDER BY COUNT(*) DESC",
            (start_iso,),
        ).fetchall()
        return [{"kind": r[0], "count": r[1]} for r in rows]
    except Exception as e:
        print(f"[quarterly_retrospective] briefings_by_kind error: {e}")
        return []


# ---------------------------------------------------------------------------
# Best-effort JSON extraction from Sonnet output
# ---------------------------------------------------------------------------

def _extract_goals_to_drop(content_md: str) -> list[dict]:
    """Parse 'Goals to drop' bullets from Sonnet markdown. Best effort."""
    try:
        section = re.search(
            r"\*\*Goals to drop\*\*(.*?)(?:\*\*Goals to add\*\*|\*\*Goals to re-prioritize\*\*|##)",
            content_md, re.DOTALL | re.IGNORECASE,
        )
        if not section:
            return []
        lines = [l.strip().lstrip("- ").strip() for l in section.group(1).split("\n") if l.strip()]
        return [{"text": l[:300]} for l in lines if l]
    except Exception:
        return []


def _extract_goals_to_add(content_md: str) -> list[dict]:
    """Parse 'Goals to add' bullets from Sonnet markdown. Best effort."""
    try:
        section = re.search(
            r"\*\*Goals to add\*\*(.*?)(?:\*\*Goals to re-prioritize\*\*|##)",
            content_md, re.DOTALL | re.IGNORECASE,
        )
        if not section:
            return []
        lines = [l.strip().lstrip("- ").strip() for l in section.group(1).split("\n") if l.strip()]
        return [{"text": l[:300]} for l in lines if l]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("quarterly_retrospective")
def job() -> str:
    """Read 90 days of goal portfolio activity and write a quarterly retrospective briefing."""
    now_utc = datetime.now(timezone.utc)
    year, q = _quarter_of(now_utc)
    quarter_label = f"{year}-Q{q}"

    # Quarter window: 90 days back → now
    period_end = now_utc
    period_start = now_utc - timedelta(days=90)
    period_start_iso = period_start.isoformat()
    period_end_iso = period_end.isoformat()

    # Also compute the canonical quarter start for dedup check
    canon_q_start = _quarter_start(year, q)
    canon_q_start_iso = canon_q_start.isoformat()

    # -----------------------------------------------------------------------
    # Step 2 — Idempotency check: skip if retrospective already exists
    # -----------------------------------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = conn.execute(
            "SELECT 1 FROM briefings WHERE kind='quarterly_retrospective' AND created_at > ?",
            (canon_q_start_iso,),
        ).fetchone()
        conn.close()
        if existing:
            return f"retrospective: {quarter_label} already exists — skipping"
    except Exception as e:
        print(f"[quarterly_retrospective] dedup check error: {e}")

    # -----------------------------------------------------------------------
    # Step 3 — Pull data from past 90 days
    # -----------------------------------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        goals = _goals_current(conn)
        milestones = _milestones_in_quarter(conn, period_start_iso, period_end_iso)
        decisions = _decisions_log_entries(conn, period_start_iso)
        exec_stats = _exec_decisions_sample(conn, period_start_iso)
        email_stats = _email_triage_sample(conn, period_start_iso)
        nas_delta = _nas_metrics_delta(conn, period_start_iso, period_end_iso)
        intentions = _intentions_stats(conn, period_start_iso)
        briefing_kinds = _briefings_by_kind(conn, period_start_iso)

        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"quarterly_retrospective: data-gather failed: {e}"

    # -----------------------------------------------------------------------
    # Step 4 — Compose structured Sonnet input
    # -----------------------------------------------------------------------
    cd_str = str(nas_delta.get("citation_delta")) if nas_delta.get("citation_delta") is not None else "n/a"
    hd_str = str(nas_delta.get("h_index_delta")) if nas_delta.get("h_index_delta") is not None else "n/a"

    input_msg = f"""QUARTERLY RETROSPECTIVE INPUT
Quarter: {quarter_label}
Period: {period_start_iso[:10]} → {period_end_iso[:10]}

=== GOALS (current state, all) ===
{json.dumps(goals, indent=2)}

=== MILESTONES IN QUARTER (by status) ===
{json.dumps(milestones, indent=2)}

=== DECISIONS LOG (all entries in quarter) ===
{json.dumps(decisions, indent=2)}

=== EXECUTIVE DECISIONS SAMPLE ===
Action counts (quarter):
{json.dumps(exec_stats.get('action_counts', []), indent=2)}

5 highest-confidence non-nothing actions:
{json.dumps(exec_stats.get('top_5_confident', []), indent=2)}

=== EMAIL TRIAGE ===
Classification counts (quarter):
{json.dumps(email_stats.get('class_counts', []), indent=2)}

Service requests specifically (up to 10):
{json.dumps(email_stats.get('service_requests', []), indent=2)}

=== NAS METRICS DELTA ===
{json.dumps(nas_delta, indent=2)}

=== INTENTIONS ===
Added this quarter: {intentions['added']}
Completed this quarter: {intentions['completed']}
Abandoned this quarter: {intentions['abandoned']}
Still pending (all time): {intentions['still_pending']}

=== BRIEFINGS BY KIND (quarter) ===
{json.dumps(briefing_kinds, indent=2)}
"""

    # -----------------------------------------------------------------------
    # Step 5 — Call Sonnet 4.6
    # -----------------------------------------------------------------------
    try:
        client = Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=RETRO_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": input_msg}],
        )
        content_md: str = msg.content[0].text
    except Exception as e:
        content_md = f"[quarterly_retrospective] Sonnet call failed: {e}\n\nRaw stats:\n{input_msg}"

    # -----------------------------------------------------------------------
    # Steps 6 & 7 — Parse and save
    # -----------------------------------------------------------------------
    drops = _extract_goals_to_drop(content_md)
    adds = _extract_goals_to_add(content_md)
    citation_delta = nas_delta.get("citation_delta")
    h_index_delta = nas_delta.get("h_index_delta")

    title = f"Quarterly retrospective — Q{q} {year}"

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        # Ensure quarterly_retrospectives table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quarterly_retrospectives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quarter_label TEXT NOT NULL UNIQUE,
                period_start_iso TEXT NOT NULL,
                period_end_iso TEXT NOT NULL,
                summary_md TEXT NOT NULL,
                goals_to_drop_json TEXT,
                goals_to_add_json TEXT,
                citation_delta INTEGER,
                h_index_delta INTEGER,
                created_at TEXT NOT NULL
            )
        """)

        # Step 6 — Insert briefing row
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('quarterly_retrospective', 'warn', ?, ?, ?)",
            (title, content_md, now_utc.isoformat()),
        )

        # Step 7 — Insert quarterly_retrospectives row (UPSERT on quarter_label)
        conn.execute(
            """INSERT INTO quarterly_retrospectives
               (quarter_label, period_start_iso, period_end_iso, summary_md,
                goals_to_drop_json, goals_to_add_json, citation_delta, h_index_delta, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(quarter_label) DO UPDATE SET
                   summary_md=excluded.summary_md,
                   goals_to_drop_json=excluded.goals_to_drop_json,
                   goals_to_add_json=excluded.goals_to_add_json,
                   citation_delta=excluded.citation_delta,
                   h_index_delta=excluded.h_index_delta,
                   created_at=excluded.created_at
            """,
            (
                quarter_label,
                period_start_iso,
                period_end_iso,
                content_md,
                json.dumps(drops),
                json.dumps(adds),
                citation_delta,
                h_index_delta,
                now_utc.isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"quarterly_retrospective: DB write failed: {e}"

    # -----------------------------------------------------------------------
    # Step 8 — Return summary
    # -----------------------------------------------------------------------
    cd_display = f"+{citation_delta}" if (citation_delta is not None and citation_delta >= 0) else str(citation_delta) if citation_delta is not None else "n/a"
    return (
        f"retrospective: Q{q} {year} | citation_delta={cd_display} | "
        f"recommendations: drop={len(drops)} add={len(adds)}"
    )


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
