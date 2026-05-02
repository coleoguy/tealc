"""Vendor-portable LLM dispatch — single chokepoint for all model calls.

Goal: any caller wanting a model response calls ``agent.llm.chat()`` with a
canonical request shape; the shim routes to the right vendor backend
(Anthropic SDK natively, OpenAI/Gemini via LiteLLM) and returns a canonical
response shape with normalized usage stats.

Why this exists: TEALC was 100% Anthropic-coupled — `client.messages.create`
in 35+ files, `model = "claude-opus-4-7"` literals in critic.py,
model_router.py only knowing SONNET/OPUS/HAIKU constants. Goal of this shim
is to let users (community deployments) set TEALC_PRIMARY_MODEL=gpt-5 or
gemini-2.0-pro in `.env` and have TEALC route accordingly without touching
job code.

Public API
----------
``chat(model, system, messages, tools=None, max_tokens=4096,
       cache_hint=False, effort="medium") -> Response``

``Response`` is a dataclass: ``content`` (canonical content-block list,
Anthropic-style), ``stop_reason`` (normalized), ``usage`` (Usage dataclass).

``Usage`` normalizes input/output/cache-read/cache-write/reasoning tokens
plus vendor + model + estimated_cost_usd across providers. Anthropic-style
field names are canonical; OpenAI's ``cached_tokens`` and Gemini's tokens
are mapped into ``cache_read_tokens`` / ``cache_write_tokens``.

Tool format: canonical is Anthropic-style ``{"name", "description",
"input_schema"}``. Backends translate to OpenAI's
``{"type": "function", "function": {...}}`` and Gemini's
``{"function_declarations": [...]}`` internally.

System prompt: accept ``str`` (simple) or ``list[dict]`` (Anthropic
cache-control blocks). Backends translate as needed.

Cache hint: if ``cache_hint=True``, the system prompt's stable prefix is
marked cacheable. Anthropic uses ``cache_control: ephemeral``; OpenAI's
prompt caching is implicit (kicks in for repeated prefixes ≥1024 tokens —
no explicit control); Gemini uses ``cachedContent`` (not implemented in
this shim's first cut; documented as a future TODO).

Effort tier: ``"xhigh" | "high" | "medium" | "low"`` — translated to
Anthropic's adaptive thinking budget, OpenAI's ``reasoning_effort`` for
o-series/GPT-5, Gemini's ``thinkingBudget``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import litellm

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

_client = Anthropic()


# ---------------------------------------------------------------------------
# Canonical types
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    """Canonical usage stats normalized across vendors."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0       # Anthropic: cache_read_input_tokens
                                     # OpenAI: prompt_tokens_details.cached_tokens
                                     # Gemini: cached_content_token_count
    cache_write_tokens: int = 0      # Anthropic: cache_creation_input_tokens
                                     # OpenAI: 0 (no explicit write metric)
                                     # Gemini: 0 (separate cache.create call)
    reasoning_tokens: int = 0        # Anthropic: thinking budget consumed (if exposed)
                                     # OpenAI: completion_tokens_details.reasoning_tokens
                                     # Gemini: 0 (not separately reported)
    vendor: str = ""                 # "anthropic" | "openai" | "gemini"
    model: str = ""                  # canonical model id, as caller passed
    estimated_cost_usd: float = 0.0  # populated by cost_tracking based on pricing tables


@dataclass
class Response:
    """Canonical chat response across vendors.

    ``content`` is a list of Anthropic-style content blocks:
        [{"type": "text", "text": "..."}]
        [{"type": "tool_use", "id": str, "name": str, "input": dict}]
    OpenAI's tool_calls and Gemini's functionCall are translated to
    ``tool_use`` blocks with deterministic `id`s.
    """
    content: list[dict] = field(default_factory=list)
    stop_reason: str = ""              # "end_turn" | "tool_use" | "max_tokens" | "stop_sequence"
    usage: Usage = field(default_factory=Usage)
    raw: Any = None                    # vendor-native response object, for debugging


# ---------------------------------------------------------------------------
# Vendor detection
# ---------------------------------------------------------------------------

_ANTHROPIC_PREFIXES = ("claude-", "claude_")
_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4")
_GEMINI_PREFIXES = ("gemini-", "gemini/")


def detect_vendor(model: str) -> str:
    """Map a model id to its vendor. Raises ValueError if unknown."""
    m = model.lower()
    if any(m.startswith(p) for p in _ANTHROPIC_PREFIXES):
        return "anthropic"
    if any(m.startswith(p) for p in _OPENAI_PREFIXES):
        return "openai"
    if any(m.startswith(p) for p in _GEMINI_PREFIXES):
        return "gemini"
    raise ValueError(
        f"Cannot detect vendor for model {model!r}. "
        f"Known prefixes: {_ANTHROPIC_PREFIXES + _OPENAI_PREFIXES + _GEMINI_PREFIXES}"
    )


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def chat(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    max_tokens: int = 4096,
    cache_hint: bool = False,
    effort: str = "medium",
    **vendor_kwargs: Any,
) -> Response:
    """Vendor-portable chat dispatch.

    Parameters
    ----------
    model : canonical model id, e.g. "claude-sonnet-4-6", "gpt-5",
        "gemini-2.0-pro". Vendor is auto-detected.
    system : system prompt — str or list of Anthropic-style content blocks
        (the latter to allow callers to set their own cache_control).
    messages : list of {"role": "user"|"assistant", "content": str | list[dict]}.
        Content blocks can include tool_use / tool_result for multi-turn
        tool conversations.
    tools : optional list of canonical (Anthropic-style) tool definitions:
        [{"name", "description", "input_schema": <jsonschema>}]
    max_tokens : output token cap.
    cache_hint : mark the system prompt as cacheable (vendor-translated).
    effort : "xhigh" | "high" | "medium" | "low" — translated per vendor.
    **vendor_kwargs : escape-hatch for vendor-specific knobs the canonical
        interface doesn't expose.

    Returns
    -------
    Response with normalized content + stop_reason + Usage.

    Raises
    ------
    ValueError if model can't be vendor-detected, or vendor-specific
    error if the underlying API call fails.
    """
    vendor = detect_vendor(model)
    if vendor == "anthropic":
        return _call_anthropic(
            model, system, messages, tools, max_tokens, cache_hint, effort, **vendor_kwargs
        )
    if vendor == "openai":
        return _call_openai(
            model, system, messages, tools, max_tokens, cache_hint, effort, **vendor_kwargs
        )
    if vendor == "gemini":
        return _call_gemini(
            model, system, messages, tools, max_tokens, cache_hint, effort, **vendor_kwargs
        )
    raise ValueError(f"Unhandled vendor: {vendor}")


# ---------------------------------------------------------------------------
# Translation helpers (shared by OpenAI and Gemini backends)
# ---------------------------------------------------------------------------

def _anthropic_to_openai_tools(tools: list[dict]) -> list[dict]:
    """Translate canonical Anthropic tool defs to OpenAI function-calling shape."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return result


def _anthropic_to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translate Anthropic-style multi-turn messages to OpenAI message shape.

    Handles the two special cases that differ from the simple pass-through:
    - assistant messages whose content contains ``tool_use`` blocks are
      converted to OpenAI's ``tool_calls`` field on the assistant message.
    - user messages whose content contains ``tool_result`` blocks are split
      into one ``{"role": "tool", ...}`` message per result block.

    Plain string content and plain text blocks are passed through unchanged.
    """
    out: list[dict] = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        # Fast path: plain string — no translation needed.
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        # content is a list of blocks.
        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in content:
                btype = block.get("type")
                if btype == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
                elif btype in ("text", "thinking"):
                    text = block.get("text", "")
                    if text:
                        text_parts.append(text)
                # other block types (e.g. redacted_thinking) are silently dropped

            oai_msg: dict[str, Any] = {"role": "assistant"}
            oai_msg["content"] = " ".join(text_parts) if text_parts else None
            if tool_calls:
                oai_msg["tool_calls"] = tool_calls
            out.append(oai_msg)

        elif role == "user":
            # Check if any block is a tool_result; if so, emit tool-role messages.
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if has_tool_result:
                for block in content:
                    if block.get("type") == "tool_result":
                        # tool_result content may itself be str or list of blocks
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rc = " ".join(
                                b.get("text", "") for b in rc if b.get("type") == "text"
                            )
                        out.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": rc,
                        })
                    elif block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            out.append({"role": "user", "content": text})
            else:
                # Flatten text/image blocks into a single user message as a list
                # (LiteLLM accepts the OpenAI multipart list format).
                flat: list[dict] = []
                for block in content:
                    if block.get("type") == "text":
                        flat.append({"type": "text", "text": block.get("text", "")})
                    else:
                        flat.append(block)
                out.append({"role": "user", "content": flat if len(flat) > 1 else (flat[0]["text"] if flat else "")})
        else:
            # system or other roles — pass through as-is
            out.append(msg)

    return out


# ---------------------------------------------------------------------------
# Backend stubs — to be filled in by parallel implementation agents
# ---------------------------------------------------------------------------

def _call_anthropic(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: Optional[list[dict]],
    max_tokens: int,
    cache_hint: bool,
    effort: str,
    **kwargs: Any,
) -> Response:
    """Anthropic backend — uses the native anthropic SDK directly.

    Implementation notes for the agent filling this in:
    - Use the existing `from anthropic import Anthropic` client pattern
      from `agent/critic.py:16`. Module-level client OK; load .env first.
    - If `cache_hint=True`, wrap the system prompt as a list with
      `{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}`.
      If the caller passed `system` as already a list, respect it as-is.
    - Effort tier translation:
        xhigh  → thinking={"type": "enabled", "budget_tokens": 16000}
        high   → thinking={"type": "enabled", "budget_tokens": 8000}
        medium → thinking={"type": "enabled", "budget_tokens": 4000}
        low    → no thinking key
      (Use the adaptive thinking API for Claude 4.6+ models; gracefully
      omit for older models if it errors.)
    - Tool translation: canonical tool dict already matches Anthropic's
      shape — pass through unchanged.
    - Response translation: msg.content is already a list of content
      blocks ({"type": "text"|"tool_use", ...}). Pass through after
      converting from SDK objects to dicts via .model_dump() or manual.
    - Usage: map cache_creation_input_tokens → cache_write_tokens,
      cache_read_input_tokens → cache_read_tokens.
    - stop_reason: pass through Anthropic's value ("end_turn" | "tool_use"
      | "max_tokens" | "stop_sequence").
    """
    # --- System prompt translation ---
    if isinstance(system, list):
        system_param = system
    elif cache_hint:
        system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    else:
        system_param = system  # plain str; SDK accepts it directly

    # --- Effort tier → thinking kwargs ---
    _THINKING_CAPABLE_PREFIXES = ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5")
    _EFFORT_MAP = {
        "xhigh": 16000,
        "high": 8000,
        "medium": 4000,
    }
    thinking_kwargs: dict[str, Any] = {}
    if effort in _EFFORT_MAP and any(model.lower().startswith(p) for p in _THINKING_CAPABLE_PREFIXES):
        budget = _EFFORT_MAP[effort]
        # Anthropic constraint: max_tokens MUST be > thinking.budget_tokens.
        # If caller's max_tokens is too small to fit the thinking budget,
        # silently demote thinking rather than error — caller's max_tokens
        # signals their intent for output size.
        if max_tokens > budget:
            thinking_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # else: skip thinking; caller asked for shorter output than the
        # effort tier's thinking budget, so honor the output cap.
    # "low" or unrecognized effort → omit thinking entirely

    # --- Build call kwargs ---
    call_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_param,
        "messages": messages,
        **thinking_kwargs,
        **kwargs,
    }
    if tools:
        call_kwargs["tools"] = tools

    # --- API call ---
    try:
        msg = _client.messages.create(**call_kwargs)
    except Exception as exc:
        # Don't try to reconstruct the original exception type — anthropic's
        # APIStatusError requires kwargs that we don't have. Just re-raise
        # with chaining; the original exception is preserved as __cause__.
        raise RuntimeError(
            f"Anthropic API call failed for model {model!r}: {exc}"
        ) from exc

    # --- Content translation ---
    content_blocks: list[dict] = []
    for block in msg.content:
        if hasattr(block, "model_dump"):
            content_blocks.append(block.model_dump())
        elif hasattr(block, "type"):
            if block.type == "tool_use":
                content_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            else:
                content_blocks.append({"type": block.type, "text": block.text})
        else:
            content_blocks.append({"type": "unknown", "raw": str(block)})

    # --- Usage translation ---
    u = msg.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        reasoning_tokens=0,
        vendor="anthropic",
        model=model,
        estimated_cost_usd=0.0,
    )

    return Response(
        content=content_blocks,
        stop_reason=msg.stop_reason or "",
        usage=usage,
        raw=msg,
    )


def _call_openai(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: Optional[list[dict]],
    max_tokens: int,
    cache_hint: bool,
    effort: str,
    **kwargs: Any,
) -> Response:
    """OpenAI backend — uses LiteLLM for translation + dispatch.

    Implementation notes:
    - Use `litellm.completion(...)` not the openai SDK directly. LiteLLM
      handles translation and unified streaming.
    - Convert canonical messages format. For a simple `system: str`, prepend
      a {"role": "system", "content": system} message; if `system` is a
      list (Anthropic-style cache blocks), flatten the text into a single
      system message (OpenAI's prompt caching is implicit — no explicit
      cache_control needed; just keep stable prefixes long).
    - Convert canonical tool dict to OpenAI format:
        {"type": "function", "function": {"name", "description",
        "parameters": <input_schema>}}
    - Effort tier translation for o-series/GPT-5:
        xhigh → reasoning_effort="high"
        high  → reasoning_effort="high"
        medium→ reasoning_effort="medium"
        low   → reasoning_effort="low"
      (For gpt-4o and earlier, omit reasoning_effort — they don't accept it.)
    - Response translation:
        Text content → [{"type": "text", "text": choice.message.content}]
        Tool calls → [{"type": "tool_use", "id": call.id, "name":
            call.function.name, "input": json.loads(call.function.arguments)}]
    - Usage: map prompt_tokens → input_tokens, completion_tokens →
      output_tokens, prompt_tokens_details.cached_tokens →
      cache_read_tokens (default 0 if absent), completion_tokens_details
      .reasoning_tokens → reasoning_tokens (default 0).
    - stop_reason mapping:
        "stop"          → "end_turn"
        "tool_calls"    → "tool_use"
        "length"        → "max_tokens"
        "content_filter"→ "stop_sequence"
    """
    # --- System prompt translation ---
    if isinstance(system, list):
        # Flatten Anthropic cache-control blocks; OpenAI caching is implicit.
        system_text = " ".join(
            b.get("text", "") for b in system if b.get("type") == "text"
        )
    else:
        system_text = system  # plain str

    translated: list[dict] = [{"role": "system", "content": system_text}]
    translated.extend(_anthropic_to_openai_messages(messages))

    # --- Tool translation ---
    oai_tools = _anthropic_to_openai_tools(tools) if tools else None

    # --- Effort tier translation (o-series / GPT-5 only) ---
    _REASONING_MODELS = ("o1", "o3", "o4", "gpt-5")
    effort_kwargs: dict[str, Any] = {}
    if any(model.startswith(p) for p in _REASONING_MODELS):
        _effort_map = {
            "xhigh": "high",
            "high": "high",
            "medium": "medium",
            "low": "low",
        }
        if effort in _effort_map:
            effort_kwargs["reasoning_effort"] = _effort_map[effort]
    # For gpt-4, gpt-4o, etc. reasoning_effort is omitted entirely.

    # --- API call via LiteLLM ---
    call_kwargs: dict[str, Any] = {
        "model": model,
        "messages": translated,
        "max_tokens": max_tokens,
        **effort_kwargs,
        **kwargs,
    }
    if oai_tools:
        call_kwargs["tools"] = oai_tools

    response = litellm.completion(**call_kwargs)

    # --- Content translation ---
    choice = response.choices[0]
    msg_obj = choice.message
    content_blocks: list[dict] = []

    if msg_obj.content:
        content_blocks.append({"type": "text", "text": msg_obj.content})

    if hasattr(msg_obj, "tool_calls") and msg_obj.tool_calls:
        for call in msg_obj.tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": call.id,
                "name": call.function.name,
                "input": json.loads(call.function.arguments),
            })

    # --- Stop reason mapping ---
    _stop_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "stop_sequence",
    }
    finish_reason = choice.finish_reason or ""
    stop_reason = _stop_map.get(finish_reason, finish_reason)

    # --- Usage translation ---
    u = response.usage
    cache_read = (
        getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    )
    reasoning = (
        getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
    )
    usage = Usage(
        input_tokens=u.prompt_tokens or 0,
        output_tokens=u.completion_tokens or 0,
        cache_read_tokens=cache_read,
        cache_write_tokens=0,
        reasoning_tokens=reasoning,
        vendor="openai",
        model=model,
        estimated_cost_usd=0.0,
    )

    return Response(content=content_blocks, stop_reason=stop_reason, usage=usage, raw=response)


def _call_gemini(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: Optional[list[dict]],
    max_tokens: int,
    cache_hint: bool,
    effort: str,
    **kwargs: Any,
) -> Response:
    """Gemini backend — uses LiteLLM for translation + dispatch.

    Implementation notes:
    - Use `litellm.completion(model=f"gemini/{model}" if not already prefixed, ...)`.
    - System prompt: Gemini prefers system_instruction at the top level
      rather than a system-role message. LiteLLM should handle this if you
      pass `system` as the first message with role "system".
    - cache_hint: Gemini supports explicit cached content but it's a
      separate API call (cache.create then reference cachedContent).
      v1 of this shim: ignore cache_hint for Gemini (note in log/docs).
    - Effort tier:
        xhigh → thinking_budget=16000
        high  → thinking_budget=8000
        medium→ thinking_budget=4000
        low   → omit (no thinking)
      Pass via litellm's thinking={"type": "enabled", "budget_tokens": N}
      kwarg or vendor-specific kwarg.
    - Tool translation: Gemini uses `function_declarations` shape but
      LiteLLM accepts OpenAI-style tools and converts internally — just
      pass OpenAI-style.
    - Response translation: same as OpenAI backend (LiteLLM normalizes).
    - Usage: same field mapping as OpenAI; cache_read_tokens defaults to 0
      since v1 doesn't use cached content.
    """
    # --- Normalise model name: LiteLLM wants "gemini/<model>" prefix ---
    clean_model = model.replace("gemini/", "")
    litellm_model = f"gemini/{clean_model}"

    # --- System prompt translation (same as OpenAI) ---
    if isinstance(system, list):
        system_text = " ".join(
            b.get("text", "") for b in system if b.get("type") == "text"
        )
    else:
        system_text = system

    translated: list[dict] = [{"role": "system", "content": system_text}]
    translated.extend(_anthropic_to_openai_messages(messages))

    # --- Tool translation (LiteLLM converts OpenAI shape to Gemini internally) ---
    oai_tools = _anthropic_to_openai_tools(tools) if tools else None

    # --- cache_hint is a no-op in v1 ---
    # TODO: Implement Gemini explicit caching via cache.create() + cachedContent
    # reference when litellm exposes a stable interface for it.

    # --- Effort tier → thinking budget ---
    effort_kwargs: dict[str, Any] = {}
    _thinking_map = {
        "xhigh": 16000,
        "high": 8000,
        "medium": 4000,
    }
    if effort in _thinking_map:
        effort_kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": _thinking_map[effort],
        }
    # "low" → omit thinking entirely

    # --- API call via LiteLLM ---
    call_kwargs: dict[str, Any] = {
        "model": litellm_model,
        "messages": translated,
        "max_tokens": max_tokens,
        **effort_kwargs,
        **kwargs,
    }
    if oai_tools:
        call_kwargs["tools"] = oai_tools

    response = litellm.completion(**call_kwargs)

    # --- Content translation (identical to OpenAI path; LiteLLM normalises) ---
    choice = response.choices[0]
    msg_obj = choice.message
    content_blocks: list[dict] = []

    if msg_obj.content:
        content_blocks.append({"type": "text", "text": msg_obj.content})

    if hasattr(msg_obj, "tool_calls") and msg_obj.tool_calls:
        for call in msg_obj.tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": call.id,
                "name": call.function.name,
                "input": json.loads(call.function.arguments),
            })

    # --- Stop reason mapping (same table as OpenAI) ---
    _stop_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "stop_sequence",
    }
    finish_reason = choice.finish_reason or ""
    stop_reason = _stop_map.get(finish_reason, finish_reason)

    # --- Usage translation ---
    u = response.usage
    # Gemini v1 via this shim does not use explicit cached content; default 0.
    cache_read = (
        getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    )
    reasoning = (
        getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
    )
    usage = Usage(
        input_tokens=u.prompt_tokens or 0,
        output_tokens=u.completion_tokens or 0,
        cache_read_tokens=cache_read,
        cache_write_tokens=0,
        reasoning_tokens=reasoning,
        vendor="gemini",
        model=model,
        estimated_cost_usd=0.0,
    )

    return Response(content=content_blocks, stop_reason=stop_reason, usage=usage, raw=response)


# ---------------------------------------------------------------------------
# Convenience: cost tracking integration
# ---------------------------------------------------------------------------

def chat_and_record(
    job_name: str,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    **kwargs: Any,
) -> Response:
    """Wrapper that calls chat() then records cost via agent.cost_tracking.

    Use this from job sites that previously did
    ``client.messages.create(...)`` followed by
    ``cost_tracking.record_call(job_name, model, msg.usage.__dict__)``.
    """
    response = chat(model, system, messages, tools=tools, **kwargs)

    # Lazy import to avoid circular imports at module load
    from agent import cost_tracking  # noqa: PLC0415
    cost_tracking.record_call(
        job_name=job_name,
        model=model,
        usage=response.usage,
    )
    return response
