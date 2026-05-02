"""Cost telemetry — records every LLM API call and summarises spend."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from agent.scheduler import DB_PATH

# ---------------------------------------------------------------------------
# Guarded import — keeps cost_tracking importable even before llm.py exists
# ---------------------------------------------------------------------------
try:
    from agent.llm import Usage, detect_vendor
    _USAGE_CLS = Usage
except ImportError:
    Usage = None          # type: ignore[assignment,misc]
    detect_vendor = None  # type: ignore[assignment]
    _USAGE_CLS = None


# ---------------------------------------------------------------------------
# Vendor-aware pricing table  (USD per million tokens)
# model_id_lowercase: (input, output, cache_read, cache_write)
# ---------------------------------------------------------------------------
_PRICING_USD_PER_M_TOKENS: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4-6":         (3.00,  15.00,  0.30,  3.75),
    "claude-opus-4-7":           (15.00, 75.00,  1.50, 18.75),
    "claude-haiku-4-5-20251001": (1.00,   5.00,  0.10,  1.25),
    "claude-haiku-4-5":          (1.00,   5.00,  0.10,  1.25),
    "gpt-5":                     (5.00,  15.00,  0.50,  0.0),
    "gpt-4o":                    (2.50,  10.00,  1.25,  0.0),
    "gpt-4o-mini":               (0.15,   0.60,  0.075, 0.0),
    "o1":                        (15.00, 60.00,  7.50,  0.0),
    "o3":                        (15.00, 60.00,  7.50,  0.0),
    "o4-mini":                   (1.10,   4.40,  0.55,  0.0),
    "gemini-2.0-pro":            (1.25,   5.00,  0.0,   0.0),
    "gemini-2.0-flash":          (0.075,  0.30,  0.0,   0.0),
    "gemini-1.5-pro":            (1.25,   5.00,  0.0,   0.0),
}

# Legacy pricing dict kept for any direct callers (backward compat — not used internally)
CLAUDE_PRICING_USD = {
    "claude-opus-4-7":           {"in": 15.0,  "out": 75.0,  "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6":         {"in": 3.0,   "out": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {"in": 1.00,  "out": 5.0,   "cache_write": 1.25,  "cache_read": 0.10},
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _normalize_usage(usage: "Usage | dict", model: str) -> "Usage":
    """Return a canonical Usage dataclass regardless of input type.

    Accepts:
    - A Usage dataclass (passthrough, with vendor/model back-filled if blank)
    - A dict with Anthropic-shape keys (cache_creation_input_tokens, cache_read_input_tokens)
    - A dict with canonical keys (cache_write_tokens, cache_read_tokens)
    """
    # --- Already a Usage dataclass ---
    if _USAGE_CLS is not None and isinstance(usage, _USAGE_CLS):
        if not usage.vendor:
            try:
                usage.vendor = detect_vendor(model) if detect_vendor else "unknown"
            except Exception:
                usage.vendor = "unknown"
        if not usage.model:
            usage.model = model
        return usage

    # Duck-type fallback: object with input_tokens attribute (future-proof)
    if not isinstance(usage, dict) and hasattr(usage, "input_tokens"):
        # Treat as Usage-compatible; wrap minimal fields
        try:
            vendor = getattr(usage, "vendor", "") or (detect_vendor(model) if detect_vendor else "unknown")
        except Exception:
            vendor = "unknown"
        if _USAGE_CLS is not None:
            return _USAGE_CLS(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
                reasoning_tokens=getattr(usage, "reasoning_tokens", 0) or 0,
                vendor=vendor,
                model=model,
                estimated_cost_usd=getattr(usage, "estimated_cost_usd", 0.0) or 0.0,
            )

    # --- Dict path ---
    d: dict = usage if isinstance(usage, dict) else {}

    input_tokens  = d.get("input_tokens", 0) or 0
    output_tokens = d.get("output_tokens", 0) or 0
    # Support both Anthropic-legacy keys and canonical keys
    cache_write   = (d.get("cache_creation_input_tokens", 0) or 0) or (d.get("cache_write_tokens", 0) or 0)
    cache_read    = (d.get("cache_read_input_tokens", 0) or 0) or (d.get("cache_read_tokens", 0) or 0)
    reasoning     = d.get("reasoning_tokens", 0) or 0

    vendor_val = d.get("vendor", "")
    if not vendor_val:
        try:
            vendor_val = detect_vendor(model) if detect_vendor else "unknown"
        except Exception:
            vendor_val = "unknown"

    if _USAGE_CLS is not None:
        return _USAGE_CLS(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
            vendor=vendor_val,
            model=model,
            estimated_cost_usd=0.0,
        )

    # Fallback: return a simple namespace-like object when Usage class unavailable
    class _FallbackUsage:
        pass

    fu = _FallbackUsage()
    fu.input_tokens = input_tokens          # type: ignore[attr-defined]
    fu.output_tokens = output_tokens        # type: ignore[attr-defined]
    fu.cache_read_tokens = cache_read       # type: ignore[attr-defined]
    fu.cache_write_tokens = cache_write     # type: ignore[attr-defined]
    fu.reasoning_tokens = reasoning         # type: ignore[attr-defined]
    fu.vendor = vendor_val                  # type: ignore[attr-defined]
    fu.model = model                        # type: ignore[attr-defined]
    fu.estimated_cost_usd = 0.0             # type: ignore[attr-defined]
    return fu  # type: ignore[return-value]


def _compute_cost(usage: "Usage") -> float:
    """Return estimated USD cost for a single API call, vendor-aware."""
    model_key = (getattr(usage, "model", "") or "").lower()
    prices = _PRICING_USD_PER_M_TOKENS.get(model_key)
    if prices is None:
        print(
            f"[cost_tracking] WARNING: no pricing data for model {model_key!r}; cost recorded as 0.0",
            file=sys.stderr,
        )
        return 0.0

    input_price, output_price, cache_read_price, cache_write_price = prices
    inp   = getattr(usage, "input_tokens", 0) or 0
    out   = getattr(usage, "output_tokens", 0) or 0
    cr    = getattr(usage, "cache_read_tokens", 0) or 0
    cw    = getattr(usage, "cache_write_tokens", 0) or 0

    # Providers include cached tokens in reported input; subtract to avoid double-counting
    net_input = max(inp - cr, 0)

    cost = (
        net_input  * input_price       / 1_000_000
        + cr       * cache_read_price  / 1_000_000
        + cw       * cache_write_price / 1_000_000
        + out      * output_price      / 1_000_000
    )
    return round(cost, 8)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_call(job_name: str, model: str, usage: "Usage | dict") -> None:
    """Write one row to cost_tracking for every LLM API call.

    ``usage`` may be a canonical Usage dataclass (from agent.llm) or a legacy
    dict with Anthropic-shape keys — both are accepted for backward compat.
    """
    u = _normalize_usage(usage, model)
    u.estimated_cost_usd = _compute_cost(u)

    ts = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO cost_tracking
           (ts, job_name, model, tokens_in, tokens_out,
            cache_read_tokens, cache_write_tokens, estimated_cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ts,
            job_name,
            model,
            getattr(u, "input_tokens", 0) or 0,
            getattr(u, "output_tokens", 0) or 0,
            getattr(u, "cache_read_tokens", 0) or 0,
            getattr(u, "cache_write_tokens", 0) or 0,
            u.estimated_cost_usd,
        ),
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
