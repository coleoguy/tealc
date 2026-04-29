"""Weekly job that proposes next_action text for research projects that don't
have one. Without a next_action, the drafter/analyzer can't act on a project.
Current coverage: ~9 of 76 projects have next_action — this job raises that over time.

Strategy: pick up to 3 active projects without next_action, ask Sonnet to
propose a concrete, testable action, and surface as briefings for Heath to
adopt (he confirms, we update the row). Does NOT auto-write to the project —
human review required because these drive other jobs' behavior.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402


_SYSTEM = (
    "You propose the single most concrete next action for a research project. "
    "Input: project name, description, current_hypothesis. "
    "Output JSON: {\"next_action\": \"<one sentence, imperative, concrete>\", "
    "\"rationale\": \"<1 sentence why this is the right next step>\"}. "
    "Good example: \"Fit a BiSSE model to the updated Coleoptera karyotype dataset and compare AIC against MuSSE.\" "
    "Bad example: \"Continue working on the project.\" "
    "If the project description is too vague to propose anything, return "
    "{\"next_action\": null, \"rationale\": \"insufficient_info\"}."
)


@tracked("next_action_filler")
def job() -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT id, name, description, current_hypothesis FROM research_projects "
            "WHERE status='active' AND (next_action IS NULL OR next_action='') "
            "ORDER BY last_touched_iso DESC LIMIT 3"
        ).fetchall()
        conn.close()
    except Exception as e:
        return f"db_error: {e}"

    if not rows:
        return "no_projects_need_next_action"

    client = Anthropic()
    proposals = []
    for pid, name, desc, hyp in rows:
        user = f"Project: {name}\nDescription: {desc or '(empty)'}\nHypothesis: {hyp or '(empty)'}"
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=250,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            raw = msg.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            if data.get("next_action"):
                proposals.append((pid, name, data["next_action"], data.get("rationale", "")))
            # record cost (best effort)
            try:
                from agent.cost_tracking import record_call  # noqa: PLC0415
                record_call(
                    job_name="next_action_filler", model="claude-sonnet-4-6",
                    usage={
                        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
                        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
                    },
                )
            except Exception:
                pass
        except Exception:
            continue

    if not proposals:
        return "no_proposals_produced"

    # Surface as a single briefing for Heath to review and adopt
    lines = ["**Proposed next actions for projects that currently have none:**\n"]
    for pid, name, action, rationale in proposals:
        lines.append(f"- **{name}** (`{pid}`): {action}")
        if rationale:
            lines.append(f"  _Why: {rationale}_")
    lines.append("\n_To adopt, tell me: \"set next_action on {id} to '...'\" and I'll update it._")

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('next_action_proposals', 'info', ?, ?, ?)",
        (
            f"{len(proposals)} proposed next action(s) for review",
            "\n".join(lines),
            now_iso,
        ),
    )
    conn.commit()
    conn.close()
    return f"proposed_next_actions: {len(proposals)}"


if __name__ == "__main__":
    print(job())
