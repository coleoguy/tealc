"""Cross-project synthesis — runs Saturday 4am Central via APScheduler.

Recommended schedule:
    CronTrigger(day_of_week="sat", hour=4, minute=0, timezone="America/Chicago")

Scans ALL active research projects that have a current_hypothesis, then asks
claude-opus-4-7 to identify non-obvious cross-project connections — shared
methods, analogous mechanisms, or unifying hypotheses that span different
organisms, temporal scales, or disciplines.  Results are written to the
output_ledger (kind='cross_project_synthesis') and a consolidated briefing
is inserted into the briefings table.

Cost estimate: 1 Opus call/week ≈ $0.15–0.40 depending on project count.

Run manually to test:
    python -m agent.jobs.cross_project_synthesis
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

_ALLOWED_HOURS = range(0, 8)  # 0–7am Central only (heavy Opus call)

# ---------------------------------------------------------------------------
# Opus system prompt (stable — cached via prompt caching)
# ---------------------------------------------------------------------------

from agent.jobs import SCIENTIST_MODE  # noqa: E402

_SYNTHESIS_SYSTEM = SCIENTIST_MODE + "\n\n" + (
    "You are a systems thinker looking at a scientist's active research programs. "
    "Your job is to find non-obvious CROSS-PROJECT connections — ideas, methods, or "
    "hypotheses that tie two or more active projects together in a way that would be "
    "unusual for a single human to notice.\n\n"
    "Output JSON array of 1-3 proposals. Each proposal:\n"
    "{\n"
    '  "title": "<short name, e.g. \'Apply BiSSE test used in dysploidy to sex-chromosome retention\'>",\n'
    '  "connected_project_ids": ["...", "..."],\n'
    '  "synthesis": "<2-3 sentences: what\'s the shared thread, what new hypothesis emerges>",\n'
    '  "proposed_test": "<one sentence: how would one test this, including (a) what observation would support and (b) what observation would falsify>",\n'
    '  "strongest_counter": "<one sentence naming the most likely reason a careful reviewer would dismiss the connection, and how it could be ruled out>",\n'
    '  "novelty_score": <1-5>,\n'
    '  "feasibility_score": <1-5>,\n'
    '  "why_non_obvious": "<1 sentence: why a reasonable researcher might miss this>"\n'
    "}\n\n"
    "Prefer cross-domain connections (different study organisms, different temporal scales, "
    "different methods). Avoid trivial connections (\"both use phylogenies\"). If no genuine "
    "cross-project connection exists, return an empty array []."
)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("cross_project_synthesis")
def job() -> str:
    """Find non-obvious cross-project connections across all active hypotheses."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("cross_project_synthesis"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # 1. Time guard: only run 0–7am Central (heavy Opus call).
    # FORCE_RUN=1 (set by run_job_now / run_scheduled_job) bypasses the guard
    # so chat-driven manual triggers actually work.
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if hour not in _ALLOWED_HOURS and os.environ.get("FORCE_RUN") != "1":
        return "off-hours"

    # 2. Pull active research projects with non-empty current_hypothesis.
    #    keywords column may not yet exist — fall back gracefully.
    projects = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        # Try with keywords column first.
        try:
            rows = conn.execute(
                "SELECT id, name, description, current_hypothesis, keywords "
                "FROM research_projects "
                "WHERE status='active' "
                "AND current_hypothesis IS NOT NULL "
                "AND current_hypothesis != ''"
            ).fetchall()
            has_keywords = True
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT id, name, description, current_hypothesis "
                "FROM research_projects "
                "WHERE status='active' "
                "AND current_hypothesis IS NOT NULL "
                "AND current_hypothesis != ''"
            ).fetchall()
            has_keywords = False
        conn.close()
        projects = [
            {
                "id": r[0],
                "name": r[1],
                "description": r[2],
                "current_hypothesis": r[3],
                "keywords": r[4] if has_keywords else None,
            }
            for r in rows
        ]
    except Exception as e:
        return f"error_loading_projects: {e}"

    # 3. Need at least 2 projects with hypotheses to find cross-project patterns.
    if len(projects) < 2:
        return "insufficient_hypotheses"

    # 4. Build the synthesis prompt — condensed list (<100 words per project).
    project_blocks = []
    for p in projects:
        kw_str = f" | keywords: {p['keywords']}" if p.get("keywords") else ""
        # Truncate hypothesis to keep each block well under 100 words.
        hyp = (p["current_hypothesis"] or "").strip()
        if len(hyp.split()) > 60:
            hyp = " ".join(hyp.split()[:60]) + "…"
        desc = (p["description"] or "").strip()
        if len(desc.split()) > 20:
            desc = " ".join(desc.split()[:20]) + "…"
        block = (
            f"[{p['id']}] {p['name']}{kw_str}\n"
            f"Description: {desc or '(none)'}\n"
            f"Current hypothesis: {hyp}"
        )
        project_blocks.append(block)

    user_msg = (
        f"The lab has {len(projects)} active research projects "
        "with current hypotheses. Identify 1–3 non-obvious cross-project connections.\n\n"
        + "\n\n".join(project_blocks)
    )

    # 5. Call Opus — NO temperature (Opus 4.7 rejects it).
    client = Anthropic()
    opus_msg = None
    try:
        opus_msg = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1200,
            system=[
                {
                    "type": "text",
                    "text": _SYNTHESIS_SYSTEM,
                    "cache_control": {"type": "ephemeral"},  # 6. Prompt caching on stable system prompt
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = opus_msg.content[0].text.strip()
    except Exception as e:
        return f"api_error: {e}"

    # 7. Parse JSON — strip ```json fences if present.
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        proposals = json.loads(raw)
        if not isinstance(proposals, list):
            proposals = []
    except Exception:
        return "parse_failed"

    # 8. Record cost (best-effort).
    try:
        if opus_msg is not None:
            _usage = {
                "input_tokens": getattr(opus_msg.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(opus_msg.usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(opus_msg.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(opus_msg.usage, "cache_read_input_tokens", 0) or 0,
            }
            record_call(
                job_name="cross_project_synthesis",
                model="claude-opus-4-7",
                usage=_usage,
            )
    except Exception as _e:
        print(f"[cross_project_synthesis] cost_tracking error: {_e}")

    if not proposals:
        return "no_connections_found"

    # Shared token counts for ledger rows.
    _tok_in = getattr(opus_msg.usage, "input_tokens", 0) or 0 if opus_msg else 0
    _tok_out = getattr(opus_msg.usage, "output_tokens", 0) or 0 if opus_msg else 0

    # 9. Record each proposal to output_ledger.
    briefing_sections = []
    for prop in proposals:
        title = prop.get("title", "(untitled connection)")
        connected_ids = prop.get("connected_project_ids", [])
        synthesis = prop.get("synthesis", "")
        proposed_test = prop.get("proposed_test", "")
        novelty = prop.get("novelty_score", 0)
        feasibility = prop.get("feasibility_score", 0)
        why = prop.get("why_non_obvious", "")

        content_md = (
            f"### {title}\n\n"
            f"**Connected projects:** {', '.join(str(i) for i in connected_ids)}\n\n"
            f"**Synthesis:** {synthesis}\n\n"
            f"**Proposed test:** {proposed_test}\n\n"
            f"**Why non-obvious:** {why}\n\n"
            f"Novelty: {novelty}/5 | Feasibility: {feasibility}/5"
        )

        try:
            record_output(
                kind="cross_project_synthesis",
                job_name="cross_project_synthesis",
                model="claude-opus-4-7",
                project_id=None,
                content_md=content_md,
                tokens_in=_tok_in,
                tokens_out=_tok_out,
                provenance={
                    "connected_project_ids": connected_ids,
                    "novelty_score": novelty,
                    "feasibility_score": feasibility,
                },
            )
        except Exception as _e:
            print(f"[cross_project_synthesis] ledger error: {_e}")

        briefing_sections.append(content_md)

    # 10. Insert a single consolidated briefing.
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        full_content = (
            f"Tealc found **{len(proposals)} cross-project connection(s)** "
            f"across {len(projects)} active research projects.\n\n"
            + "\n\n---\n\n".join(briefing_sections)
        )
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT INTO briefings
               (kind, urgency, title, content_md, created_at)
               VALUES (?, 'info', ?, ?, ?)""",
            (
                "cross_project_synthesis",
                f"{len(proposals)} cross-project connection(s) proposed",
                full_content,
                now_iso,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as _e:
        print(f"[cross_project_synthesis] briefing error: {_e}")

    # 11. Return result string.
    return f"proposed {len(proposals)} cross-project syntheses"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
