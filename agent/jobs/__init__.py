"""Job registry utilities for Tealc's background scheduler.

Provides the @tracked() decorator that logs every job run to the
job_runs table in data/agent.db.  Usage:

    from agent.jobs import tracked

    @tracked("morning_briefing")
    def job():
        ...
        return "1 briefing written"   # optional summary string

Also provides run_job_now(name, **kwargs) for on-demand execution from the
chat tool, dashboard, or a manual REPL.  Sets FORCE_RUN=1 so jobs with
working-hours guards still run during the day.
"""
import importlib
import inspect
import os
import sqlite3
import traceback
from datetime import datetime, timezone
from functools import wraps


def tracked(name: str):
    """Decorator that records a row in job_runs for every invocation.

    Opens a fresh SQLite connection per call — never holds one across jobs
    so the WAL-coordinated dual-process setup stays safe.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from agent.scheduler import DB_PATH  # noqa: PLC0415 (late import avoids cycles)

            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            started = datetime.now(timezone.utc).isoformat()

            cur = conn.execute(
                "INSERT INTO job_runs(job_name, started_at, status) "
                "VALUES (?, ?, 'running')",
                (name, started),
            )
            run_id = cur.lastrowid
            conn.commit()

            try:
                result = fn(*args, **kwargs)
                summary = result or ""
                conn.execute(
                    "UPDATE job_runs "
                    "SET finished_at=?, status='success', output_summary=? "
                    "WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), str(summary)[:1000], run_id),
                )
            except Exception:
                conn.execute(
                    "UPDATE job_runs "
                    "SET finished_at=?, status='error', error=? "
                    "WHERE id=?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        traceback.format_exc()[:2000],
                        run_id,
                    ),
                )
                raise
            finally:
                conn.commit()
                conn.close()

            return result

        return wrapper
    return deco


# ---------------------------------------------------------------------------
# On-demand execution — used by the chat tool `run_scheduled_job` and the
# dashboard's POST /api/run_job endpoint.  Bypasses working-hours guards
# via FORCE_RUN=1.
# ---------------------------------------------------------------------------

_KWARG_ALIASES: dict[str, tuple[str, ...]] = {
    # caller-friendly name → sequence of job-parameter names to try in order
    "dry_run": ("dry_run_override", "dry_run"),
    "target":  ("force_slug", "force_term", "force_topic", "target"),
    "verbose": ("verbose",),
}


def list_available_jobs() -> list[str]:
    """Return sorted list of job module names (files under agent/jobs/ with a
    top-level `job()` callable).  Cheap — scans filenames, does NOT import."""
    here = os.path.dirname(os.path.abspath(__file__))
    names = []
    for fname in sorted(os.listdir(here)):
        if not fname.endswith(".py"):
            continue
        if fname.startswith("_") or fname in ("__init__.py",):
            continue
        names.append(fname[:-3])
    return names


def run_job_now(name: str, **kwargs) -> str:
    """Force-run `agent.jobs.<name>.job(**kwargs)` with FORCE_RUN=1.

    `kwargs` may use the caller-friendly names `dry_run`, `target`, `verbose`
    (or any exact parameter name the job declares).  Unknown kwargs and kwargs
    the target job doesn't accept are silently dropped so callers don't have
    to know each job's signature.

    Raises ValueError for invalid/missing job names; re-raises anything the
    job itself raises.  Returns whatever the job returns (usually a string
    summary).
    """
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"invalid job name: {name!r}")
    if name.startswith("_"):
        raise ValueError(f"invalid job name: {name!r}")

    try:
        mod = importlib.import_module(f"agent.jobs.{name}")
    except ModuleNotFoundError as exc:
        raise ValueError(f"no such job: {name}") from exc

    fn = getattr(mod, "job", None)
    if not callable(fn):
        raise ValueError(f"agent.jobs.{name} has no callable job()")

    sig = inspect.signature(fn)
    accepted_params = set(sig.parameters)
    resolved: dict = {}

    for caller_key, value in kwargs.items():
        if value is None:
            continue
        # Exact match wins
        if caller_key in accepted_params:
            resolved[caller_key] = value
            continue
        # Try aliases
        for alt in _KWARG_ALIASES.get(caller_key, ()):
            if alt in accepted_params:
                resolved[alt] = value
                break
        # Unknown kwarg: silently drop

    prev_force = os.environ.get("FORCE_RUN")
    os.environ["FORCE_RUN"] = "1"
    try:
        result = fn(**resolved)
    finally:
        if prev_force is None:
            os.environ.pop("FORCE_RUN", None)
        else:
            os.environ["FORCE_RUN"] = prev_force

    return str(result) if result is not None else ""
