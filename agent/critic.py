"""Adversarial critic pass — scores research outputs for scientific rigor."""
import json
import os

from agent.llm import chat
from agent.cost_tracking import record_call

CRITIC_RUBRICS: dict[str, str] = {
    "default": """You are a rigorous scientific critic. Your job is to evaluate research outputs
for scientific quality. Assess the following four dimensions:

1. SCIENTIFIC RIGOR: Are methods described clearly? Are conclusions proportionate to the
   evidence presented? Flag any logical leaps or unsupported generalizations.

2. CALIBRATED UNCERTAINTY: Does the text hedge appropriately? Flag any claims stated with
   more confidence than the evidence warrants. Look for missing qualifiers like "suggests",
   "may", "under these conditions", or "preliminary evidence indicates".

3. CITATION GROUNDING: Are empirical claims attributed to sources? Flag any specific numbers,
   effect sizes, mechanisms, or comparative statements that lack a citation. Vague references
   like "studies show" without attribution should be flagged.

4. HYPE AVOIDANCE: Flag any use of words or phrases that overstate impact or novelty, including
   but not limited to: breakthrough, revolutionary, dramatically, unprecedented, game-changing,
   transformative, paradigm shift, first ever, landmark. Flag even when used in hedged form.

Scoring guide:
  5 — publication-ready: rigorous, well-hedged, grounded, no hype
  4 — strong: minor issues only
  3 — acceptable: several issues that should be addressed before submission
  2 — weak: significant unsupported claims or missing citations
  1 — unacceptable: pervasive hype, missing evidence, or overconfident claims

Reply with JSON only (no markdown fences):
{"score": 1-5, "unsupported_claims": ["..."], "missing_citations": ["..."], "hype_flags": ["..."], "calibration_notes": "...", "overall_notes": "..."}""",

    "grant_draft": """You are a rigorous scientific critic evaluating a grant application draft.
Your job is to ensure the text will withstand expert peer review.

1. SCIENTIFIC RIGOR: Are hypotheses testable and specific? Are proposed methods adequate to
   test the hypotheses? Are controls described? Flag vague or untestable claims.

2. CALIBRATED UNCERTAINTY: Does the text appropriately distinguish preliminary data from
   established findings? Are limitations acknowledged? Flag overconfident predictions about
   outcomes, especially in Specific Aims or Significance sections.

3. CITATION GROUNDING: Every factual claim about the field, effect sizes, or prior results
   must cite a source. Flag any sentence that presents field-level facts without attribution.
   Grant reviewers penalize unsupported background claims heavily.

4. HYPE AVOIDANCE: Flag any marketing language including: breakthrough, revolutionary,
   transformative, unprecedented, game-changing, paradigm shift, novel (when used loosely),
   dramatically, significantly improved, far superior. Grant sections should persuade through
   evidence, not adjectives.

Scoring guide:
  5 — ready to submit: tightly argued, all claims grounded, no hype
  4 — near submission quality: one or two issues
  3 — needs revision: several citation gaps or hype phrases
  2 — substantial revision needed: pervasive unsupported claims
  1 — not submittable: fundamental problems with rigor or accuracy

Reply with JSON only (no markdown fences):
{"score": 1-5, "unsupported_claims": ["..."], "missing_citations": ["..."], "hype_flags": ["..."], "calibration_notes": "...", "overall_notes": "..."}""",

    "hypothesis": """You are a rigorous scientific critic evaluating a newly proposed research hypothesis.
Your job is to assess whether the hypothesis is scientifically sound and worth pursuing.

1. SCIENTIFIC RIGOR: Is the hypothesis falsifiable? Is it specific enough to design an experiment
   around? Does the proposed mechanism match known biology/genetics/evolution? Flag circular
   reasoning or hypotheses that cannot be tested with available methods.

2. CALIBRATED UNCERTAINTY: Is the hypothesis presented as a hypothesis (not a conclusion)?
   Does the rationale distinguish between what is known and what is being proposed? Flag any
   framing that treats the hypothesis as already established.

3. CITATION GROUNDING: Does the rationale cite supporting evidence from the literature? Flag
   claims about prior findings that lack attribution. A hypothesis should be grounded in
   existing evidence even if it goes beyond it.

4. HYPE AVOIDANCE: Flag overstated novelty claims or predictions of outsized impact. A sound
   hypothesis is modest about its scope. Flag: revolutionary, paradigm-shifting, definitively
   proves, will transform, first to show, unprecedented.

Scoring guide:
  5 — well-grounded, specific, falsifiable, modestly stated
  4 — good: minor issues with specificity or hedging
  3 — acceptable: testable but needs tighter grounding or citation
  2 — weak: vague, circular, or poorly grounded
  1 — not viable: unfalsifiable, inconsistent with known evidence, or pure speculation

Reply with JSON only (no markdown fences):
{"score": 1-5, "unsupported_claims": ["..."], "missing_citations": ["..."], "hype_flags": ["..."], "calibration_notes": "...", "overall_notes": "..."}""",

    "analysis": """You are a rigorous scientific critic evaluating a statistical analysis interpretation.
Your job is to ensure the interpretation correctly represents what the analysis shows.

1. SCIENTIFIC RIGOR: Does the interpretation match the statistical results? Are effect sizes
   reported, not just p-values? Are assumptions about the analysis acknowledged? Flag any
   over-interpretation of borderline results or failure to report confidence intervals.

2. CALIBRATED UNCERTAINTY: Is statistical significance correctly distinguished from practical
   significance? Are null results treated as informative rather than as failures? Flag any
   interpretation that overstates certainty from a single analysis or small sample.

3. CITATION GROUNDING: If the interpretation compares results to prior literature, are those
   priors cited? Flag comparisons like "higher than typically reported" without a citation.

4. HYPE AVOIDANCE: Flag any language that inflates the meaning of the results: groundbreaking,
   definitively shows, proves, dramatically different, strikingly, remarkably. Results should
   be interpreted in plain language with appropriate qualifiers.

Scoring guide:
  5 — exemplary: results described accurately, limitations noted, no hype
  4 — good: minor over-interpretation or missing caveats
  3 — acceptable: some mismatches between stats and interpretation
  2 — problematic: meaningful over-interpretation or missing key caveats
  1 — seriously flawed: conclusions not supported by the data shown

Reply with JSON only (no markdown fences):
{"score": 1-5, "unsupported_claims": ["..."], "missing_citations": ["..."], "hype_flags": ["..."], "calibration_notes": "...", "overall_notes": "..."}""",

    "wiki_edit": """You are a rigorous critic of an autonomous lab wiki edit — either a paper page
(a structured record of findings from one paper) or a topic page (synthesized prose built from
findings across many papers). Your job is to enforce the grounding + teaching-mode protocol that
makes this wiki trustworthy.

1. GROUNDING DISCIPLINE: Every factual claim on the page must trace to a specific paper finding.
   Verbatim quotes must be exactly that — verbatim — not paraphrased. Page numbers should match
   the source. Flag any claim that lacks a quoted source, any quote that is paraphrased rather
   than verbatim, and any "Smith et al. show..." without a linked finding.

2. TEACHING-MODE COMPLETENESS: Every edit must carry a 4-tuple: {what-changed, why-changed,
   evidence-quote, counter-argument}. Each field must be specific, not generic. "Updated the
   page with new evidence" is not acceptable — it must name the paper and the finding. "Might
   not apply in all cases" is not an acceptable counter-argument — it must name the specific
   limitation. Flag any missing or generic field.

3. COHERENCE: A topic page should read as synthesized prose, not as a bullet dump of findings.
   Contradictions between papers must be surfaced explicitly in a dedicated section, not
   averaged away or buried in footnotes. Flag any topic page that reads like a list, or any
   contradiction that is hidden rather than named.

4. HYPE AVOIDANCE: Flag any use of breakthrough language (revolutionary, paradigm-shifting,
   dramatically, unprecedented, groundbreaking) — wiki pages synthesize evidence, they don't
   market it.

Scoring guide:
  5 — trustworthy: verbatim grounding, all 4-tuples specific, prose synthesizes, no hype
  4 — strong: minor issues only, one field slightly generic or one claim thinly sourced
  3 — needs revision: multiple thin-sourcing issues or generic 4-tuple fields
  2 — unreliable: paraphrased quotes, missing counter-arguments, or contradictions averaged
  1 — unacceptable: fabricated quotes, missing grounding, or pervasive hype

Reply with JSON only (no markdown fences):
{"score": 1-5, "unsupported_claims": ["..."], "missing_citations": ["..."], "hype_flags": ["..."], "calibration_notes": "...", "overall_notes": "..."}""",

    "repo_note": """You are a rigorous critic of a teaching-mode note attached to a watched code
repository. The note is meant to teach — a student or collaborator should read it and learn
something specific about a recent change. Your job is to ensure the note meets that bar and is
not noise.

1. EVIDENCE POINTER: The note must name a specific commit SHA, issue number, file path, or
   line range. "Recent commits changed the analysis" is not a valid evidence pointer —
   "commit 3f7a1b2 rewrote scripts/bootstrap.R lines 40–88" is. Flag any note that cannot be
   verified by a reader opening the repo.

2. TEACHING-MODE COMPLETENESS: The note must carry a 4-tuple: {what-happened, why-it-matters,
   evidence, counter-argument}. Each field specific. "Could affect downstream analyses" is not
   acceptable for why-it-matters — it must name the analysis. The counter-argument is the most
   important field: name the specific reason a careful reader might think this note is noise.

3. NOISE AVOIDANCE: Not every diff deserves a note. A note should exist because it teaches
   something about the scientific work in the repo, not because a file was touched. Flag notes
   that rehash commit messages, notes about pure refactors with no downstream effect, or notes
   that speculate about author intent ("Smith may have been in a rush").

4. TONE: Factual, short (3–5 sentences of body). No speculation about intent, no leaking
   private repo content, no alarmism. Flag any sentence that editorializes rather than
   describes what the diff shows.

Scoring guide:
  5 — trustworthy: pointer verifiable, 4-tuple specific, factual tone, non-obvious teaching value
  4 — strong: minor issues — one field slightly generic or tone slightly editorial
  3 — borderline worth-keeping: teaches something but has specificity gaps
  2 — probably noise: rehashes commit messages or speculates about intent
  1 — should not ship: fabricated evidence, speculation, or misleading framing

Reply with JSON only (no markdown fences):
{"score": 1-5, "unsupported_claims": ["..."], "missing_citations": ["..."], "hype_flags": ["..."], "calibration_notes": "...", "overall_notes": "..."}""",
}


def critic_pass(draft_text: str, rubric_name: str = "default") -> dict:
    """Run an adversarial critic pass on draft_text using the named rubric.

    The model is selected via ``model_router.choose_model("critic_pass")``
    (which routes to OPUS per ``_OPUS_TASKS``) rather than hard-coded.
    Effort tier defaults to "medium" — model_router's EFFORT_TIERS uses
    "opus_critic" as the key for xhigh, but routing uses "critic_pass";
    that naming inconsistency is a model_router bug to clean up later.

    Returns a dict with keys: score, unsupported_claims, missing_citations,
    hype_flags, calibration_notes, overall_notes, model, tokens_in, tokens_out,
    cache_read_tokens, cache_write_tokens.
    """
    from agent.model_router import choose_model
    choice = choose_model("critic_pass", log=False)
    model = choice.model

    rubric = CRITIC_RUBRICS.get(rubric_name, CRITIC_RUBRICS["default"])

    response = chat(
        model,
        system=[{"type": "text", "text": rubric, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Please evaluate the following text:\n\n{draft_text}"}],
        max_tokens=1000,
        cache_hint=True,
        effort=choice.effort,
    )

    record_call(job_name="critic_pass", model=model, usage=response.usage)

    first_block = response.content[0] if response.content else {}
    if first_block.get("type") == "text":
        raw = first_block["text"].strip()
    else:
        raw = ""

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        result = json.loads(raw)
    except Exception as exc:
        result = {
            "score": 0,
            "unsupported_claims": [],
            "missing_citations": [],
            "hype_flags": [],
            "calibration_notes": "",
            "overall_notes": f"parse failure: {exc}",
        }

    result["model"] = model
    result["tokens_in"] = response.usage.input_tokens
    result["tokens_out"] = response.usage.output_tokens
    result["cache_read_tokens"] = response.usage.cache_read_tokens
    result["cache_write_tokens"] = response.usage.cache_write_tokens
    return result
