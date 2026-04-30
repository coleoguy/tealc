"""Morning briefing job — runs daily at 7:45am Central via APScheduler.

Pulls emails, calendar, citations, and recent papers; synthesises with Claude
Sonnet 4.6; writes a row to the briefings table.  If any [URGENT] tags appear
it also fires a warn-level desktop notification.

Run manually to test:
    python -m agent.jobs.morning_briefing
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

# Scheduler process doesn't go through app.py, so we must load .env explicitly.
# Use the project root path so this works regardless of CWD.
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402 (after load_dotenv)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
DEADLINES_PATH = os.path.join(_DATA, "deadlines.json")
SEEN_PATH = os.path.join(_DATA, "last_seen_state.json")

# ---------------------------------------------------------------------------
# the researcher's keyword set for literature monitoring
# ---------------------------------------------------------------------------
KEYWORDS = [
    "fragile Y hypothesis",
    "sex chromosome turnover",
    "chromosome number evolution",
    "karyotype evolution",
    "dysploidy",
    "chromosomal stasis",
    "Coleoptera genome",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_seen() -> dict:
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Grant helper
# ---------------------------------------------------------------------------

def _fetch_top_grants_from_db() -> str:
    """Query grant_opportunities for today's briefing block.

    Returns a markdown string (or a fallback message if none found).
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT title, fit_score, deadline_iso, url, fit_reasoning "
            "FROM grant_opportunities "
            "WHERE fit_score >= 0.5 AND (dismissed IS NULL OR dismissed = 0) "
            "ORDER BY fit_score DESC LIMIT 3"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return f"DB query failed: {exc}"

    if not rows:
        return (
            "No new high-fit grants this morning. "
            "Run `force_grant_radar` to re-scan."
        )

    lines: list[str] = []
    for title, fit_score, deadline, url, reasoning in rows:
        deadline_str = deadline or "deadline unknown"
        lines.append(
            f"- **{title}** (fit {fit_score:.2f}) | {deadline_str}\n"
            f"  {reasoning or ''}\n"
            f"  {url or ''}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("morning_briefing")
def job() -> str:
    """Assemble and store the researcher's daily morning briefing."""
    # the researcher can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("morning_briefing"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    from agent.tools import (  # noqa: PLC0415
        list_recent_emails,
        list_upcoming_events,
        track_citations,
        search_openalex,
    )

    # 1. Pull data from existing tools (each wrapped in try/except so a single
    #    flaky API can't crater the whole briefing).
    try:
        emails = list_recent_emails.invoke({"max_results": 30, "query": "newer_than:1d"})
    except Exception as exc:
        emails = f"Tool failed: list_recent_emails — {exc}"

    try:
        today_calendar = list_upcoming_events.invoke({"days_ahead": 2})
    except Exception as exc:
        today_calendar = f"Tool failed: list_upcoming_events — {exc}"

    try:
        citations = track_citations.invoke({"author_name": "the researcher", "max_results": 15})
    except Exception as exc:
        citations = f"Tool failed: track_citations — {exc}"

    # 2. Search literature in the researcher's keyword set
    new_papers: list[str] = []
    for kw in KEYWORDS:
        try:
            res = search_openalex.invoke({"query": kw, "max_results": 3})
            new_papers.append(f"### Topic: {kw}\n{res}")
        except Exception as exc:
            new_papers.append(f"### Topic: {kw}\nTool failed: search_openalex — {exc}")
    papers_block = "\n\n".join(new_papers)

    # 3. Compute deadline countdowns from data/deadlines.json
    try:
        with open(DEADLINES_PATH) as f:
            deadlines = json.load(f).get("deadlines", [])
    except (FileNotFoundError, json.JSONDecodeError):
        deadlines = []

    today = datetime.now(timezone.utc).date()
    countdowns: list[str] = []
    for d in deadlines:
        try:
            due = datetime.fromisoformat(d["due_iso"]).date()
            days = (due - today).days
            flag = " [URGENT]" if days <= 3 else ""
            countdowns.append(
                f"- **{d['name']}**: {days} days away ({d['priority']}){flag}"
            )
        except (KeyError, ValueError):
            continue
    countdowns_block = "\n".join(countdowns) if countdowns else "No deadlines loaded."

    # 4. Fetch top grant opportunities from DB
    grants_block = _fetch_top_grants_from_db()

    # 5. Synthesise with Claude Sonnet 4.6
    client = Anthropic()

    raw_input = (
        f"## Emails (last 24h)\n{emails}\n\n"
        f"## Calendar (next 48h)\n{today_calendar}\n\n"
        f"## Citation activity\n{citations}\n\n"
        f"## New papers in your topics\n{papers_block}\n\n"
        f"## Deadline countdowns\n{countdowns_block}\n\n"
        f"## Grant Opportunities\n{grants_block}"
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        system=(
            "You are Tealc preparing the researcher's morning briefing. Be terse. "
            "Use this exact structure with these headers in this order:\n"
            "## Today's calendar\n"
            "## Emails needing action\n"
            "## New papers in your topics\n"
            "## New citations of your work\n"
            "## Deadline status\n"
            "## Grant Opportunities\n\n"
            "Tag any same-day-action items with [URGENT] at line start. "
            "Skip empty sections with 'Nothing of note.' "
            "Do not pad. Aim for under 600 words total."
        ),
        messages=[{"role": "user", "content": raw_input}],
    )
    content_md: str = msg.content[0].text
    title = f"Morning briefing — {datetime.now().strftime('%a %b %d')}"
    urgency = "warn" if "[URGENT]" in content_md else "info"

    # 5. Write to briefings table
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('morning', ?, ?, ?, ?)",
        (urgency, title, content_md, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    # 6. Desktop notification if any URGENT items found
    if urgency == "warn":
        try:
            from agent.notify import notify  # noqa: PLC0415
            notify("warn", "Tealc morning briefing", "Urgent items present — open chat.")
        except Exception:
            pass  # notification failure must never kill the job

    return f"briefing_created urgency={urgency} bytes={len(content_md)}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
