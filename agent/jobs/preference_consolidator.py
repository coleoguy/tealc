"""Weekly preference consolidator — Sunday 7:30pm Central via APScheduler.

Recommended schedule for Agent E to register:
    CronTrigger(day_of_week="sun", hour=19, minute=30, timezone="America/Chicago")

Reads raw preference_signals from the past 7 days, groups them, asks Sonnet to
distill them into durable rules, prepends a new section to data/heath_preferences.md,
and inserts a briefing so the update surfaces in Heath's next chat session.

Run manually to test:
    python -m agent.jobs.preference_consolidator
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
PREFERENCES_PATH = os.path.join(_DATA, "heath_preferences.md")

_SEED_LINE = (
    "_No consolidated preferences yet — they'll appear here after the first "
    "Sunday 7:30pm run or when enough signals accumulate._"
)

_SYSTEM_PROMPT = (
    "You consolidate a scientist's preference signals into durable rules about how their "
    "AI postdoc should behave. Input: grouped reactions (dismissals, rejections, adoptions, "
    "praise) with reasons. Output: 3-8 bullet points in markdown that capture what this "
    "week's data says about preferences. Each bullet should be actionable, specific, and "
    "cite the signal count in parens. Example: \"- When briefings focus on service requests "
    "(3 dismissals), surface them filtered rather than with recommendations.\" Avoid vagueness. "
    "Do not restate the raw signals. Output only the bullets — no preamble, no heading."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_summary(signals: list[dict]) -> str:
    """Group signals by (signal_type, target_kind) and format a summary string."""
    groups: dict[tuple, list[str]] = {}
    for row in signals:
        key = (row["signal_type"], row["target_kind"])
        reason = (row["user_reason"] or "").strip()
        if key not in groups:
            groups[key] = []
        if reason:
            groups[key].append(reason)

    lines = [f"## Past week of preference signals ({len(signals)} total)\n"]
    for (signal_type, target_kind), reasons in groups.items():
        count = sum(
            1 for r in signals
            if r["signal_type"] == signal_type and r["target_kind"] == target_kind
        )
        lines.append(f"\n### {signal_type} / {target_kind} ({count} signals)")
        for r in reasons:
            lines.append(f"- {r}")

    return "\n".join(lines)


def _prepend_section(section_md: str) -> None:
    """Prepend section_md to heath_preferences.md, preserving the intro block."""
    if not os.path.exists(PREFERENCES_PATH):
        # Write seed first so we can then prepend
        with open(PREFERENCES_PATH, "w") as f:
            f.write(
                "# Heath's Preferences — Living Document\n\n"
                "This file is updated weekly by `agent/jobs/preference_consolidator.py` "
                "from signals captured via the `record_preference_signal` tool. Each section "
                "represents one week of consolidated observations.\n\n"
                "Tealc's SYSTEM_PROMPT can read the top 3-5 weeks of this file at "
                "chat-session start to stay current with Heath's evolving preferences.\n\n"
                "---\n\n"
                + _SEED_LINE + "\n"
            )

    with open(PREFERENCES_PATH, "r") as f:
        existing = f.read()

    # Find the boundary: first "## Week ending" or the seed line
    boundary = -1
    for marker in ["## Week ending", _SEED_LINE]:
        idx = existing.find(marker)
        if idx != -1:
            boundary = idx
            break

    if boundary == -1:
        # No known marker — append at end
        intro = existing.rstrip() + "\n\n"
        tail = ""
    else:
        intro = existing[:boundary]
        tail = existing[boundary:]
        # Remove seed line if present
        if tail.startswith(_SEED_LINE):
            tail = tail[len(_SEED_LINE):].lstrip("\n")

    new_content = intro + section_md + "\n\n" + tail.lstrip("\n")
    with open(PREFERENCES_PATH, "w") as f:
        f.write(new_content)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("preference_consolidator")
def job() -> str:
    """Consolidate past week's preference signals into durable rules in heath_preferences.md."""
    now_utc = datetime.now(timezone.utc)
    week_start = now_utc - timedelta(days=7)
    week_start_iso = week_start.isoformat()
    week_end_date = now_utc.strftime("%Y-%m-%d")

    # 1. Query preference_signals for the past 7 days
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT captured_at, signal_type, target_kind, target_id, user_reason "
            "FROM preference_signals "
            "WHERE captured_at > ? "
            "ORDER BY captured_at ASC",
            (week_start_iso,),
        ).fetchall()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"preference_consolidator: DB query failed: {e}"

    if not rows:
        return "no preference signals this week"

    signals = [
        {
            "captured_at": r[0],
            "signal_type": r[1],
            "target_kind": r[2],
            "target_id": r[3],
            "user_reason": r[4],
        }
        for r in rows
    ]
    n_signals = len(signals)

    # 2-3. Build grouping summary
    summary_text = _build_summary(signals)

    # 4. Call Sonnet
    client = Anthropic()
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": summary_text}],
        )
        consolidated_bullets = msg.content[0].text.strip()
    except Exception as e:
        return f"preference_consolidator: Sonnet call failed: {e}"

    # 5. Record cost
    try:
        from agent.cost_tracking import record_call  # noqa: PLC0415
        usage = {
            "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        }
        record_call(job_name="preference_consolidator", model="claude-sonnet-4-6", usage=usage)
    except Exception as e:
        print(f"[preference_consolidator] cost_tracking error (non-fatal): {e}")

    # 6. Prepend new section to heath_preferences.md (newest first)
    section_md = (
        f"## Week ending {week_end_date} ({n_signals} signals)\n\n"
        f"{consolidated_bullets}\n\n"
        f"---"
    )
    try:
        _prepend_section(section_md)
    except Exception as e:
        return f"preference_consolidator: file write failed: {e}"

    # 7. Insert briefing row
    briefing_content = (
        f"Heath's preference rules have been updated from {n_signals} signal(s) this week.\n\n"
        f"**File:** `data/heath_preferences.md`\n\n"
        f"**Consolidated rules (week ending {week_end_date}):**\n\n"
        f"{consolidated_bullets}"
    )
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('preference_update', 'info', 'Heath preferences updated', ?, ?)",
            (briefing_content, now_utc.isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[preference_consolidator] briefing insert error (non-fatal): {e}")
        try:
            conn.close()
        except Exception:
            pass

    # 8. Return summary
    n_bullets = sum(1 for line in consolidated_bullets.splitlines() if line.startswith("- "))
    return f"consolidated {n_signals} signals into {n_bullets} rules"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
