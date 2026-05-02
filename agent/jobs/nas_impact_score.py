"""NAS impact score job — runs every Sunday at 8pm Central via APScheduler.

Classifies last week's activity by which goal it advanced and produces
a NAS-impact breakdown: trajectory%, service_drag%, maintenance%, unattributed%.
Writes to nas_impact_weekly table and creates a briefing for the next session.

Run manually to test:
    python -m agent.jobs.nas_impact_score
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

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
from agent.jobs import SCIENTIST_MODE  # noqa: E402

IMPACT_SYSTEM_PROMPT = SCIENTIST_MODE + "\n\n" + """\
You write the researcher's weekly recognition-impact summary. He has the goal \
of national recognition and is drowning in responsibilities. The data below \
shows what Tealc and Heath spent the week on, attributed to goals.

Output structure (use these headers exactly):
## This week, by recognition-impact
(4-5 bullets — be specific with percentages. e.g., "62% of activity went to \
recognition-trajectory goals, dominated by Google grant work and the \
chromosomal stasis paper push.")
## What advanced national-recognition trajectory
(2-3 specific items)
## Where time leaked
(1-2 items that were unattributed or service-drag — be honest, don't soften)
## Suggestion for next week
(1 specific behavioral nudge — actionable, not aspirational. If the data don't \
support one, say so rather than padding.)

Don't soft-pedal where time was wasted; that's the whole point of the review. \
Be terse. 300-500 words total. Output ONLY the markdown, no JSON."""


# ---------------------------------------------------------------------------
# Week boundary helpers
# ---------------------------------------------------------------------------

def _week_bounds() -> tuple[date, date]:
    """Return (last_monday, last_sunday) as date objects.
    If today is Sunday, 'last Monday' is the Monday 6 days ago so the full
    Mon–Sun week is complete."""
    today = datetime.now(timezone.utc).date()
    # isoweekday: Mon=1, Sun=7
    days_since_monday = today.isoweekday() - 1   # 0 on Mon, 6 on Sun
    last_monday = today - timedelta(days=days_since_monday + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _to_iso(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# Activity gathering helpers
# ---------------------------------------------------------------------------

def _load_goals(conn: sqlite3.Connection) -> list[dict]:
    """Return all goals with id, name, nas_relevance, and lowercased name for matching."""
    try:
        rows = conn.execute(
            "SELECT id, name, nas_relevance, why, success_metric FROM goals"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1] or "",
                "nas_relevance": r[2] or "med",
                "keywords": ((r[1] or "") + " " + (r[2] or "") + " " + (r[3] or "")).lower(),
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_impact] load_goals error: {e}")
        return []


def _match_goal(text: str, goals: list[dict]) -> str | None:
    """Heuristic: find the goal whose name/keywords appear in text.
    Returns goal_id of best match, or None."""
    if not text:
        return None
    text_lower = text.lower()
    best_id = None
    best_len = 0
    for g in goals:
        # Use goal name as the primary signal — longer name matches are more specific
        if g["name"] and g["name"] in text_lower:
            if len(g["name"]) > best_len:
                best_len = len(g["name"])
                best_id = g["id"]
    if best_id:
        return best_id
    # Fallback: keyword overlap
    for g in goals:
        for kw in g["keywords"].split():
            if len(kw) >= 5 and kw in text_lower:
                return g["id"]
    return None


def _get_executive_decisions(conn: sqlite3.Connection, week_start_iso: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, decided_at, action, reasoning, linked_goal_id "
            "FROM executive_decisions "
            "WHERE decided_at >= ? AND action != 'nothing'",
            (week_start_iso,),
        ).fetchall()
        return [
            {
                "source": "executive_decision",
                "id": r[0],
                "ts": r[1],
                "text": f"{r[2]} {r[3] or ''}",
                "linked_goal_id": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_impact] executive_decisions error: {e}")
        return []


def _get_email_triage(conn: sqlite3.Connection, week_start_iso: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, decided_at, subject, reasoning, classification "
            "FROM email_triage_decisions "
            "WHERE decided_at >= ? AND classification IN ('drafts_reply', 'notify')",
            (week_start_iso,),
        ).fetchall()
        return [
            {
                "source": "email_triage",
                "id": r[0],
                "ts": r[1],
                "text": f"{r[2] or ''} {r[3] or ''}",
                "linked_goal_id": None,
                "classification": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_impact] email_triage error: {e}")
        return []


def _get_briefings(conn: sqlite3.Connection, week_start_iso: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, created_at, kind, title, content_md "
            "FROM briefings "
            "WHERE kind != 'morning' AND created_at >= ?",
            (week_start_iso,),
        ).fetchall()
        return [
            {
                "source": "briefing",
                "id": r[0],
                "ts": r[1],
                "text": f"{r[3] or ''} {(r[4] or '')[:200]}",
                "linked_goal_id": None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_impact] briefings error: {e}")
        return []


def _get_intentions(conn: sqlite3.Connection, week_start_iso: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, completed_at, description, kind "
            "FROM intentions "
            "WHERE status='done' AND completed_at >= ?",
            (week_start_iso,),
        ).fetchall()
        return [
            {
                "source": "intention",
                "id": r[0],
                "ts": r[1],
                "text": r[2] or "",
                "linked_goal_id": None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_impact] intentions error: {e}")
        return []


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _classify_item(item: dict, goals: list[dict]) -> tuple[str, str]:
    """Return (goal_id_or_sentinel, attribution_method).

    goal_id_or_sentinel:
      - a real goal id  → check nas_relevance
      - 'service_drag'  → counts as drag
      - 'unattributed'  → counts as unattributed

    attribution_method: 'linked_goal_id' | 'heuristic' | 'unattributed'
    """
    # 1) Explicit linked_goal_id wins
    lgid = item.get("linked_goal_id")
    if lgid:
        return lgid, "linked_goal_id"

    # 2) Email items that are 'requires_human' → service_drag
    #    (spec: "unattributed requires_human email triage as service_drag")
    #    We map: email_triage items with no goal match → service_drag
    if item["source"] == "email_triage":
        match = _match_goal(item["text"], goals)
        if match:
            return match, "heuristic"
        return "service_drag", "unattributed"

    # 3) Heuristic text match
    match = _match_goal(item["text"], goals)
    if match:
        return match, "heuristic"

    return "unattributed", "unattributed"


def _nas_bucket(goal_id: str, goals_by_id: dict) -> str:
    """Return 'nas_trajectory', 'maintenance', 'service_drag', or 'unattributed'."""
    if goal_id == "service_drag":
        return "service_drag"
    if goal_id == "unattributed":
        return "unattributed"
    goal = goals_by_id.get(goal_id)
    if not goal:
        return "unattributed"
    relevance = goal.get("nas_relevance", "med")
    if relevance == "high":
        return "nas_trajectory"
    # med or low → maintenance
    return "maintenance"


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("nas_impact_score")
def job() -> str:
    """Classify last week's activity by NAS-impact and write summary briefing."""
    now_utc = datetime.now(timezone.utc)

    # 1. Compute week bounds (last complete Mon–Sun)
    week_start, week_end = _week_bounds()
    week_start_iso = _to_iso(week_start)
    week_end_iso = _to_iso(week_end)

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        # 2. Skip if already computed for this week
        existing = conn.execute(
            "SELECT id FROM nas_impact_weekly WHERE week_start_iso=?",
            (week_start_iso,),
        ).fetchone()
        if existing:
            conn.close()
            return f"nas_impact: already computed for {week_start_iso} — skipped"

        # 3. Load goals index
        goals = _load_goals(conn)
        goals_by_id = {g["id"]: g for g in goals}

        # Build week_start datetime string for DB comparisons (use ISO date prefix)
        week_start_dt = week_start_iso + "T00:00:00"

        # 3. Pull activity
        exec_items = _get_executive_decisions(conn, week_start_dt)
        email_items = _get_email_triage(conn, week_start_dt)
        briefing_items = _get_briefings(conn, week_start_dt)
        intention_items = _get_intentions(conn, week_start_dt)

        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"nas_impact: data-gather failed: {e}"

    all_items = exec_items + email_items + briefing_items + intention_items
    total = len(all_items)

    if total == 0:
        # Nothing happened this week — write a zero row and a briefing
        summary_md = (
            "## This week, by NAS-impact\n"
            "- No activity recorded this week.\n\n"
            "## What advanced NAS-trajectory\n- Nothing recorded.\n\n"
            "## Where time leaked\n- No data.\n\n"
            "## Suggestion for next week\n"
            "- Start at least one deep-work block on the chromosomal stasis paper.\n"
        )
    else:
        # 4 & 5. Attribute each item and classify
        buckets: dict[str, int] = {
            "nas_trajectory": 0,
            "service_drag": 0,
            "maintenance": 0,
            "unattributed": 0,
        }
        goal_breakdown: dict[str, int] = {}
        attribution_methods: dict[str, int] = {
            "linked_goal_id": 0,
            "heuristic": 0,
            "unattributed": 0,
        }
        top_items: list[dict] = []  # for Sonnet input

        for item in all_items:
            gid, method = _classify_item(item, goals)
            attribution_methods[method] = attribution_methods.get(method, 0) + 1
            bucket = _nas_bucket(gid, goals_by_id)
            buckets[bucket] = buckets.get(bucket, 0) + 1
            if gid not in ("service_drag", "unattributed"):
                goal_breakdown[gid] = goal_breakdown.get(gid, 0) + 1
            if len(top_items) < 5:
                goal_name = goals_by_id[gid]["name"] if gid in goals_by_id else gid
                top_items.append({
                    "source": item["source"],
                    "text": item["text"][:120],
                    "goal": goal_name,
                    "bucket": bucket,
                    "method": method,
                })

        # 6. Compute percentages
        def pct(n: int) -> float:
            return round(100.0 * n / total, 1) if total > 0 else 0.0

        nas_pct = pct(buckets["nas_trajectory"])
        drag_pct = pct(buckets["service_drag"])
        maint_pct = pct(buckets["maintenance"])
        unattr_pct = pct(buckets["unattributed"])

        # Top goals by count
        top_goals_str = ", ".join(
            f"{goals_by_id[gid]['name']} ({cnt})"
            for gid, cnt in sorted(goal_breakdown.items(), key=lambda x: -x[1])[:5]
            if gid in goals_by_id
        ) or "none"

        attr_pct_linked = round(100.0 * attribution_methods["linked_goal_id"] / total, 1)
        attr_pct_heuristic = round(100.0 * attribution_methods["heuristic"] / total, 1)
        attr_pct_unattr = round(100.0 * attribution_methods["unattributed"] / total, 1)

        # 7. Compose Sonnet input
        input_msg = f"""WEEKLY NAS-IMPACT BREAKDOWN
Week: {week_start_iso} → {week_end_iso}
Total activity items: {total}

BUCKET COUNTS:
- NAS trajectory (high-relevance goals): {buckets['nas_trajectory']} items ({nas_pct}%)
- Service drag (email/unattributed non-goal): {buckets['service_drag']} items ({drag_pct}%)
- Maintenance (med/low-relevance goals): {buckets['maintenance']} items ({maint_pct}%)
- Unattributed: {buckets['unattributed']} items ({unattr_pct}%)

TOP GOALS ADVANCED:
{top_goals_str}

SAMPLE TOP-5 HIGHEST-IMPACT ITEMS:
{json.dumps(top_items, indent=2)}

ATTRIBUTION METHOD BREAKDOWN:
- Via linked_goal_id (explicit): {attribution_methods['linked_goal_id']} items ({attr_pct_linked}%)
- Via heuristic text match: {attribution_methods['heuristic']} items ({attr_pct_heuristic}%)
- Unattributed: {attribution_methods['unattributed']} items ({attr_pct_unattr}%)

Goals with high NAS relevance in the DB:
{json.dumps([g['name'] for g in goals if g['nas_relevance'] == 'high'], indent=2)}
"""

        # 8. Call Sonnet 4.6
        try:
            client = Anthropic()
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1200,
                system=IMPACT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": input_msg}],
            )
            summary_md: str = msg.content[0].text
        except Exception as e:
            summary_md = (
                f"[nas_impact] Sonnet call failed: {e}\n\n"
                f"Raw breakdown:\n"
                f"- NAS trajectory: {nas_pct}%\n"
                f"- Service drag: {drag_pct}%\n"
                f"- Maintenance: {maint_pct}%\n"
                f"- Unattributed: {unattr_pct}%\n"
                f"- Total items: {total}\n"
            )

    # 9. Insert into nas_impact_weekly
    try:
        conn2 = sqlite3.connect(DB_PATH)
        conn2.execute("PRAGMA journal_mode=WAL")
        conn2.execute(
            """INSERT INTO nas_impact_weekly
               (week_start_iso, week_end_iso,
                nas_trajectory_pct, service_drag_pct, maintenance_pct, unattributed_pct,
                goal_breakdown_json, total_activity_count, summary_md, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                week_start_iso,
                week_end_iso,
                nas_pct if total > 0 else 0.0,
                drag_pct if total > 0 else 0.0,
                maint_pct if total > 0 else 0.0,
                unattr_pct if total > 0 else 0.0,
                json.dumps(goal_breakdown) if total > 0 else "{}",
                total,
                summary_md,
                now_utc.isoformat(),
            ),
        )

        # 10. Create briefing row
        briefing_title = f"Weekly NAS-impact — week of {week_start_iso}"
        conn2.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('nas_impact', 'info', ?, ?, ?)",
            (briefing_title, summary_md, now_utc.isoformat()),
        )

        conn2.commit()
        conn2.close()
    except Exception as e:
        try:
            conn2.close()
        except Exception:
            pass
        return f"nas_impact: DB write failed: {e}"

    # 11. Return summary
    if total == 0:
        return "nas_impact: trajectory=0% drag=0% items=0 (no activity this week)"
    return (
        f"nas_impact: trajectory={nas_pct:.0f}% "
        f"drag={drag_pct:.0f}% "
        f"items={total}"
    )


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
