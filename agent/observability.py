"""Langfuse observability scaffolding for Tealc.

What this module does
---------------------
Provides lightweight, opt-in tracing of Anthropic API calls via Langfuse
(https://langfuse.com).  When the package is installed and the three env vars
are set, every function decorated with @traced() automatically records a
Langfuse trace/generation with inputs, outputs, model, token usage, latency,
and arbitrary metadata.  When the package is absent or the env vars are unset
the module is a complete no-op — nothing crashes and decorated functions return
exactly their normal results.

How to enable
-------------
1. Install the package (NOT in requirements.txt — this is an opt-in dependency):
       pip install langfuse

2. Set three env vars in your .env file (same file used by every other Tealc
   module):
       LANGFUSE_PUBLIC_KEY=pk-lf-...
       LANGFUSE_SECRET_KEY=sk-lf-...
       LANGFUSE_HOST=https://us.cloud.langfuse.com   # optional, this is the default

3. Verify with:
       PYTHONPATH=. python -c "from agent.observability import enabled; print(enabled())"

How @traced is used by a job (integration example)
---------------------------------------------------
When wiring observability into a job, import `traced` and wrap the function
that makes the Anthropic API call:

    from agent.observability import traced

    @traced(name="nightly_literature_synthesis.extract_findings",
            job="nightly_literature_synthesis",
            paper_doi="10.1234/example")
    def _extract_findings(client, project_name, hypothesis, keywords, paper):
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return json.loads(msg.content[0].text), msg

The decorator captures the args dict, the return value, model + usage from the
Anthropic response (if the return value is or contains an Anthropic Message),
and any **metadata kwargs passed to @traced().

No-op guarantee
---------------
If `langfuse` is not installed OR the env vars are absent, every function
decorated with @traced() behaves identically to the unwrapped function.
`get_langfuse_client()` returns None.  `enabled()` returns False.
`score_output()` silently does nothing.  No import error is ever raised.
"""
from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load env vars from .env (same pattern as every other Tealc module)
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

log = logging.getLogger("tealc.observability")

# ---------------------------------------------------------------------------
# Lazy / deferred import of langfuse — absence must not break Tealc
# ---------------------------------------------------------------------------
try:
    from langfuse import Langfuse  # type: ignore
    _LANGFUSE_AVAILABLE = True
except ImportError:
    Langfuse = None  # type: ignore[assignment,misc]
    _LANGFUSE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Env-var constants
# ---------------------------------------------------------------------------
_PUBLIC_KEY: str = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
_SECRET_KEY: str = os.environ.get("LANGFUSE_SECRET_KEY", "")
_HOST: str = os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com")

# ---------------------------------------------------------------------------
# Module-level memoized client — initialized at most once
# ---------------------------------------------------------------------------
_client: "Langfuse | None" = None
_client_initialized: bool = False
_warned_once: bool = False


def get_langfuse_client() -> "Langfuse | None":
    """Return a memoized Langfuse client, or None if unavailable / unconfigured.

    Initialization is lazy — the first call does the work; subsequent calls
    return the cached result immediately.  Never raises.
    """
    global _client, _client_initialized, _warned_once

    if _client_initialized:
        return _client

    _client_initialized = True  # set before any early return so we only try once

    if not _LANGFUSE_AVAILABLE:
        if not _warned_once:
            log.warning(
                "Langfuse package not installed — observability disabled. "
                "Install with: pip install langfuse"
            )
            _warned_once = True
        _client = None
        return None

    if not _PUBLIC_KEY or not _SECRET_KEY:
        if not _warned_once:
            log.warning(
                "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set — "
                "observability disabled. Add them to your .env to enable tracing."
            )
            _warned_once = True
        _client = None
        return None

    try:
        _client = Langfuse(
            public_key=_PUBLIC_KEY,
            secret_key=_SECRET_KEY,
            host=_HOST,
        )
        log.info("Langfuse observability client initialized (host=%s)", _HOST)
    except Exception as exc:
        log.warning("Langfuse client init failed — observability disabled: %s", exc)
        _client = None

    return _client


def enabled() -> bool:
    """Return True if Langfuse is installed, configured, and the client is live."""
    return get_langfuse_client() is not None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_anthropic_usage(value: Any) -> dict:
    """Try to pull model + usage fields from an Anthropic Message or a tuple/list
    whose first/second element is one.  Returns a dict (may be empty)."""
    # Direct Message object
    if hasattr(value, "model") and hasattr(value, "usage"):
        usage_obj = value.usage
        return {
            "model": getattr(value, "model", None),
            "input_tokens": getattr(usage_obj, "input_tokens", None),
            "output_tokens": getattr(usage_obj, "output_tokens", None),
            "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", None),
            "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", None),
        }
    # Tuple / list — many Tealc jobs return (parsed_dict, msg)
    if isinstance(value, (tuple, list)):
        for item in value:
            result = _extract_anthropic_usage(item)
            if result:
                return result
    return {}


def _safe_str(value: Any, max_chars: int = 4096) -> str:
    """Convert to string, truncated for Langfuse metadata limits."""
    try:
        s = str(value)
    except Exception:
        s = "<unrepresentable>"
    return s[:max_chars]


# ---------------------------------------------------------------------------
# @traced decorator
# ---------------------------------------------------------------------------

def traced(name: str, **metadata: Any) -> Callable:
    """Decorator — wraps an Anthropic-calling function with Langfuse tracing.

    Parameters
    ----------
    name:
        Human-readable trace name (e.g. "nightly_literature_synthesis.extract_findings").
    **metadata:
        Arbitrary key/value pairs attached as trace metadata
        (e.g. job="nightly_literature_synthesis", project_id="abc123").

    When Langfuse is unavailable, the decorator is a pure pass-through —
    the wrapped function is called and its return value is returned unchanged.

    Errors raised by the wrapped function are captured in the trace and
    then re-raised so normal error handling is unaffected.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            lf = get_langfuse_client()

            # ----------------------------------------------------------------
            # No-op path — Langfuse not available
            # ----------------------------------------------------------------
            if lf is None:
                return fn(*args, **kwargs)

            # ----------------------------------------------------------------
            # Tracing path
            # ----------------------------------------------------------------
            trace = None
            generation = None
            start_ms = int(time.time() * 1000)

            try:
                # Build a serialisable snapshot of the input args
                input_snapshot: dict = {}
                try:
                    # Positional args: use parameter names from the signature
                    import inspect
                    sig = inspect.signature(fn)
                    bound = sig.bind_partial(*args, **kwargs)
                    bound.apply_defaults()
                    for k, v in bound.arguments.items():
                        # Skip the Anthropic client itself (not serialisable)
                        if hasattr(v, "messages"):
                            input_snapshot[k] = "<Anthropic client>"
                        else:
                            input_snapshot[k] = _safe_str(v, max_chars=1000)
                except Exception:
                    input_snapshot["_args"] = _safe_str(args, max_chars=1000)

                trace = lf.trace(
                    name=name,
                    input=input_snapshot,
                    metadata={**metadata, "function": fn.__qualname__},
                )
                generation = trace.generation(
                    name=name,
                    input=input_snapshot,
                    metadata=metadata,
                    start_time=start_ms,
                )
            except Exception as trace_exc:
                log.debug("Langfuse trace setup failed (non-fatal): %s", trace_exc)
                # Still call the real function even if tracing setup fails
                return fn(*args, **kwargs)

            # ----------------------------------------------------------------
            # Call the real function
            # ----------------------------------------------------------------
            error_to_raise: Exception | None = None
            result: Any = None

            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                error_to_raise = exc
                # Record the error in the generation before re-raising
                try:
                    end_ms = int(time.time() * 1000)
                    if generation is not None:
                        generation.end(
                            output={"error": type(exc).__name__, "message": str(exc)},
                            level="ERROR",
                            status_message=str(exc),
                            end_time=end_ms,
                        )
                    if trace is not None:
                        trace.update(
                            output={"error": type(exc).__name__, "message": str(exc)},
                            metadata={**metadata, "error": True},
                        )
                    lf.flush()
                except Exception as flush_exc:
                    log.debug("Langfuse error recording failed (non-fatal): %s", flush_exc)
                raise error_to_raise  # always re-raise

            # ----------------------------------------------------------------
            # Record success
            # ----------------------------------------------------------------
            try:
                end_ms = int(time.time() * 1000)
                usage_info = _extract_anthropic_usage(result)

                output_snapshot = _safe_str(result, max_chars=2000)

                generation_kwargs: dict = {
                    "output": output_snapshot,
                    "end_time": end_ms,
                }
                if usage_info.get("model"):
                    generation_kwargs["model"] = usage_info["model"]
                if usage_info.get("input_tokens") is not None:
                    generation_kwargs["usage"] = {
                        "input": usage_info["input_tokens"],
                        "output": usage_info["output_tokens"],
                        "unit": "TOKENS",
                    }

                if generation is not None:
                    generation.end(**generation_kwargs)
                if trace is not None:
                    trace.update(output=output_snapshot, metadata={
                        **metadata,
                        **{k: v for k, v in usage_info.items() if v is not None},
                    })

                lf.flush()
            except Exception as record_exc:
                log.debug("Langfuse result recording failed (non-fatal): %s", record_exc)

            return result

        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# LLM-as-judge / human regression scoring
# ---------------------------------------------------------------------------

def score_output(
    trace_id: str,
    name: str,
    value: float,
    comment: str = "",
) -> None:
    """Record an LLM-as-judge or human regression score against a trace.

    Parameters
    ----------
    trace_id:
        The Langfuse trace ID returned by trace.id.
    name:
        Score name / rubric label (e.g. "relevance", "factual_accuracy").
    value:
        Numeric score (typically 0.0–1.0 but Langfuse accepts any float).
    comment:
        Optional human-readable explanation of the score.

    No-op when Langfuse is unconfigured.  Never raises.
    """
    lf = get_langfuse_client()
    if lf is None:
        return

    try:
        lf.score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment or None,
        )
        lf.flush()
    except Exception as exc:
        log.debug("Langfuse score recording failed (non-fatal): %s", exc)
