"""Daily deadline countdown — 7:30am Central. For every deadline within 10 days,
creates a briefing with the day count. One consolidated briefing per day,
skipped if there are no imminent deadlines.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
DEADLINES_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "data", "deadlines.json"))


def _days_until(due_iso: str):
    try:
        due = datetime.fromisoformat(due_iso)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return (due - datetime.now(timezone.utc)).days
    except Exception:
        return None


@tracked("deadline_countdown")
def job() -> str:
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("deadline_countdown"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    try:
        with open(DEADLINES_PATH) as f:
            deadlines = json.load(f).get("deadlines", [])
    except Exception:
        return "no_deadlines_file"

    imminent = []
    for dl in deadlines:
        days = _days_until(dl.get("due_iso", ""))
        if days is not None and 0 <= days <= 10:
            imminent.append((days, dl.get("name", "(unnamed)"), dl.get("due_iso", "")))

    if not imminent:
        return "no_imminent_deadlines"

    imminent.sort()
    urgency = "critical" if imminent[0][0] <= 3 else "warn"
    lines = ["**Deadlines within 10 days:**\n"]
    for days, name, due in imminent:
        marker = "🔴" if days <= 3 else "🟠" if days <= 7 else "🟡"
        lines.append(f"- {marker} **{name}** — {days} day(s) left (due {due[:10]})")

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('deadline_countdown', ?, ?, ?, ?)",
        (urgency, f"{len(imminent)} deadline(s) within 10 days", "\n".join(lines), now_iso),
    )
    conn.commit()
    conn.close()
    return f"surfaced {len(imminent)} deadline(s); soonest={imminent[0][0]}d"


if __name__ == "__main__":
    print(job())
