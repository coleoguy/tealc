"""Task-aware model selection with routing decision logging."""
import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from agent.scheduler import DB_PATH

SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"
HAIKU = "claude-haiku-4-5-20251001"

_OPUS_TASKS = frozenset({
    "flagship_paper",
    "grant_narrative",
    "manuscript_draft",
    "critic_pass",
    "quarterly_retrospective",
})
_HAIKU_TASKS = frozenset({
    "email_triage",
    "simple_classification",
    "heartbeat_critic",
    "retrieval_quality_sampling",
    "executive_loop",
})
_SONNET_TASKS = frozenset({
    "chat_default",
    "literature_synthesis",
    "hypothesis_generation",
    "analysis_interpretation",
    "daily_plan",
    "morning_briefing",
    "grant_radar",
    "weekly_review",
})

# Effort tiers for adaptive thinking (used with thinking={type:"adaptive"} in output_config).
# Values: "xhigh" | "high" | "medium" | "low"
EFFORT_TIERS: "dict[str, str]" = {
    # xhigh — deep synthesis / formal review / critic passes
    "cross_project_synthesis": "xhigh",
    "run_formal_hypothesis_pass": "xhigh",
    "formal_hypothesis_pass": "xhigh",
    "pre_submission_review": "xhigh",
    "opus_critic": "xhigh",
    "hypothesis_critique": "xhigh",
    "weekly_review_critic": "xhigh",
    # high — default chat and major drafting / analysis jobs
    "chat": "high",
    "chat_default": "high",
    "nightly_grant_drafter": "high",
    "weekly_comparative_analysis": "high",
    "weekly_comparative_analysis_interpreter": "high",
    "morning_briefing": "high",
    "nightly_grant_drafter_drafter": "high",
    "drafter": "high",
    # medium — routine generation / planning / health checks
    "weekly_hypothesis_generator": "medium",
    "nightly_literature_synthesis": "medium",
    "literature_synthesis": "medium",
    "daily_plan": "medium",
    "midday_check": "medium",
    "nas_pipeline_health": "medium",
    "nas_impact_score": "medium",
    "weekly_review": "medium",
    # low — triage / classification / lightweight scans
    "email_triage": "low",
    "email_triage_classifier": "low",
    "paper_of_the_day": "low",
    "midday_lit_pulse": "low",
    "vip_email_watch": "low",
    "executive": "low",
    "populate_project_keywords": "low",
    "citation_watch": "low",
    "paper_radar": "low",
}


class ModelChoice(NamedTuple):
    """Return value from choose_model(); back-compat with plain str tuple-unpacking."""
    model: str
    effort: str


def choose_model(
    task_type: str,
    complexity_hint: Optional[str] = None,
    require_opus: bool = False,
    log: bool = True,
) -> ModelChoice:
    """Return a ModelChoice(model, effort) for the given task type.

    Back-compatible with callers that treat the return value as a plain string:
    both ``model, effort = choose_model(...)`` and ``choose_model(...).model``
    work, and ``choose_model(...)[0]`` still yields the model name string.
    """
    if require_opus:
        model = OPUS
        reasoning = "require_opus=True override"
    elif task_type in _OPUS_TASKS:
        model = OPUS
        reasoning = f"task_type '{task_type}' is in OPUS_TASKS"
    elif task_type in _HAIKU_TASKS:
        model = HAIKU
        reasoning = f"task_type '{task_type}' is in HAIKU_TASKS"
    elif task_type in _SONNET_TASKS:
        model = SONNET
        reasoning = f"task_type '{task_type}' is in SONNET_TASKS"
    else:
        model = SONNET
        reasoning = f"task_type '{task_type}' unknown — defaulting to SONNET"

    if task_type in EFFORT_TIERS:
        effort = EFFORT_TIERS[task_type]
    else:
        effort = "medium"
        reasoning += f"; effort for '{task_type}' unknown — defaulting to medium"

    if log:
        decided_at = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT INTO model_routing_decisions
               (decided_at, task_type, complexity_hint, chosen_model, reasoning)
               VALUES (?, ?, ?, ?, ?)""",
            (decided_at, task_type, complexity_hint, model, reasoning),
        )
        conn.commit()
        conn.close()

    return ModelChoice(model=model, effort=effort)
