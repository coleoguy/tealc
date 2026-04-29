"""Weekly student pulse job — surfaces students needing Heath's attention.

Schedule: Sundays 6pm Central (so the briefing is ready for Monday morning).
"""
import sqlite3
from datetime import datetime, timezone

from agent.jobs import tracked
from agent.scheduler import DB_PATH


@tracked("student_pulse")
def job():
    from agent.tools import students_needing_attention  # noqa: PLC0415

    report = students_needing_attention.invoke({})

    if "no students" in report.lower():
        return "no_attention_needed"

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('student_alert', 'warn', 'Student check-ins for the week', ?, ?)",
        (report, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return "flagged_students_in_briefing"
