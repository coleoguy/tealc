"""hypothesis_pipeline.py — type-aware hypothesis gating.

Pipeline stages, in order:
  Tier 0 (free, regex):       smoke-test / placeholder / length filter
  Tier 1 (Haiku):             type classifier — directional/mechanistic/etc
  Tier 2 (Sonnet|Opus):       critic with type-conditional rubric
  Tier 3 (Opus):              uncertainty escalation when Sonnet score is borderline

Three entry points use the pipeline:
  - record_chat_artifact (kind='hypothesis')   — chat mode (Sonnet critic)
  - run_formal_hypothesis_pass(claim)          — formal mode (Opus critic, no escalation needed)
  - weekly_hypothesis_generator                 — scheduled mode (Sonnet critic)

The pipeline is type-aware, not domain-aware: the rubric applied depends on whether
the hypothesis is directional (sign-coherence check), mechanistic (mechanism check),
observational (sampling/observable check), methodological (comparison-to-current),
synthesis (bridge-claim), or speculative (no hard gate). It does NOT hardcode any
particular biological subdiscipline.
"""
from __future__ import annotations

import json
import os
import re
from collections import deque
from typing import Optional

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

import agent.cost_tracking as cost_tracking  # noqa: E402

_client = Anthropic()

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"

# ---------------------------------------------------------------------------
# Tier 0 — deterministic regex filters (free)
# ---------------------------------------------------------------------------

_SMOKE_TEST_TOKENS = [
    r"\[SMOKE\s*TEST\]",
    r"<TO[\s-]*BE[\s-]*FILLED>",
    r"<PLACEHOLDER>",
    r"\bTODO\b",
    r"\bFIXME\b",
    r"<INSERT[^>]*>",
    r"\bXXX\b(?!\s*chromosome|\s*allotetraploid)",
]
_SMOKE_TEST_RE = re.compile("|".join(_SMOKE_TEST_TOKENS), re.IGNORECASE)

_NOTES_REJECT_TOKENS = [
    "backfill",
    "tool validation",
    "smoke test",
    "tool-validation",
    "smoke-test",
]


def tier0_filter(content_md: str, notes: str = "") -> dict:
    """Free deterministic filter. Blocks smoke-test markers, junk notes, length issues.

    Returns {ok, blocked_reasons, warnings}.
    """
    blocked: list[str] = []
    warnings: list[str] = []

    matches = _SMOKE_TEST_RE.findall(content_md or "")
    if matches:
        unique = sorted(set(m for m in matches if m))
        blocked.append(
            f"Smoke-test/placeholder marker(s) in content: {unique}. "
            f"This looks like a test artifact, not a real hypothesis."
        )

    notes_lower = (notes or "").lower()
    for tok in _NOTES_REJECT_TOKENS:
        if tok in notes_lower:
            blocked.append(
                f"Notes field contains '{tok}' — this looks like a test artifact, not durable research."
            )
            break

    text = (content_md or "").strip()
    if len(text) < 30:
        blocked.append(
            f"Hypothesis content too short ({len(text)} chars; min 30). "
            f"State the claim in at least one full sentence."
        )
    elif len(text) > 8000:
        warnings.append(
            f"Hypothesis content very long ({len(text)} chars) — looks like a draft section, not a hypothesis. Consider trimming to the core claim."
        )

    return {
        "ok": len(blocked) == 0,
        "blocked_reasons": blocked,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tier 1 — Haiku type classifier
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = """You classify proposed research hypotheses into ONE of six types. Output JSON only — no markdown fences, no preamble.

TYPES:
- directional — claims a direction of effect: "X scales inversely with Y", "increasing A reduces B", "X is higher in group P than Q". Has a sign that could be checked against a mechanism.
- mechanistic — claims a mechanism but not a direction: "X is regulated by Y via pathway P", "A and B interact through C". The claim is HOW something works.
- observational — claims an observable property of a system: "Most chromosome counts in beetles fall in the 9-11 range", "the karyotype dataset is biased toward N. American taxa". No causal mechanism asserted.
- methodological — claims about how to do something better: "stochastic mapping outperforms parsimony for trait inference", "Bayesian phylogenetics is more reliable for short branches".
- synthesis — bridges findings across separate literatures: "the fragile-Y and meiotic-drive literatures predict the same pattern".
- speculative — claims framed as "what if", with no current evidentiary anchor.

For directional, identify the sign (+, -, or none if unclear).
For directional and mechanistic, set requires_mechanism=true.
For directional only, set requires_sign_check=true.

OUTPUT JSON ONLY:
{
  "type": "directional|mechanistic|observational|methodological|synthesis|speculative",
  "confidence": 0.0-1.0,
  "claim_summary": "<one short sentence summarizing the claim>",
  "directional_sign": "+|-|none",
  "requires_mechanism": true|false,
  "requires_sign_check": true|false
}"""


def tier1_classify(content_md: str) -> dict:
    """Classify hypothesis type with Haiku. Cheap routing decision."""
    msg = _client.messages.create(
        model=HAIKU,
        max_tokens=400,
        system=_CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": f"Classify this hypothesis:\n\n{content_md}"}],
    )

    try:
        usage_dict = msg.usage.model_dump() if hasattr(msg.usage, "model_dump") else dict(msg.usage)
        cost_tracking.record_call(job_name="hypothesis_classifier", model=HAIKU, usage=usage_dict)
    except Exception:
        pass

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {
            "type": "mechanistic",
            "confidence": 0.3,
            "claim_summary": "(classifier parse failure — defaulted to mechanistic)",
            "directional_sign": "none",
            "requires_mechanism": True,
            "requires_sign_check": False,
        }

    parsed.setdefault("type", "mechanistic")
    parsed.setdefault("confidence", 0.5)
    parsed.setdefault("requires_mechanism", parsed["type"] in ("directional", "mechanistic"))
    parsed.setdefault("requires_sign_check", parsed["type"] == "directional")
    return parsed


# ---------------------------------------------------------------------------
# Tier 2 — type-aware critic
# ---------------------------------------------------------------------------

_CRITIC_BASE = """You are a rigorous scientific critic evaluating a proposed research hypothesis. The hypothesis has been classified as type='{htype}'. Apply the rubric items appropriate to this type.

ALL TYPES — ALWAYS APPLY:

1. CLAIM COHERENCE — Is the claim stated clearly and unambiguously? Could two readers disagree about what's being claimed? Flag vague phrasing or unstated referents.

2. REASONING — Does the rationale connect to the claim? Walk through the cause-and-effect or evidentiary chain. Flag rationale that is just restatement of the claim, or that depends on a step the rationale doesn't articulate.

3. CALIBRATION — Is the hypothesis stated as a hypothesis (not a conclusion)? Are limitations acknowledged? Flag overconfidence and hype words (revolutionary, paradigm-shifting, definitively, transformative).

4. NOVELTY/PRIOR-ART — Does the rationale acknowledge what's already known? Flag if it appears to restate prior work without saying so."""

_CRITIC_TYPE_BLOCKS = {
    "directional": """

ADDITIONAL FOR DIRECTIONAL CLAIMS:

5. SIGN-COHERENCE (CRITICAL) — The claim asserts a direction (e.g. "X scales inversely with Y"). Read the proposed mechanism step by step. Does the mechanism predict the SAME direction as the claim, the OPPOSITE direction, or NO clear direction? A mechanism that predicts the wrong sign is a fatal flaw, not a minor issue. If the mechanism predicts the opposite direction, score the hypothesis ≤2 and put "sign mismatch: mechanism predicts opposite of claim" in blocking_issues.

6. ALTERNATIVE-DIRECTION MECHANISM — Construct at least one alternative mechanism that would predict the OPPOSITE direction. Evaluate plausibility. If the alternative is comparably plausible and the hypothesis doesn't address it, flag this in recommendations.

7. STRUCTURED CAUSAL GRAPH (REQUIRED for directional) — In addition to the narrative sign-coherence check above, emit a structured causal_graph in your JSON output (schema below). The causal_graph must connect claim_cause to claim_effect via a chain of intermediate nodes with explicit signs (+, -, or 0). A separate deterministic propagation algorithm will compose the signs along each path and verify the propagated sign matches claim_predicted_sign. If you cannot construct such a chain, that itself is a sign-mismatch flaw — score ≤2 and emit causal_graph with empty causal_links. Be honest: do not fabricate a chain to make the signs work.""",
    "mechanistic": """

ADDITIONAL FOR MECHANISTIC CLAIMS:

5. MECHANISM ARTICULATION — Is the proposed mechanism stated specifically enough that a reader could draw the cause-effect graph? Flag mechanisms that are name-only ("via X pathway") without articulation of the actual steps.

6. ALTERNATIVE MECHANISM — Construct at least one alternative mechanism that could explain the same observation. Evaluate plausibility. If the alternative is comparably plausible, note in recommendations.""",
    "observational": """

ADDITIONAL FOR OBSERVATIONAL CLAIMS:

5. OBSERVABLE — If the observation is true, what specifically would we see? Flag observational claims that don't specify a measurable outcome.

6. SAMPLING/ASCERTAINMENT — Is there a sampling concern that could produce the apparent observation independently of the underlying claim? (Selection bias, study-effort effects, geographic clustering.) Flag if not addressed.""",
    "methodological": """

ADDITIONAL FOR METHODOLOGICAL CLAIMS:

5. COMPARISON-TO-CURRENT — Does the proposal articulate why the new method improves over what's currently used? Flag claims of methodological superiority that don't engage with current methods by name.

6. FALSIFIABILITY — How would we know the new method ISN'T better? Is there a benchmark, a holdout, a simulation that could falsify the claim?""",
    "synthesis": """

ADDITIONAL FOR SYNTHESIS CLAIMS:

5. BRIDGE-CLAIM — Does the synthesis make a claim that goes beyond the sum of the parts? "X says A and Y says B" is not a synthesis. The bridge — the inference that requires both literatures — is the interesting move; flag if absent.

6. CITATION GROUNDING — Synthesis claims live or die on the citations. Are both literatures cited specifically?""",
    "speculative": """

NOTE: This hypothesis is classified as SPECULATIVE — no current evidentiary anchor. It is not held to mechanism or sign-coherence standards. Score modestly (2-3 typical) and put "acceptable as speculation; should not be promoted to executable intention without further grounding" in recommendations.""",
}

_CRITIC_OUTPUT_SPEC = """

SCORING:
  5 — well-grounded, specific, falsifiable, modestly stated. Mechanism/reasoning aligns with claim.
  4 — strong with minor issues
  3 — acceptable but needs tightening
  2 — significant flaws (esp. sign mismatch, name-only mechanism, hype, smoke-test feel)
  1 — not viable (e.g. mechanism predicts opposite of claim, unfalsifiable, or pure speculation in a non-speculative type)

BLOCKING vs RECOMMENDATIONS — IMPORTANT DISTINCTION:

Use blocking_issues ONLY for FATAL flaws — issues that cannot be fixed without rewriting the core claim or mechanism. Examples that ARE blocking:
  - Sign mismatch (mechanism predicts the opposite direction of the claim)
  - Mechanism-free reasoning when type is directional or mechanistic
  - Smoke-test / placeholder language slipped through Tier 0
  - Claim is unfalsifiable as stated
  - Claim restates an already-published finding without acknowledgment

Improvements to the PROPOSED TEST (statistical methods, controls, sample sizes, additional comparators) are NOT blocking — they go in recommendations. A hypothesis can be sound even if the test design needs work; the test can be revised without rewriting the claim. Examples that are NOT blocking and belong in recommendations:
  - "Should use PGLS to control for phylogenetic non-independence"
  - "Should specify Ne proxy more precisely"
  - "Should add a null-model prediction"
  - "Mechanism is internally sound but acknowledges no prior literature"

If the hypothesis is fundamentally sound and only has methodological-test caveats, score it 4 with empty blocking_issues and the caveats in recommendations.

OUTPUT JSON ONLY (no markdown fences, no preamble):
{
  "score": 1-5,
  "type_reviewed": "<echo the type>",
  "claim_coherence_notes": "<1-3 sentences>",
  "reasoning_notes": "<1-3 sentences>",
  "calibration_notes": "<1-2 sentences>",
  "novelty_notes": "<1-2 sentences>",
  "type_specific_notes": "<1-4 sentences — sign-coherence, mechanism articulation, comparison-to-current, etc as relevant>",
  "alternative_explanation_md": "<the alternative mechanism or explanation you constructed, if applicable>",
  "blocking_issues": ["<FATAL flaws only — see definition above>"],
  "recommendations": ["<soft suggestions to improve, including test-design and methodological caveats>"],
  "causal_graph": {                                    /* REQUIRED for directional, optional otherwise (use null) */
    "claim_cause": "<antecedent variable, snake_case>",
    "claim_effect": "<consequent variable, snake_case>",
    "claim_predicted_sign": "+|-|0",
    "causal_links": [
      {"from": "<node>", "to": "<node>", "sign": "+|-|0"},
      ...
    ]
  }
}"""


def _build_critic_system_prompt(htype: str) -> str:
    """Construct a critic prompt with type-conditional rubric items."""
    base = _CRITIC_BASE.format(htype=htype)
    type_block = _CRITIC_TYPE_BLOCKS.get(htype, "")
    return base + type_block + _CRITIC_OUTPUT_SPEC


def tier2_critic(content_md: str, htype: str, model: str = SONNET) -> dict:
    """Run type-aware critic. model defaults to Sonnet (cheap); pass OPUS for formal mode."""
    system_prompt = _build_critic_system_prompt(htype)

    msg = _client.messages.create(
        model=model,
        max_tokens=1500,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": f"Evaluate this hypothesis:\n\n{content_md}"}],
    )

    try:
        usage_dict = msg.usage.model_dump() if hasattr(msg.usage, "model_dump") else dict(msg.usage)
        cost_tracking.record_call(job_name="hypothesis_critic", model=model, usage=usage_dict)
    except Exception:
        pass

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        parsed = {
            "score": 0,
            "type_reviewed": htype,
            "claim_coherence_notes": "",
            "reasoning_notes": "",
            "calibration_notes": "",
            "novelty_notes": "",
            "type_specific_notes": f"parse failure: {exc}",
            "alternative_explanation_md": "",
            "blocking_issues": [f"Critic output failed to parse: {exc}"],
            "recommendations": [],
        }

    parsed["model"] = model
    parsed["tokens_in"] = getattr(msg.usage, "input_tokens", 0) or 0
    parsed["tokens_out"] = getattr(msg.usage, "output_tokens", 0) or 0
    return parsed


# ---------------------------------------------------------------------------
# Tier 0.5 — deterministic sign-propagation (no LLM, runs after Tier 2 for directional)
# ---------------------------------------------------------------------------

def _compose_signs(signs: list[str]) -> str:
    """Multiply a sequence of '+' / '-' / '0' along a path. Any '0' makes the result '0'."""
    result = "+"
    for s in signs:
        if s == "0":
            return "0"
        if s == "-":
            result = "-" if result == "+" else "+"
        # '+' is identity
    return result


def tier05_sign_propagation(causal_graph: dict) -> dict:
    """Walk causal_graph from claim_cause to claim_effect; check propagated sign matches claim.

    Args:
        causal_graph: {claim_cause, claim_effect, claim_predicted_sign, causal_links}
            causal_links: list of {from, to, sign} where sign in {'+','-','0'}

    Returns dict with: ok, propagated_sign, predicted_sign, paths_found, notes,
    sign_counts (per-path sign histogram).
    """
    if not causal_graph:
        return {"ok": False, "ran": False, "notes": "No causal_graph provided"}

    cause = (causal_graph.get("claim_cause") or "").strip().lower()
    effect = (causal_graph.get("claim_effect") or "").strip().lower()
    predicted = (causal_graph.get("claim_predicted_sign") or "").strip()
    links = causal_graph.get("causal_links") or []

    pred_map = {"positive": "+", "negative": "-", "none": "0", "no": "0", "+": "+", "-": "-", "0": "0"}
    pred = pred_map.get(predicted.lower() if isinstance(predicted, str) else "", "?")

    if not cause or not effect:
        return {
            "ok": False, "ran": True, "propagated_sign": "?", "predicted_sign": pred,
            "paths_found": 0, "sign_counts": {"+": 0, "-": 0, "0": 0},
            "notes": "claim_cause or claim_effect missing from causal_graph",
        }
    if not links:
        return {
            "ok": False, "ran": True, "propagated_sign": "?", "predicted_sign": pred,
            "paths_found": 0, "sign_counts": {"+": 0, "-": 0, "0": 0},
            "notes": "causal_links is empty — no chain to propagate",
        }

    graph: dict[str, list[tuple[str, str]]] = {}
    for link in links:
        f = (link.get("from") or "").strip().lower()
        t = (link.get("to") or "").strip().lower()
        s = (link.get("sign") or "").strip()
        if not f or not t or s not in {"+", "-", "0"}:
            continue
        graph.setdefault(f, []).append((t, s))

    if cause not in graph:
        return {
            "ok": False, "ran": True, "propagated_sign": "?", "predicted_sign": pred,
            "paths_found": 0, "sign_counts": {"+": 0, "-": 0, "0": 0},
            "notes": f"cause node '{cause}' has no outgoing edges in causal_links",
        }

    paths_signs: list[str] = []
    queue: deque = deque([(cause, [], {cause})])
    MAX_DEPTH = 10
    MAX_PATHS = 50
    while queue and len(paths_signs) < MAX_PATHS:
        node, path_signs, visited = queue.popleft()
        if len(path_signs) > MAX_DEPTH:
            continue
        for (nxt, sgn) in graph.get(node, []):
            if nxt in visited:
                continue  # skip cycles
            new_signs = path_signs + [sgn]
            if nxt == effect:
                paths_signs.append(_compose_signs(new_signs))
            else:
                queue.append((nxt, new_signs, visited | {nxt}))

    if not paths_signs:
        return {
            "ok": False, "ran": True, "propagated_sign": "?", "predicted_sign": pred,
            "paths_found": 0, "sign_counts": {"+": 0, "-": 0, "0": 0},
            "notes": f"no path found from '{cause}' to '{effect}' in causal_links",
        }

    counts = {"+": 0, "-": 0, "0": 0}
    for s in paths_signs:
        counts[s] = counts.get(s, 0) + 1

    if counts["+"] > 0 and counts["-"] == 0:
        prop = "+"
    elif counts["-"] > 0 and counts["+"] == 0:
        prop = "-"
    elif counts["0"] >= counts["+"] + counts["-"]:
        prop = "0"
    elif counts["+"] > counts["-"]:
        prop = "+"
    elif counts["-"] > counts["+"]:
        prop = "-"
    else:
        prop = "ambiguous"

    if prop == "ambiguous":
        return {
            "ok": False, "ran": True, "propagated_sign": "ambiguous", "predicted_sign": pred,
            "paths_found": len(paths_signs), "sign_counts": counts,
            "notes": f"ambiguous: {counts['+']} paths give +, {counts['-']} give -",
        }

    matches = (prop == pred)
    return {
        "ok": matches, "ran": True,
        "propagated_sign": prop, "predicted_sign": pred,
        "paths_found": len(paths_signs), "sign_counts": counts,
        "notes": (
            f"composed sign {prop} matches claim direction {pred}" if matches
            else f"SIGN MISMATCH: causal_links compose to {prop}, but claim predicts {pred}"
        ),
    }


# ---------------------------------------------------------------------------
# Tier 2.5 — contradiction primitive (Europe PMC + Sonnet)
# ---------------------------------------------------------------------------

_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _pmc_search(query: str, max_results: int = 5) -> list[dict]:
    """Query Europe PMC. Returns list of {title, abstract, doi, year, pmid}."""
    try:
        resp = requests.get(
            _PMC_SEARCH_URL,
            params={"query": query, "format": "json", "pageSize": max_results, "resultType": "core"},
            timeout=12,
        )
        resp.raise_for_status()
        results = resp.json().get("resultList", {}).get("result", [])
        return [{
            "title": r.get("title", ""),
            "abstract": (r.get("abstractText") or "")[:1500],
            "doi": r.get("doi", ""),
            "year": r.get("pubYear", ""),
            "pmid": r.get("pmid", ""),
        } for r in results]
    except Exception:
        return []


_CONTRADICTION_SYSTEM = """You read paper abstracts and decide whether any contradict a proposed hypothesis. Output JSON only — no markdown, no preamble.

A "contradiction" means the paper's findings argue AGAINST the hypothesis being true — they don't merely fail to support it. Examples:
  - hypothesis "X scales positively with Y", paper finds X negatively correlated with Y → contradiction
  - hypothesis "mechanism M produces effect E", paper tests M directly and rejects it → contradiction
  - hypothesis "method A outperforms B", paper benchmarks them and finds A worse → contradiction

Be conservative: only flag a paper as contradicting if the abstract clearly states a finding incompatible with the hypothesis. "Partial overlap with prior work" or "doesn't replicate" alone is not a contradiction. Different system, different scope, different organism is not a contradiction.

OUTPUT JSON ONLY:
{
  "contradictions_found": true|false,
  "contradicting_papers": [
    {"doi": "...", "title": "...", "year": "...", "contradiction_summary": "<1 sentence on what specifically contradicts>"}
  ],
  "supporting_papers": [
    {"doi": "...", "title": "...", "year": "..."}
  ],
  "notes": "<2-3 sentences summarizing what the literature looks like>"
}"""


def tier25_contradiction_check(content_md: str, claim_summary: str = "") -> dict:
    """Search Europe PMC for potentially-contradicting papers; have Sonnet judge each abstract."""
    query = (claim_summary or "").strip() or content_md[:240]
    papers = _pmc_search(query, max_results=5)

    if not papers:
        return {
            "ran": True, "contradictions_found": False, "contradicting_papers": [],
            "supporting_papers": [], "notes": "Europe PMC returned no papers for the query.",
            "papers_examined": 0, "query_used": query[:200],
        }

    paper_blocks = []
    for i, p in enumerate(papers, 1):
        paper_blocks.append(
            f"[{i}] {p['title']} ({p['year']}) DOI: {p['doi']}\n"
            f"Abstract: {p['abstract']}"
        )
    user_msg = (
        f"HYPOTHESIS: {content_md}\n\n"
        f"CLAIM SUMMARY: {claim_summary or '(none)'}\n\n"
        f"PAPERS RETRIEVED FROM EUROPE PMC ({len(papers)}):\n\n"
        + "\n\n---\n\n".join(paper_blocks)
        + "\n\nWhich (if any) of these contradict the hypothesis?"
    )

    msg = _client.messages.create(
        model=SONNET,
        max_tokens=1200,
        system=[{
            "type": "text",
            "text": _CONTRADICTION_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    )

    try:
        usage_dict = msg.usage.model_dump() if hasattr(msg.usage, "model_dump") else dict(msg.usage)
        cost_tracking.record_call(job_name="hypothesis_contradiction", model=SONNET, usage=usage_dict)
    except Exception:
        pass

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(raw)
    except Exception as e:
        parsed = {
            "contradictions_found": False,
            "contradicting_papers": [],
            "supporting_papers": [],
            "notes": f"contradiction-check JSON parse failure: {e}",
        }

    parsed["ran"] = True
    parsed["papers_examined"] = len(papers)
    parsed["query_used"] = query[:200]
    return parsed


# ---------------------------------------------------------------------------
# Pairwise tournament (Sonnet)
# ---------------------------------------------------------------------------

_TOURNAMENT_SYSTEM = """You judge two proposed scientific hypotheses head-to-head and pick the stronger one. Output JSON only — no markdown, no preamble.

Criteria, in priority order:
  1. Mechanism coherence — does the proposed mechanism actually predict the claimed effect?
  2. Specificity and falsifiability — could you design an experiment around it?
  3. Novelty relative to known work
  4. Feasibility of the proposed test

You must pick a winner. Ties resolve toward stronger mechanism. If both are flawed, still pick the less-bad one and explain in the rationale.

OUTPUT JSON ONLY:
{
  "winner": "A" | "B",
  "rationale": "<2-3 sentences>",
  "margin": "narrow" | "clear" | "decisive"
}"""


def pairwise_judge(hypothesis_a: str, hypothesis_b: str) -> dict:
    """Use Sonnet to judge A vs B. Returns {winner, rationale, margin}."""
    user_msg = (
        f"HYPOTHESIS A:\n{hypothesis_a}\n\n---\n\n"
        f"HYPOTHESIS B:\n{hypothesis_b}\n\n"
        f"Which is stronger?"
    )
    msg = _client.messages.create(
        model=SONNET,
        max_tokens=400,
        system=[{
            "type": "text",
            "text": _TOURNAMENT_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    )
    try:
        usage_dict = msg.usage.model_dump() if hasattr(msg.usage, "model_dump") else dict(msg.usage)
        cost_tracking.record_call(job_name="hypothesis_tournament", model=SONNET, usage=usage_dict)
    except Exception:
        pass
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```")).strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {"winner": "A", "rationale": "(judge JSON parse failure)", "margin": "narrow"}
    if parsed.get("winner") not in ("A", "B"):
        parsed["winner"] = "A"
    return parsed


def pairwise_tournament(items: list[dict], k_elo: int = 16) -> list[dict]:
    """Run a tournament on items (each must have key 'content_md'; optional 'id', 'label').

    For N <= 6 hypotheses: round-robin (all pairs, exhaustive).
    For N > 6: capped to top-6 after one Haiku-style pre-screen would go here in
    the future; current implementation just round-robins the first 6.

    Returns sorted (highest Elo first) list with each item augmented by:
      elo (float), wins (int), losses (int), last_judge_rationale (str).
    """
    if len(items) < 2:
        return [{**it, "elo": 1000.0, "wins": 0, "losses": 0} for it in items]

    items = items[:6]  # hard cap to keep cost bounded
    state = [{**it, "elo": 1000.0, "wins": 0, "losses": 0, "last_judge_rationale": ""} for it in items]

    for i in range(len(state)):
        for j in range(i + 1, len(state)):
            judge = pairwise_judge(state[i]["content_md"], state[j]["content_md"])
            winner_idx = i if judge.get("winner") == "A" else j
            loser_idx = j if winner_idx == i else i
            Ra = state[winner_idx]["elo"]
            Rb = state[loser_idx]["elo"]
            Ea = 1.0 / (1 + 10 ** ((Rb - Ra) / 400))
            state[winner_idx]["elo"] = Ra + k_elo * (1 - Ea)
            state[loser_idx]["elo"] = Rb + k_elo * (0 - (1 - Ea))
            state[winner_idx]["wins"] += 1
            state[loser_idx]["losses"] += 1
            state[winner_idx]["last_judge_rationale"] = judge.get("rationale", "")

    return sorted(state, key=lambda x: x["elo"], reverse=True)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

def run_pipeline(
    content_md: str,
    notes: str = "",
    mode: str = "chat",
    project_id: Optional[str] = None,
    run_contradiction_check: Optional[bool] = None,
) -> dict:
    """Run the full hypothesis pipeline.

    Args:
        content_md: the hypothesis text
        notes: provenance notes (used in Tier 0 filter)
        mode: 'chat' (Sonnet critic), 'formal' (Opus critic), 'scheduled' (Sonnet)
        project_id: project association (passed through to result)
        run_contradiction_check: if True, run Europe PMC contradiction check.
            If None (default): True only in formal mode for applicable types.

    Returns dict with: gate_passed, score, block_reasons, warnings,
    type_classification, critic_result, sign_propagation, contradiction_check,
    tier_summary, cost_estimate_usd.
    """
    result: dict = {
        "mode": mode,
        "project_id": project_id,
        "gate_passed": False,
        "score": 0,
        "block_reasons": [],
        "warnings": [],
        "type_classification": None,
        "critic_result": None,
        "sign_propagation": None,
        "contradiction_check": None,
        "tier_summary": "",
        "cost_estimate_usd": 0.0,
    }

    # Tier 0
    t0 = tier0_filter(content_md, notes)
    result["warnings"].extend(t0.get("warnings", []))
    if not t0["ok"]:
        result["block_reasons"].extend(t0["blocked_reasons"])
        result["tier_summary"] = "Blocked at Tier 0 (deterministic filters); no LLM calls made."
        return result

    # Tier 1 — Haiku classifier
    try:
        t1 = tier1_classify(content_md)
        result["type_classification"] = t1
        result["cost_estimate_usd"] += 0.004
    except Exception as e:
        result["block_reasons"].append(f"Tier 1 classifier failed: {e}")
        result["tier_summary"] = f"Failed at Tier 1 (classifier error: {e})."
        return result

    htype = t1.get("type", "mechanistic")

    # Tier 2 — type-aware critic
    critic_model = OPUS if mode == "formal" else SONNET
    try:
        t2 = tier2_critic(content_md, htype, model=critic_model)
        result["critic_result"] = t2
        result["score"] = int(t2.get("score", 0) or 0)
        result["cost_estimate_usd"] += 0.05 if critic_model == OPUS else 0.012
    except Exception as e:
        result["block_reasons"].append(f"Tier 2 critic failed: {e}")
        result["tier_summary"] = f"Failed at Tier 2 (critic error: {e})."
        return result

    blocking = list(t2.get("blocking_issues", []) or [])
    if blocking:
        result["block_reasons"].extend([f"Critic flag: {b}" for b in blocking])

    # Tier 0.5 — deterministic sign-propagation (only for directional with structured graph)
    if htype == "directional":
        cgraph = (t2.get("causal_graph") or {})
        sp = tier05_sign_propagation(cgraph)
        result["sign_propagation"] = sp
        if sp.get("ran") and not sp.get("ok"):
            mismatch_msg = f"Deterministic sign check: {sp.get('notes', 'mismatch')}"
            result["block_reasons"].append(mismatch_msg)
            # Force score down only if narrative critic missed it (i.e. score was >=3)
            if result["score"] >= 3:
                result["score"] = 2
                result["warnings"].append(
                    "score reduced to 2 because deterministic sign-propagation found a mismatch the narrative critic missed"
                )

    # Tier 3 — Opus escalation when chat/scheduled hit borderline AND no other blocking
    if mode != "formal" and 2 < result["score"] < 4 and not result["block_reasons"]:
        try:
            t3 = tier2_critic(content_md, htype, model=OPUS)
            result["critic_result_sonnet"] = t2
            result["critic_result"] = t3
            result["score"] = int(t3.get("score", result["score"]) or result["score"])
            result["cost_estimate_usd"] += 0.05
            t3_blocking = t3.get("blocking_issues", []) or []
            if t3_blocking:
                result["block_reasons"].extend([f"Opus escalation flag: {b}" for b in t3_blocking])
            # Re-run sign-prop on Opus's graph if directional
            if htype == "directional":
                sp2 = tier05_sign_propagation(t3.get("causal_graph") or {})
                result["sign_propagation"] = sp2
                if sp2.get("ran") and not sp2.get("ok"):
                    result["block_reasons"].append(
                        f"Deterministic sign check (Opus graph): {sp2.get('notes', 'mismatch')}"
                    )
                    if result["score"] >= 3:
                        result["score"] = 2
        except Exception:
            pass

    # Tier 2.5 — contradiction check (formal mode default; off in chat unless requested)
    apply_contradiction = run_contradiction_check
    if apply_contradiction is None:
        apply_contradiction = (
            mode == "formal" and htype in ("directional", "mechanistic", "observational", "synthesis")
        )
    if apply_contradiction:
        try:
            cs = (t1.get("claim_summary") or "")
            cc = tier25_contradiction_check(content_md, claim_summary=cs)
            result["contradiction_check"] = cc
            result["cost_estimate_usd"] += 0.014
            if cc.get("contradictions_found"):
                contras = cc.get("contradicting_papers") or []
                if contras:
                    citations = "; ".join(
                        f"{c.get('doi') or c.get('title', '?')}: {c.get('contradiction_summary', '')}"
                        for c in contras[:3]
                    )
                    result["block_reasons"].append(
                        f"Contradiction-check flag: literature contradicts the claim — {citations}"
                    )
                    if result["score"] >= 3:
                        result["score"] = 2
        except Exception as e:
            result["warnings"].append(f"contradiction-check failed: {e}")

    result["gate_passed"] = (result["score"] >= 3) and (len(result["block_reasons"]) == 0)

    cr = result.get("critic_result") or {}
    sp_summary = ""
    if result.get("sign_propagation") and result["sign_propagation"].get("ran"):
        sp = result["sign_propagation"]
        sp_summary = f" SignProp:{sp.get('propagated_sign', '?')}vs{sp.get('predicted_sign', '?')}={'OK' if sp.get('ok') else 'MISMATCH'}."
    cc_summary = ""
    if result.get("contradiction_check") and result["contradiction_check"].get("ran"):
        cc = result["contradiction_check"]
        cc_summary = f" Contradiction:{'YES' if cc.get('contradictions_found') else 'no'}({cc.get('papers_examined', 0)}papers)."

    result["tier_summary"] = (
        f"Type={htype} (conf={t1.get('confidence', 0):.2f}). "
        f"Score={result['score']}/5 (critic={cr.get('model', '?')}).{sp_summary}{cc_summary} "
        f"Gate {'PASSED' if result['gate_passed'] else 'BLOCKED'}. "
        f"Cost ≈ ${result['cost_estimate_usd']:.3f}."
    )
    return result


# ---------------------------------------------------------------------------
# Result formatting helpers
# ---------------------------------------------------------------------------

def format_result_md(result: dict) -> str:
    """Render a pipeline result dict as a markdown block for chat display."""
    lines: list[str] = []
    lines.append("### Hypothesis gate result")
    lines.append("")
    lines.append(f"**Summary:** {result.get('tier_summary', '(no summary)')}")
    lines.append("")

    if result.get("block_reasons"):
        lines.append("**Block reasons:**")
        for r in result["block_reasons"]:
            lines.append(f"- {r}")
        lines.append("")

    if result.get("warnings"):
        lines.append("**Warnings:**")
        for w in result["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    tc = result.get("type_classification") or {}
    if tc:
        lines.append(f"**Type:** {tc.get('type', '?')} (confidence {tc.get('confidence', 0):.2f})")
        if tc.get("claim_summary"):
            lines.append(f"  - Claim: {tc['claim_summary']}")
        if tc.get("requires_sign_check"):
            lines.append(f"  - Stated direction: {tc.get('directional_sign', '?')}")
        lines.append("")

    cr = result.get("critic_result") or {}
    if cr:
        lines.append(f"**Critic ({cr.get('model', '?')}):** score {cr.get('score', '?')}/5")
        for field, label in [
            ("claim_coherence_notes", "Claim coherence"),
            ("reasoning_notes", "Reasoning"),
            ("type_specific_notes", "Type-specific (mechanism / sign / etc.)"),
            ("calibration_notes", "Calibration"),
            ("novelty_notes", "Novelty / prior art"),
        ]:
            val = cr.get(field, "")
            if val:
                lines.append(f"  - *{label}:* {val}")
        if cr.get("alternative_explanation_md"):
            lines.append(f"  - *Alternative explanation:* {cr['alternative_explanation_md']}")
        if cr.get("recommendations"):
            lines.append("  - *Recommendations:*")
            for r in cr["recommendations"]:
                lines.append(f"    - {r}")
        lines.append("")

    sp = result.get("sign_propagation") or {}
    if sp.get("ran"):
        lines.append(
            f"**Deterministic sign propagation:** "
            f"{'OK' if sp.get('ok') else 'MISMATCH'} — "
            f"composed {sp.get('propagated_sign', '?')} vs claim {sp.get('predicted_sign', '?')} "
            f"over {sp.get('paths_found', 0)} path(s). {sp.get('notes', '')}"
        )
        lines.append("")

    cc = result.get("contradiction_check") or {}
    if cc.get("ran"):
        if cc.get("contradictions_found"):
            lines.append(f"**Contradiction check:** {len(cc.get('contradicting_papers', []))} contradicting paper(s) found in Europe PMC")
            for p in cc.get("contradicting_papers", [])[:3]:
                doi = p.get("doi") or "(no DOI)"
                lines.append(f"  - {p.get('title', '(no title)')} ({p.get('year', '?')}) — {doi}")
                if p.get("contradiction_summary"):
                    lines.append(f"    *Why:* {p['contradiction_summary']}")
        else:
            lines.append(f"**Contradiction check:** no contradicting papers found ({cc.get('papers_examined', 0)} examined)")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manual run for verification
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    smoke_fixture = (
        "[SMOKE TEST] XO sex chromosome frequency scales inversely with diploid "
        "number in Coleoptera (Fragile Y mechanism). Embargoed until 2026-04-28."
    )
    r = run_pipeline(smoke_fixture, notes="backfill of 2026-04-21 chat session", mode="chat")
    print(format_result_md(r))
