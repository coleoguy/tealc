"""Meeting prep briefer — runs every 15 minutes via IntervalTrigger(minutes=15).

For any calendar event starting 45–75 minutes from now (duration ≥20 min),
composes a skimmable prep briefing using claude-sonnet-4-6 and inserts it into
the briefings table.  Deduplicates by event ID so each event gets at most one
briefing regardless of how many 15-min ticks fire before it starts.

Schedule (for the registering agent):
    IntervalTrigger(minutes=15)

Run manually to test:
    python -m agent.jobs.meeting_prep
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
_PREP_SYSTEM = (
    "You prepare Heath for an imminent meeting. Write a short, skimmable briefing "
    "with these sections (use markdown headers):\n"
    "- **Who**: attendees + 1-line context for non-obvious ones\n"
    "- **Likely topic**: infer from calendar title, description, attendees\n"
    "- **What Heath should have ready**: concrete docs, numbers, or decisions\n"
    "- **Suggested opening question or talking point** (if useful)\n"
    "Keep it under 250 words. Do not invent facts about attendees you cannot verify. "
    "If Heath's student is attending, note last milestone status if known."
)


# ---------------------------------------------------------------------------
# Calendar helper — raw event data via Google Calendar API
# ---------------------------------------------------------------------------

def _get_google_service(service_name: str, version: str):
    """Reuse the auth helper from agent.tools."""
    try:
        from agent.tools import _get_google_service as _gs  # noqa: PLC0415
        return _gs(service_name, version)
    except Exception as e:
        return None, str(e)


def _fetch_events_window(mins_lo: int = 45, mins_hi: int = 75) -> list[dict]:
    """Return raw event dicts whose start time is mins_lo–mins_hi minutes from now."""
    service, err = _get_google_service("calendar", "v3")
    if err:
        return []

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(minutes=mins_lo)
    window_end = now + timedelta(minutes=mins_hi)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=window_start.isoformat(),
            timeMax=window_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except Exception:
        return []


def _event_duration_minutes(event: dict) -> float:
    """Return event duration in minutes. Returns 0 if unparseable (e.g. all-day)."""
    start_str = event.get("start", {}).get("dateTime")
    end_str = event.get("end", {}).get("dateTime")
    if not start_str or not end_str:
        return 0.0
    try:
        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str)
        return (end_dt - start_dt).total_seconds() / 60.0
    except Exception:
        return 0.0


def _already_prepared(event_id: str) -> bool:
    """Return True if a meeting_prep briefing already exists for this event_id."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT id FROM briefings WHERE kind='meeting_prep' AND metadata_json IS NOT NULL",
        ).fetchall()
        conn.close()
        for (row_id,) in rows:
            pass  # we need content; re-query with metadata_json
        # Re-query with the actual column
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT metadata_json FROM briefings WHERE kind='meeting_prep' AND metadata_json IS NOT NULL",
        ).fetchall()
        conn.close()
        for (meta_str,) in rows:
            try:
                meta = json.loads(meta_str)
                if meta.get("event_id") == event_id:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


def _format_event_for_prompt(event: dict, mins_until: float) -> str:
    """Format raw event dict into a readable user message for Sonnet."""
    title = event.get("summary", "(no title)")
    start_str = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", "?"))
    end_str = event.get("end", {}).get("dateTime", event.get("end", {}).get("date", "?"))
    description = event.get("description", "").strip() or "(none)"
    location = event.get("location", "").strip() or "(none)"

    attendees = event.get("attendees", [])
    attendee_lines = []
    for a in attendees:
        name = a.get("displayName", "")
        email = a.get("email", "")
        label = name if name else email
        organizer_note = " [organizer]" if a.get("organizer") else ""
        self_note = " [Heath]" if a.get("self") else ""
        attendee_lines.append(f"  - {label} <{email}>{organizer_note}{self_note}")
    attendees_str = "\n".join(attendee_lines) if attendee_lines else "  (none listed)"

    linked_docs = []
    for attachment in event.get("attachments", []):
        title_att = attachment.get("title", "untitled")
        url = attachment.get("fileUrl", "")
        linked_docs.append(f"  - [{title_att}]({url})" if url else f"  - {title_att}")
    docs_str = "\n".join(linked_docs) if linked_docs else "  (none)"

    duration_min = _event_duration_minutes(event)

    return (
        f"**Meeting title:** {title}\n"
        f"**Starts in:** {int(mins_until)} minutes\n"
        f"**Duration:** {int(duration_min)} minutes\n"
        f"**Start:** {start_str}\n"
        f"**End:** {end_str}\n"
        f"**Location:** {location}\n\n"
        f"**Description:**\n{description}\n\n"
        f"**Attendees:**\n{attendees_str}\n\n"
        f"**Linked documents:**\n{docs_str}"
    )


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("meeting_prep")
def job() -> str:
    """For each event starting 45–75 min from now (≥20 min long), write a prep briefing."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("meeting_prep"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # 1. Time guard: only run 7am–10pm Central
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if not (7 <= hour < 22):
        return "off-hours"

    # 2. Fetch events in the 45–75-minute window
    now = datetime.now(timezone.utc)
    events = _fetch_events_window(mins_lo=45, mins_hi=75)

    # 3. Filter to events with duration ≥20 min
    qualifying = []
    for event in events:
        if _event_duration_minutes(event) < 20:
            continue
        qualifying.append(event)

    if not qualifying:
        return "no_upcoming_meetings"

    client = Anthropic()
    prepared = 0

    for event in qualifying:
        event_id = event.get("id", "")
        event_title = event.get("summary", "(no title)")

        # 4. Deduplicate: skip if already prepared
        if event_id and _already_prepared(event_id):
            continue

        # Compute minutes until start
        start_str = event.get("start", {}).get("dateTime")
        mins_until = 60.0  # fallback
        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                mins_until = max(0.0, (start_dt - now).total_seconds() / 60.0)
            except Exception:
                pass

        # 5. Compose briefing via Sonnet
        user_msg = _format_event_for_prompt(event, mins_until)
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system=_PREP_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            briefing_md = response.content[0].text.strip()
        except Exception as e:
            briefing_md = f"_(Briefing generation failed: {e})_"
            response = None

        # 6. Record cost
        if response is not None:
            try:
                usage = {
                    "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                }
                record_call(job_name="meeting_prep", model="claude-sonnet-4-6", usage=usage)
            except Exception as _e:
                print(f"[meeting_prep] cost_tracking error: {_e}")

        # 7. Insert briefing into DB
        now_iso = datetime.now(timezone.utc).isoformat()
        start_iso = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        metadata = json.dumps({"event_id": event_id, "start_iso": start_iso})
        briefing_title = f"Meeting prep: {event_title} — in {int(mins_until)}min"

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO briefings(kind, urgency, title, content_md, metadata_json, created_at) "
                "VALUES ('meeting_prep', 'info', ?, ?, ?, ?)",
                (briefing_title, briefing_md, metadata, now_iso),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[meeting_prep] DB insert error: {e}")
            continue

        prepared += 1

    if prepared == 0:
        return "no_upcoming_meetings"
    return f"prepared {prepared} meeting(s)"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
