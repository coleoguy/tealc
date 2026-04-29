"""Daily student 1:1 agenda drafter — composes meeting prep for Heath's PhD students.

Schedule: CronTrigger(hour=6, minute=0, timezone="America/Chicago") — daily 6am Central.
Each run produces prep for all active students whose last interaction was >5 days ago
(proxy for "due for a 1:1 soon").
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

client = Anthropic()

_AGENDA_SYSTEM = """\
You draft meeting agendas for Heath's weekly 1:1s with his PhD students and staff. \
Heath is their advisor. Goal: surface the 3 highest-leverage things to discuss. \
Format per-student:

### {student name} — {role} — last 1:1: {days_ago}d
1. {topic} — {1-line context from last milestone/interaction}
2. {topic} — ...
3. {topic} — ...
_Suggested opener: "{opener question}"_

Heuristics:
- Overdue milestones are HIGH priority
- If no recent interaction, lead with "How are things? What's blocking?"
- If primary_project is known, include one project-specific technical prompt
- Don't invent details not in the data. If unsure, prompt an open-ended question instead.
Output only the markdown sections, no preamble."""


@tracked("student_agenda_drafter")
def job() -> str:
    """Draft 1:1 agendas for all active students due for a check-in."""

    # 1. Time guard: only run between 4am and 10am Central
    now_central = datetime.now(ZoneInfo("America/Chicago"))
    hour = now_central.hour
    if not (4 <= hour < 10):
        return "off-hours"

    # 2. Pull active students
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    student_rows = conn.execute(
        "SELECT id, full_name, short_name, role, primary_project, notes_md "
        "FROM students WHERE status='active'"
    ).fetchall()

    if not student_rows:
        conn.close()
        return "no_students_due"

    # 3 & 4. For each student pull last interaction and recent milestones; filter
    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    qualifying = []

    for s_id, full_name, short_name, role, primary_project, notes_md in student_rows:
        # Last interaction
        int_row = conn.execute(
            "SELECT max(occurred_iso), topic, action_items "
            "FROM interactions WHERE student_id=?",
            (s_id,),
        ).fetchone()
        last_iso, last_topic, last_action_items = int_row if int_row else (None, None, None)

        # Determine days_ago
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                days_ago = (datetime.now(timezone.utc) - last_dt).days
                overdue = last_dt < cutoff
            except Exception:
                days_ago = None
                overdue = True
        else:
            days_ago = None
            overdue = True  # never had interaction

        if not overdue:
            continue

        # Recent milestones (up to 3)
        milestone_rows = conn.execute(
            "SELECT kind, target_iso, completed_iso, notes "
            "FROM milestones WHERE student_id=? ORDER BY target_iso DESC LIMIT 3",
            (s_id,),
        ).fetchall()

        qualifying.append(
            {
                "id": s_id,
                "full_name": full_name,
                "short_name": short_name or full_name,
                "role": role or "PhD student",
                "primary_project": primary_project or "",
                "notes_md": (notes_md or "")[:300],
                "days_ago": days_ago,
                "last_topic": last_topic or "",
                "last_action_items": last_action_items or "",
                "milestones": milestone_rows,
            }
        )

    conn.close()

    # 5. Nothing due?
    if not qualifying:
        return "no_students_due"

    n = len(qualifying)

    # 6. Build user message with all qualifying students and call Sonnet once
    student_blobs = []
    for s in qualifying:
        days_label = f"{s['days_ago']}d" if s["days_ago"] is not None else "never"
        milestone_lines = []
        for kind, target_iso, completed_iso, ms_notes in s["milestones"]:
            status = "completed" if completed_iso else "pending"
            if target_iso and not completed_iso:
                try:
                    t = datetime.fromisoformat(target_iso)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    diff = (t - datetime.now(timezone.utc)).days
                    status = f"overdue by {-diff}d" if diff < 0 else f"due in {diff}d"
                except Exception:
                    pass
            milestone_lines.append(f"  - {kind}: {status}" + (f" | notes: {ms_notes}" if ms_notes else ""))

        blob = (
            f"STUDENT: {s['full_name']}\n"
            f"Role: {s['role']}\n"
            f"Primary project: {s['primary_project'] or '(not set)'}\n"
            f"Last interaction: {days_label} ago\n"
            f"Last topic: {s['last_topic'] or '(none logged)'}\n"
            f"Last action items: {s['last_action_items'] or '(none)'}\n"
            f"Recent milestones:\n" + ("\n".join(milestone_lines) if milestone_lines else "  (none logged)") + "\n"
            f"Notes: {s['notes_md'] or '(none)'}\n"
        )
        student_blobs.append(blob)

    user_msg = "\n---\n".join(student_blobs)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=_AGENDA_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    agenda_md = response.content[0].text.strip()

    # 7. Record cost (best-effort)
    try:
        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        }
        record_call(job_name="student_agenda_drafter", model="claude-sonnet-4-6", usage=usage)
    except Exception as _e:
        print(f"[student_agenda_drafter] cost_tracking error: {_e}")

    # 8. Insert briefing
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('student_agenda', 'info', ?, ?, ?)",
        (f"Weekly 1:1 agendas — {n} student(s) due", agenda_md, now_iso),
    )
    conn.commit()
    conn.close()

    # 9. Return summary
    return f"agendas_drafted: {n}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
