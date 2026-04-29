"""Task-aware model selection with routing decision logging."""
import sqlite3
from datetime import datetime, timezone

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


def choose_model(
    task_type: str,
    complexity_hint: str | None = None,
    require_opus: bool = False,
    log: bool = True,
) -> str:
    """Return the canonical model string for a given task type."""
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

    return model
