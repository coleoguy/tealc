"""Retrieval quality monitor — daily Haiku-scored audit of overnight literature relevance.

Recommended schedule:
    CronTrigger(hour=6, minute=15, timezone="America/Chicago")   # daily 6:15am Central

Run manually to test:
    python -m agent.jobs.retrieval_quality_monitor
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

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = (
    "You score the relevance of a scientific paper to a specific research project. "
    "Score 1=irrelevant, 2=loosely related, 3=relevant domain but peripheral, "
    "4=directly informs the project, 5=perfectly on-topic for the project's active question. "
    'Reply with JSON: {"score": int, "reasoning": "<1-2 sentence explanation>"}'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_quality (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at      TEXT NOT NULL,
            source_job      TEXT NOT NULL,
            project_id      INTEGER,
            paper_doi       TEXT,
            paper_title     TEXT,
            relevance_score INTEGER,
            critic_reasoning TEXT,
            critic_model    TEXT
        )
    """)
    conn.commit()


def _record_cost(job_name: str, model: str, usage) -> None:
    try:
        from agent.cost_tracking import record_call  # noqa: PLC0415
        record_call(job_name=job_name, model=model, usage=usage)
    except (ImportError, Exception):
        pass


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("retrieval_quality_monitor")
def job() -> str:
    client = Anthropic()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    sampled_at = now.isoformat()

    # 1. Sample recent literature_notes
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)

    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='literature_notes'"
        ).fetchone()
        if not tbl:
            conn.close()
            return "no new literature notes in past 24h"

        rows = conn.execute(
            "SELECT id, project_id, title, doi, raw_abstract "
            "FROM literature_notes "
            "WHERE created_at > ? "
            "ORDER BY RANDOM() LIMIT 3",
            (cutoff,),
        ).fetchall()
    except Exception as e:
        conn.close()
        return f"no new literature notes in past 24h: {e}"

    if not rows:
        conn.close()
        return "no new literature notes in past 24h"

    n = len(rows)
    scores_this_run: list[int] = []

    for note_id, project_id, title, doi, raw_abstract in rows:
        # 2. Look up the research project
        try:
            proj_row = conn.execute(
                "SELECT name, description, current_hypothesis, next_action "
                "FROM research_projects WHERE id=?",
                (project_id,),
            ).fetchone()
        except Exception:
            proj_row = None

        if not proj_row:
            continue  # skip if project not found

        proj_name, proj_desc, proj_hyp, proj_next = proj_row
        current_q = proj_hyp or proj_next or ""

        # 3. Score with Haiku
        user_msg = (
            f"Project: {proj_name}\n"
            f"Project description: {proj_desc or ''}\n"
            f"Current question: {current_q}\n\n"
            f"Paper title: {title or ''}\n"
            f"Paper abstract (first 500 chars): {(raw_abstract or '')[:500]}"
        )

        relevance_score = None
        critic_reasoning = None

        try:
            msg = client.messages.create(
                model=_MODEL,
                max_tokens=300,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw_text = msg.content[0].text.strip()
            # Strip markdown fences if present
            if raw_text.startswith("```"):
                parts = raw_text.split("```")
                raw_text = parts[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            parsed = json.loads(raw_text)
            relevance_score = int(parsed["score"])
            critic_reasoning = str(parsed.get("reasoning", ""))
            scores_this_run.append(relevance_score)

            # 7. Record cost
            _record_cost("retrieval_quality_monitor", _MODEL, msg.usage)

        except json.JSONDecodeError as err:
            critic_reasoning = f"parse failure: {err}"
        except Exception as err:
            critic_reasoning = f"api error: {err}"

        # 5. INSERT row into retrieval_quality
        conn.execute(
            """INSERT INTO retrieval_quality
               (sampled_at, source_job, project_id, paper_doi, paper_title,
                relevance_score, critic_reasoning, critic_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sampled_at,
                "nightly_literature_synthesis",
                project_id,
                doi,
                title,
                relevance_score,
                critic_reasoning,
                _MODEL,
            ),
        )
        conn.commit()

    # 6. Compute 7-day rolling mean
    week_ago = (now - timedelta(days=7)).isoformat()
    try:
        agg = conn.execute(
            "SELECT AVG(relevance_score), COUNT(*) FROM retrieval_quality "
            "WHERE sampled_at > ? AND relevance_score IS NOT NULL",
            (week_ago,),
        ).fetchone()
        rolling_mean = agg[0] or 0.0
        rolling_count = agg[1] or 0
    except Exception:
        rolling_mean = 0.0
        rolling_count = 0

    # Insert drift briefing if quality is low and we have enough samples
    if rolling_mean < 3.0 and rolling_count >= 15:
        sample_lines = "\n".join(
            f"- score={s}" for s in scores_this_run
        )
        briefing_content = (
            f"7-day mean relevance score: **{rolling_mean:.2f}** "
            f"({rolling_count} samples)\n\n"
            f"Scores from this run:\n{sample_lines}\n\n"
            "_Consider reviewing the search keywords for active projects._"
        )
        try:
            conn.execute(
                "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
                "VALUES ('retrieval_quality_drift', 'warn', ?, ?, ?)",
                (
                    f"Retrieval quality trending low (7-day mean: {rolling_mean:.2f})",
                    briefing_content,
                    sampled_at,
                ),
            )
            conn.commit()
        except Exception:
            pass  # briefing failure must not crash the job

    conn.close()
    return f"sampled {n} entries, mean_score={rolling_mean:.2f}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
