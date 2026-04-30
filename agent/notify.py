"""Notification module for Tealc — desktop banners + Gmail self-send.

Call notify(level, title, body, link=None) from anywhere in the codebase.
Level semantics:
  'info'     — purely logged (no desktop, no email)
  'warn'     — macOS desktop notification banner
  'critical' — desktop banner + Gmail self-send; rate-limited to 5/hour
"""
import json
import os
import subprocess
import base64
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

RATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "notification_rate.json")
RESEARCHER_EMAIL = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
MAX_CRITICAL_PER_HOUR = 5


def _osascript_notify(title: str, body: str):
    safe_title = title.replace('"', '\\"')[:80]
    safe_body = body.replace('"', '\\"')[:200]
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{safe_body}" with title "Tealc \u2014 {safe_title}"',
        ],
        check=False,
        timeout=5,
    )


def _check_rate(level: str) -> bool:
    """Return True if the notification is allowed under the rate limit."""
    if level != "critical":
        return True
    state = {"events": []}
    if os.path.exists(RATE_PATH):
        try:
            with open(RATE_PATH) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {"events": []}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    state["events"] = [
        t for t in state["events"]
        if datetime.fromisoformat(t) > cutoff
    ]
    if len(state["events"]) >= MAX_CRITICAL_PER_HOUR:
        return False
    state["events"].append(datetime.now(timezone.utc).isoformat())
    try:
        with open(RATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError:
        pass
    return True


def _send_email(title: str, body: str, link: str | None) -> bool:
    """Send a self-email via Gmail OAuth. Returns True on success."""
    try:
        from agent.tools import _get_google_service
        service, err = _get_google_service("gmail", "v1")
        if err:
            return False
        full_body = body
        if link:
            full_body += f"\n\nLink: {link}"
        msg = MIMEText(full_body, "plain")
        msg["To"] = RESEARCHER_EMAIL
        msg["From"] = RESEARCHER_EMAIL
        msg["Subject"] = f"[Tealc] {title}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception:
        return False


def notify(level: str, title: str, body: str, link: str | None = None) -> str:
    """Push a notification to Heath.

    level: 'info'     — no-op (logged only)
           'warn'     — macOS desktop banner
           'critical' — desktop banner + Gmail self-send (rate-limited 5/hour)

    Returns one of: 'logged', 'rate_limited', 'notified', 'notified+emailed'
    """
    if level == "info":
        return "logged"
    if not _check_rate(level):
        return "rate_limited"
    _osascript_notify(title, body)
    if level == "critical":
        _send_email(title, body, link)
        return "notified+emailed"
    return "notified"
