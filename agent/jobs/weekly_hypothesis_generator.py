"""Weekly hypothesis generator — runs Sunday 5am Central via APScheduler.

For each active research project (up to 3 per run) with a non-empty current_hypothesis,
pulls recent literature notes and asks Sonnet 4.6 to propose 1-2 new testable hypotheses
grounded in cited papers. Writes proposals to hypothesis_proposals table.

Run manually to test:
    python -m agent.jobs.weekly_hypothesis_generator

Idle threshold: job only runs when idle_class='idle' OR idle_class='deep_idle'.
Cost estimate: 3 projects × 1 Sonnet call = ~$0.30/run = ~$1/month.
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
from agent.cost_tracking import record_call  # noqa: E402
from agent.tools import pick_relevant_wiki_topics, read_wiki_topics_block  # noqa: E402
from agent.hypothesis_pipeline import run_pipeline as _run_gate, pairwise_tournament  # noqa: E402

try:
    from agent.voice_index import voice_system_prompt_addendum as _voice_addendum  # noqa: E402
except ImportError:
    _voice_addendum = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_IDLE_CLASSES = {"idle", "deep_idle"}
_MAX_PROJECTS = 3
_MIN_LIT_NOTES = 3
_MAX_LIT_NOTES = 10
_DAYS_BACK_LIT = 14

# ---------------------------------------------------------------------------
# Sonnet system prompt
# ---------------------------------------------------------------------------

_HYPOTHESIS_SYSTEM = (
    "You propose 1-2 new testable hypotheses for the researcher's research project. "
    "He works in genome structure evolution, sex chromosomes, comparative phylogenetics, "
    "karyotype evolution. The project's current hypothesis and the literature notes below "
    "frame what's already known. "
    "\n\n"
    "CRITICAL: the user message includes an EXISTING CLAIMS block — these are synthesized "
    "topic pages from the lab wiki covering findings from HIS OWN prior papers. Before "
    "finalizing any hypothesis, check it against those claims. If an existing finding "
    "already supports, refutes, or has previously tested the hypothesis, DO NOT propose it "
    "as novel — either refine into a genuine extension (new clade, new method, new time "
    "window, new mechanism step) or output {\"hypotheses\": []} honestly. the researcher will not "
    "be surprised by a hypothesis that restates his own prior work; he will be surprised "
    "by one the wiki doesn't already answer. The 2026-04-21 Fragile Y failure (proposing "
    "what Smith & Jones 2014 already tested) is the exact failure mode this rule exists "
    "to prevent. "
    "\n\n"
    "Propose hypotheses that: "
    "(a) BUILD ON or CONTRADICT specific findings from the literature (cite DOIs); "
    "(b) are testable with data the researcher plausibly has access to (Coleoptera + Diptera + Mammalia "
    "karyotype DBs, Tree of Sex, time-calibrated phylogenies, comparative methods); "
    "(c) are not trivial restatements of the existing hypothesis OR of anything in EXISTING CLAIMS. "
    'Output JSON: {"hypotheses": [{"hypothesis_md": "<2-4 sentences>", '
    '"rationale_md": "<2-3 sentences citing the literature AND referencing which existing '
    'wiki finding this extends beyond, if applicable>", '
    '"proposed_test_md": "<2-3 sentences>", '
    '"cited_paper_dois": "<comma-separated>", '
    '"novelty_score": <0-1>, '
    '"feasibility_score": <0-1>}, ...]}. '
    "1-2 hypotheses max. If none meet the bar, output "
    '{"hypotheses": []} honestly.'
)


# ---------------------------------------------------------------------------
# Sonnet hypothesis call
# ---------------------------------------------------------------------------

def _call_sonnet_for_hypotheses(
    client: Anthropic,
    project_name: str,
    project_description: str,
    current_hypothesis: str,
    lit_notes: list[dict],
    system_prompt: str | None = None,
    wiki_block: str = "",
    consulted_slugs: list[str] | None = None,
):
    """Call Sonnet 4.6 to propose hypotheses.

    Returns (list[dict], msg_object | None).
    msg_object is the raw Anthropic response for cost/usage logging.
    system_prompt overrides _HYPOTHESIS_SYSTEM when provided (used to prepend voice exemplars).
    wiki_block, if non-empty, is prepended to the user message as EXISTING CLAIMS
    (synthesized prior findings from the lab wiki) — the LLM must check against it
    before proposing to avoid duplicating the researcher's own published results.
    """
    lit_block_parts = []
    for i, note in enumerate(lit_notes, 1):
        doi_str = f"DOI: {note['doi']}" if note.get("doi") else "(no DOI)"
        lit_block_parts.append(
            f"[{i}] {note.get('title', '(untitled)')} "
            f"({note.get('publication_year', '?')}) {doi_str}\n"
            f"{note.get('extracted_findings_md', '(no findings)')}"
        )
    lit_block = "\n\n".join(lit_block_parts) if lit_block_parts else "(no literature notes)"

    wiki_section = ""
    if wiki_block:
        slugs_str = ", ".join(consulted_slugs or [])
        wiki_section = (
            f"=== EXISTING CLAIMS (synthesized from the lab wiki — topics consulted: {slugs_str}) ===\n"
            f"Before proposing, check each candidate hypothesis against these findings. "
            f"Do not re-propose anything already tested or established below.\n\n"
            f"{wiki_block}\n\n"
            f"=== END EXISTING CLAIMS ===\n\n"
        )
    elif consulted_slugs is not None:
        wiki_section = (
            "=== EXISTING CLAIMS ===\n"
            "No matching topic pages found in the lab wiki for this project's claim domain. "
            "Proceed, but be especially careful — the researcher's 65+ published papers may already "
            "cover this ground even if the wiki does not.\n\n"
            "=== END EXISTING CLAIMS ===\n\n"
        )

    user_msg = (
        f"Project: {project_name}\n"
        f"Description: {project_description or '(not specified)'}\n"
        f"Current hypothesis: {current_hypothesis}\n\n"
        f"{wiki_section}"
        f"Recent literature ({len(lit_notes)} papers):\n\n"
        f"{lit_block}"
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=system_prompt if system_prompt is not None else _HYPOTHESIS_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text.strip()
        # Extract JSON object: strip ```json fences if present, otherwise
        # find the first '{' and parse from there (Sonnet often adds reasoning
        # preamble when the wiki-consultation rule fires, which is valuable to
        # log but must be sliced off before json.loads).
        candidate = raw
        if candidate.startswith("```"):
            parts = candidate.split("```")
            candidate = parts[1] if len(parts) > 1 else parts[0]
            if candidate.startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
        if not candidate.startswith("{"):
            brace_idx = candidate.find("{")
            if brace_idx == -1:
                return [], msg
            candidate = candidate[brace_idx:]
        # If JSON is followed by more prose, trim to the matching closing brace.
        depth = 0
        end_idx = None
        in_string = False
        escape = False
        for i, ch in enumerate(candidate):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx is not None:
            candidate = candidate[:end_idx]
        parsed = json.loads(candidate)
        return parsed.get("hypotheses", []), msg
    except Exception:
        return [], msg if "msg" in locals() else None


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("weekly_hypothesis_generator")
def job() -> str:
    """For each active research project with a hypothesis, propose new testable hypotheses."""
    # the researcher can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("weekly_hypothesis_generator"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # 1. Time guard only — scheduled 5am Central. Idle-class was too strict.
    # Bypass with FORCE_RUN=1 for manual test invocations.
    from datetime import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = _dt.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and not os.environ.get("FORCE_RUN"):
        return f"skipped: working-hours guard (hour={hour}; set FORCE_RUN=1 to bypass)"

    # 2. Pull active projects with non-empty current_hypothesis.
    projects = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='research_projects'"
        ).fetchone()
        if not tbl:
            conn.close()
            return "no_active_projects_with_hypothesis"
        rows = conn.execute(
            "SELECT id, name, description, current_hypothesis, keywords "
            "FROM research_projects "
            "WHERE status='active' AND current_hypothesis IS NOT NULL AND current_hypothesis != '' "
            "ORDER BY last_touched_iso ASC NULLS FIRST "
            "LIMIT ?",
            (_MAX_PROJECTS,),
        ).fetchall()
        conn.close()
        projects = rows
    except Exception as e:
        return f"error loading projects: {e}"

    if not projects:
        return "no_active_projects_with_hypothesis"

    # 3. Process each project.
    client = Anthropic()
    total_proposals = 0
    project_count = 0
    briefing_parts = []
    per_project_results = []
    _all_proposals_this_run: list[dict] = []  # populated for cross-project tournament

    now_iso = datetime.now(timezone.utc).isoformat()
    since_iso = (datetime.now(timezone.utc) - timedelta(days=_DAYS_BACK_LIT)).isoformat()

    for proj_row in projects:
        proj_id, proj_name, proj_desc, current_hypothesis, proj_keywords = proj_row

        # 3a. Pull recent literature notes for this project.
        lit_notes = []
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            tbl_lit = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='literature_notes'"
            ).fetchone()
            if tbl_lit:
                lit_rows = conn.execute(
                    """SELECT title, publication_year, doi, extracted_findings_md
                       FROM literature_notes
                       WHERE project_id=? AND created_at >= ?
                       ORDER BY COALESCE(citations_count, 0) DESC, created_at DESC
                       LIMIT ?""",
                    (proj_id, since_iso, _MAX_LIT_NOTES),
                ).fetchall()
                for lr in lit_rows:
                    lit_notes.append({
                        "title": lr[0],
                        "publication_year": lr[1],
                        "doi": lr[2],
                        "extracted_findings_md": lr[3],
                    })
            conn.close()
        except Exception:
            lit_notes = []

        # 3b. Skip if fewer than _MIN_LIT_NOTES notes.
        if len(lit_notes) < _MIN_LIT_NOTES:
            per_project_results.append(f"insufficient_literature: project={proj_id}")
            continue

        # 3c. WIKI CONSULTATION — pick relevant topic pages for this project's
        # claim domain and inject into user message so Sonnet can check against
        # the researcher's already-published findings before proposing.
        _wiki_query = " ".join(filter(None, [
            proj_name, current_hypothesis, proj_desc or "", proj_keywords or "",
        ]))
        _consulted_slugs: list[str] = []
        _wiki_block = ""
        try:
            _consulted_slugs = pick_relevant_wiki_topics(_wiki_query, k=3)
            if _consulted_slugs:
                _wiki_block = read_wiki_topics_block(_consulted_slugs)
        except Exception as _e:
            print(f"[weekly_hypothesis_generator] wiki consultation error: {_e}")
            _consulted_slugs = []

        # 3d. Build voice-exemplar system prompt, then call Sonnet.
        _hyp_system = _HYPOTHESIS_SYSTEM
        if _voice_addendum is not None:
            try:
                _voice_query = " ".join(filter(None, [proj_name, current_hypothesis]))
                _addendum = _voice_addendum(_voice_query, k=3)
                if _addendum:
                    _hyp_system = _addendum + "\n\n" + _HYPOTHESIS_SYSTEM
            except Exception:
                pass  # voice index errors must not block hypothesis generation

        hypotheses, hyp_msg = _call_sonnet_for_hypotheses(
            client, proj_name, proj_desc or "", current_hypothesis, lit_notes,
            system_prompt=_hyp_system,
            wiki_block=_wiki_block,
            consulted_slugs=_consulted_slugs,
        )

        # Record cost for the Sonnet call
        try:
            if hyp_msg is not None:
                _hyp_usage = {
                    "input_tokens": getattr(hyp_msg.usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(hyp_msg.usage, "output_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(hyp_msg.usage, "cache_creation_input_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(hyp_msg.usage, "cache_read_input_tokens", 0) or 0,
                }
                record_call(job_name="weekly_hypothesis_generator", model="claude-sonnet-4-6", usage=_hyp_usage)
        except Exception as _e:
            print(f"[weekly_hypothesis_generator] cost_tracking error: {_e}")

        if not hypotheses:
            per_project_results.append(f"no_proposals: project={proj_id}")
            continue

        # 3d. Insert each hypothesis into hypothesis_proposals + ledger, gated by pipeline.
        project_proposals = []
        for h in hypotheses[:2]:  # hard cap at 2
            try:
                hyp_md = (h.get("hypothesis_md") or "").strip()
                if not hyp_md:
                    continue
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA journal_mode=WAL")
                _hp_cur = conn.execute(
                    """INSERT INTO hypothesis_proposals
                       (project_id, proposed_iso, hypothesis_md, rationale_md,
                        proposed_test_md, cited_paper_dois, novelty_score,
                        feasibility_score, status)
                       VALUES (?,?,?,?,?,?,?,?,'proposed')""",
                    (
                        proj_id,
                        now_iso,
                        hyp_md,
                        (h.get("rationale_md") or "").strip() or None,
                        (h.get("proposed_test_md") or "").strip() or None,
                        (h.get("cited_paper_dois") or "").strip() or None,
                        h.get("novelty_score"),
                        h.get("feasibility_score"),
                    ),
                )
                hypothesis_id = _hp_cur.lastrowid
                conn.commit()
                conn.close()
                total_proposals += 1
                project_proposals.append({**h, "_hypothesis_id": hypothesis_id})

                # Run the type-aware gate (scheduled mode → Sonnet critic, no contradiction
                # check by default to keep weekly cost low).
                _gate = None
                try:
                    _gate = _run_gate(
                        content_md=hyp_md,
                        mode="scheduled",
                        project_id=proj_id,
                    )
                except Exception as _ge:
                    print(f"[weekly_hypothesis_generator] gate error: {_ge}")

                # Ledger row with gate result in provenance
                try:
                    _tok_in = getattr(hyp_msg.usage, "input_tokens", 0) or 0 if hyp_msg else 0
                    _tok_out = getattr(hyp_msg.usage, "output_tokens", 0) or 0 if hyp_msg else 0
                    _critic_for_row = (_gate or {}).get("critic_result") or {}
                    _tc = (_gate or {}).get("type_classification") or {}
                    _provenance: dict = {
                        "hypothesis_id": hypothesis_id,
                        "rationale_md": (h.get("rationale_md") or "").strip(),
                        "proposed_test_md": (h.get("proposed_test_md") or "").strip(),
                        "cited_paper_dois": (h.get("cited_paper_dois") or "").strip(),
                        "novelty_score": h.get("novelty_score"),
                        "feasibility_score": h.get("feasibility_score"),
                        "wiki_topics_consulted": _consulted_slugs,
                        "wiki_block_chars": len(_wiki_block),
                    }
                    if _gate is not None:
                        _provenance["hypothesis_gate"] = {
                            "passed": _gate.get("gate_passed", False),
                            "score": _gate.get("score", 0),
                            "type": _tc.get("type"),
                            "type_confidence": _tc.get("confidence"),
                            "block_reasons": _gate.get("block_reasons", []),
                            "warnings": _gate.get("warnings", []),
                            "cost_estimate_usd": _gate.get("cost_estimate_usd", 0),
                            "tier_summary": _gate.get("tier_summary", ""),
                            "alternative_explanation_md": _critic_for_row.get("alternative_explanation_md", ""),
                            "recommendations": _critic_for_row.get("recommendations", []),
                            "sign_propagation": _gate.get("sign_propagation"),
                        }
                    _ledger_id = record_output(
                        kind="hypothesis",
                        job_name="weekly_hypothesis_generator",
                        model=_critic_for_row.get("model") or "claude-sonnet-4-6",
                        project_id=proj_id,
                        content_md=hyp_md,
                        tokens_in=_tok_in,
                        tokens_out=_tok_out,
                        provenance=_provenance,
                    )
                    if _gate is not None:
                        _summary_notes = (
                            _critic_for_row.get("type_specific_notes")
                            or _critic_for_row.get("claim_coherence_notes")
                            or _gate.get("tier_summary", "")
                        )
                        update_critic(
                            _ledger_id,
                            int(_critic_for_row.get("score") or 0),
                            _summary_notes,
                            _critic_for_row.get("model", ""),
                        )
                    project_proposals[-1]["_ledger_id"] = _ledger_id
                    project_proposals[-1]["_gate"] = _gate
                except Exception as _e:
                    print(f"[weekly_hypothesis_generator] ledger/gate error: {_e}")

            except Exception:
                continue

        if project_proposals:
            project_count += 1
            per_project_results.append(
                f"proposed {len(project_proposals)} hypothesis(es): project={proj_id}"
            )
            # Build briefing block for this project, including gate status per hypothesis.
            proj_block = [f"### {proj_name} ({proj_id})\n"]
            for i, h in enumerate(project_proposals, 1):
                nov = h.get("novelty_score") or 0
                feas = h.get("feasibility_score") or 0
                gate = h.get("_gate") or {}
                gate_line = ""
                if gate:
                    pid = h.get("_hypothesis_id") or "?"
                    status = "PASSED" if gate.get("gate_passed") else "BLOCKED"
                    score = gate.get("score", "?")
                    htype = (gate.get("type_classification") or {}).get("type", "?")
                    block_reasons = gate.get("block_reasons", []) or []
                    gate_line = (
                        f"\n*Gate:* **{status}** (proposal #{pid}, type={htype}, "
                        f"score={score}/5)"
                    )
                    if block_reasons:
                        gate_line += "  \n  Block reasons: " + "; ".join(block_reasons[:2])
                proj_block.append(
                    f"**Hypothesis {i}** (novelty={nov:.1f}, feasibility={feas:.1f})"
                    f"{gate_line}\n\n"
                    f"{h.get('hypothesis_md', '')}\n\n"
                    f"*Rationale:* {h.get('rationale_md', '')}\n\n"
                    f"*Proposed test:* {h.get('proposed_test_md', '')}\n\n"
                    f"*Cited DOIs:* {h.get('cited_paper_dois', '(none)')}\n"
                )
            briefing_parts.append("\n".join(proj_block))
            for h in project_proposals:
                _all_proposals_this_run.append({
                    "id": h.get("_hypothesis_id"),
                    "label": f"#{h.get('_hypothesis_id')} ({proj_id})",
                    "content_md": h.get("hypothesis_md", ""),
                    "ledger_id": h.get("_ledger_id"),
                })

    # 3e. Cross-project tournament (Sonnet pairwise) when N >= 3 — gives the researcher
    # a ranked list across all proposals from this run, not just within projects.
    tournament_block = ""
    if len(_all_proposals_this_run) >= 3:
        try:
            ranked = pairwise_tournament(_all_proposals_this_run)
            t_lines = [
                "### Cross-project tournament (Sonnet pairwise judging)",
                "",
                "| Rank | Proposal | Elo | W-L | Last rationale |",
                "|---|---|---|---|---|",
            ]
            for i, r in enumerate(ranked, 1):
                rationale = (r.get("last_judge_rationale") or "").replace("\n", " ")[:120]
                t_lines.append(
                    f"| {i} | {r['label']} | {r['elo']:.0f} | "
                    f"{r['wins']}-{r['losses']} | {rationale} |"
                )
            n = len(ranked)
            pairs = n * (n - 1) // 2
            t_lines.append("")
            t_lines.append(f"_{pairs} pairs judged. Approximate cost: ${pairs * 0.012:.3f} (Sonnet)._")
            tournament_block = "\n".join(t_lines)
        except Exception as _te:
            tournament_block = f"_(Tournament failed: {_te})_"

    # 4. Write briefing if any proposals were made.
    if total_proposals > 0 and briefing_parts:
        try:
            monday_iso = (
                datetime.now(timezone.utc).date()
                - timedelta(days=datetime.now(timezone.utc).weekday())
            ).isoformat()
            tournament_section = (
                "\n\n---\n\n" + tournament_block + "\n"
            ) if tournament_block else ""
            content_md = (
                f"Tealc proposed **{total_proposals} new testable hypothesis(es)** "
                f"across {project_count} project(s). "
                f"Each was gated by the type-aware pipeline (Tier 0 filter → Haiku "
                f"classifier → Sonnet critic, Opus escalation on borderline; sign-coherence "
                f"check for directional types). "
                f"Review via `list_hypothesis_proposals`; gate status is on each entry. "
                f"Adopt with `adopt_hypothesis` (refuses gate-blocked unless override_gate=True); "
                f"reject with `reject_hypothesis`."
                f"{tournament_section}\n\n"
                + "\n\n---\n\n".join(briefing_parts)
            )
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """INSERT INTO briefings
                   (kind, urgency, title, content_md, created_at)
                   VALUES ('hypothesis_proposals', 'info', ?, ?, ?)""",
                (
                    f"Hypothesis proposals — week of {monday_iso}",
                    content_md,
                    now_iso,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass  # briefing failure must not abort the job

    return f"hypothesized: projects={project_count} proposals={total_proposals}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
