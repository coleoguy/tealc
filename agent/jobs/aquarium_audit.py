"""Aquarium audit — daily privacy leak scan on the public activity feed.

Recommended schedule:
    CronTrigger(hour=1, minute=30, timezone="America/Chicago")   # 1:30am Central

Run manually to test:
    python -m agent.jobs.aquarium_audit
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_AQUARIUM_PATH = "/Users/blackmon/Desktop/GitHub/coleoguy.github.io/tealc_activity.json"
_DATA = os.path.normpath(os.path.join(_PROJECT_ROOT, "data"))

# Common short words to skip when matching grant title keywords
_COMMON_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "have", "will",
    "are", "was", "not", "but", "been", "they", "our", "its", "can",
    "such", "into", "via", "any", "all", "more", "each", "new", "use",
    "also", "both", "data", "base", "year", "time", "type", "code",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aquarium_audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at      TEXT NOT NULL,
            entries_scanned INTEGER NOT NULL,
            leaks_found     INTEGER NOT NULL,
            incidents_json  TEXT
        )
    """)
    conn.commit()


def _load_grant_keywords() -> set[str]:
    """Extract significant words from deadlines.json and grant_sources.json."""
    keywords: set[str] = set()

    for fname in ("deadlines.json", "grant_sources.json"):
        fpath = os.path.join(_DATA, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue

        # Flatten all string values in the JSON to extract words
        def _extract_strings(obj):
            if isinstance(obj, str):
                yield obj
            elif isinstance(obj, dict):
                for v in obj.values():
                    yield from _extract_strings(v)
            elif isinstance(obj, list):
                for item in obj:
                    yield from _extract_strings(item)

        for text in _extract_strings(data):
            for word in re.findall(r"\b[A-Za-z]{4,}\b", text):
                w = word.lower()
                if w not in _COMMON_WORDS:
                    keywords.add(w)

    return keywords


def _parse_time(time_str: str) -> datetime | None:
    """Parse ISO-8601 time string, returning UTC datetime or None."""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("aquarium_audit")
def job() -> str:
    # 1. Read the aquarium file
    if not os.path.exists(_AQUARIUM_PATH):
        return "aquarium file not found"

    try:
        with open(_AQUARIUM_PATH) as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            entries = []
    except Exception as e:
        return f"aquarium file not found: {e}"

    # 2. Filter to past 24h
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    recent = [
        e for e in entries
        if isinstance(e, dict) and (_parse_time(e.get("time", "")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    n = len(recent)

    # 3. Load privacy rules
    from agent.privacy import DENY_REGEX, LAB_PEOPLE  # noqa: PLC0415
    grant_keywords = _load_grant_keywords()

    # 4. Scan for leaks
    incidents: list[dict] = []

    for entry in recent:
        description = str(entry.get("description", ""))
        entry_type = str(entry.get("type", ""))
        time_str = str(entry.get("time", ""))
        combined_text = f"{description} {entry_type}"

        # Check DENY_REGEX
        if DENY_REGEX:
            m = DENY_REGEX.search(combined_text)
            if m:
                incidents.append({
                    "time": time_str,
                    "description": description[:120],
                    "match": m.group(0),
                    "rule": "deny_regex",
                })
                continue  # one report per entry

        # Check LAB_PEOPLE (whole-word, case-insensitive)
        matched_person = None
        for person in LAB_PEOPLE:
            if re.search(rf"\b{re.escape(person)}\b", combined_text, re.IGNORECASE):
                matched_person = person
                break
        if matched_person:
            incidents.append({
                "time": time_str,
                "description": description[:120],
                "match": matched_person,
                "rule": "lab_person",
            })
            continue

        # Check grant keywords (>= 4 chars, not common words)
        matched_kw = None
        for kw in grant_keywords:
            if len(kw) >= 4 and kw not in _COMMON_WORDS:
                if re.search(rf"\b{re.escape(kw)}\b", combined_text, re.IGNORECASE):
                    matched_kw = kw
                    break
        if matched_kw:
            incidents.append({
                "time": time_str,
                "description": description[:120],
                "match": matched_kw,
                "rule": "grant_title",
            })

    leaks_found = len(incidents)
    scanned_at = now.isoformat()

    # 6. INSERT audit log row
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)

    conn.execute(
        "INSERT INTO aquarium_audit_log(scanned_at, entries_scanned, leaks_found, incidents_json) "
        "VALUES (?, ?, ?, ?)",
        (scanned_at, n, leaks_found, json.dumps(incidents) if incidents else None),
    )
    conn.commit()

    # 7. Alert if leaks found
    if leaks_found > 0:
        incident_lines = "\n".join(
            f"- [{inc['rule']}] `{inc['match']}` — {inc['description']} (at {inc['time']})"
            for inc in incidents
        )
        briefing_content = (
            f"**{leaks_found} potential privacy incident(s)** detected in the public "
            f"aquarium feed during the past 24 hours.\n\n"
            f"### Incidents\n{incident_lines}\n\n"
            "_Review `tealc_activity.json` and remove any entries that expose private information._"
        )
        try:
            conn.execute(
                "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
                "VALUES ('aquarium_leak', 'critical', ?, ?, ?)",
                (
                    f"Aquarium leak detected: {leaks_found} incident(s)",
                    briefing_content,
                    scanned_at,
                ),
            )
            conn.commit()
        except Exception:
            pass  # briefing failure must not crash the job

        # Best-effort notify
        try:
            from agent.tools import notify_heath  # noqa: PLC0415
            notify_heath.invoke({"message": f"Aquarium leak: {leaks_found} incidents"})
        except Exception:
            pass

    conn.close()
    return f"scanned {n} entries, {leaks_found} leaks"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
