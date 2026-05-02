"""VIP email watcher — scans inbox every 5 minutes for messages from high-priority senders.

Recommended schedule: IntervalTrigger(minutes=5) during work hours (7am–8pm Central).
The job self-guards against off-hours runs and returns "off-hours" immediately.

Source of truth for who is a VIP: data/vip_senders.json (Heath owns this file).
Each match inserts a briefing with kind='vip_email' and urgency='critical'.

Run manually:
    cd "$HOME/Google Drive/My Drive/00-Lab-Agent"
    python -m agent.jobs.vip_email_watch
"""
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

# Load .env from project root — same pattern as nightly_grant_drafter.py lines 20-23
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

log = logging.getLogger("tealc.vip_email_watch")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
VIP_LIST_PATH = os.path.join(_DATA, "vip_senders.json")
CHECKPOINT_PATH = os.path.join(_DATA, "last_vip_check.json")


# ---------------------------------------------------------------------------
# Off-hours guard (7am–8pm Central)
# ---------------------------------------------------------------------------
def _is_working_hours() -> bool:
    """Return True if current Central time is between 7am and 8pm."""
    try:
        import zoneinfo
        central = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        try:
            import pytz
            central = pytz.timezone("America/Chicago")
        except Exception:
            return True  # allow run if timezone unavailable
    now_central = datetime.now(central)
    return 7 <= now_central.hour < 20


# ---------------------------------------------------------------------------
# VIP list loader
# ---------------------------------------------------------------------------
def _load_vip_list() -> tuple[list[dict], list[str]]:
    """Return (vip_senders, vip_domains). Both empty lists on failure."""
    try:
        with open(VIP_LIST_PATH) as f:
            data = json.load(f)
        senders = data.get("vip_senders", [])
        domains = [d.lower() for d in data.get("vip_domains", [])]
        return senders, domains
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return [], []


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------
def _load_last_check() -> datetime:
    """Return last-check UTC datetime; default to 1h ago if missing."""
    try:
        with open(CHECKPOINT_PATH) as f:
            iso = json.load(f).get("last_check_iso")
        if iso:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return datetime.now(timezone.utc) - timedelta(hours=1)


def _save_last_check(dt: datetime):
    with open(CHECKPOINT_PATH, "w") as f:
        json.dump({"last_check_iso": dt.isoformat()}, f)


# ---------------------------------------------------------------------------
# Extract bare email address from a "Name <email>" string
# ---------------------------------------------------------------------------
def _extract_email(from_field: str) -> str:
    """Pull the bare email address out of a From: header value."""
    m = re.search(r"<([^>]+)>", from_field)
    if m:
        return m.group(1).strip().lower()
    return from_field.strip().lower()


# ---------------------------------------------------------------------------
# Parse the formatted string returned by list_recent_emails
# (same logic as email_triage.py _parse_email_blocks)
# ---------------------------------------------------------------------------
def _parse_email_blocks(raw: str) -> list[dict]:
    """Parse list_recent_emails text output into structured dicts."""
    if not raw or raw.startswith("No messages") or raw.startswith("Gmail not connected"):
        return []
    blocks = raw.split("\n\n---\n\n")
    emails = []
    for block in blocks:
        lines = block.strip().splitlines()
        item: dict = {}
        for line in lines:
            if line.startswith("ID:"):
                item["message_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("From:"):
                item["from_raw"] = line.split(":", 1)[1].strip()
            elif line.startswith("Subject:"):
                item["subject"] = line.split(":", 1)[1].strip()
            elif line.startswith("Preview:"):
                item["snippet"] = line.split(":", 1)[1].strip()
            elif line.startswith("Date:"):
                item["date_str"] = line.split(":", 1)[1].strip()
        if item.get("message_id"):
            emails.append(item)
    return emails


# ---------------------------------------------------------------------------
# Parse a date string into UTC datetime (best-effort)
# ---------------------------------------------------------------------------
def _parse_date(date_str: str) -> Optional[datetime]:
    """Try to parse the email date string; return None on failure."""
    if not date_str:
        return None
    # Try ISO format first
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Try RFC 2822-ish: "Mon, 14 Apr 2025 10:30:00 +0000"
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# VIP match: check sender against vip_senders list and vip_domains
# Returns (matched: bool, label: str) where label is the VIP label or domain
# ---------------------------------------------------------------------------
def _entry_matches_email(entry: dict, bare_email: str) -> bool:
    """Check an email against an entry's `email` field and any `aliases`."""
    if entry.get("email", "").lower() == bare_email:
        return True
    for alias in entry.get("aliases", []) or []:
        if alias.lower() == bare_email:
            return True
    return False


def _match_vip(
    from_raw: str,
    vip_senders: list[dict],
    vip_domains: list[str],
) -> tuple[bool, str]:
    bare_email = _extract_email(from_raw)
    domain = bare_email.split("@")[-1] if "@" in bare_email else ""

    # Exact email or alias match (case-insensitive)
    for entry in vip_senders:
        if _entry_matches_email(entry, bare_email):
            return True, entry.get("label", bare_email)

    # Domain match
    if domain and domain in vip_domains:
        return True, f"VIP domain ({domain})"

    return False, ""


# ---------------------------------------------------------------------------
# Deduplication: check if a briefing for this message_id already exists
# ---------------------------------------------------------------------------
def _already_briefed(conn: sqlite3.Connection, message_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM briefings "
            "WHERE kind='vip_email' AND metadata_json LIKE ?",
            (f'%"message_id": "{message_id}"%',),
        ).fetchone()
        return row is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Find the label/note for a matched sender (for content_md)
# ---------------------------------------------------------------------------
def _get_sender_meta(
    from_raw: str,
    vip_senders: list[dict],
    vip_domains: list[str],
) -> tuple[str, str]:
    """Return (label, note) for a matched VIP sender."""
    bare_email = _extract_email(from_raw)
    domain = bare_email.split("@")[-1] if "@" in bare_email else ""

    for entry in vip_senders:
        if _entry_matches_email(entry, bare_email):
            return entry.get("label", bare_email), entry.get("note", "")

    if domain and domain in vip_domains:
        return f"VIP domain ({domain})", ""

    return bare_email, ""


# ---------------------------------------------------------------------------
# Check if a matched sender has the text_alert flag set
# ---------------------------------------------------------------------------
def _sender_wants_text_alert(
    from_raw: str, vip_senders: list[dict],
) -> bool:
    bare_email = _extract_email(from_raw)
    for entry in vip_senders:
        if _entry_matches_email(entry, bare_email):
            return bool(entry.get("text_alert", False))
    return False


# ---------------------------------------------------------------------------
# Classify whether a VIP email is "important enough to text about" via Haiku.
# ---------------------------------------------------------------------------
def _classify_vip_urgency(
    sender_label: str, subject: str, snippet: str,
) -> tuple[str, float]:
    """Return (urgency, cost_usd). Urgency is one of: critical, high, medium, low."""
    try:
        from anthropic import Anthropic  # noqa: PLC0415
        from agent.cost_tracking import record_call  # noqa: PLC0415
    except Exception:
        return ("medium", 0.0)  # fallback: send at medium (but imessage tool won't text medium)

    client = Anthropic()
    system_prompt = (
        "You classify whether a VIP email is important enough to interrupt "
        "Heath with a text message right now.\n\n"
        "Respond with ONE of:\n"
        "  critical — text right now, even off-hours (deadline today, urgent ask, family emergency)\n"
        "  high     — text during waking hours (important question, opportunity, personal message from a close contact)\n"
        "  medium   — do not text; keep as a briefing only (routine work correspondence, calendar invites, FYI)\n"
        "  low      — do not text; keep as a briefing only (auto-generated, unsubscribe-able, newsletter-like)\n\n"
        "Respond with ONE token only. No punctuation."
    )
    user_msg = (
        f"Sender: {sender_label}\n"
        f"Subject: {subject}\n"
        f"Preview: {snippet[:500]}"
    )
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        log.warning(f"vip_email_watch: urgency classifier failed: {e}")
        return ("medium", 0.0)

    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    try:
        record_call(job_name="vip_email_urgency_classifier",
                    model="claude-haiku-4-5-20251001", usage=usage)
    except Exception:
        pass
    cost = (usage["input_tokens"] + usage["output_tokens"] * 5) / 1_000_000  # rough

    raw = (msg.content[0].text or "").strip().lower().rstrip(".")
    if raw in ("critical", "high", "medium", "low"):
        return (raw, cost)
    return ("medium", cost)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("vip_email_watch")
def job() -> str:
    """Scan inbox for VIP sender emails and insert critical briefings."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("vip_email_watch"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # 1. Time guard. FORCE_RUN=1 bypasses for chat-driven manual triggers.
    if not _is_working_hours() and os.environ.get("FORCE_RUN") != "1":
        return "off-hours"

    # 2. Load VIP list
    vip_senders, vip_domains = _load_vip_list()
    if not vip_senders and not vip_domains:
        log.warning("vip_email_watch: vip_senders.json missing or empty")
        return "no_vip_list"

    # 3. Last-check timestamp
    last_check = _load_last_check()
    log.info(f"vip_email_watch: checking since {last_check.isoformat()}")

    # 4. Query Gmail
    from agent.tools import list_recent_emails  # noqa: PLC0415

    try:
        raw_emails = list_recent_emails.invoke(
            {"query": "in:inbox newer_than:1h", "max_results": 50}
        )
    except Exception as exc:
        log.error(f"vip_email_watch: list_recent_emails failed: {exc}")
        return f"error: list_recent_emails — {exc}"

    emails = _parse_email_blocks(raw_emails)
    log.info(f"vip_email_watch: fetched {len(emails)} candidate emails")

    # 5–7. Filter by timestamp, match VIPs, deduplicate, insert briefings
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    now_iso = datetime.now(timezone.utc).isoformat()
    n_new = 0

    for email in emails:
        message_id = email["message_id"]
        from_raw = email.get("from_raw", "")
        subject = email.get("subject", "(no subject)")
        snippet = email.get("snippet", "")
        date_str = email.get("date_str", "")

        # Filter by timestamp: only process messages newer than last check
        msg_dt = _parse_date(date_str)
        if msg_dt is not None and msg_dt <= last_check:
            continue

        # VIP match
        matched, label = _match_vip(from_raw, vip_senders, vip_domains)
        if not matched:
            continue

        # Deduplication
        if _already_briefed(conn, message_id):
            log.info(f"vip_email_watch: skipping already-briefed {message_id}")
            continue

        # Build briefing content
        sender_label, sender_note = _get_sender_meta(from_raw, vip_senders, vip_domains)
        gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
        title = f"VIP email from {sender_label}: {subject[:60]}"

        content_lines = [
            f"**From:** {from_raw}",
            f"**Subject:** {subject}",
            f"**Snippet:** {snippet}",
            f"",
            f"[Open in Gmail]({gmail_link})",
            f"",
            f"_Source: `data/vip_senders.json` — label: {sender_label}_",
        ]
        if sender_note:
            content_lines.append(f"_Note: {sender_note}_")

        content_md = "\n".join(content_lines)
        metadata = json.dumps({"message_id": message_id, "from": from_raw, "label": sender_label})

        try:
            conn.execute(
                "INSERT INTO briefings(kind, urgency, title, content_md, created_at, metadata_json) "
                "VALUES ('vip_email', 'critical', ?, ?, ?, ?)",
                (title, content_md, now_iso, metadata),
            )
            conn.commit()
            n_new += 1
            log.info(f"vip_email_watch: briefing inserted for {message_id} (label={sender_label})")
        except Exception as exc:
            log.error(f"vip_email_watch: failed to insert briefing for {message_id}: {exc}")

        # Push-notification path via ntfy.sh: only for senders with
        # text_alert=true in vip_senders.json, and only if Haiku classifies
        # this specific email as critical or high. Medium/low stays
        # briefing-only. Notification includes a tap-to-open Gmail link.
        if _sender_wants_text_alert(from_raw, vip_senders):
            try:
                urgency, _cost = _classify_vip_urgency(sender_label, subject, snippet)
                log.info(f"vip_email_watch: {message_id} urgency={urgency}")
                if urgency in ("critical", "high"):
                    from agent.tools import send_ntfy_to_heath  # noqa: PLC0415
                    body_preview = snippet[:160]
                    notif_title = f"VIP {urgency}: {sender_label}"
                    notif_body = f"Re: {subject[:80]}\n{body_preview}"
                    tag = "rotating_light" if urgency == "critical" else "envelope"
                    notif_result = send_ntfy_to_heath.invoke({
                        "message": notif_body,
                        "urgency": urgency,
                        "title": notif_title,
                        "click_url": gmail_link,
                        "tags": tag,
                    })
                    log.info(f"vip_email_watch: ntfy result = {notif_result}")
            except Exception as exc:
                log.error(f"vip_email_watch: text-alert path failed for {message_id}: {exc}")

    conn.close()

    # 8. Update checkpoint
    _save_last_check(datetime.now(timezone.utc))

    # 9. Return summary
    if n_new > 0:
        return f"vip_alerts: {n_new}"
    return "no_new_vip_emails"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = job()
    print(result)
