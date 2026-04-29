"""Weekly comparative analysis job — runs Sunday 4am Central via APScheduler.

Picks ONE active research project whose next_action looks like an R analysis,
has Sonnet 4.6 write the R code, runs it via run_r_script, interprets results,
and stores everything in analysis_runs + creates a briefing for Heath.

Run manually to test:
    python -m agent.jobs.weekly_comparative_analysis

Idle threshold: job only runs when idle_class in ('idle', 'deep_idle').
Cost estimate: 1 project/run × 2 Sonnet 4.6 calls ≈ $0.50/run ≈ $2/month.
"""
import json
import os
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
from agent.ledger import record_output, update_critic  # noqa: E402
from agent.critic import critic_pass  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.bundle import package_analysis_run  # noqa: E402
from agent.tools import known_data_resources_summary  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_IDLE_CLASSES = {"idle", "deep_idle"}

# Keywords that indicate an R analysis next_action
_R_KEYWORDS = {
    "run", "analyze", "analyse", "fit", "simulate", "bamm", "phylo",
    "ape", "phytools", "geiger", "diversitree", "tree", "plot", "pgls",
    "ancestral", "ancestral state", "trait", "diversification", "rate",
    "model", "mcmc", "bayesian", "lambda", "kappa", "ou", "bm",
    "brownian", "ornstein", ".R", ".r",
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_CODE_WRITER_SYSTEM = """\
You write R code to execute one specific analysis Heath Blackmon has queued for an active \
research project. Heath's lab uses ape, phytools, geiger, diversitree, tidyverse, ggplot2. \
The project's `data_dir` contains relevant inputs; `output_dir` is where any artifacts \
should be saved.

CRITICAL DATA-RESOURCE RULE: the user message contains a block titled \
"AVAILABLE LAB DATA RESOURCES" listing verified absolute file paths to every registered \
lab database (Coleoptera karyotypes, CURES, epistasis DB, tau DB, etc.) with row and \
column counts. If your analysis needs a lab database, use ONLY the exact paths from that \
block. DO NOT invent file paths, reference placeholders like PASTE_ID or <TO-BE-FILLED>, \
or assume files exist that aren't in the block. If the needed resource is not listed, \
explain in `expected_outputs` that the resource is missing and emit MINIMAL placeholder \
code that prints the shortfall rather than silently producing invalid output. The \
2026-04-21 Fragile Y preregistration was written against an unset Sheet ID — this rule \
prevents that failure.

Output JSON: {"libraries": "comma-separated package names", \
"code": "<the R code>", "expected_outputs": "<what files/plots should appear>"}. \
Do NOT modify Heath's source data files; only read them. Save plots and tables to the \
working directory the runner will set, plus copy any final artifacts to output_dir if \
specified. Do NOT use system() or shell-out from R. Output JSON only."""

_INTERPRETER_SYSTEM = """\
You interpret the output of an R analysis Heath just ran (he's away — you're documenting \
for him to read tomorrow). Be honest about what the data show, including null results. \
If the analysis errored, explain what went wrong and what to try next. \
Output ONLY the markdown interpretation, no preamble. 200-400 words."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_r_shaped_heuristic(text: str) -> bool:
    """Return True if next_action text looks like a runnable R analysis."""
    if not text:
        return False
    lower = text.lower()
    for kw in _R_KEYWORDS:
        if kw.lower() in lower:
            return True
    return False


def _classify_with_haiku(client: Anthropic, next_action: str) -> bool:
    """Ask Haiku whether the next_action is a runnable R analysis.
    Falls back to False on any error."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=128,
            system='Classify whether the following lab task is a runnable R analysis. '
                   'Output JSON only: {"is_r": true|false, "reasoning": "one sentence"}',
            messages=[{"role": "user", "content": next_action}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return bool(data.get("is_r", False))
    except Exception:
        return False


def _last_run_days_ago(project_id: str) -> float:
    """Return days since the last successful analysis_runs row for this project.
    Returns infinity if never run."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT run_iso FROM analysis_runs WHERE project_id=? "
            "ORDER BY run_iso DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        conn.close()
        if not row:
            return float("inf")
        last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - last
        return delta.total_seconds() / 86400
    except Exception:
        return float("inf")


def _insert_analysis_run(row: dict) -> int:
    """Insert a row into analysis_runs and return its id."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.execute(
        """INSERT INTO analysis_runs (
            project_id, run_iso, next_action_text, r_code, working_dir,
            exit_code, stdout_truncated, stderr_truncated,
            plot_paths, created_files, interpretation_md, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["project_id"],
            row["run_iso"],
            row.get("next_action_text"),
            row.get("r_code"),
            row.get("working_dir"),
            row.get("exit_code"),
            row.get("stdout_truncated"),
            row.get("stderr_truncated"),
            row.get("plot_paths"),
            row.get("created_files"),
            row.get("interpretation_md"),
            row.get("outcome"),
        ),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def _create_briefing(kind: str, urgency: str, title: str, content_md: str):
    """Write a briefing row."""
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


@tracked("weekly_comparative_analysis")
def job():
    """Weekly comparative analysis — picks one project, writes R, runs it, interprets."""

    # Step 1: time guard only — scheduled Sunday 4am Central. Idle-class was too strict.
    # Bypass with FORCE_RUN=1 for manual test invocations.
    from datetime import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = _dt.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and not os.environ.get("FORCE_RUN"):
        return f"skipped: working-hours guard (hour={hour}; set FORCE_RUN=1 to bypass)"

    # Step 2: pull active projects with non-empty next_action
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        projects = conn.execute(
            "SELECT id, name, description, current_hypothesis, next_action, "
            "data_dir, output_dir "
            "FROM research_projects "
            "WHERE status='active' AND next_action IS NOT NULL AND trim(next_action) != '' "
            "ORDER BY last_touched_iso DESC"
        ).fetchall()
        conn.close()
    except Exception as e:
        return f"db_error: {e}"

    if not projects:
        return "no_actions_queued"

    # Step 3 & 4: classify and pick the best candidate
    client = Anthropic()
    candidate = None
    candidate_data = None

    for proj_row in projects:
        proj_id, proj_name, proj_desc, hypothesis, next_action, data_dir, output_dir = proj_row

        # Heuristic first (cheap)
        is_r = _is_r_shaped_heuristic(next_action)

        # Haiku fallback if heuristic says no
        if not is_r:
            is_r = _classify_with_haiku(client, next_action)

        if not is_r:
            continue

        # Skip if analyzed in past 7 days
        days_ago = _last_run_days_ago(proj_id)
        if days_ago < 7:
            continue

        candidate = proj_id
        candidate_data = {
            "name": proj_name,
            "description": proj_desc or "",
            "hypothesis": hypothesis or "",
            "next_action": next_action,
            "data_dir": data_dir or "",
            "output_dir": output_dir or "",
        }
        break  # take the first qualifying project (ordered by last_touched_iso)

    if candidate is None:
        return "no_r_actions"

    # Step 6: Sonnet 4.6 writes the R script.
    # Inject the available-resources block so Sonnet uses real paths, not placeholders.
    run_iso = datetime.now(timezone.utc).isoformat()
    try:
        _resources_block = known_data_resources_summary()
    except Exception as _e:
        print(f"[weekly_comparative_analysis] resources summary error: {_e}")
        _resources_block = ""

    user_content = (
        f"Project: {candidate_data['name']}\n"
        f"Description: {candidate_data['description']}\n"
        f"Current hypothesis: {candidate_data['hypothesis']}\n"
        f"Next action (what to do): {candidate_data['next_action']}\n"
        f"data_dir: {candidate_data['data_dir']}\n"
        f"output_dir: {candidate_data['output_dir']}\n\n"
        f"{_resources_block}\n"
    )

    try:
        code_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=_CODE_WRITER_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_code_json = code_resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_code_json.startswith("```"):
            parts = raw_code_json.split("```")
            raw_code_json = parts[1] if len(parts) > 1 else raw_code_json
            if raw_code_json.startswith("json"):
                raw_code_json = raw_code_json[4:]
        code_data = json.loads(raw_code_json)
        r_code = code_data.get("code", "")
        libraries = code_data.get("libraries", "")
    except Exception as e:
        _insert_analysis_run({
            "project_id": candidate,
            "run_iso": run_iso,
            "next_action_text": candidate_data["next_action"],
            "r_code": None,
            "working_dir": None,
            "exit_code": -1,
            "stdout_truncated": "",
            "stderr_truncated": str(e)[:2000],
            "plot_paths": "[]",
            "created_files": "[]",
            "interpretation_md": f"Sonnet failed to write R code: {e}",
            "outcome": "r_error",
        })
        return f"code_write_error: {e}"

    # Step 6b: RESOURCE-PLACEHOLDER GUARD — abort if the R code references
    # unset DB IDs. Matches the 2026-04-21 Fragile Y failure mode at the gate.
    _FORBIDDEN_PLACEHOLDERS = ("PASTE_ID", "<TO-BE-FILLED>", "TO-BE-FILLED", "PASTE_SPREADSHEET_ID")
    _hit = next((p for p in _FORBIDDEN_PLACEHOLDERS if p in (r_code or "")), None)
    if _hit:
        abort_msg = (
            f"R code referenced unset data-resource placeholder '{_hit}'. "
            f"The analysis was aborted before execution to prevent running against "
            f"a non-existent resource. Fix: either fill the missing resource in "
            f"data/known_sheets.json, or revise the next_action to use a registered DB."
        )
        _insert_analysis_run({
            "project_id": candidate,
            "run_iso": run_iso,
            "next_action_text": candidate_data["next_action"],
            "r_code": r_code,
            "working_dir": None,
            "exit_code": -2,
            "stdout_truncated": "",
            "stderr_truncated": abort_msg,
            "plot_paths": "[]",
            "created_files": "[]",
            "interpretation_md": abort_msg,
            "outcome": "resource_placeholder_abort",
        })
        _create_briefing(
            kind="analysis_run",
            urgency="warn",
            title=f"Analysis aborted: {candidate_data['name']} — unset data resource",
            content_md=abort_msg,
        )
        return f"aborted_placeholder: project={candidate} hit={_hit}"

    # Step 7: execute via run_r_script tool
    try:
        from agent.tools import run_r_script  # noqa: PLC0415
        result_str = run_r_script.invoke({
            "code": r_code,
            "libraries": libraries,
            "timeout_seconds": 600,
        })
        # run_r_script returns a JSON string
        result = json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        _insert_analysis_run({
            "project_id": candidate,
            "run_iso": run_iso,
            "next_action_text": candidate_data["next_action"],
            "r_code": r_code,
            "working_dir": None,
            "exit_code": -1,
            "stdout_truncated": "",
            "stderr_truncated": str(e)[:2000],
            "plot_paths": "[]",
            "created_files": "[]",
            "interpretation_md": f"run_r_script invocation failed: {e}",
            "outcome": "r_error",
        })
        return f"run_error: {e}"

    exit_code = result.get("exit_code", -1)
    stdout = (result.get("stdout") or "")[:4000]
    stderr = (result.get("stderr") or "")[:2000]
    working_dir = result.get("working_dir", "")
    plot_paths = json.dumps(result.get("plot_paths") or [])
    created_files = json.dumps(result.get("created_files") or [])
    n_files = len(result.get("created_files") or [])

    # Step 8: Sonnet 4.6 interprets results
    interp_user = (
        f"R code executed:\n```r\n{r_code[:3000]}\n```\n\n"
        f"stdout:\n{stdout}\n\n"
        f"stderr:\n{stderr}\n\n"
        f"Files generated: {created_files}\n"
        f"Exit code: {exit_code}\n"
    )
    interp_resp = None
    try:
        interp_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_INTERPRETER_SYSTEM,
            messages=[{"role": "user", "content": interp_user}],
        )
        interpretation = interp_resp.content[0].text.strip()
    except Exception as e:
        interpretation = f"Interpretation failed: {e}"

    # Step 9: insert into analysis_runs
    outcome = "success" if exit_code == 0 else "r_error"
    run_id = _insert_analysis_run({
        "project_id": candidate,
        "run_iso": run_iso,
        "next_action_text": candidate_data["next_action"],
        "r_code": r_code,
        "working_dir": working_dir,
        "exit_code": exit_code,
        "stdout_truncated": stdout,
        "stderr_truncated": stderr,
        "plot_paths": plot_paths,
        "created_files": created_files,
        "interpretation_md": interpretation,
        "outcome": outcome,
    })

    # Step 9a. Record cost for Sonnet code-writer + interpreter calls
    try:
        for _resp_obj in (code_resp, interp_resp):
            if _resp_obj is None:
                continue
            _ca_usage = {
                "input_tokens": getattr(_resp_obj.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(_resp_obj.usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(_resp_obj.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(_resp_obj.usage, "cache_read_input_tokens", 0) or 0,
            }
            record_call(job_name="weekly_comparative_analysis", model="claude-sonnet-4-6", usage=_ca_usage)
    except Exception as _e:
        print(f"[weekly_comparative_analysis] cost_tracking error: {_e}")

    # Step 9b. Ledger record for the analysis
    _ledger_provenance = {
        "analysis_run_id": run_id,
        "r_code_first_500": r_code[:500],
        "exit_code": exit_code,
        "plot_paths": plot_paths,
        "bundle_path": None,
    }
    ledger_row_id: int | None = None
    try:
        _interp_tok_in = getattr(interp_resp.usage, "input_tokens", 0) or 0 if interp_resp is not None else 0
        _interp_tok_out = getattr(interp_resp.usage, "output_tokens", 0) or 0 if interp_resp is not None else 0
        ledger_row_id = record_output(
            kind="analysis",
            job_name="weekly_comparative_analysis",
            model="claude-sonnet-4-6",
            project_id=candidate,
            content_md=interpretation,
            tokens_in=_interp_tok_in,
            tokens_out=_interp_tok_out,
            provenance=_ledger_provenance,
        )
    except Exception as _e:
        print(f"[weekly_comparative_analysis] ledger error: {_e}")

    # Step 9c. Bundle the analysis run and update ledger provenance with bundle path
    if ledger_row_id is not None:
        try:
            bundle_path = package_analysis_run(run_id)
            _ledger_provenance["bundle_path"] = bundle_path
            # Update the provenance_json in the ledger row
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "UPDATE output_ledger SET provenance_json=? WHERE id=?",
                (json.dumps(_ledger_provenance), ledger_row_id),
            )
            conn.commit()
            conn.close()
        except Exception as _e:
            print(f"[weekly_comparative_analysis] bundle error: {_e}")

    # Step 9d. Critic pass on interpretation
    if ledger_row_id is not None:
        try:
            _critic = critic_pass(interpretation, rubric_name="analysis")
            update_critic(
                ledger_row_id,
                _critic.get("score", 0),
                _critic.get("overall_notes", ""),
                _critic.get("model", ""),
            )
        except Exception as _e:
            print(f"[weekly_comparative_analysis] critic error: {_e}")

    # Step 10: create briefing
    briefing_urgency = "warn" if exit_code == 0 else "info"
    briefing_title = f"Overnight analysis: {candidate_data['name']}"
    briefing_content = (
        f"**Project:** {candidate_data['name']}\n"
        f"**Action run:** {candidate_data['next_action']}\n"
        f"**Exit code:** {exit_code} | **Files produced:** {n_files}\n"
        f"**Working dir:** `{working_dir}`\n\n"
        f"---\n\n{interpretation}"
    )
    _create_briefing(
        kind="analysis_run",
        urgency=briefing_urgency,
        title=briefing_title,
        content_md=briefing_content,
    )

    # Step 11: return summary
    return f"analyzed: project={candidate} exit={exit_code} outputs={n_files}"


# ---------------------------------------------------------------------------
# Manual entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = job()
    print(f"Result: {result}")
