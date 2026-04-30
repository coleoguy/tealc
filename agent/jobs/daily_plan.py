"""Daily plan job — runs every morning at 6:30am Central via APScheduler.

Generates 3-5 prioritized concrete actions for Heath's day using Sonnet 4.6,
drawing on active goals, calendar, intentions, deadlines, and recent briefings.
Writes results to today_plan table (synced to Sheet within 5 min by sync_goals_sheet).
Also writes a daily_plan briefing for visibility in the chat UI.

Run manually to test:
    python -m agent.jobs.daily_plan
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load .env from project root — same pattern as executive.py
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402 (after load_dotenv)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DATA = os.path.join(_PROJECT_ROOT, "data")
DEADLINES_PATH = os.path.join(_DATA, "deadlines.json")

# ---------------------------------------------------------------------------
# Sonnet system prompt
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You generate the researcher's daily plan: 3-5 concrete, prioritized actions for TODAY \
that advance their active goals. Output JSON: \
{"items": [{"rank": 1, "description": "...", "linked_goal_id": "g_XXX", "estimated_minutes": N}, ...]}. \
Each description: a single concrete action Heath could complete in one sitting \
(NOT vague like "work on grant"; specific like "draft Significance section of \
Google.org grant — 90 min uninterrupted block at your Tuesday 9am window"). \
Match length and difficulty to today's calendar — if Heath has 6 hours of meetings, \
only 1-2 items. linked_goal_id should reference the actual goal advanced. \
Output JSON only, no preamble.\
"""


# ---------------------------------------------------------------------------
# Data-fetching helpers
# ---------------------------------------------------------------------------

def _get_active_goals_with_milestones(conn: sqlite3.Connection) -> list[dict]:
    """Fetch active goals with importance >= 4 and their nearest milestones."""
    try:
        rows = conn.execute(
            """SELECT id, name, importance, time_horizon, success_metric, why, notes
               FROM goals
               WHERE importance >= 4 AND status = 'active'
               ORDER BY importance DESC"""
        ).fetchall()
    except Exception:
        return []

    goals = []
    for row in rows:
        goal_id, name, importance, time_horizon, success_metric, why, notes = row
        goal_dict = {
            "id": goal_id,
            "name": name,
            "importance": importance,
            "time_horizon": time_horizon,
            "success_metric": success_metric,
            "why": why,
            "notes": notes,
            "nearest_milestone": None,
        }
        # Get nearest non-done milestone for this goal
        try:
            mil = conn.execute(
                """SELECT milestone, target_iso, status
                   FROM milestones_v2
                   WHERE goal_id = ? AND status NOT IN ('done')
                   ORDER BY CASE WHEN target_iso IS NULL THEN 1 ELSE 0 END, target_iso ASC
                   LIMIT 1""",
                (goal_id,),
            ).fetchone()
            if mil:
                goal_dict["nearest_milestone"] = {
                    "milestone": mil[0],
                    "target_iso": mil[1],
                    "status": mil[2],
                }
        except Exception:
            pass
        goals.append(goal_dict)
    return goals


def _get_top_intentions(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Fetch top pending high-priority intentions."""
    try:
        rows = conn.execute(
            """SELECT id, kind, description, target_iso, priority
               FROM intentions
               WHERE status IN ('pending', 'in_progress')
                 AND priority IN ('high', 'critical')
               ORDER BY
                 CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                 CASE WHEN target_iso IS NULL THEN 1 ELSE 0 END,
                 target_iso ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "description": r[2],
                "target_iso": r[3],
                "priority": r[4],
            }
            for r in rows
        ]
    except Exception:
        return []


def _get_last_briefings(conn: sqlite3.Connection, limit: int = 3) -> list[dict]:
    """Fetch the last N surfaced briefings (kind + title) for context."""
    try:
        rows = conn.execute(
            """SELECT kind, title FROM briefings
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"kind": r[0], "title": r[1]} for r in rows]
    except Exception:
        return []


def _get_deadlines(n: int = 3) -> list[dict]:
    """Read next N deadlines from data/deadlines.json with days remaining."""
    try:
        with open(DEADLINES_PATH) as f:
            raw = json.load(f)
        deadlines = raw.get("deadlines", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    today = datetime.now(timezone.utc).date()
    items = []
    for d in deadlines:
        try:
            due = datetime.fromisoformat(d["due_iso"]).date()
            days_remaining = (due - today).days
            items.append(
                {
                    "name": d.get("name", ""),
                    "due_iso": d.get("due_iso", ""),
                    "kind": d.get("kind", ""),
                    "priority": d.get("priority", ""),
                    "days_remaining": days_remaining,
                }
            )
        except (KeyError, ValueError):
            continue

    # Sort soonest first, take n
    items.sort(key=lambda x: x["days_remaining"])
    return items[:n]


def _get_calendar_today() -> str:
    """Call list_upcoming_events for today only. Returns raw string output."""
    try:
        from agent.tools import list_upcoming_events  # noqa: PLC0415
        return list_upcoming_events.invoke({"days_ahead": 1})
    except Exception as e:
        return f"Calendar unavailable: {e}"


def _strip_fences(text: str) -> str:
    """Strip markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
        text = "\n".join(inner).strip()
    return text


# ---------------------------------------------------------------------------
# Markdown rendering of plan items
# ---------------------------------------------------------------------------

def _render_plan_md(items: list[dict], date_iso: str) -> str:
    """Render plan items as a clean markdown string for the briefing."""
    lines = [f"## Today's Plan — {date_iso}\n"]
    for item in items:
        rank = item.get("rank", "?")
        desc = item.get("description", "")
        goal_id = item.get("linked_goal_id") or ""
        mins = item.get("estimated_minutes")
        goal_str = f" [→{goal_id}]" if goal_id else ""
        time_str = f" (~{mins} min)" if mins else ""
        lines.append(f"**#{rank}**{goal_str}{time_str}  \n{desc}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("daily_plan")
def job() -> str:
    """Generate and store Heath's daily plan. Idempotent — skips if already planned today."""
    today_iso = datetime.now(timezone.utc).date().isoformat()

    # 1. Skip if a plan already exists for today
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = conn.execute(
            "SELECT COUNT(*) FROM today_plan WHERE date_iso=?", (today_iso,)
        ).fetchone()
        conn.close()
        if existing and existing[0] > 0:
            return f"already_planned for {today_iso}"
    except Exception as e:
        # Table might not exist yet — safe to proceed
        try:
            conn.close()
        except Exception:
            pass

    # 2. Pull all inputs; each wrapped in try/except so one failure can't kill the job
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        goals = _get_active_goals_with_milestones(conn)
    except Exception:
        goals = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        intentions = _get_top_intentions(conn)
    except Exception:
        intentions = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        recent_briefings = _get_last_briefings(conn)
    except Exception:
        recent_briefings = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    deadlines = _get_deadlines(n=3)
    calendar_str = _get_calendar_today()

    # 3. Compose structured input for Sonnet
    input_parts = []

    if goals:
        input_parts.append("## Active Goals (importance ≥ 4)")
        for g in goals:
            ms = g["nearest_milestone"]
            ms_str = ""
            if ms:
                ms_str = f" | Next milestone: {ms['milestone']} (target: {ms['target_iso'] or 'TBD'}, {ms['status']})"
            input_parts.append(
                f"- [{g['id']}] {g['name']} (importance={g['importance']}, "
                f"horizon={g.get('time_horizon') or '?'}){ms_str}"
            )
    else:
        input_parts.append("## Active Goals\n(No goals synced yet — plan from deadlines and intentions)")

    input_parts.append(f"\n## Today's Calendar\n{calendar_str}")

    if intentions:
        input_parts.append("\n## Top Pending High-Priority Intentions")
        for i in intentions:
            due = f", due {i['target_iso']}" if i.get("target_iso") else ""
            input_parts.append(f"- [{i['priority'].upper()}] {i['description']}{due}")
    else:
        input_parts.append("\n## Top Pending High-Priority Intentions\nNone currently.")

    if deadlines:
        input_parts.append("\n## Upcoming Deadlines")
        for d in deadlines:
            days = d["days_remaining"]
            flag = " *** URGENT ***" if days <= 3 else ""
            input_parts.append(
                f"- {d['name']} ({d['priority']}): {days} days remaining{flag}"
            )
    else:
        input_parts.append("\n## Upcoming Deadlines\nNone found.")

    if recent_briefings:
        input_parts.append("\n## Recent Briefings (context)")
        for b in recent_briefings:
            input_parts.append(f"- [{b['kind']}] {b['title']}")

    input_parts.append(f"\n## Today's Date\n{today_iso}")

    user_message = "\n".join(input_parts)

    # 4. Call Sonnet 4.6
    try:
        client = Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw_output = response.content[0].text.strip()
    except Exception as e:
        return f"Sonnet API error: {e}"

    # 5. Parse JSON (try/except + markdown fence stripping)
    try:
        clean = _strip_fences(raw_output)
        parsed = json.loads(clean)
        items = parsed.get("items", [])
        if not isinstance(items, list) or len(items) == 0:
            return f"Sonnet returned empty items list: {raw_output[:200]}"
    except Exception as e:
        return f"JSON parse error ({e}): {raw_output[:300]}"

    # 6. Write items to today_plan via write_today_plan tool
    try:
        from agent.tools import write_today_plan  # noqa: PLC0415
        items_json = json.dumps(
            [
                {
                    "rank": item.get("rank"),
                    "description": item.get("description", ""),
                    "linked_goal_id": item.get("linked_goal_id") or None,
                    "status": "pending",
                    "notes": (
                        f"estimated_minutes={item['estimated_minutes']}"
                        if item.get("estimated_minutes")
                        else None
                    ),
                }
                for item in items
            ]
        )
        write_result = write_today_plan.invoke({"items": items_json})
    except Exception as e:
        write_result = f"write_today_plan failed: {e}"

    # 7. Create daily_plan briefing
    try:
        content_md = _render_plan_md(items, today_iso)
        briefing_title = f"Today's plan ({today_iso})"
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('daily_plan', 'info', ?, ?, ?)",
            (briefing_title, content_md, now_iso),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Briefing failure must never crash the job
        pass

    # 8. Return summary
    n = len(items)
    first_desc = items[0].get("description", "") if items else ""
    return f"plan: {n} items, top: {first_desc[:60]}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
