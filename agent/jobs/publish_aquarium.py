"""Publish Tealc's heartbeat + recent-job activity to the public aquarium feed.

The aquarium page (lab's GitHub Pages /tealc.html) shows "Offline" if the JSON's
`last_updated` is older than 10 minutes. Chat-driven tool calls update it via
_log_activity in app.py, but scheduled jobs do NOT — so when Heath isn't
chatting the page flips to Offline even though the scheduler is doing plenty.

This job runs every 2 minutes. It:
  1. Updates `last_updated` on the aquarium JSON (keeps status dot green).
  2. If a noteworthy scheduled job completed since the last aquarium event
     AND the last event is >10 min old, adds ONE privacy-safe event reflecting
     that job. Never exposes project names, grant titles, student names, etc.
  3. Writes local JSON + pushes to the Cloudflare Worker (same endpoint app.py uses).

Recommended schedule: IntervalTrigger(minutes=2). Low cost — pure SQLite read +
one small HTTP PUT.
"""
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

AQUARIUM_LOG = os.environ.get(
    "AQUARIUM_LOG_PATH",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/tealc_activity.json"),
)
AQUARIUM_MAX_EVENTS = 50
AQUARIUM_WORKER_URL = os.environ.get("AQUARIUM_WORKER_URL", "")
AQUARIUM_WORKER_SECRET = os.environ.get("AQUARIUM_WORKER_SECRET", "")

# Jobs we don't want reflected in the public feed (too noisy, too internal).
# Public visitors should see substantive science work, not housekeeping pings.
_SKIP_JOBS = {
    "heartbeat", "refresh_context", "watch_deadlines", "email_burst",
    "publish_aquarium", "aquarium_audit",
    # Housekeeping that fires every few min — drowns out the interesting work.
    "vip_email_watch",   # was "Watched priority inbox"
    "executive",         # was "Reviewed priorities"
}

# Privacy-safe (type, description) labels per noteworthy job.
# Never expose project names, grant titles, student names, email addresses,
# deadline labels, or any specific strings from output_summary.
_JOB_LABELS: dict[str, tuple[str, str]] = {
    "paper_of_the_day":              ("read",     "Reviewed paper of the day"),
    "nightly_literature_synthesis":  ("search",   "Synthesized recent literature"),
    "nightly_grant_drafter":         ("drive",    "Drafted a research document"),
    "weekly_hypothesis_generator":   ("tool",     "Proposed new hypotheses"),
    "weekly_comparative_analysis":   ("tool",     "Ran a comparative analysis"),
    "cross_project_synthesis":       ("tool",     "Synthesized across projects"),
    "email_triage":                  ("email",    "Processed emails"),
    "executive":                     ("tool",     "Reviewed priorities"),
    "morning_briefing":              ("tool",     "Composed morning briefing"),
    "daily_plan":                    ("tool",     "Planned the day"),
    "weekly_review":                 ("tool",     "Ran weekly self-review"),
    "track_nas_metrics":             ("cite",     "Updated citation metrics"),
    "nas_impact_score":              ("cite",     "Computed impact scores"),
    "nas_pipeline_health":           ("cite",     "Evaluated research pipeline"),
    "grant_radar":                   ("search",   "Scanned funding opportunities"),
    "student_pulse":                 ("tool",     "Checked on students"),
    "summarize_sessions":            ("note",     "Summarized past sessions"),
    "deadline_countdown":            ("tool",     "Scanned upcoming deadlines"),
    "midday_check":                  ("tool",     "Midday check-in"),
    "meeting_prep":                  ("calendar", "Prepared for an upcoming meeting"),
    "student_agenda_drafter":        ("tool",     "Drafted 1:1 agendas"),
    "vip_email_watch":               ("email",    "Watched priority inbox"),
    "next_action_filler":            ("tool",     "Proposed project next actions"),
    "populate_project_keywords":     ("search",   "Refined retrieval keywords"),
    "retrieval_quality_monitor":     ("tool",     "Audited retrieval quality"),
    "replication_docs":              ("read",     "Refreshed replication docs"),
    "preference_consolidator":       ("note",     "Consolidated preferences"),
    "goal_conflict_check":           ("tool",     "Scanned goal conflicts"),
    "weekly_database_health":        ("drive",    "Checked database health"),
    "quarterly_retrospective":       ("tool",     "Quarterly review"),
    # Daytime science micro-jobs
    "midday_lit_pulse":              ("read",     "Read a recent preprint"),
    "citation_watch":                ("cite",     "Checked citations of lab work"),
    "paper_radar":                   ("search",   "Scanned new preprints"),
    "database_pulse":                ("drive",    "Audited a karyotype database"),
    # Prereg-to-Replication Loop (Bet 2 / Tier 1 #1)
    ("prereg_replication_loop", "monday_prereg"):   ("tool", "Published a public preregistration"),
    ("prereg_replication_loop", "t7_adjudication"): ("tool", "Published a replication verdict (supported/refuted/null)"),
}


def _load_aquarium() -> dict:
    try:
        with open(AQUARIUM_LOG) as f:
            return json.load(f)
    except Exception:
        return {"last_updated": "", "recent_activity": []}


def _coalesce_adjacent(events: list) -> list:
    """Collapse consecutive events with identical (type, description) into a
    single entry. Events are ordered newest-first, so the first of a run keeps
    its time (which is already the newest) and the older duplicates are dropped.

    This converts feeds like:
        ["Watched priority inbox" 12:00, "Watched priority inbox" 11:50,
         "Watched priority inbox" 11:40, "Reviewed paper" 11:30, ...]
    into:
        ["Watched priority inbox" 12:00, "Reviewed paper" 11:30, ...]

    A web visitor sees one card per kind, with the time of the most recent
    occurrence — not 20 stamps of the same description.
    """
    if not events:
        return events
    out = [dict(events[0])]
    for e in events[1:]:
        last = out[-1]
        if (
            e.get("type") == last.get("type")
            and e.get("description") == last.get("description")
        ):
            continue  # adjacent duplicate — drop, keeping last's newer time
        out.append(dict(e))
    return out


def _push_to_worker(payload_bytes: bytes) -> None:
    if not AQUARIUM_WORKER_URL or not AQUARIUM_WORKER_SECRET:
        return
    try:
        req = urllib.request.Request(
            AQUARIUM_WORKER_URL,
            data=payload_bytes,
            method="PUT",
            headers={
                "Content-Type": "application/json",
                "X-Tealc-Auth": AQUARIUM_WORKER_SECRET,
                "User-Agent": "Tealc-Scheduler/1.0",
            },
        )
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # transient network errors are fine — next tick retries


@tracked("publish_aquarium")
def job() -> str:
    log = _load_aquarium()
    events = log.get("recent_activity", [])
    # Drop events whose (type, description) matches a job in _SKIP_JOBS — this
    # cleans up historical noise on the very next publish when a job is added
    # to the skip list. New entries are filtered upstream by the SQL NOT IN.
    _hidden = {
        _JOB_LABELS[j] for j in _SKIP_JOBS if j in _JOB_LABELS
    }
    if _hidden:
        events = [
            e for e in events
            if (e.get("type"), e.get("description")) not in _hidden
        ]
    # Then collapse any adjacent duplicates that may exist (especially after
    # the filter above re-aligns rows).
    events = _coalesce_adjacent(events)
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Step 1: decide whether to add a new event.
    added_event_label: str | None = None
    try:
        last_event_ts = None
        if events:
            raw = events[0].get("time", "")
            try:
                last_event_ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                last_event_ts = None

        # Only consider adding a new event if the most recent one is >=10 min old
        # (i.e. the page would otherwise read "Offline").
        stale = last_event_ts is None or (now - last_event_ts) > timedelta(minutes=10)
        if stale:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute(
                "SELECT job_name, finished_at FROM job_runs "
                "WHERE status='success' AND finished_at > ? "
                "  AND job_name NOT IN (" + ",".join("?" * len(_SKIP_JOBS)) + ") "
                "ORDER BY finished_at DESC LIMIT 1",
                ((now - timedelta(minutes=10)).isoformat(), *_SKIP_JOBS),
            ).fetchone()
            conn.close()
            if row:
                job_name, _finished = row
                label = _JOB_LABELS.get(job_name)
                if label is not None:
                    # Same as the most recent event? Just bump its time.
                    # Otherwise insert a new row at the top.
                    if (
                        events
                        and events[0].get("type") == label[0]
                        and events[0].get("description") == label[1]
                    ):
                        events[0]["time"] = now_iso
                        added_event_label = f"(updated time on {label[1]})"
                    else:
                        events.insert(0, {
                            "time": now_iso, "type": label[0], "description": label[1],
                        })
                        events = events[:AQUARIUM_MAX_EVENTS]
                        added_event_label = label[1]
    except Exception:
        pass  # never block the heartbeat write on a DB/query issue

    # Step 2: always update last_updated so the page status dot stays green.
    log["last_updated"] = now_iso
    log["recent_activity"] = events

    try:
        with open(AQUARIUM_LOG, "w") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        return f"local_write_error: {e}"

    _push_to_worker(json.dumps(log, indent=2).encode("utf-8"))

    if added_event_label:
        return f"heartbeat + event: {added_event_label}"
    return "heartbeat"


if __name__ == "__main__":
    print(job())
