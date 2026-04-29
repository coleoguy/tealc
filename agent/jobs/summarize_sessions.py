"""Long-term conversation memory job for Tealc.

Finds Chainlit/LangGraph chat sessions that have gone idle (>30 min since
last checkpoint) and have not yet been summarized.  Sends the message history
to Sonnet 4.6 for a structured 3-5 paragraph summary, then stores it in
session_summaries with FTS5 indexing so recall_past_conversations can search it.

Runs every 30 minutes via APScheduler (IntervalTrigger).
Limit: 5 threads per run to avoid blowing API budget on first backfill.
"""
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

from agent.jobs import tracked

log = logging.getLogger("tealc.scheduler")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
_PEOPLE_PATH = os.path.join(_DATA, "lab_people.json")

# Gregorian epoch offset — 100ns intervals from 1582-10-15 to 1970-01-01
_GREGORIAN_EPOCH_DIFF = 122192928000000000

BATCH_LIMIT = 5
IDLE_MINUTES = 30


# ---------------------------------------------------------------------------
# UUID v6/v1 timestamp decoder (same approach as refresh_context.py)
# ---------------------------------------------------------------------------
def _uuid_to_utc(cid: str) -> datetime | None:
    """Decode a UUID v6 (or v1) checkpoint_id to a UTC datetime.  Returns None on failure."""
    try:
        u = uuid.UUID(cid)
        if u.version == 6:
            i = u.int
            time_high = i >> 80           # top 48 bits
            time_low = (i >> 64) & 0x0FFF  # 12 bits after version nibble
            time_100ns = (time_high << 12) | time_low
        elif u.version == 1:
            time_100ns = u.time
        else:
            return None
        unix_us = (time_100ns - _GREGORIAN_EPOCH_DIFF) / 10  # microseconds
        return datetime.fromtimestamp(unix_us / 1e6, tz=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Find candidate threads
# ---------------------------------------------------------------------------
def _find_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Return up to BATCH_LIMIT thread_ids that are idle and not yet summarized."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=IDLE_MINUTES)

    # Pull all distinct thread_ids with their latest checkpoint_id
    rows = conn.execute(
        """
        SELECT thread_id, MAX(checkpoint_id) AS latest_cid
        FROM checkpoints
        GROUP BY thread_id
        """
    ).fetchall()

    # Also collect earliest checkpoint_id per thread for started_at
    earliest_rows = conn.execute(
        """
        SELECT thread_id, MIN(checkpoint_id) AS earliest_cid
        FROM checkpoints
        GROUP BY thread_id
        """
    ).fetchall()
    earliest_map = {r[0]: r[1] for r in earliest_rows}

    # Already summarized
    already = {r[0] for r in conn.execute("SELECT thread_id FROM session_summaries").fetchall()}

    candidates = []
    for thread_id, latest_cid in rows:
        if thread_id in already:
            continue
        dt = _uuid_to_utc(latest_cid)
        if dt is None or dt > cutoff:
            continue  # still active or can't decode
        earliest_cid = earliest_map.get(thread_id)
        started_dt = _uuid_to_utc(earliest_cid) if earliest_cid else None
        candidates.append({
            "thread_id": thread_id,
            "latest_cid": latest_cid,
            "earliest_cid": earliest_cid,
            "ended_at": dt.isoformat(),
            "started_at": started_dt.isoformat() if started_dt else None,
        })
        if len(candidates) >= BATCH_LIMIT:
            break

    return candidates


# ---------------------------------------------------------------------------
# Message extraction from checkpoint blob
# ---------------------------------------------------------------------------
def _extract_messages(conn: sqlite3.Connection, thread_id: str, latest_cid: str) -> list[dict]:
    """Deserialize the LangGraph checkpoint and extract the messages channel.

    Primary: use JsonPlusSerializer.loads_typed((type, blob)) — LangGraph stores checkpoints
    as msgpack blobs with a 'type' column indicating the serialization format.
    Fallback: regex extraction of content strings from the raw blob bytes.
    Returns a list of {"role": ..., "content": ...} dicts.
    """
    row = conn.execute(
        "SELECT checkpoint, type FROM checkpoints WHERE thread_id=? AND checkpoint_id=?",
        (thread_id, latest_cid),
    ).fetchone()
    if not row:
        return []

    blob, blob_type = row[0], (row[1] or "msgpack")

    # --- Primary: LangGraph serde ---
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        serde = JsonPlusSerializer()
        state = serde.loads_typed((blob_type, blob))
        # state is the checkpoint dict; channel_values holds the graph state
        channel_values = state.get("channel_values", {})
        messages_raw = channel_values.get("messages", [])
        result = []
        for msg in messages_raw:
            # Each message is a LangChain BaseMessage instance
            role = getattr(msg, "type", None) or getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                # Multi-part content — join text parts
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            if role == "ai":
                role = "assistant"
            if content and str(content).strip():
                result.append({"role": str(role), "content": str(content)[:2000]})
        return result
    except Exception as primary_err:
        log.debug("Primary serde failed for thread %s: %s", thread_id[:8], primary_err)

    # --- Fallback: regex over the raw bytes (works on msgpack-encoded JSON strings) ---
    try:
        raw_text = blob.decode("utf-8", errors="replace") if isinstance(blob, (bytes, bytearray)) else str(blob)
        # Extract quoted strings that look like non-trivial message content
        matches = re.findall(r'"content"\s*:\s*"((?:[^"\\]|\\.){20,})"', raw_text)
        if matches:
            return [{"role": "unknown", "content": m[:2000]} for m in matches[:40]]
    except Exception as fallback_err:
        log.debug("Fallback extraction failed for thread %s: %s", thread_id[:8], fallback_err)

    return []


# ---------------------------------------------------------------------------
# Sonnet summarization
# ---------------------------------------------------------------------------
def _load_roster() -> list[str]:
    try:
        with open(_PEOPLE_PATH) as f:
            return json.load(f).get("names", [])
    except Exception:
        return []


def _strip_fences(text: str) -> str:
    """Strip markdown code fences from a string."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def _summarize_with_sonnet(messages: list[dict], roster: list[str]) -> dict | None:
    """Call Sonnet 4.6 to produce a structured session summary.  Returns parsed dict or None."""
    import anthropic

    roster_str = ", ".join(roster) if roster else "(none)"

    messages_json = json.dumps(
        [{"role": m["role"], "content": m["content"][:1500]} for m in messages],
        indent=2,
    )

    system = (
        "You write a 3-5 paragraph summary of one Tealc chat session for Heath Blackmon's "
        "long-term memory. Capture: what was discussed, what decisions were made, what Tealc "
        "was asked to do, what's pending. Be concrete and specific — names, papers, deadlines. "
        "Output JSON: {\"summary_md\": \"<3-5 paragraphs>\", \"topics\": \"<comma-separated short tags>\", "
        "\"people_mentioned\": \"<comma-separated names from roster>\"}. Output ONLY the JSON."
    )

    user = (
        f"Lab roster (for people_mentioned field — only include names that appear in the session):\n"
        f"{roster_str}\n\n"
        f"Session messages:\n{messages_json}"
    )

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = resp.content[0].text if resp.content else ""
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
        return {
            "summary_md": str(data.get("summary_md", "")),
            "topics": str(data.get("topics", "")),
            "people_mentioned": str(data.get("people_mentioned", "")),
        }
    except Exception as e:
        log.warning("Sonnet JSON parse failed: %s | raw: %r", e, raw[:300])
        return None


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------
def _insert_summary(
    conn: sqlite3.Connection,
    thread_id: str,
    started_at: str | None,
    ended_at: str | None,
    message_count: int,
    summary_md: str,
    topics: str,
    people_mentioned: str,
):
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO session_summaries
            (thread_id, started_at, ended_at, message_count, summary_md, topics,
             people_mentioned, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (thread_id, started_at, ended_at, message_count, summary_md, topics,
         people_mentioned, now_iso),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("summarize_sessions")
def job():
    """Find idle threads and summarize them with Sonnet for long-term memory."""
    from agent.scheduler import DB_PATH  # noqa: PLC0415

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # Check that session_summaries table exists (schema applied by _migrate)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    if "session_summaries" not in tables:
        conn.close()
        return "skipped: session_summaries table missing — run _migrate() first"

    # Check that checkpoints table exists
    if "checkpoints" not in tables:
        conn.close()
        return "skipped: no checkpoints table yet (no conversations recorded)"

    candidates = _find_candidates(conn)
    if not candidates:
        conn.close()
        return "summarized=0 threads (none idle >30min or all already summarized)"

    roster = _load_roster()
    summarized = 0

    for cand in candidates:
        thread_id = cand["thread_id"]
        try:
            messages = _extract_messages(conn, thread_id, cand["latest_cid"])

            # Skip threads with <2 messages (likely empty/test sessions)
            if len(messages) < 2:
                # Insert placeholder so we don't retry
                _insert_summary(
                    conn, thread_id,
                    cand["started_at"], cand["ended_at"],
                    len(messages),
                    "[session too short to summarize]",
                    "", "",
                )
                log.info("summarize_sessions: skipped %s (only %d messages)", thread_id[:12], len(messages))
                continue

            result = _summarize_with_sonnet(messages, roster)
            if result is None:
                _insert_summary(
                    conn, thread_id,
                    cand["started_at"], cand["ended_at"],
                    len(messages),
                    "[summarization failed: JSON parse error]",
                    "", "",
                )
                log.warning("summarize_sessions: Sonnet parse failed for %s", thread_id[:12])
                continue

            _insert_summary(
                conn, thread_id,
                cand["started_at"], cand["ended_at"],
                len(messages),
                result["summary_md"],
                result["topics"],
                result["people_mentioned"],
            )
            summarized += 1
            log.info(
                "summarize_sessions: summarized %s (%d msgs, topics: %s)",
                thread_id[:12], len(messages), result["topics"][:60],
            )

        except Exception as e:
            log.error("summarize_sessions: error processing %s: %s", thread_id[:12], e)
            try:
                _insert_summary(
                    conn, thread_id,
                    cand["started_at"], cand["ended_at"],
                    0,
                    f"[summarization failed: {e}]",
                    "", "",
                )
            except Exception:
                pass  # don't let insert failure propagate

    conn.close()
    return f"summarized={summarized} threads"


if __name__ == "__main__":
    result = job()
    print(result)
