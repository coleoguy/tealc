"""Exploratory analysis job — AI-scientist behavior for Tealc.

Picks ONE active project with a data_dir, generates a Python script via
Sonnet 4.6, executes it, interprets the result, and surfaces a briefing.

Schedule (registered by Wave 3 agent):
    Primary:   CronTrigger(day_of_week="fri", hour=3, minute=0, timezone="America/Chicago")
    Secondary: CronTrigger(day_of_week="tue", hour=3, minute=0, timezone="America/Chicago")

Cost estimate: 2 Sonnet 4.6 calls per run ≈ $0.30–0.60/run ≈ $2–5/month.

Run manually to test:
    python -m agent.jobs.exploratory_analysis
"""
import os
import random
import sqlite3
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECENCY_DAYS = 14          # skip projects analyzed within this window
_MAX_DIR_LISTING_CHARS = 800  # truncate data_dir listing sent to Sonnet

_CODE_WRITER_SYSTEM = """\
You are a research analyst. Given a project description, hypothesis, keywords, \
and a data directory listing, write ONE Python script that produces ONE \
interesting plot or summary statistic. Constraints:
- 30-60 lines total
- Uses pandas/numpy/matplotlib only (no seaborn, no sklearn — keep simple)
- Reads files from the project's data_dir — the directory listing is provided
- Saves plot to `plot.png` in the cwd
- Prints 3-5 lines of summary stats to stdout
- Does NOT modify input data
- If you cannot write a script because the data is insufficient, output only: \
`SKIP: <reason>`

Output ONLY the script (no markdown fences, no explanation), or `SKIP: <reason>`."""

_INTERPRETER_SYSTEM = """\
You are a research scientist. Given a Python script, its stdout, and \
(if exists) a plot it produced, write a 2-3 paragraph interpretation:
- What did this analysis actually find?
- Does it support, contradict, or refine the project's stated hypothesis?
- What would be a sensible next step?
Be honest. If the result is null/uninteresting, say so clearly."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _central_hour() -> int:
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    return datetime.now(ZoneInfo("America/Chicago")).hour


def _last_exploratory_days_ago(project_id: str) -> float:
    """Days since last exploratory_analysis ledger entry for this project."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT created_at FROM output_ledger "
            "WHERE kind='exploratory_analysis' AND project_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        conn.close()
        if not row:
            return float("inf")
        last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).total_seconds() / 86400
    except Exception:
        return float("inf")


def _dir_listing(data_dir: str) -> str:
    """Return a truncated directory listing string for the given path."""
    try:
        entries = []
        for root, dirs, files in os.walk(data_dir):
            # Prune hidden dirs in-place
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel = os.path.relpath(root, data_dir)
            prefix = "" if rel == "." else rel + "/"
            for f in files:
                if not f.startswith("."):
                    entries.append(prefix + f)
            if len("\n".join(entries)) > _MAX_DIR_LISTING_CHARS:
                break
        listing = "\n".join(entries)
        if len(listing) > _MAX_DIR_LISTING_CHARS:
            listing = listing[:_MAX_DIR_LISTING_CHARS] + "\n... (truncated)"
        return listing or "(empty)"
    except Exception as exc:
        return f"(listing failed: {exc})"


def _create_briefing(kind: str, urgency: str, title: str, content_md: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, urgency, title, content_md, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------


@tracked("exploratory_analysis")
def job():
    """Exploratory analysis — picks a project, generates + runs Python, interprets."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("exploratory_analysis"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # Step 1: time guard — only run 0–5am Central. FORCE_RUN=1 bypasses.
    hour = _central_hour()
    if not (0 <= hour < 5) and os.environ.get("FORCE_RUN") != "1":
        return "off-hours"

    # Step 2: pick an eligible project
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        projects = conn.execute(
            "SELECT id, name, description, current_hypothesis, keywords, data_dir "
            "FROM research_projects "
            "WHERE status='active' "
            "  AND data_dir IS NOT NULL AND trim(data_dir) != '' "
            "ORDER BY last_touched_iso DESC"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return f"db_error: {exc}"

    if not projects:
        return "no_eligible_projects"

    # Prefer projects with no entry in the past 14 days; shuffle within tiers
    fresh = [p for p in projects if _last_exploratory_days_ago(p[0]) > _RECENCY_DAYS]
    stale = [p for p in projects if p not in fresh]

    pool = fresh if fresh else stale
    random.shuffle(pool)

    if not pool:
        return "no_eligible_projects"

    chosen = pool[0]
    proj_id, proj_name, proj_desc, hypothesis, keywords, data_dir = chosen

    # Step 4: introspect data_dir
    try:
        from agent.data_introspect import inspect_project_data  # noqa: PLC0415
        introspect_info = inspect_project_data(data_dir)
    except ImportError:
        return "introspect_unavailable"
    except Exception:
        introspect_info = None

    listing = _dir_listing(data_dir)

    # Step 5: ask Sonnet to generate a Python script
    client = Anthropic()
    run_ts = datetime.now(timezone.utc).isoformat()

    user_blob = (
        f"Project: {proj_name}\n"
        f"Description: {proj_desc or '(none)'}\n"
        f"Hypothesis: {hypothesis or '(none)'}\n"
        f"Keywords: {keywords or '(none)'}\n"
        f"data_dir: {data_dir}\n\n"
        f"Files in data_dir:\n{listing}\n"
    )
    if introspect_info:
        user_blob += f"\nIntrospection summary:\n{str(introspect_info)[:400]}\n"

    script_resp = None
    try:
        script_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=_CODE_WRITER_SYSTEM,
            messages=[{"role": "user", "content": user_blob}],
        )
        script_text = script_resp.content[0].text.strip()
    except Exception as exc:
        return f"script_generation_error: {exc}"

    # Step 6: check for SKIP
    if script_text.upper().startswith("SKIP:"):
        reason = script_text[5:].strip()
        try:
            record_output(
                kind="exploratory_analysis",
                job_name="exploratory_analysis",
                model="claude-sonnet-4-6",
                project_id=proj_id,
                content_md=f"Skipped: {reason}",
                tokens_in=getattr(script_resp.usage, "input_tokens", 0) or 0,
                tokens_out=getattr(script_resp.usage, "output_tokens", 0) or 0,
                provenance={"project_id": proj_id, "skip_reason": reason, "data_dir": data_dir},
            )
        except Exception:
            pass
        return f"skipped_by_analyst: {reason}"

    # Step 7: execute the script
    try:
        from agent.python_runtime.executor import run_python  # noqa: PLC0415
    except ImportError:
        return "python_runtime_unavailable"

    try:
        exec_result = run_python(script_text, working_dir=data_dir)
    except Exception as exc:
        return f"python_runtime_error: {exc}"

    stdout = (exec_result.get("stdout") or "")[:2000]
    stderr = (exec_result.get("stderr") or "")[:500]
    exit_code = exec_result.get("exit_code", -1)

    plot_path = os.path.join(data_dir, "plot.png")
    plot_exists = os.path.isfile(plot_path)

    # Step 9: record cost for script-generation call
    try:
        _usage_gen = {
            "input_tokens": getattr(script_resp.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(script_resp.usage, "output_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(script_resp.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(script_resp.usage, "cache_read_input_tokens", 0) or 0,
        }
        record_call(job_name="exploratory_analysis", model="claude-sonnet-4-6", usage=_usage_gen)
    except Exception as _ce:
        print(f"[exploratory_analysis] cost_tracking (gen) error: {_ce}")

    # Step 8: interpret results
    interp_user = (
        f"Python script:\n{script_text}\n\n"
        f"stdout:\n{stdout}\n\n"
        f"stderr:\n{stderr}\n\n"
        f"Exit code: {exit_code}\n"
        f"Plot produced: {'yes — plot.png exists' if plot_exists else 'no'}\n\n"
        f"Project hypothesis: {hypothesis or '(none)'}\n"
    )
    interp_resp = None
    try:
        interp_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=_INTERPRETER_SYSTEM,
            messages=[{"role": "user", "content": interp_user}],
        )
        interpretation = interp_resp.content[0].text.strip()
    except Exception as exc:
        interpretation = f"Interpretation failed: {exc}"

    # Record cost for interpretation call
    try:
        if interp_resp is not None:
            _usage_interp = {
                "input_tokens": getattr(interp_resp.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(interp_resp.usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(interp_resp.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(interp_resp.usage, "cache_read_input_tokens", 0) or 0,
            }
            record_call(job_name="exploratory_analysis", model="claude-sonnet-4-6", usage=_usage_interp)
    except Exception as _ce:
        print(f"[exploratory_analysis] cost_tracking (interp) error: {_ce}")

    # Step 10: write to output ledger
    provenance = {
        "project_id": proj_id,
        "script_first_500": script_text[:500],
        "stdout": stdout[:500],
        "plot_path": plot_path if plot_exists else None,
        "data_dir": data_dir,
        "exit_code": exit_code,
    }
    ledger_id = None
    try:
        _tok_in = getattr(interp_resp.usage, "input_tokens", 0) or 0 if interp_resp else 0
        _tok_out = getattr(interp_resp.usage, "output_tokens", 0) or 0 if interp_resp else 0
        ledger_id = record_output(
            kind="exploratory_analysis",
            job_name="exploratory_analysis",
            model="claude-sonnet-4-6",
            project_id=proj_id,
            content_md=interpretation,
            tokens_in=_tok_in,
            tokens_out=_tok_out,
            provenance=provenance,
        )
    except Exception as _le:
        print(f"[exploratory_analysis] ledger error: {_le}")

    # Step 11: insert briefing
    plot_note = f"\n**Plot:** `{plot_path}`" if plot_exists else "\n**Plot:** not produced"
    ledger_note = f"\n**Ledger entry:** id={ledger_id}" if ledger_id else ""
    briefing_content = (
        f"**Project:** {proj_name}\n"
        f"**data_dir:** `{data_dir}`\n"
        f"**Exit code:** {exit_code}"
        f"{plot_note}"
        f"{ledger_note}\n\n"
        f"---\n\n{interpretation}"
    )
    try:
        _create_briefing(
            kind="exploratory_analysis",
            urgency="info",
            title=f"Exploratory finding: {proj_name}",
            content_md=briefing_content,
        )
    except Exception as _be:
        print(f"[exploratory_analysis] briefing error: {_be}")

    # Step 12: return summary
    return f"analyzed: project={proj_id} plot={plot_path if plot_exists else 'none'}"


# ---------------------------------------------------------------------------
# Manual entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = job()
    print(f"Result: {result}")
