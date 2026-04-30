"""Pre-submission reviewer — runs 3 distinct reviewer personas per document.

Usage:
    from agent.submission_review import pre_submission_review
    result = pre_submission_review(doc_text, venue="nature_tier")
"""
import json
import os

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

import agent.cost_tracking as cost_tracking  # noqa: E402

# 4th + 5th personas — Tier 2 features (deterministic + LLM-judged)
try:
    from agent.personas.self_plagiarism import check_self_plagiarism as _check_sp  # noqa: E402
    _self_plag_available = True
except ImportError:
    _self_plag_available = False
try:
    from agent.contradiction_radar import detect_contradictions as _detect_contradictions  # noqa: E402
    _contradiction_radar_available = True
except ImportError:
    _contradiction_radar_available = False

client = Anthropic()

# ---------------------------------------------------------------------------
# Venue presets
# ---------------------------------------------------------------------------

VENUE_PRESETS: dict[str, str] = {
    "journal_generic": (
        "This manuscript will be reviewed for a standard peer-reviewed journal. "
        "Reviewers expect methodological soundness, appropriate scope, clear writing, "
        "and conclusions proportionate to the evidence. Novelty and significance should "
        "be moderate to high within the field. Standard reporting practices and citation "
        "norms for the discipline apply."
    ),
    "nature_tier": (
        "Nature-family journals (Nature, Nature Ecology & Evolution, Nature Genetics, etc.) "
        "weight broad cross-disciplinary significance extremely heavily. A technically "
        "flawless study that speaks only to specialists will be desk-rejected. Reviewers "
        "probe whether findings will reshape thinking beyond the immediate subfield. "
        "Novelty must be exceptional, not incremental. Editors screen aggressively for "
        "modest claims dressed in ambitious language. Every figure must earn its place; "
        "conciseness is prized. Reviewers will flag: incremental advances, narrow "
        "taxonomic or geographic scope presented as universal, lack of mechanistic insight "
        "in favor of purely descriptive findings."
    ),
    "MIRA_study_section": (
        "This application will be evaluated by an NIH MIRA (Maximizing Investigators' "
        "Research Award) study section. Reviewers assess overall research program coherence "
        "and PI independence rather than project-by-project feasibility. Innovation and "
        "significance across the portfolio are paramount. Reviewers expect explicit "
        "statements of long-term research vision, integration of aims, and evidence the "
        "PI has the expertise and independence to pursue the program. Preliminary data "
        "must support feasibility of the approach without over-promising deliverables. "
        "Overly narrow or purely descriptive programs score poorly. Budget justification "
        "must align with the scope of the research program."
    ),
    "NSF_DEB": (
        "This proposal will be reviewed for NSF Division of Environmental Biology (DEB). "
        "Reviewers apply NSF's dual criteria: Intellectual Merit and Broader Impacts, "
        "weighted roughly equally. Intellectual Merit emphasizes advances to evolutionary "
        "biology, ecology, or systematics — conceptual novelty matters more than applied "
        "relevance. Broader Impacts must be concrete: training plans, outreach, diversity "
        "initiatives, or infrastructure. Reviewers will flag vague broader impacts. "
        "Budget must be justified item-by-item; indirect costs and student stipends are "
        "scrutinized. Preliminary data requirements are lower than NIH but a clear "
        "rationale for feasibility is still expected. Integration with open data and "
        "reproducibility norms (data management plans) are checked."
    ),
    "google_org_grant": (
        "Google.org grants are reviewed by program officers who are not necessarily "
        "domain scientists. Proposals must articulate impact in plain language accessible "
        "to a sophisticated non-specialist. Reviewers favor bold, scalable solutions with "
        "a clear theory of change: inputs → activities → outputs → outcomes → impact. "
        "Technical rigor matters but must be translated into evidence-based intervention "
        "logic. Reviewers will flag: academic language without practical implications, "
        "solutions without a deployment or scale-up pathway, and budgets that don't map "
        "clearly to deliverables. Equity and accessibility considerations are weighted "
        "heavily. Timeline and milestones must be realistic and measurable."
    ),
}

# ---------------------------------------------------------------------------
# Persona prompts
# ---------------------------------------------------------------------------

PERSONA_PROMPTS: dict[str, str] = {
    "methodologist": (
        "You focus on study design, statistical rigor, reproducibility, confounders, "
        "sample size justification. You flag when claims exceed evidence. You look for "
        "missing controls, underpowered analyses, pseudoreplication, inappropriate "
        "statistical tests, and failure to account for multiple comparisons. You ask: "
        "Is the design adequate to test the stated hypothesis? Are effect sizes and "
        "confidence intervals reported? Are raw data and analysis scripts available? "
        "You assign a score reflecting methodological soundness only."
    ),
    "domain_expert": (
        "You know the field's prior art and will flag when the manuscript misreads "
        "precedent, cites weak sources, or misses obvious related work. You assess "
        "significance within the context of what is already known. You ask: Does this "
        "genuinely advance the field beyond what was published in the last five years? "
        "Are the most relevant papers cited and accurately characterized? Is the "
        "framing consistent with the current scientific consensus? You will note "
        "landmark papers the authors should have engaged with."
    ),
    "skeptic": (
        "You are a tough but fair reviewer who searches for what is wrong. You probe "
        "for alternative explanations, overfitting, hidden assumptions, and logical "
        "gaps. You write 'I am not convinced that...' often. You challenge causal "
        "language when only correlational evidence exists, flag post-hoc "
        "rationalization of unexpected results, and question whether the data actually "
        "support the central claim. You are not hostile, but you will not let "
        "weaknesses pass unremarked. A score of 3 or above from you is a genuine "
        "endorsement."
    ),
}

# ---------------------------------------------------------------------------
# JSON output schema instruction appended to every persona prompt
# ---------------------------------------------------------------------------

_JSON_INSTRUCTION = (
    '\n\nOutput ONLY valid JSON with exactly these keys — no markdown fences, '
    'no preamble:\n'
    '{"score": <integer 1-5>, '
    '"strengths": [<string>, ...], '
    '"concerns": [<string>, ...], '
    '"blocking_issues": [<string>, ...], '
    '"suggested_revisions": [<string>, ...], '
    '"notes": "<2-3 sentence overall take>"}'
)

# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

_OPUS_MODEL = "claude-opus-4-7"
_SONNET_MODEL = "claude-sonnet-4-6"
_MAX_DOC_CHARS = 30_000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_DOC_CHARS:
        return text
    return text[:_MAX_DOC_CHARS] + "\n\n[<truncated>]"


def _parse_review_json(raw: str, persona: str) -> dict:
    """Strip markdown fences and parse JSON; return safe fallback on failure."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except Exception as exc:
        return {
            "score": 0,
            "strengths": [],
            "concerns": [],
            "blocking_issues": [],
            "suggested_revisions": [],
            "notes": f"[parse failure for persona '{persona}': {exc}] Raw: {raw[:300]}",
        }


def pre_submission_review(
    doc_text: str,
    venue: str = "journal_generic",
    personas: list[str] | None = None,
) -> dict:
    """Run 3 reviewer personas on doc_text and synthesise a consensus.

    Parameters
    ----------
    doc_text : str
        The manuscript, grant, or other document to review.
    venue : str
        Key into VENUE_PRESETS. Defaults to 'journal_generic'.
    personas : list[str] | None
        Subset/override of PERSONA_PROMPTS keys. Defaults to all three.

    Returns
    -------
    dict
        Full structured review with per-persona scores, aggregated consensus,
        and token counts.
    """
    if personas is None:
        personas = ["methodologist", "domain_expert", "skeptic"]

    venue_text = VENUE_PRESETS.get(venue, VENUE_PRESETS["journal_generic"])
    doc_snippet = _truncate(doc_text)

    reviews: list[dict] = []
    tokens_in_total = 0
    tokens_out_total = 0

    for persona in personas:
        persona_base = PERSONA_PROMPTS.get(
            persona,
            f"You are a reviewer with perspective: {persona}."
        )
        system_text = (
            persona_base
            + "\n\nVenue context: "
            + venue_text
            + _JSON_INSTRUCTION
        )

        msg = client.messages.create(
            model=_OPUS_MODEL,
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Please review the following document:\n\n" + doc_snippet
                    ),
                }
            ],
        )

        usage = msg.usage
        t_in = getattr(usage, "input_tokens", 0) or 0
        t_out = getattr(usage, "output_tokens", 0) or 0
        tokens_in_total += t_in
        tokens_out_total += t_out

        try:
            usage_dict = (
                usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
            )
            cost_tracking.record_call(
                job_name="pre_submission_review",
                model=_OPUS_MODEL,
                usage=usage_dict,
            )
        except Exception:
            pass  # cost tracking is best-effort

        raw = msg.content[0].text if msg.content else ""
        review = _parse_review_json(raw, persona)
        review["persona"] = persona
        reviews.append(review)

    # ------------------------------------------------------------------
    # Consensus call — Sonnet synthesises what all reviewers agreed on
    # ------------------------------------------------------------------
    reviews_summary = "\n\n".join(
        f"Reviewer ({r['persona']}, score {r.get('score', '?')}/5):\n"
        f"  Strengths: {r.get('strengths', [])}\n"
        f"  Concerns: {r.get('concerns', [])}\n"
        f"  Blocking issues: {r.get('blocking_issues', [])}\n"
        f"  Notes: {r.get('notes', '')}"
        for r in reviews
    )

    consensus_msg = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    "Three independent reviewers evaluated the same manuscript. "
                    "Synthesize what all three reviewers AGREED was the critical "
                    "issue(s). 3-4 sentences. Output plain text only.\n\n"
                    + reviews_summary
                ),
            }
        ],
    )

    c_usage = consensus_msg.usage
    c_in = getattr(c_usage, "input_tokens", 0) or 0
    c_out = getattr(c_usage, "output_tokens", 0) or 0
    tokens_in_total += c_in
    tokens_out_total += c_out

    try:
        c_usage_dict = (
            c_usage.model_dump() if hasattr(c_usage, "model_dump") else dict(c_usage)
        )
        cost_tracking.record_call(
            job_name="pre_submission_review_consensus",
            model=_SONNET_MODEL,
            usage=c_usage_dict,
        )
    except Exception:
        pass

    consensus_text = (
        consensus_msg.content[0].text.strip() if consensus_msg.content else ""
    )

    # ------------------------------------------------------------------
    # 4th persona — Self-Plagiarism Sentinel (deterministic, no LLM calls)
    # ------------------------------------------------------------------
    sp_result = {
        "flags": [], "n_draft_sentences": 0, "n_flagged": 0,
        "foundation_ready": False, "summary": "Self-plagiarism sentinel not available.",
    }
    if _self_plag_available:
        try:
            sp_result = _check_sp(doc_text)
        except Exception as _sp_exc:
            sp_result["summary"] = f"Self-plagiarism check error: {_sp_exc}"

    # ------------------------------------------------------------------
    # 5th persona — Contradiction Radar (LLM-judged via heath_claims graph)
    # ------------------------------------------------------------------
    contradiction_result = {
        "contradictions": [], "n_claim_sentences": 0, "n_overlapping": 0,
        "foundation_ready": False, "summary": "Contradiction radar not available.",
    }
    if _contradiction_radar_available:
        try:
            contradiction_result = _detect_contradictions(doc_text)
        except Exception as _cr_exc:
            contradiction_result["summary"] = f"Contradiction radar error: {_cr_exc}"

    return {
        "venue": venue,
        "personas_run": personas,
        "reviews": reviews,
        "consensus": consensus_text,
        "self_plagiarism": sp_result,
        "contradiction_radar": contradiction_result,
        "tokens_in_total": tokens_in_total,
        "tokens_out_total": tokens_out_total,
        "model": _OPUS_MODEL,
    }
