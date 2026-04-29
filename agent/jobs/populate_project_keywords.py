"""Populate per-project retrieval keywords for research_projects with empty keywords.

Recommended schedule: CronTrigger(day_of_week="wed", hour=4, minute=30, timezone="America/Chicago")
— Wednesdays 4:30am Central.

For each active research_project with empty keywords (up to 5 per run), calls
Sonnet to propose 5-10 specific scientific search terms and stores them as a
comma-separated string in the keywords column.

Run manually to test:
    python -m agent.jobs.populate_project_keywords
"""
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# Sonnet system prompt for keyword extraction
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You extract 5-10 scientific search keywords for a research project. "
    "Good keywords are specific terms a literature-search engine would match precisely — "
    "species names, techniques, phenomena, gene/protein names. "
    "Bad keywords are vague (\"evolution\", \"genetics\"). "
    "Output ONLY a JSON array of strings, no preamble. "
    "Example: [\"Coleoptera\", \"karyotype evolution\", \"BiSSE\", \"dysploidy\", "
    "\"chromosomal stasis\", \"microchromosomes\", \"Drosophila sex chromosomes\"]."
)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("populate_project_keywords")
def job() -> str:
    """Populate keywords for active projects that have none (up to 5 per run)."""

    # 1. Time guard: only run 0-7am Central.
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22:
        return f"off-hours (hour={hour})"

    # 2. Pull active projects with empty keywords.
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        rows = conn.execute(
            "SELECT id, name, description, current_hypothesis "
            "FROM research_projects "
            "WHERE status='active' AND (keywords IS NULL OR keywords='') "
            "LIMIT 5"
        ).fetchall()
    except Exception as e:
        conn.close()
        return f"error loading projects: {e}"
    conn.close()

    if not rows:
        return "no_projects_need_keywords"

    client = Anthropic()
    populated = 0

    for proj_id, name, description, hypothesis in rows:
        user_msg = (
            f"Project name: {name}\n"
            f"Description: {description or '(not specified)'}\n"
            f"Current hypothesis: {hypothesis or '(not specified)'}"
        )
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = msg.content[0].text.strip()
            # Tolerate markdown code fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            keywords_list = json.loads(raw)
            if not isinstance(keywords_list, list):
                continue
            keywords_str = ", ".join(str(k).strip() for k in keywords_list if k)
        except Exception:
            continue  # skip this project on parse or API failure

        # Update the DB
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "UPDATE research_projects SET keywords=? WHERE id=?",
                (keywords_str, proj_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            continue

        # Record cost
        try:
            usage = {
                "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
            }
            record_call(
                job_name="populate_project_keywords",
                model="claude-sonnet-4-6",
                usage=usage,
            )
        except Exception:
            pass

        populated += 1

    return f"populated_keywords: {populated} project(s)"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
