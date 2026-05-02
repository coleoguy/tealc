"""Consolidated activity report — what Tealc has been doing.

Used by app.py at chat start (so Heath sees it immediately) and by the
get_activity_report tool (so the agent can answer "what have you been up to?").

Pure SQLite reads + filesystem checks. No network, no API calls.
"""
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from agent.scheduler import DB_PATH

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "data"))
HEARTBEAT_PATH = os.path.join(_DATA, "scheduler_heartbeat.json")
PID_PATH = os.path.join(_DATA, "scheduler.pid")


def _cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _age_seconds(iso: str) -> int | None:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return None


def _scheduler_status() -> str:
    """Returns a short markdown line: heartbeat age + PID + job count."""
    alive = "unknown"
    age = None
    try:
        with open(HEARTBEAT_PATH) as f:
            hb = json.load(f)
        age = _age_seconds(hb.get("alive_at", ""))
        if age is None:
            alive = "heartbeat file unreadable"
        elif age < 120:
            alive = f"alive ({age}s since last heartbeat)"
        elif age < 600:
            alive = f"WARN: stale ({age}s since last heartbeat)"
        else:
            alive = f"DOWN: no heartbeat for {age // 60} min"
    except FileNotFoundError:
        alive = "heartbeat file missing"
    except Exception as e:
        alive = f"heartbeat read error: {e}"

    pid_line = ""
    try:
        with open(PID_PATH) as f:
            pid = f.read().strip()
        pid_line = f" · PID {pid}"
    except Exception:
        pass

    return f"{alive}{pid_line}"


def _job_run_summary(hours: int) -> tuple[int, int, int, list[str]]:
    """(total, success, failed, sample_errors) over the window."""
    since = _cutoff_iso(hours)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT job_name, status, error, output_summary FROM job_runs "
            "WHERE started_at >= ? ORDER BY started_at DESC",
            (since,),
        ).fetchall()
        conn.close()
    except Exception as e:
        return 0, 0, 0, [f"query error: {e}"]

    total = len(rows)
    success = sum(1 for r in rows if r[1] == "success")
    failed = sum(1 for r in rows if r[1] == "error")

    # Per-job sample errors
    errs: list[str] = []
    for job_name, status, error, _summary in rows:
        if status == "error" and error and len(errs) < 3:
            first_line = (error or "").strip().split("\n")[0][:140]
            errs.append(f"`{job_name}`: {first_line}")

    return total, success, failed, errs


def _job_run_by_name(hours: int) -> list[tuple[str, int, int, str | None]]:
    """Per-job: (name, runs, success_count, most_recent_summary)."""
    since = _cutoff_iso(hours)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT job_name, status, output_summary FROM job_runs "
            "WHERE started_at >= ? ORDER BY started_at DESC",
            (since,),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    by_name: dict[str, dict] = defaultdict(lambda: {"runs": 0, "success": 0, "summary": None})
    for job_name, status, summary in rows:
        rec = by_name[job_name]
        rec["runs"] += 1
        if status == "success":
            rec["success"] += 1
        if rec["summary"] is None and summary:
            rec["summary"] = summary[:100]
    return [(n, rec["runs"], rec["success"], rec["summary"]) for n, rec in by_name.items()]


def _ledger_summary(hours: int) -> tuple[int, dict, list[dict]]:
    """(total, counts_by_kind, sample_entries_with_critic)."""
    since = _cutoff_iso(hours)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT id, kind, job_name, project_id, critic_score, created_at "
            "FROM output_ledger WHERE created_at >= ? ORDER BY created_at DESC",
            (since,),
        ).fetchall()
        conn.close()
    except Exception:
        return 0, {}, []

    total = len(rows)
    counts = Counter(r[1] for r in rows)
    samples = [
        {
            "id": r[0], "kind": r[1], "job_name": r[2],
            "project_id": r[3], "critic_score": r[4], "created_at": r[5],
        }
        for r in rows[:5]
    ]
    return total, dict(counts), samples


def _cost_summary(hours: int) -> dict:
    since = _cutoff_iso(hours)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT model, tokens_in, tokens_out, cache_read_tokens, estimated_cost_usd "
            "FROM cost_tracking WHERE ts >= ?",
            (since,),
        ).fetchall()
        conn.close()
    except Exception:
        return {"total_usd": 0.0, "calls": 0, "by_model": {}, "cache_hit_rate": 0.0}

    total_usd = sum((r[4] or 0.0) for r in rows)
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "usd": 0.0})
    total_in = 0
    total_cache_read = 0
    for model, tin, tout, cache_read, usd in rows:
        by_model[model]["calls"] += 1
        by_model[model]["usd"] += (usd or 0.0)
        total_in += (tin or 0)
        total_cache_read += (cache_read or 0)
    denom = total_in + total_cache_read
    cache_hit = (total_cache_read / denom) if denom else 0.0
    return {
        "total_usd": round(total_usd, 3),
        "calls": len(rows),
        "by_model": {m: {"calls": v["calls"], "usd": round(v["usd"], 3)} for m, v in by_model.items()},
        "cache_hit_rate": round(cache_hit, 3),
    }


def _retrieval_quality_trend(days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT relevance_score FROM retrieval_quality "
            "WHERE sampled_at >= ? AND relevance_score IS NOT NULL",
            (since,),
        ).fetchall()
        conn.close()
    except Exception:
        return {"count": 0, "mean": None}
    scores = [r[0] for r in rows]
    if not scores:
        return {"count": 0, "mean": None}
    return {"count": len(scores), "mean": round(sum(scores) / len(scores), 2)}


def _aquarium_audit_recent() -> dict | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT scanned_at, entries_scanned, leaks_found FROM aquarium_audit_log "
            "ORDER BY scanned_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    return {"scanned_at": row[0], "entries_scanned": row[1], "leaks_found": row[2]}


def _pending_briefings_count() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        (n,) = conn.execute(
            "SELECT count(*) FROM briefings WHERE surfaced_at IS NULL"
        ).fetchone()
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def build_activity_report(hours: int = 24) -> str:
    """Build a compact markdown report of Tealc's recent activity.

    Covers scheduler status, last-window job runs, output ledger, cost,
    retrieval quality, and privacy audit. Designed to be skimmable in
    under 10 seconds.
    """
    # Scheduler
    sched_line = _scheduler_status()

    # Jobs
    total, success, failed, sample_errors = _job_run_summary(hours)
    by_name = _job_run_by_name(hours)
    # Rank non-heartbeat jobs by "interesting" (prefer jobs with summary text, then recent)
    interesting = [row for row in by_name if row[0] not in {"heartbeat", "refresh_context", "email_burst", "watch_deadlines"}]
    interesting.sort(key=lambda r: (r[3] is None, -r[1]))  # have summary first, then by run count
    job_lines = []
    for name, runs, ok, summary in interesting[:10]:
        status_badge = "ok" if ok == runs else f"{ok}/{runs} ok"
        tail = f" — {summary}" if summary else ""
        job_lines.append(f"- `{name}` ({status_badge}){tail}")

    # Ledger
    ledger_total, ledger_counts, ledger_samples = _ledger_summary(hours)
    ledger_kinds = ", ".join(f"{v} {k}" for k, v in sorted(ledger_counts.items(), key=lambda x: -x[1]))
    critic_lines = []
    for s in ledger_samples:
        if s["critic_score"] is not None:
            critic_lines.append(
                f"- {s['kind']} (id={s['id']}, job=`{s['job_name']}`, project={s['project_id'] or '—'}, critic={s['critic_score']}/5)"
            )

    # Cost
    cost = _cost_summary(hours)
    by_model_str = ", ".join(f"{m.replace('claude-','')}: ${v['usd']:.3f} ({v['calls']} calls)" for m, v in cost["by_model"].items()) or "no API calls"

    # Retrieval quality
    rq = _retrieval_quality_trend(days=7)
    rq_line = (
        f"{rq['count']} samples, mean {rq['mean']}/5 (last 7 days)"
        if rq["count"] > 0
        else "no samples yet"
    )

    # Aquarium audit
    aud = _aquarium_audit_recent()
    if aud:
        age = _age_seconds(aud["scanned_at"])
        age_str = f"{age // 3600}h ago" if age and age > 3600 else f"{(age or 0) // 60}m ago"
        aud_line = f"last scan {age_str} · {aud['entries_scanned']} entries · **{aud['leaks_found']} leaks**"
    else:
        aud_line = "no scans yet"

    # Pending briefings count
    pending_n = _pending_briefings_count()

    # Compose
    parts = [f"## What I've been doing (last {hours}h)"]
    parts.append(f"\n**Scheduler:** {sched_line}")

    if total == 0:
        parts.append(f"\n**Jobs:** no runs in the window (scheduler may be idle or just restarted).")
    else:
        parts.append(f"\n**Jobs:** {total} runs · {success} ok · {failed} failed")
        if failed > 0 and sample_errors:
            parts.append("\n_Errors:_")
            for e in sample_errors:
                parts.append(f"- {e}")
        if job_lines:
            parts.append("\n_Notable jobs:_")
            parts.extend(job_lines)

    if ledger_total > 0:
        parts.append(f"\n**Research output:** {ledger_total} ledger entries ({ledger_kinds})")
        if critic_lines:
            parts.append("\n_Recent (with critic scores):_")
            parts.extend(critic_lines)
    else:
        parts.append(f"\n**Research output:** none in this window.")

    parts.append(f"\n**Cost ({hours}h):** ${cost['total_usd']:.3f} across {cost['calls']} API calls · cache hit rate {cost['cache_hit_rate']*100:.0f}%")
    if cost["by_model"]:
        parts.append(f"_{by_model_str}_")

    parts.append(f"\n**Retrieval quality:** {rq_line}")
    parts.append(f"\n**Privacy audit:** {aud_line}")

    if pending_n > 0:
        parts.append(f"\n**Pending briefings:** {pending_n} (surfacing below)")

    return "\n".join(parts)


if __name__ == "__main__":
    print(build_activity_report(24))
