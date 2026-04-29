"""Cost telemetry — records every Anthropic API call and summarises spend."""
import sqlite3
from datetime import datetime, timezone

from agent.scheduler import DB_PATH

CLAUDE_PRICING_USD = {
    "claude-opus-4-7": {
        "in": 15.0,
        "out": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "in": 3.0,
        "out": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "in": 0.80,
        "out": 4.0,
        "cache_write": 1.0,
        "cache_read": 0.08,
    },
}


def _compute_cost(model: str, usage: dict) -> float:
    """Return estimated USD cost for a single API call."""
    pricing = CLAUDE_PRICING_USD.get(model, {"in": 3.0, "out": 15.0, "cache_write": 0.0, "cache_read": 0.0})
    tokens_in = usage.get("input_tokens", 0) or 0
    tokens_out = usage.get("output_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cost = (
        tokens_in * pricing["in"] / 1_000_000
        + tokens_out * pricing["out"] / 1_000_000
        + cache_write * pricing["cache_write"] / 1_000_000
        + cache_read * pricing["cache_read"] / 1_000_000
    )
    return round(cost, 8)


def record_call(job_name: str, model: str, usage: dict) -> None:
    """Write one row to cost_tracking for every Anthropic API call."""
    tokens_in = usage.get("input_tokens", 0) or 0
    tokens_out = usage.get("output_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cost = _compute_cost(model, usage)
    ts = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO cost_tracking
           (ts, job_name, model, tokens_in, tokens_out,
            cache_read_tokens, cache_write_tokens, estimated_cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts, job_name, model, tokens_in, tokens_out, cache_read, cache_write, cost),
    )
    conn.commit()
    conn.close()


def summarize_costs(since_iso: str | None = None, job_name: str | None = None) -> dict:
    """Return aggregate cost stats, optionally filtered by time window or job."""
    conditions = []
    params: list = []
    if since_iso:
        conditions.append("ts >= ?")
        params.append(since_iso)
    if job_name:
        conditions.append("job_name = ?")
        params.append(job_name)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        f"SELECT * FROM cost_tracking {where}", params
    ).fetchall()
    conn.close()

    total_usd = 0.0
    total_in = 0
    total_cache_read = 0
    by_model: dict = {}
    by_job: dict = {}

    for r in rows:
        total_usd += r["estimated_cost_usd"] or 0.0
        total_in += r["tokens_in"] or 0
        total_cache_read += r["cache_read_tokens"] or 0

        m = r["model"]
        if m not in by_model:
            by_model[m] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0}
        by_model[m]["calls"] += 1
        by_model[m]["tokens_in"] += r["tokens_in"] or 0
        by_model[m]["tokens_out"] += r["tokens_out"] or 0
        by_model[m]["usd"] += r["estimated_cost_usd"] or 0.0

        j = r["job_name"]
        if j not in by_job:
            by_job[j] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0}
        by_job[j]["calls"] += 1
        by_job[j]["tokens_in"] += r["tokens_in"] or 0
        by_job[j]["tokens_out"] += r["tokens_out"] or 0
        by_job[j]["usd"] += r["estimated_cost_usd"] or 0.0

    denom = total_cache_read + total_in
    cache_hit_rate = total_cache_read / denom if denom > 0 else 0.0

    return {
        "total_usd": round(total_usd, 6),
        "total_calls": len(rows),
        "by_model": by_model,
        "by_job": by_job,
        "cache_hit_rate": round(cache_hit_rate, 4),
    }
