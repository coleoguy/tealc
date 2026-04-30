"""Privacy classifier for the public Tealc activity feed.

Rule: anything written to the aquarium JSON must pass through public_event().
public_event() returns a dict with ONLY {time, type, description}. It never
includes raw tool_input or tool_output strings unless the tool is in
PUBLIC_RESEARCH_TOOLS *and* the input passes the keyword denylist.
"""
from __future__ import annotations

import re
from typing import Any

# Tools whose input is safe to expose (public scientific literature only).
PUBLIC_RESEARCH_TOOLS = {
    "search_pubmed", "search_biorxiv", "search_openalex", "track_citations",
    "read_lab_website", "get_datetime",
}

# Per-tool generic label for the aquarium (used for everything not in
# PUBLIC_RESEARCH_TOOLS, and as the fallback when a research query hits the
# denylist). Add new tools here as they're built.
GENERIC_LABELS = {
    # Existing private-class tools
    "list_recent_emails":      ("email",    "Processed emails"),
    "draft_email_reply":       ("email",    "Drafted an email reply"),
    "list_upcoming_events":    ("calendar", "Reviewed schedule"),
    "search_drive":            ("drive",    "Searched the lab archive"),
    "read_drive_file":         ("read",     "Reviewed a research document"),
    "read_local_file":         ("read",     "Reviewed a local document"),
    "save_note":               ("note",     "Saved a research note"),
    "list_notes":              ("note",     "Reviewed notes"),
    "read_note":               ("note",     "Read a note"),
    "delete_note":             ("note",     "Cleaned up a note"),
    "web_search":              ("search",   "Searched the web"),
    # Tools added by Tasks 1-10
    "notify_heath":            ("tool",     "Sent a notification"),
    "read_docx_with_comments": ("read",     "Reviewed a draft with comments"),
    "read_wiki_handoff":       ("read",     "Reviewed wiki authoring spec"),
    "send_ntfy_to_heath":      ("tool",     "Pushed an urgent notification"),
    "read_drive_docx":         ("read",     "Reviewed a research document"),
    "create_google_doc":       ("drive",    "Drafted a new document"),
    "append_to_google_doc":    ("drive",    "Updated a document"),
    "replace_in_google_doc":   ("drive",    "Edited a document"),
    "insert_comment_in_google_doc": ("drive", "Added a comment to a document"),
    "create_calendar_event":   ("calendar", "Updated schedule"),
    "update_calendar_event":   ("calendar", "Updated schedule"),
    "delete_calendar_event":   ("calendar", "Updated schedule"),
    "find_free_slots":         ("calendar", "Reviewed schedule"),
    "run_r_script":            ("tool",     "Ran a phylogenetic analysis"),
    "list_sheets_in_spreadsheet": ("drive", "Queried a database"),
    "read_sheet":              ("drive",    "Queried a database"),
    "append_rows_to_sheet":    ("drive",    "Updated a database"),
    "update_sheet_cells":      ("drive",    "Updated a database"),
    "search_sheet":            ("drive",    "Queried a database"),
    "list_grant_opportunities":("search",   "Scanned funding opportunities"),
    "dismiss_grant_opportunity":("tool",    "Updated funding tracker"),
    "list_students":           ("tool",     "Reviewed lab roster"),
    "student_dashboard":       ("tool",     "Reviewed student progress"),
    "log_milestone":           ("tool",     "Updated student milestone"),
    "log_interaction":         ("tool",     "Logged a meeting"),
    "students_needing_attention": ("tool",  "Reviewed student progress"),
    # Pending intentions queue
    "add_intention":              ("note",  "Updated to-do queue"),
    "list_intentions":            ("note",  "Reviewed to-do queue"),
    "complete_intention":         ("note",  "Updated to-do queue"),
    "abandon_intention":          ("note",  "Updated to-do queue"),
    "update_intention":           ("note",  "Updated to-do queue"),
    # Rolling context snapshot
    "get_idle_class":             ("tool",  "Reviewed availability"),
    "get_current_context":        ("tool",  "Reviewed current state"),
    "refresh_context_now":        ("tool",  "Refreshed current state"),
    # Executive loop audit
    "list_executive_decisions":   ("tool",  "Reviewed executive log"),
    # Email triage subagent
    "list_email_triage_decisions":   ("email", "Reviewed inbox triage"),
    "list_pending_service_requests": ("email", "Reviewed service requests"),
    "review_recent_drafts":          ("email", "Reviewed drafts"),
    "respond_to_review_invitation":  ("email", "Reviewed a peer-review invitation"),
    # Paper of the day
    "get_paper_of_the_day":           ("read", "Reviewed paper of the day"),
    "list_recent_papers_of_the_day":  ("read", "Reviewed paper history"),
    # Long-term conversation memory
    "recall_past_conversations":      ("note", "Searched past conversations"),
    "list_recent_sessions":           ("note", "Reviewed past sessions"),
    # Weekly self-review
    "get_latest_weekly_review":       ("tool", "Reviewed weekly self-review"),
    # Quarterly retrospective
    "get_latest_quarterly_retrospective": ("tool", "Reviewed quarterly retrospective"),
    # NAS-metric tracker
    "get_latest_nas_metrics":         ("cite", "Reviewed NAS metrics"),
    "nas_metrics_trend":              ("cite", "Reviewed NAS metric trend"),
    # NAS impact scoring
    "get_nas_impact_trend":           ("cite", "Reviewed NAS impact"),
    # Task 11 — Goals Sheet
    "list_goals":                     ("tool", "Reviewed goals"),
    "get_goal":                       ("tool", "Reviewed goals"),
    "add_goal":                       ("tool", "Updated goals"),
    "propose_goal_from_idea":         ("note", "Captured a goal idea"),
    "update_goal":                    ("tool", "Updated goals"),
    "add_milestone_to_goal":          ("tool", "Updated goals"),
    "update_milestone":               ("tool", "Updated goals"),
    "list_milestones_for_goal":       ("tool", "Reviewed goals"),
    "write_today_plan":               ("tool", "Updated today's plan"),
    "get_today_plan":                 ("tool", "Reviewed today's plan"),
    "log_decision":                   ("tool", "Logged a decision"),
    "decompose_goal":                  ("note", "Decomposed a goal"),
    # Goal-conflict surfacing
    "list_goal_conflicts":             ("tool", "Reviewed goal conflicts"),
    "acknowledge_goal_conflict":       ("tool", "Acknowledged a conflict"),
    # Research project abstraction
    "list_research_projects":          ("tool", "Reviewed projects"),
    "get_research_project":            ("tool", "Reviewed projects"),
    "add_research_project":            ("tool", "Added a project"),
    "update_research_project":         ("tool", "Updated projects"),
    "set_project_next_action":         ("tool", "Updated next action"),
    "complete_project_next_action":    ("tool", "Updated projects"),
    # Overnight grant drafter
    "list_overnight_drafts":           ("drive", "Reviewed overnight drafts"),
    "review_overnight_draft":          ("drive", "Marked draft reviewed"),
    # Database health
    "list_database_flags":             ("drive", "Reviewed database health"),
    "trigger_database_health_check":   ("drive", "Ran database health check"),
    # Overnight comparative analysis
    "list_analysis_runs":              ("tool",  "Reviewed analyses"),
    "get_analysis_run_detail":         ("tool",  "Reviewed analysis detail"),
    # Weekly hypothesis generator
    "list_hypothesis_proposals":       ("tool",  "Reviewed hypotheses"),
    "adopt_hypothesis":                ("tool",  "Adopted a hypothesis"),
    "reject_hypothesis":               ("tool",  "Rejected a hypothesis"),
    # Web fetching
    "fetch_url":                       ("read",  "Fetched a web page"),
    "fetch_url_links":                 ("read",  "Reviewed page links"),
    # v2: output ledger + critic + preference learning + observability
    "list_output_ledger":         ("tool", "Reviewed research output log"),
    "get_output_ledger_entry":    ("tool", "Reviewed a research output"),
    "list_retrieval_quality":     ("tool", "Reviewed retrieval quality"),
    "list_aquarium_audit":        ("tool", "Reviewed privacy audit"),
    "get_cost_summary":           ("tool", "Reviewed cost telemetry"),
    "list_preference_signals":    ("tool", "Reviewed preference history"),
    "record_preference_signal":   ("tool", "Captured a preference signal"),
    "list_analysis_bundles":      ("tool", "Reviewed reproducibility bundles"),
    "get_activity_report":        ("tool", "Reviewed recent activity"),
    "export_state_to_sheet":      ("drive",  "Updated the Goals Sheet"),
    "run_python_script":          ("tool",   "Ran a Python analysis"),
    "inspect_project_data":       ("tool",   "Inspected project data"),
    "propose_data_dir":           ("tool",   "Proposed a data directory"),
    "pre_submission_review":      ("tool",   "Ran pre-submission review"),
    "enter_war_room":             ("tool",   "Entered war-room focus mode"),
    # v6: External Science APIs
    "fetch_paper_full_text":      ("read",   "Read a paper's full text"),
    "search_literature_full_text":("search", "Searched open-access literature"),
    "get_citation_contexts":      ("cite",   "Reviewed citation contexts"),
    "get_paper_recommendations":  ("search", "Found related papers"),
    "get_my_author_profile":      ("cite",   "Reviewed author profile"),
    "get_phylogenetic_tree":      ("tool",   "Built a phylogenetic tree"),
    "get_divergence_time":        ("tool",   "Looked up divergence time"),
    "resolve_taxonomy":           ("tool",   "Resolved taxonomy"),
    "search_sra_runs":            ("search", "Searched SRA"),
    "search_funded_grants":       ("search", "Searched funded grants"),
    "get_species_distribution":   ("tool",   "Reviewed species distribution"),
    "list_zenodo_deposits":       ("drive",  "Reviewed Zenodo deposits"),
    # v7: Knowledge Map
    "find_resource":    ("tool", "Looked up a resource"),
    "add_resource":     ("tool", "Cataloged a resource"),
    "list_resources":   ("tool", "Reviewed the knowledge map"),
    "confirm_resource": ("tool", "Confirmed a resource"),
    "update_resource":  ("tool", "Updated a resource"),
    # Zenodo write-side (PRIVATE — touches Heath's Zenodo account)
    "zenodo_create_deposit":   ("drive", "Registered a Zenodo dataset"),
    "zenodo_upload_file":      ("drive", "Uploaded a file to Zenodo"),
    "zenodo_publish_deposit":  ("drive", "Published a Zenodo deposit"),
    # Open Lab Notebook publish controls
    "request_publish_artifact": ("tool", "Queued an artifact for the public notebook"),
    "unpublish_artifact":        ("tool", "Redacted an artifact from the public notebook"),
    "list_publish_queue":        ("tool", "Reviewed the publish queue"),
    # CrossRef + subagent telemetry
    "resolve_citation":          ("search", "Resolved a citation to DOI"),
    "list_subagent_runs":        ("search", "Listed subagent run history"),
    # Tier 4 corpus helpers (PUBLIC)
    "epmc_cache_full_text":           ("read",   "Cached a full-text article"),
    "s2_search_papers":               ("search", "Searched Semantic Scholar"),
    "gbif_bulk_occurrence_centroid":  ("tool",   "Computed species centroids"),
    "pubmed_batch_fetch":             ("read",   "Fetched PubMed records"),
    "ncbi_assembly_summary":          ("tool",   "Reviewed assembly metadata"),
    "timetree_age_distribution":      ("tool",   "Looked up divergence times"),
    # Prereg-Replication Loop
    "list_pending_preregs":  ("tool", "Reviewed preregistrations"),
    "get_prereg_outcome":    ("tool", "Reviewed a preregistration outcome"),
    # Reviewer Circle
    "list_reviewer_invitations": ("tool", "Reviewed reviewer invitations"),
    "get_reviewer_correlation":  ("tool", "Reviewed reviewer correlations"),
    # Tier 2 — own-record RAG
    "ask_my_record":             ("read", "Searched my own published record"),
    # Subagent spawning — fan-out research
    "spawn_subagent":            ("tool", "Dispatched a research subagent"),
    "spawn_parallel_subagents":  ("tool", "Dispatched parallel research subagents"),
}

# Words/patterns that, if present in a research query, force vagueness.
# (Strategy leaks, people, internal grant codes, anything personal.)
DENY_PATTERNS = [
    # Grant program names / strategy words
    r"\bMIRA\b", r"\bR35\b", r"\bR01\b", r"\bNIGMS\b", r"\bNHGRI\b",
    r"\bSloan\b", r"\bPew\b", r"\bTempleton\b", r"\bKeck\b", r"\bGoogle\.org\b",
    r"\bNSF\s+DEB\b", r"\brenewal\b", r"\bspecific aims\b", r"\bprogram officer\b",
    r"\bCPRIT\b", r"\bDOD\b", r"\bUSDA\b", r"\bRateScape\b",
    # Personal identifiers
    r"@tamu\.edu", r"@gmail\.com", r"\bblackmon\b",
    # Lab people (loaded dynamically below — placeholder)
]

# Loaded at import from data/lab_people.json so this list stays maintainable.
import os, json
_PEOPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "data", "lab_people.json")
LAB_PEOPLE: list[str] = []
try:
    with open(_PEOPLE_PATH) as f:
        LAB_PEOPLE = json.load(f).get("names", [])
except FileNotFoundError:
    LAB_PEOPLE = []

DENY_REGEX = re.compile("|".join(DENY_PATTERNS + [
    rf"\b{re.escape(name)}\b" for name in LAB_PEOPLE
]), re.IGNORECASE) if (DENY_PATTERNS or LAB_PEOPLE) else None


# ---------------------------------------------------------------------------
# Per-kind publish rules for the Open Lab Notebook
# ---------------------------------------------------------------------------

# Kinds that are never publishable regardless of content.
_KIND_ALWAYS_BLOCK = frozenset({
    "grant_draft",          # leaks grant strategy
    "manuscript_section",   # unpublished results
})

# Kinds that default-block unless Heath explicitly approves (decided_by='heath').
_KIND_DEFAULT_BLOCK = frozenset({
    "exploratory_analysis", # may contain unpublished data directions
    "nas_case_packet",      # internal strategy
})

# Kinds that are publishable after DENY_REGEX passes.
_KIND_ALLOW = frozenset({
    "hypothesis",
    "analysis",
    "literature_synthesis",
    "literature_note",
    "undercited_papers",
    "replication_snapshot",
    "weekly_review",
    "paper_of_the_day",
})


def classify_artifact(
    kind: str,
    content_md: str,
    project_id: str | None = None,
    decided_by: str = "auto",
) -> dict:
    """Classify whether an artifact is safe to publish on the public notebook.

    Returns::
        {
          "ok": bool,
          "kind": str,
          "blockers": list[str],   # human-readable reasons for blocking
        }

    Rules (applied in order, first match wins):
    1. Always-block kinds (grant_draft, manuscript_section).
    2. Drafts (kind contains 'draft') block unless decided_by == 'heath'.
    3. Default-block kinds block unless decided_by == 'heath'.
    4. DENY_REGEX scan of content_md — any hit is a blocker.
    5. If no blockers remain, ok=True.
    """
    blockers: list[str] = []

    # Rule 1 — always-block kinds
    if kind in _KIND_ALWAYS_BLOCK:
        blockers.append(f"kind '{kind}' is never publishable (strategy leak)")
        return {"ok": False, "kind": kind, "blockers": blockers}

    # Rule 2 — any 'draft' kind blocks unless Heath explicitly approved
    if "draft" in kind and decided_by != "heath":
        blockers.append(f"kind '{kind}' contains 'draft' — requires explicit Heath approval")

    # Rule 3 — default-block kinds
    if kind in _KIND_DEFAULT_BLOCK and decided_by != "heath":
        blockers.append(f"kind '{kind}' is blocked by default — requires explicit Heath approval")

    # Rule 4 — content denylist scan
    if DENY_REGEX and content_md and DENY_REGEX.search(content_md):
        # Identify the first matching pattern for the blocker message
        match = DENY_REGEX.search(content_md)
        snippet = match.group(0) if match else "unknown"
        blockers.append(f"content matches DENY_REGEX (first match: '{snippet}') — personal or strategic info detected")

    return {"ok": len(blockers) == 0, "kind": kind, "blockers": blockers}


def _query_is_public_safe(q: str) -> bool:
    if not q or len(q) > 120:
        return False
    if DENY_REGEX and DENY_REGEX.search(q):
        return False
    # Reject queries that look like emails or contain @ symbols
    if "@" in q:
        return False
    return True


def public_event(tool_name: str, tool_input: dict, ts_iso: str) -> dict:
    """Return the aquarium-safe event dict for a tool call.
    NEVER pass tool_output here — outputs are never exposed publicly.
    """
    # Default: generic label
    label_type, label_text = GENERIC_LABELS.get(tool_name, ("tool", "Worked on a task"))
    description = label_text

    # Research-public tools may expose limited input
    if tool_name in PUBLIC_RESEARCH_TOOLS:
        if tool_name in {"search_pubmed", "search_biorxiv", "search_openalex"}:
            q = (tool_input or {}).get("query", "")
            if _query_is_public_safe(q):
                description = f"Searched literature: \"{q[:80]}\""
            else:
                description = "Searched the literature"
            label_type = "search"
        elif tool_name == "track_citations":
            description = "Checked citations of lab work"
            label_type = "cite"
        elif tool_name == "read_lab_website":
            description = "Reviewed lab context"
            label_type = "read"
        elif tool_name == "get_datetime":
            description = "Checked the time"
            label_type = "clock"

    return {"time": ts_iso, "type": label_type, "description": description}
