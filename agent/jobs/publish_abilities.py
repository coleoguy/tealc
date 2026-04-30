"""Publish Tealc's tool + job catalog to data/abilities.json.

Powers the "What I can do" tab on the localhost:8001 dashboard.

Recommended schedule (Wave 5): CronTrigger(day_of_week="wed", hour=5, minute=0, timezone="America/Chicago")

Run manually to test:
    python -m agent.jobs.publish_abilities
"""
import ast
import importlib
import inspect
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
ABILITIES_PATH = os.path.join(_DATA, "abilities.json")
PUBLIC_ABILITIES_PATH = os.path.join(_DATA, "public_abilities.json")
JOBS_DIR = _HERE  # agent/jobs/


# ---------------------------------------------------------------------------
# Tool themes — category → list of name substrings to match
# ---------------------------------------------------------------------------
TOOL_THEMES: dict[str, list[str]] = {
    "Literature & Search": [
        "search_pubmed", "search_biorxiv", "search_openalex", "web_search",
        "fetch_url", "get_paper_of_the_day", "list_recent_papers_of_the_day",
        "get_recent_literature", "list_recent_literature",
    ],
    "Email & Calendar": [
        "list_recent_emails", "draft_email_reply", "list_upcoming_events",
        "create_calendar_event", "update_calendar_event", "delete_calendar_event",
        "find_free_slots", "list_email_triage", "list_pending_service",
        "vip_email",
    ],
    "Google Drive & Docs": [
        "search_drive", "read_drive_file", "create_google_doc",
        "append_to_google_doc", "replace_in_google_doc", "insert_comment_in_google_doc",
        "read_local_file", "read_docx", "read_lab_website",
    ],
    "Sheets & Data": [
        "list_sheets", "read_sheet", "append_rows_to_sheet",
        "update_sheet_cells", "search_sheet",
    ],
    "Research Projects & Goals": [
        "list_research_projects", "get_research_project", "add_research_project",
        "update_research_project", "set_project_next_action", "complete_project_next_action",
        "list_goals", "get_goal", "add_goal", "propose_goal", "update_goal",
        "add_milestone", "decompose_goal", "update_milestone", "list_milestones",
        "list_goal_conflicts", "acknowledge_goal_conflict",
    ],
    "Grants & Funding": [
        "list_grant_opportunities", "dismiss_grant_opportunity",
        "list_overnight_drafts", "review_overnight_draft",
    ],
    "Students": [
        "list_students", "student_dashboard", "log_milestone",
        "log_interaction", "students_needing_attention",
        "student_agenda",
    ],
    "Hypotheses & Science": [
        "list_hypothesis_proposals", "adopt_hypothesis", "reject_hypothesis",
        "track_citations",
    ],
    "Notes & Memory": [
        "save_note", "list_notes", "read_note", "delete_note",
        "recall_past_conversations", "list_recent_sessions",
        "log_decision", "list_executive_decisions",
    ],
    "Planning & Intentions": [
        "add_intention", "list_intentions", "complete_intention",
        "abandon_intention", "update_intention",
        "write_today_plan", "get_today_plan",
    ],
    "Metrics & Reporting": [
        "nas_metrics", "get_nas_impact", "nas_impact",
        "get_latest_weekly_review", "get_latest_quarterly",
        "list_database_flags", "trigger_database_health",
        "list_analysis_runs", "get_analysis_run",
        "retrieval_quality",
    ],
    "System & Utilities": [
        "notify_heath", "get_datetime", "run_r_script",
        "get_idle_class", "get_current_context", "refresh_context",
        "review_recent_drafts", "respond_to_review_invitation",
    ],
    "Self-introspection & Control": [
        "describe_capabilities", "run_scheduled_job",
    ],
}


# Canned example prompts for domain-specific tools
_CANNED_EXAMPLES: dict[str, str] = {
    "search_pubmed": "Show me papers on sex chromosome turnover from the last year",
    "search_biorxiv": "Find recent preprints about karyotype evolution",
    "search_openalex": "Search OpenAlex for fragile Y hypothesis papers",
    "list_grant_opportunities": "Show me open grant opportunities relevant to NAS projects",
    "dismiss_grant_opportunity": "Dismiss that grant opportunity — not a good fit for Heath's lab",
    "list_hypothesis_proposals": "What new hypotheses has Tealc proposed recently?",
    "adopt_hypothesis": "Adopt the top hypothesis for the sex chromosome project",
    "reject_hypothesis": "Reject hypothesis #3 — not feasible with current data",
    "student_dashboard": "Show me the student dashboard for Heath's current students",
    "list_goals": "List Heath's active high-importance goals",
    "list_research_projects": "Show me all active research projects",
    "get_latest_weekly_review": "Pull up the latest weekly review",
    "nas_metrics_trend": "Show me the NAS metric trend over the past 6 months",
    "track_citations": "How are Heath's papers performing on citations this year?",
    "list_overnight_drafts": "Show me the unreviewed overnight drafts",
    "recall_past_conversations": "What did we discuss about the NAS grant last month?",
}


def _example_prompt(name: str, desc: str) -> str:
    if name in _CANNED_EXAMPLES:
        return _CANNED_EXAMPLES[name]
    desc_lower = (desc or "").lower()
    if any(kw in desc_lower for kw in ("researcher", "nas", "grant", "student")):
        return f"Try: 'show me {name.replace('_', ' ')} for Heath'"
    return f"Try: 'show me {name} output'"


def _categorize_tool(name: str) -> str:
    for category, prefixes in TOOL_THEMES.items():
        for p in prefixes:
            if name == p or name.startswith(p) or p in name:
                return category
    return "Other"


# ---------------------------------------------------------------------------
# Tool introspection
# ---------------------------------------------------------------------------

def _get_tool_catalog() -> list[dict]:
    try:
        from agent.tools import get_all_tools  # noqa: PLC0415
        tools = get_all_tools()
    except Exception as e:
        return [{"category": "Error", "tools": [{"name": "import_error", "summary": str(e), "example_prompt": ""}]}]

    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", str(t))
        # Get docstring — langchain tools expose .description or .func.__doc__
        desc = ""
        if hasattr(t, "description") and t.description:
            desc = t.description.strip().split("\n")[0]
        elif hasattr(t, "func") and t.func and t.func.__doc__:
            desc = t.func.__doc__.strip().split("\n")[0]
        elif hasattr(t, "__doc__") and t.__doc__:
            desc = t.__doc__.strip().split("\n")[0]

        # Parameters
        params: list[str] = []
        try:
            if hasattr(t, "args_schema") and t.args_schema:
                schema = t.args_schema.model_fields if hasattr(t.args_schema, "model_fields") else {}
                params = list(schema.keys())
            elif hasattr(t, "func") and t.func:
                sig = inspect.signature(t.func)
                params = [
                    p for p in sig.parameters
                    if p not in ("self", "cls")
                ]
        except Exception:
            pass

        cat = _categorize_tool(name)
        entry = {
            "name": name,
            "summary": desc[:200] if desc else name,
            "example_prompt": _example_prompt(name, desc),
            "parameters": params,
        }
        by_cat.setdefault(cat, []).append(entry)

    # Sort categories, with "Other" last
    ordered_cats = [c for c in TOOL_THEMES if c in by_cat]
    if "Other" in by_cat:
        ordered_cats.append("Other")

    return [
        {"category": cat, "tools": sorted(by_cat[cat], key=lambda x: x["name"])}
        for cat in ordered_cats
    ]


# ---------------------------------------------------------------------------
# Job introspection
# ---------------------------------------------------------------------------

def _first_line_of_docstring(path: str) -> str:
    """Parse the module-level docstring without importing it."""
    try:
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
        ):
            doc = str(tree.body[0].value.value).strip()
            return doc.split("\n")[0]
    except Exception:
        pass
    return ""


def _schedule_from_docstring(path: str) -> str:
    """Extract schedule hint from module docstring (looks for 'Recommended schedule')."""
    try:
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
        ):
            doc = str(tree.body[0].value.value)
            for line in doc.split("\n"):
                if "Recommended schedule" in line or "schedule:" in line.lower():
                    return line.strip().lstrip("#").strip()
    except Exception:
        pass
    return ""


def _get_job_catalog() -> list[dict]:
    jobs = []
    for fname in sorted(os.listdir(JOBS_DIR)):
        if not fname.endswith(".py"):
            continue
        if fname in ("__init__.py",):
            continue
        path = os.path.join(JOBS_DIR, fname)
        name = fname[:-3]  # strip .py
        summary = _first_line_of_docstring(path)
        schedule = _schedule_from_docstring(path)
        jobs.append({
            "name": name,
            "summary": summary[:200] if summary else name,
            "schedule": schedule,
        })
    return jobs


# ---------------------------------------------------------------------------
# DB table count
# ---------------------------------------------------------------------------

def _table_count() -> int:
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        (n,) = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

def _build_public_catalog(tool_catalog: list[dict], job_catalog: list[dict],
                          counts: dict, generated_at: str) -> dict:
    """Return a privacy-safe version of the catalog for publication to the
    monitoring site.  Never includes tool parameters, internal module paths,
    or example prompts that could leak Heath-specific context.

    Emits category counts + example tool names only; omits tool summaries
    (which may hint at Heath's workflows).  For jobs, emits name + cadence
    label only — no docstring leakage.
    """
    # Reuse publish_aquarium's privacy-safe job labels so the public feed and
    # this catalog stay in sync.
    try:
        from agent.jobs.publish_aquarium import _JOB_LABELS as _AQ_LABELS  # noqa: PLC0415
    except Exception:
        _AQ_LABELS = {}

    def _cadence(schedule: str) -> str:
        s = (schedule or "").lower()
        if "interval" in s or "minute" in s:
            return "continuous"
        if "day_of_week" in s or any(d in s for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
            return "weekly"
        if "crontrigger" in s:
            return "daily"
        return "on_demand"

    public_tools = []
    for cat in tool_catalog:
        public_tools.append({
            "category": cat.get("category", ""),
            "count": len(cat.get("tools", [])),
            "examples": [t.get("name", "") for t in cat.get("tools", [])[:3]],
        })

    public_jobs = []
    for j in job_catalog:
        name = j.get("name", "")
        label = _AQ_LABELS.get(name, ("tool", name.replace("_", " ").capitalize()))
        public_jobs.append({
            "name": name,
            "cadence": _cadence(j.get("schedule", "")),
            "description": label[1],   # privacy-safe verb phrase
            "icon_type": label[0],
        })

    return {
        "generated_at": generated_at,
        "counts": counts,
        "tool_categories": public_tools,
        "jobs": public_jobs,
        "notes": (
            "This file is a privacy-safe summary of Tealc's capabilities "
            "intended for publication on the lab monitoring page. It contains "
            "category counts, tool names, job names, and privacy-safe verb "
            "phrases — no content, no per-row data, no Heath-specific context."
        ),
    }


@tracked("publish_abilities")
def job() -> str:
    tool_catalog = _get_tool_catalog()
    job_catalog = _get_job_catalog()
    n_tools = sum(len(cat["tools"]) for cat in tool_catalog)
    n_jobs = len(job_catalog)
    n_tables = _table_count()
    generated_at = datetime.now(timezone.utc).isoformat()
    counts = {"tools": n_tools, "jobs": n_jobs, "tables": n_tables}

    abilities = {
        "generated_at": generated_at,
        "counts": counts,
        "tool_catalog": tool_catalog,
        "job_catalog": job_catalog,
    }

    with open(ABILITIES_PATH, "w") as f:
        json.dump(abilities, f, indent=2)

    public = _build_public_catalog(tool_catalog, job_catalog, counts, generated_at)
    try:
        with open(PUBLIC_ABILITIES_PATH, "w") as f:
            json.dump(public, f, indent=2)
    except Exception as exc:
        print(f"[publish_abilities] public catalog write failed: {exc}")

    return f"wrote {n_tools} tools, {n_jobs} jobs (private + public catalogs)"


if __name__ == "__main__":
    print(job())
