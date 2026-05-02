"""Subagent spawning for Tealc.

Tealc IS itself a Claude agent. When the chat-loop hits a task that needs
parallel research, deep multi-step exploration, or a focused subset of tools,
it spawns a sub-Claude-agent via this module.

A subagent is just an Anthropic `messages.create()` call running its own
tool-use loop:
  1. Sub-Claude gets a focused system prompt + a curated subset of read-only tools
  2. It runs to completion (model says stop_reason='end_turn') or hits max_steps
  3. The final text answer is returned to the caller

Two entry points:
  - run_subagent(task, ...) -> str         single subagent, blocking
  - run_parallel_subagents(tasks, ...)     N subagents in parallel via threads,
                                           returns a list of strings (one per task)

Cost: each subagent run is roughly 1k–10k tokens × the model's per-token rate.
A typical Sonnet 4.6 run with 5 tool calls is ~$0.10. Parallel runs of N tasks
cost ~N × that.

Usage from chat:
    spawn_subagent("Find three preprints on dosage compensation in bats from 2024")
    spawn_parallel_subagents([
        "Find current Schmidt Sciences AI2050 deadlines",
        "Find current NSF AI Institutes deadlines",
        "Find current ARIA AI for Science deadlines",
    ])
"""
from __future__ import annotations

import concurrent.futures as _futures
import json as _json
import logging as _logging
import os as _os
import sqlite3 as _sqlite3
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv as _load_dotenv
_PROJECT_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
_load_dotenv(_os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic as _Anthropic

log = _logging.getLogger("tealc.subagents")

# Default model; override per call. Sonnet 4.6 is the right balance for most
# subagent work — Opus is overkill, Haiku misses too much nuance.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Tools the subagent is ALLOWED to use. Curated subset of read-only / search
# tools — never write, never expose secrets, never call spawn_subagent again.
# (Recursion is explicitly blocked: a subagent cannot spawn its own subagents.)
DEFAULT_ALLOWED_TOOLS = [
    # Anthropic server-side tools
    "web_search",
    # Local search / fetch tools — must exist as @tool functions in agent.tools
    "fetch_url",
    "fetch_url_links",
    "search_pubmed",
    "search_biorxiv",
    "search_openalex",
    "search_literature_full_text",
    "fetch_paper_full_text",
    "epmc_cache_full_text",
    "s2_search_papers",
    "get_citation_contexts",
    "get_paper_recommendations",
    "pubmed_batch_fetch",
    "ncbi_assembly_summary",
    "timetree_age_distribution",
    "get_phylogenetic_tree",
    "get_divergence_time",
    "resolve_taxonomy",
    "search_funded_grants",
    "search_sra_runs",
    "gbif_bulk_occurrence_centroid",
    "get_species_distribution",
    "list_zenodo_deposits",
    "ask_my_record",
]

# Hard guards — these tools are NEVER passed to subagents (write surfaces,
# privileged ops, recursion).
_BLOCKED_TOOLS = {
    "spawn_subagent",
    "spawn_parallel_subagents",
    "create_google_doc",
    "append_to_google_doc",
    "replace_in_google_doc",
    "insert_comment_in_google_doc",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "draft_email_reply",
    "save_note",
    "delete_note",
    "zenodo_create_deposit",
    "zenodo_upload_file",
    "zenodo_publish_deposit",
}

_SUBAGENT_SYSTEM = """You are a focused research subagent dispatched by Tealc, the researcher's autonomous AI postdoc agent.

Your job: investigate exactly the task you were given, then return a concise final answer. You have access to read-only research tools (web search, literature search, full-text fetchers, taxonomy/phylogeny APIs, Heath's own paper record). Use them as needed.

GROUND RULES:
- Stay strictly on task. If the task is "find 3 papers on X", return 3 papers, not a survey.
- Use your tools — don't speculate when you can verify.
- When citing papers, include DOI or PubMed ID where available.
- Respect grounding: every factual claim should be traceable to a source you actually fetched.
- Cap your final answer to ~400 words unless the user asked for more.
- Do NOT write to any external surface (no file writes, no email, no calendar). You are read-only.
- Never call yourself recursively. Subagents do not spawn subagents.

When you have what you need, stop and write your final answer in plain prose with inline citations."""


def _build_tool_specs(allowed_tool_names: list[str]) -> tuple[list[dict], dict]:
    """Build the Anthropic `tools=...` argument and a name→callable map for
    Tealc's local @tool functions. The web_search server tool is added separately.
    """
    if not allowed_tool_names:
        return [], {}
    from agent.tools import get_all_tools  # noqa: PLC0415  (avoid circular)
    name_to_fn: dict[str, Any] = {}
    for fn in get_all_tools():
        name = getattr(fn, "name", getattr(fn, "__name__", None))
        if name:
            name_to_fn[name] = fn

    tool_specs: list[dict] = []
    for name in allowed_tool_names:
        if name in _BLOCKED_TOOLS:
            log.warning("Refusing to expose blocked tool %r to subagent", name)
            continue
        if name == "web_search":
            tool_specs.append({
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 6,
            })
            continue
        if name not in name_to_fn:
            log.warning("Subagent allowed_tool %r not found in agent.tools — skipping", name)
            continue
        # Reflect the @tool's signature into a JSONSchema. LangChain @tool
        # decorator stores .args_schema (Pydantic). We extract it lazily.
        fn = name_to_fn[name]
        try:
            schema = fn.args_schema.model_json_schema() if hasattr(fn, "args_schema") and fn.args_schema else {"type": "object", "properties": {}}
        except Exception:
            schema = {"type": "object", "properties": {}}
        tool_specs.append({
            "name": name,
            "description": (getattr(fn, "description", None) or fn.__doc__ or "")[:1024],
            "input_schema": schema,
        })
    return tool_specs, name_to_fn


def _invoke_local_tool(name_to_fn: dict, name: str, tool_input: dict) -> str:
    """Call one of Tealc's @tool functions and return the result as a string."""
    fn = name_to_fn.get(name)
    if fn is None:
        return f"ERROR: tool {name!r} not available to subagent"
    try:
        # LangChain @tool exposes .invoke() (preferred) and .run() (legacy).
        if hasattr(fn, "invoke"):
            result = fn.invoke(tool_input)
        elif hasattr(fn, "run"):
            result = fn.run(tool_input)
        else:
            result = fn(**tool_input)
    except Exception as exc:
        return f"ERROR calling {name}: {exc}"
    if isinstance(result, str):
        return result[:8000]
    try:
        return _json.dumps(result, default=str)[:8000]
    except Exception:
        return str(result)[:8000]


def _record_subagent_run(
    started_at: str,
    task: str,
    model: str,
    n_steps: int,
    tokens_in: int,
    tokens_out: int,
    cache_read: int,
    cache_write: int,
    cost_usd: float,
    status: str,
    error: str | None,
    final_text_len: int,
) -> None:
    """Write one row to subagent_runs in data/agent.db."""
    from agent.scheduler import DB_PATH  # noqa: PLC0415
    finished_at = datetime.now(timezone.utc).isoformat()
    try:
        conn = _sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT INTO subagent_runs
               (started_at, finished_at, task, model, n_steps,
                tokens_in, tokens_out, cache_read, cache_write,
                cost_usd, status, error, final_text_len)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                started_at, finished_at, task[:500], model, n_steps,
                tokens_in, tokens_out, cache_read, cache_write,
                cost_usd, status, error, final_text_len,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("Failed to record subagent_run: %s", exc)


def run_subagent(
    task: str,
    model: str = DEFAULT_MODEL,
    max_steps: int = 8,
    max_tokens_per_turn: int = 2048,
    allowed_tools: list[str] | None = None,
    extra_system: str = "",
) -> str:
    """Run a single subagent loop. Returns the final assistant text."""
    if not task or not task.strip():
        return "(empty task — nothing to investigate)"

    import agent.cost_tracking as _ct  # noqa: PLC0415

    allowed = allowed_tools if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS
    tool_specs, name_to_fn = _build_tool_specs(allowed)

    # Build system as a list with cache_control on the static _SUBAGENT_SYSTEM
    # block so parallel sub-agent dispatch gets prompt-cache hits across calls
    # within the 5-minute ephemeral TTL. The dynamic `extra_system` is kept
    # separate (un-cached) so per-task variation doesn't bust the cache.
    system: list[dict] = [
        {"type": "text", "text": _SUBAGENT_SYSTEM, "cache_control": {"type": "ephemeral"}},
    ]
    if extra_system:
        system.append({"type": "text", "text": extra_system.strip()})

    client = _Anthropic()
    messages: list[dict] = [{"role": "user", "content": task}]

    # Telemetry accumulators
    started_at = datetime.now(timezone.utc).isoformat()
    total_in = total_out = total_cache_read = total_cache_write = 0
    total_cost = 0.0
    n_steps = 0

    final_text = ""
    status = "error"
    error_msg: str | None = None

    for step in range(max_steps):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens_per_turn,
                system=system,
                tools=tool_specs if tool_specs else None,
                messages=messages,
            )
        except Exception as exc:
            log.warning("Subagent API call failed at step %d: %s", step, exc)
            error_msg = str(exc)
            _record_subagent_run(
                started_at, task, model, n_steps,
                total_in, total_out, total_cache_read, total_cache_write,
                total_cost, "error", error_msg, 0,
            )
            return f"(subagent API error: {exc})"

        n_steps += 1

        # --- per-step cost telemetry ---
        usage_obj = getattr(resp, "usage", None)
        if usage_obj is not None:
            usage_dict = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            }
            try:
                _ct.record_call(f"subagent/{task[:60]}", model, usage_dict)
            except Exception as exc:
                log.warning("cost_tracking.record_call failed: %s", exc)
            step_cost = _ct._compute_cost(model, usage_dict)
            total_in += usage_dict["input_tokens"]
            total_out += usage_dict["output_tokens"]
            total_cache_read += usage_dict["cache_read_input_tokens"]
            total_cache_write += usage_dict["cache_creation_input_tokens"]
            total_cost += step_cost

        # Collect any text returned this turn.
        text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if text_parts:
            final_text = "\n".join(t for t in text_parts if t).strip() or final_text

        if resp.stop_reason != "tool_use":
            break  # end_turn or max_tokens — done

        # Append assistant response so the conversation continues correctly.
        messages.append({"role": "assistant", "content": resp.content})

        # Run all tool_use blocks this turn and append a single user message
        # of tool_result blocks (Anthropic protocol).
        tool_results: list[dict] = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            if tool_name == "web_search":
                # Anthropic handles web_search server-side — no manual handling needed.
                # If we see it here we still need to acknowledge the result block,
                # but typically web_search results arrive directly in the next turn.
                continue
            result_str = _invoke_local_tool(name_to_fn, tool_name, tool_input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })
        if not tool_results:
            # No local tools to call (likely web_search only — Anthropic loops
            # internally). Break to avoid an infinite loop.
            break
        messages.append({"role": "user", "content": tool_results})

    if not final_text:
        final_text = "(subagent returned no final text)"

    status = "success"
    _record_subagent_run(
        started_at, task, model, n_steps,
        total_in, total_out, total_cache_read, total_cache_write,
        round(total_cost, 8), status, None, len(final_text),
    )
    return final_text


def run_parallel_subagents(
    tasks: list[str],
    model: str = DEFAULT_MODEL,
    max_steps: int = 8,
    max_workers: int = 4,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    """Run N subagents in parallel via threads. Returns a list of final
    answers in the same order as the input tasks.

    `max_workers` caps concurrency to avoid hammering the Anthropic API.
    """
    if not tasks:
        return []
    n_workers = min(max(1, max_workers), len(tasks))
    results: list[str] = ["(pending)"] * len(tasks)
    with _futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        future_to_idx = {
            pool.submit(
                run_subagent, task,
                model=model, max_steps=max_steps, allowed_tools=allowed_tools,
            ): i
            for i, task in enumerate(tasks)
        }
        for fut in _futures.as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:
                results[i] = f"(subagent {i} failed: {exc})"
    return results
