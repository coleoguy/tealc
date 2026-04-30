import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import sqlite3
import aiosqlite
import tempfile
import shutil
import chainlit as cl
import urllib.request
import urllib.error
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from dotenv import load_dotenv
from agent.privacy import public_event
from agent.scheduler import _migrate
from agent.tools import _read_pdf, _read_docx

_HERE = os.path.dirname(os.path.abspath(__file__))
# override=True so .env always wins over shell-exported values (e.g. a stale
# ANTHROPIC_API_KEY left in ~/.zshrc after a key rotation).
load_dotenv(os.path.join(_HERE, ".env"), override=True)

AQUARIUM_WORKER_URL = os.environ.get("AQUARIUM_WORKER_URL", "")
AQUARIUM_WORKER_SECRET = os.environ.get("AQUARIUM_WORKER_SECRET", "")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "agent.db")

# Ensure briefings + job_runs tables exist even if the scheduler has never run
_migrate()

# Activity log written to the lab website repo for the public aquarium
AQUARIUM_LOG = os.environ.get(
    "AQUARIUM_LOG_PATH",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/tealc_activity.json"),
)
AQUARIUM_MAX_EVENTS = 50

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
OPUS_TRIGGERS = {"think hard", "use opus", "deep thinking", "opus", "think carefully", "think deeply"}

# ---------------------------------------------------------------------------
# Briefing helpers (sync sqlite3 — reads once on chat start, no async needed)
# ---------------------------------------------------------------------------

_URGENCY_ICON = {"critical": "🚨", "warn": "⚠️", "info": "ℹ️"}


def _pending_briefings():
    """Return up to 5 unsurfaced briefings, critical-first then newest."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, kind, urgency, title, content_md, metadata_json FROM briefings "
        "WHERE surfaced_at IS NULL "
        "ORDER BY "
        "CASE urgency WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
        "created_at DESC "
        "LIMIT 5"
    ).fetchall()
    conn.close()
    return rows


def _mark_surfaced(ids):
    """Stamp surfaced_at on a list of briefing ids."""
    if not ids:
        return
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE briefings SET surfaced_at=? WHERE id=?",
        [(now, i) for i in ids],
    )
    conn.commit()
    conn.close()


def _push_to_worker(payload_bytes: bytes):
    """PUT the aquarium JSON to the Cloudflare Worker. Silently no-ops if unconfigured or worker is down."""
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
                "User-Agent": "Tealc-Lab-Agent/1.0",
            },
        )
        urllib.request.urlopen(req, timeout=3).read()
    except Exception as e:
        _aquarium_log = os.path.join(_HERE, "data", "aquarium_push_errors.log")
        try:
            with open(_aquarium_log, "a") as _f:
                _f.write(f"{datetime.now(timezone.utc).isoformat()} ERROR {e}\n")
        except Exception:
            pass


def _log_activity(tool_name: str, tool_input: dict, tool_output: str = ""):
    """Append an aquarium-safe event to the public log.
    tool_output is intentionally unused — outputs are NEVER public."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = public_event(tool_name, tool_input or {}, ts)

        log = {"last_updated": ts, "recent_activity": []}
        if os.path.exists(AQUARIUM_LOG):
            with open(AQUARIUM_LOG, "r") as f:
                log = json.load(f)
        log["last_updated"] = ts
        log["recent_activity"].insert(0, event)
        log["recent_activity"] = log["recent_activity"][:AQUARIUM_MAX_EVENTS]
        with open(AQUARIUM_LOG, "w") as f:
            json.dump(log, f, indent=2)
        _push_to_worker(json.dumps(log, indent=2).encode("utf-8"))
    except Exception:
        pass


async def _rebuild_graph(model: str):
    from agent.graph import build_graph
    conn = await aiosqlite.connect(DB_PATH)
    memory = AsyncSqliteSaver(conn)
    cl.user_session.set("conn", conn)
    return build_graph(memory, model=model)


@cl.on_chat_start
async def on_chat_start():
    graph = await _rebuild_graph(SONNET)
    thread_id = cl.user_session.get("id") or "default"
    cl.user_session.set("graph", graph)
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("model", SONNET)

    # Stalled-flagship interrupt — goals with importance=5 + nas_relevance=high and no activity N+ days
    try:
        from agent.config import get_threshold  # noqa: PLC0415
        stale_days = int(get_threshold("stalled_flagship_days", 21))
    except Exception:
        stale_days = 21
    try:
        conn_sf = sqlite3.connect(DB_PATH)
        conn_sf.execute("PRAGMA journal_mode=WAL")
        stalled = conn_sf.execute(
            "SELECT id, name, last_touched_iso FROM goals "
            "WHERE status='active' AND importance=5 AND nas_relevance='high' "
            f"AND (last_touched_iso IS NULL OR julianday('now') - julianday(last_touched_iso) > {stale_days}) "
            "ORDER BY last_touched_iso ASC LIMIT 1"
        ).fetchone()
        conn_sf.close()
        if stalled:
            goal_id, goal_name, last_touched = stalled
            days = "never" if not last_touched else f"{int((datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(last_touched.replace('Z', '+00:00')).timestamp())/86400)} days"
            cl.user_session.set("_sf_goal_id", goal_id)
            actions = [
                cl.Action(name="stalled_work_now", value=goal_id, label="Work on it now (10 min)"),
                cl.Action(name="stalled_defer", value=goal_id, label="Defer (tell me why)"),
                cl.Action(name="stalled_retire", value=goal_id, label="Retire / deprioritize"),
            ]
            await cl.Message(
                content=(
                    f"⚠️ **Before we start**\n\n"
                    f"**{goal_name}** ({goal_id}) — no activity in {days}. This is a top-priority NAS-critical goal. "
                    f"Pick one:"
                ),
                actions=actions,
            ).send()
    except Exception:
        pass  # silent — never block chat start

    await cl.Message(
        content=(
            "### Lab — Tealc\n\n"
            "Hello Heath! I'm Tealc, your lab agent — running on **Sonnet 4.6**.\n\n"
            "Gmail, Calendar, Drive, Docs, and Sheets are all connected.\n\n"
            "Say **'think hard'** or **'use opus'** to switch to Opus 4.7 for deep work."
        )
    ).send()

    # Activity report: what has Tealc been doing since Heath was last here?
    try:
        from agent.activity_report import build_activity_report
        report = build_activity_report(hours=24)
        await cl.Message(content=report).send()
    except Exception as e:
        await cl.Message(content=f"_(activity report unavailable: {e})_").send()

    # Surface any pending briefings produced while Heath was away
    briefings = _pending_briefings()
    if briefings:
        ids = []
        for row in briefings:
            bid, kind, urgency, title, content_md, metadata_json = row
            icon = _URGENCY_ICON.get(urgency, "ℹ️")
            actions = []
            if kind in ("overnight_draft", "drafter_paused") and metadata_json:
                try:
                    meta = json.loads(metadata_json)
                    draft_id = meta.get("draft_id")
                    if draft_id:
                        actions = [
                            cl.Action(name="draft_accept", value=str(draft_id), label="✓ Accept"),
                            cl.Action(name="draft_edit", value=str(draft_id), label="✎ Edit"),
                            cl.Action(name="draft_reject", value=str(draft_id), label="✗ Reject"),
                        ]
                except Exception:
                    pass
            await cl.Message(
                content=f"{icon} **{title}**\n\n{content_md}",
                actions=actions,
            ).send()
            ids.append(bid)
        _mark_surfaced(ids)

    # ------------------------------------------------------------------
    # Session continuation: offer to pick up from the last 24-hour session
    # ------------------------------------------------------------------
    try:
        conn_sc = sqlite3.connect(DB_PATH)
        conn_sc.execute("PRAGMA journal_mode=WAL")
        sc_row = conn_sc.execute(
            "SELECT thread_id, summary_md, topics, ended_at FROM session_summaries "
            "WHERE ended_at > datetime('now', '-24 hours') "
            "ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        if sc_row:
            _sc_thread_id, _sc_summary_md, _sc_topics, _sc_ended_at = sc_row
            # Count pending intentions
            (pending_count,) = conn_sc.execute(
                "SELECT count(*) FROM intentions WHERE status IN ('pending','in_progress')"
            ).fetchone()
            conn_sc.close()

            # Build a short topic label
            topic_label = (_sc_topics or _sc_summary_md or "your previous work") or "your previous work"
            if len(topic_label) > 100:
                topic_label = topic_label[:97] + "..."

            # Store topic in session so the action callback can reference it
            cl.user_session.set("_sc_topic", topic_label)

            # Compose continuation message
            pending_note = f"\n\nYou also have **{pending_count} pending intention(s)** in the queue." if pending_count > 0 else ""
            msg_text = (
                f"**Last session (within 24h):** {topic_label}"
                f"{pending_note}\n\n"
                "Want to pick up where we left off, or start fresh?"
            )

            try:
                actions = [
                    cl.Action(name="continue_session", value="continue", label="Pick up"),
                    cl.Action(name="new_topic", value="new", label="New topic"),
                ]
                await cl.Message(content=msg_text, actions=actions).send()
            except Exception:
                # Fallback if cl.Action isn't supported in this Chainlit version
                await cl.Message(
                    content=msg_text + "\n\nSay **'continue'** to pick up, or ask me anything else to start fresh."
                ).send()
        else:
            conn_sc.close()
    except Exception:
        # Silently skip — never crash chat start over this feature
        pass


@cl.action_callback("continue_session")
async def on_continue_session(action: cl.Action):
    topic = cl.user_session.get("_sc_topic") or "where we left off"
    await cl.Message(
        content=f"Great — let's pick up from **{topic}**. What aspect do you want to tackle first?"
    ).send()


@cl.action_callback("new_topic")
async def on_new_topic(action: cl.Action):
    await cl.Message(content="Sure, let's start fresh. What's on your mind?").send()


@cl.action_callback("stalled_work_now")
async def on_stalled_work_now(action: cl.Action):
    goal_id = action.value
    await cl.Message(content=f"Good. Let's dig in on `{goal_id}`. What's the single most blocking question right now?").send()


@cl.action_callback("stalled_defer")
async def on_stalled_defer(action: cl.Action):
    goal_id = action.value
    await cl.Message(content=f"Understood. Tell me why in one line and I'll log it to the decisions log (use `log_decision` tool via me), then I won't interrupt on `{goal_id}` for another 14 days.").send()


@cl.action_callback("stalled_retire")
async def on_stalled_retire(action: cl.Action):
    goal_id = action.value
    await cl.Message(content=f"OK. I'll move `{goal_id}` to status='retired' if you say 'confirm retire'. Tell me the reason too — it goes into the decisions log.").send()


@cl.action_callback("draft_accept")
async def on_draft_accept(action: cl.Action):
    did = action.value
    await cl.Message(content=f"Accept draft {did} — please merge the content into the source artifact and mark reviewed.").send()


@cl.action_callback("draft_edit")
async def on_draft_edit(action: cl.Action):
    did = action.value
    await cl.Message(content=f"Draft {did} — what change do you want? Paste the edit and I'll apply it to the doc.").send()


@cl.action_callback("draft_reject")
async def on_draft_reject(action: cl.Action):
    did = action.value
    await cl.Message(content=f"Draft {did} rejected. Tell me why in one line (it becomes a preference_signal).").send()


_SUPPORTED_MIME = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/csv",
}
_ATTACHMENT_CHAR_LIMIT = 8000


def _process_attachment(element) -> str:
    """Extract text from a Chainlit file element and return a formatted block."""
    name = getattr(element, "name", "unknown")
    path = getattr(element, "path", None)
    mime = getattr(element, "mime", "") or ""

    try:
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        size_str = f"{size:,} bytes"

        # Check MIME type support
        mime_base = mime.split(";")[0].strip().lower()
        if mime_base not in _SUPPORTED_MIME:
            return f"[ATTACHMENT NOT READABLE: {name} — only PDF, DOCX, and text files are supported]\n"

        # Extract text
        ext = os.path.splitext(name)[1].lower()
        if mime_base == "application/pdf" or ext == ".pdf":
            text = _read_pdf(path)
        elif mime_base == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or ext == ".docx":
            text = _read_docx(path)
        else:
            # Plain text / markdown / csv
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()

        full_len = len(text)
        truncated = text[:_ATTACHMENT_CHAR_LIMIT]
        header = f"[USER ATTACHED FILE: {name} ({size_str})]\n{truncated}"
        if full_len > _ATTACHMENT_CHAR_LIMIT:
            header += f"\n[<truncated at {_ATTACHMENT_CHAR_LIMIT} chars; full length: {full_len}>]"
        header += "\n[END ATTACHMENT]\n"
        return header

    except Exception as e:
        return f"[ATTACHMENT FAILED: {name} — {e}]\n"


def _build_message_with_attachments(message: cl.Message) -> str:
    """Prepend extracted attachment text blocks to the user's message content."""
    elements = getattr(message, "elements", None) or []
    if not elements:
        return message.content

    blocks = []
    for element in elements:
        # Only process cl.File elements (ignore images, audio, etc. from other sources)
        if not hasattr(element, "path") or not hasattr(element, "name"):
            continue
        mime = getattr(element, "mime", "") or ""
        # Skip unsupported non-document types silently unless it has a known extension
        ext = os.path.splitext(getattr(element, "name", ""))[1].lower()
        if not mime and ext not in {".pdf", ".docx", ".txt", ".md", ".csv"}:
            continue
        blocks.append(_process_attachment(element))

    if not blocks:
        return message.content

    attachments_text = "\n".join(blocks)
    original = message.content or ""
    if original:
        return f"{attachments_text}\n{original}"
    return attachments_text.rstrip("\n")


@cl.on_message
async def on_message(message: cl.Message):
    text_lower = message.content.lower()
    current_model = cl.user_session.get("model", SONNET)

    wants_opus = any(t in text_lower for t in OPUS_TRIGGERS)
    wants_sonnet = any(t in text_lower for t in {"use sonnet", "switch back", "sonnet"})

    if wants_opus and current_model != OPUS:
        graph = await _rebuild_graph(OPUS)
        cl.user_session.set("graph", graph)
        cl.user_session.set("model", OPUS)
        await cl.Message(content="Switched to **Opus 4.7**.").send()
    elif wants_sonnet and current_model != SONNET:
        graph = await _rebuild_graph(SONNET)
        cl.user_session.set("graph", graph)
        cl.user_session.set("model", SONNET)
        await cl.Message(content="Switched back to **Sonnet 4.6**.").send()

    graph = cl.user_session.get("graph")
    thread_id = cl.user_session.get("thread_id")
    config = {"configurable": {"thread_id": thread_id}}

    response_msg = cl.Message(content="")
    await response_msg.send()

    active_tool_name = None
    active_tool_input = {}
    active_step = None

    user_content = _build_message_with_attachments(message)

    try:
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=user_content)]},
            config={**config, "recursion_limit": 100},
            version="v2",
        ):
            kind = event["event"]

            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                content = chunk.content
                if isinstance(content, str) and content:
                    await response_msg.stream_token(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            await response_msg.stream_token(part.get("text", ""))

            elif kind == "on_tool_start":
                active_tool_name = event["name"]
                active_tool_input = event["data"].get("input", {})
                active_step = cl.Step(name=active_tool_name, type="tool")
                await active_step.__aenter__()
                active_step.input = str(active_tool_input)

            elif kind == "on_tool_end":
                output = event["data"].get("output", "")
                if active_tool_name:
                    _log_activity(active_tool_name, active_tool_input, str(output))
                if active_step:
                    raw = str(output)
                    active_step.output = raw[:800] + "…" if len(raw) > 800 else raw
                    await active_step.__aexit__(None, None, None)
                active_tool_name = None
                active_tool_input = {}
                active_step = None

    except Exception as e:
        await response_msg.stream_token(f"\n\n[Error: {e}]")

    await response_msg.update()

    # Task 10 — Auto-log student mentions from both sides of the conversation
    try:
        from agent.tools import _auto_log_student_mentions
        _auto_log_student_mentions(message.content + " " + (response_msg.content or ""))
    except Exception:
        pass
