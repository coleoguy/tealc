"""contradiction_radar.py — Contradiction Radar for Tealc agent.

For each claim-like sentence in a draft, query the heath_claims knowledge graph
for historical claims by the same researcher on the same subject, then use Sonnet
to judge whether the new claim agrees, refines, shifts position, or contradicts
prior work.

Public API
----------
detect_contradictions(draft_text: str) -> dict
"""
from __future__ import annotations

import json
import logging
import os
import re

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Foundation import — gracefully degrade if Tier 2 not yet built
# ---------------------------------------------------------------------------

_foundation_available = False

try:
    from agent.voice_index import (  # type: ignore[attr-defined]
        retrieve_similar_claims,
        is_foundation_ready,
    )
    _foundation_available = True
except ImportError:
    def retrieve_similar_claims(  # type: ignore[misc]
        subject: str,
        predicate: str | None = None,
        k: int = 5,
    ) -> list[dict]:
        return []

    def is_foundation_ready() -> bool:  # type: ignore[misc]
        return False


# ---------------------------------------------------------------------------
# Anthropic client (lazy init so import doesn't hard-fail if key missing)
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic  # noqa: PLC0415
        _client = Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Cost-tracking (best-effort)
# ---------------------------------------------------------------------------

import agent.cost_tracking as _cost_tracking  # noqa: E402


def _record(job_name: str, model: str, usage) -> None:
    try:
        usage_dict = (
            usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        )
        _cost_tracking.record_call(job_name=job_name, model=model, usage=usage_dict)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cost pricing for Sonnet (per 1M tokens)
# ---------------------------------------------------------------------------

_SONNET_MODEL = "claude-sonnet-4-6"
_SONNET_IN_PER_1M = 3.0
_SONNET_OUT_PER_1M = 15.0
_COST_CAP_USD = 1.00
_MAX_CLAIM_SENTENCES = 25  # hard-cap if cost_tracking unavailable

# ---------------------------------------------------------------------------
# Claim-sentence heuristic (same verb set as citation_suggester)
# ---------------------------------------------------------------------------

_CLAIM_VERBS = {
    "show", "shows", "showed", "shown",
    "demonstrate", "demonstrates", "demonstrated",
    "find", "finds", "found",
    "suggest", "suggests", "suggested",
    "indicate", "indicates", "indicated",
    "reveal", "reveals", "revealed",
    "observe", "observes", "observed",
    "propose", "proposes", "proposed",
    "hypothesize", "hypothesizes", "hypothesized",
    "predict", "predicts", "predicted",
    "confirm", "confirms", "confirmed",
    "support", "supports", "supported",
    "contradict", "contradicts", "contradicted",
    "challenge", "challenges", "challenged",
    "refute", "refutes", "refuted",
    "establish", "establishes", "established",
    # quantitative / causal verbs common in evolutionary biology
    "increase", "increases", "increased",
    "decrease", "decreases", "decreased",
    "cause", "causes", "caused",
    "drive", "drives", "drove", "driven",
    "accelerate", "accelerates", "accelerated",
    "reduce", "reduces", "reduced",
    "promote", "promotes", "promoted",
    "correlate", "correlates", "correlated",
    "associate", "associates", "associated",
    "affect", "affects", "affected",
    "influence", "influences", "influenced",
    "determine", "determines", "determined",
    "mediate", "mediates", "mediated",
    "require", "requires", "required",
}

_MIN_SENTENCE_CHARS = 30

# ---------------------------------------------------------------------------
# Sentence splitter (reuse citation_suggester approach)
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Sentence splitter via pysbd (handles 'et al.', 'Fig. 3a', citations)."""
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"^#{1,6}\s+", "\n", text, flags=re.MULTILINE)
    from agent.text_utils import segment_sentences  # noqa: PLC0415
    return segment_sentences(text, min_chars=_MIN_SENTENCE_CHARS)


def _is_claim_sentence(sentence: str) -> bool:
    """Return True if sentence contains a claim-verb."""
    words = re.findall(r"\b[a-z]+\b", sentence.lower())
    return any(w in _CLAIM_VERBS for w in words)


# ---------------------------------------------------------------------------
# Sonnet helpers — temperature=0 on all calls
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = (
    "Extract the subject and predicate of this scientific claim. "
    "Output JSON only (no markdown fences): {\"subject\": \"<noun phrase>\", "
    "\"predicate\": \"<verb phrase>\"}. "
    "Be specific: use the actual noun phrase, not 'X' or 'this'. "
    "If the sentence has no clear scientific claim, output "
    "{\"subject\": \"\", \"predicate\": \"\"}."
)

_JUDGE_SYSTEM = (
    "You are a scientific consistency checker. Given a new claim and a list of "
    "historical claims by the same researcher, judge the relationship. "
    "Definitions:\n"
    "  agrees       — the new claim is consistent with and supports past claims.\n"
    "  refines      — the new claim adds nuance, narrows scope, or quantifies "
    "                 something the prior claim stated more broadly.\n"
    "  shifted_position — the researcher has changed their position between "
    "                 then and now in a meaningful way (not just added nuance).\n"
    "  contradicts  — the new claim directly opposes one or more prior claims.\n"
    "  no_overlap   — the prior claims do not address the same subject.\n"
    "Output JSON only (no markdown fences): "
    "{\"verdict\": \"<one of the five options>\", "
    "\"confidence\": \"<high|medium|low>\", "
    "\"rationale\": \"<1-2 sentences>\"}. "
    "Temperature is 0; be decisive. "
    "Distinguish 'shifted_position' (changed opinion across years) from "
    "'refines' (added nuance without reversing the prior stance)."
)


def _parse_json_response(raw: str, fallback: dict) -> dict:
    """Strip markdown fences and parse JSON, returning fallback on failure."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.startswith("```")).strip()
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _estimate_call_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * _SONNET_IN_PER_1M / 1_000_000
        + output_tokens * _SONNET_OUT_PER_1M / 1_000_000
    )


def _extract_subject_predicate(sentence: str, client) -> tuple[str, str]:
    """Call Sonnet to extract (subject, predicate) from a claim sentence.

    Returns ("", "") on any failure.
    """
    try:
        msg = client.messages.create(
            model=_SONNET_MODEL,
            max_tokens=120,
            temperature=0,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": sentence}],
        )
        _record("contradiction_radar_extract", _SONNET_MODEL, msg.usage)
        raw = msg.content[0].text if msg.content else "{}"
        parsed = _parse_json_response(raw, {"subject": "", "predicate": ""})
        subject = (parsed.get("subject") or "").strip()
        predicate = (parsed.get("predicate") or "").strip()
        return subject, predicate
    except Exception as exc:
        log.warning("contradiction_radar: extract call failed: %s", exc)
        return "", ""


def _judge_contradiction(
    sentence: str,
    subject: str,
    historical_claims: list[dict],
    client,
) -> dict:
    """Call Sonnet to judge relationship between new claim and historical claims.

    Returns dict with verdict, confidence, rationale.
    """
    hist_lines = []
    for i, hc in enumerate(historical_claims, 1):
        year = hc.get("year") or "?"
        pid = hc.get("paper_id") or "?"
        subj = hc.get("subject") or ""
        pred = hc.get("predicate") or ""
        obj = hc.get("object") or ""
        quote = hc.get("evidence_quote") or ""
        hist_lines.append(
            f"[{i}] ({pid}, {year}): {subj} {pred} {obj}"
            + (f' — "{quote[:200]}"' if quote else "")
        )
    hist_block = "\n".join(hist_lines)

    user_msg = (
        f"NEW CLAIM:\n{sentence}\n\n"
        f"HISTORICAL CLAIMS by the same researcher on subject '{subject}':\n"
        f"{hist_block}"
    )

    fallback = {
        "verdict": "no_overlap",
        "confidence": "low",
        "rationale": "Judge call failed; defaulting to no_overlap.",
    }

    try:
        msg = client.messages.create(
            model=_SONNET_MODEL,
            max_tokens=200,
            temperature=0,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        _record("contradiction_radar_judge", _SONNET_MODEL, msg.usage)
        raw = msg.content[0].text if msg.content else "{}"
        parsed = _parse_json_response(raw, fallback)
        verdict = parsed.get("verdict", "no_overlap")
        if verdict not in {"agrees", "refines", "shifted_position", "contradicts", "no_overlap"}:
            verdict = "no_overlap"
        confidence = parsed.get("confidence", "low")
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        rationale = (parsed.get("rationale") or "").strip()
        return {
            "verdict": verdict,
            "confidence_band": confidence,
            "rationale": rationale,
        }
    except Exception as exc:
        log.warning("contradiction_radar: judge call failed: %s", exc)
        return {
            "verdict": fallback["verdict"],
            "confidence_band": fallback["confidence"],
            "rationale": fallback["rationale"],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_contradictions(draft_text: str) -> dict:
    """For each claim-like sentence in draft_text, query heath_claims for
    historical claims with the same subject. Use Sonnet to judge whether
    the new claim agrees, refines, or contradicts past claims.

    Returns:
        {
          "contradictions": [
            {
              "draft_sentence": str,
              "extracted_subject": str,
              "extracted_predicate": str,
              "historical_claims": [
                {"paper_id", "year", "subject", "predicate", "object",
                 "evidence_quote", "similarity"},
              ],
              "verdict": "agrees" | "refines" | "shifted_position" | "contradicts" | "no_overlap",
              "confidence_band": "high" | "medium" | "low",
              "rationale": str,  # 1-2 sentences
            },
          ],
          "n_claim_sentences": int,
          "n_overlapping": int,  # subset where verdict != "no_overlap"
          "foundation_ready": bool,
          "summary": str,
        }
    """
    # Check foundation readiness
    try:
        ready = is_foundation_ready()
    except Exception:
        ready = False

    empty_result: dict = {
        "contradictions": [],
        "n_claim_sentences": 0,
        "n_overlapping": 0,
        "foundation_ready": ready,
        "summary": "",
    }

    # Guard: empty input
    if not draft_text or not draft_text.strip():
        empty_result["summary"] = "Empty draft text."
        return empty_result

    # Segment into claim-like sentences
    sentences = _split_sentences(draft_text)
    claim_sentences = [s for s in sentences if _is_claim_sentence(s)]
    n_claim_sentences = len(claim_sentences)
    empty_result["n_claim_sentences"] = n_claim_sentences

    # Guard: foundation not ready
    if not ready:
        empty_result["summary"] = (
            f"Foundation index not ready; found {n_claim_sentences} claim sentence(s) "
            "but cannot retrieve historical claims."
        )
        return empty_result

    # Guard: no claim sentences found
    if not claim_sentences:
        empty_result["summary"] = "No claim-like sentences found in draft."
        return empty_result

    # Acquire Sonnet client
    try:
        client = _get_client()
    except Exception as exc:
        log.warning("contradiction_radar: could not initialise Anthropic client: %s", exc)
        empty_result["summary"] = (
            f"Sonnet client unavailable ({exc}); "
            f"found {n_claim_sentences} claim sentence(s) but could not analyse."
        )
        return empty_result

    # Cost cap: estimate tokens available; each claim uses ~150 in + ~60 out for
    # extract and ~300 in + ~80 out for judge. Roughly $0.035 per claim at Sonnet rates.
    # Hard-cap at _MAX_CLAIM_SENTENCES to stay under $1.00 even without cost_tracking.
    effective_cap = min(n_claim_sentences, _MAX_CLAIM_SENTENCES)
    if n_claim_sentences > _MAX_CLAIM_SENTENCES:
        log.warning(
            "contradiction_radar: capping at %d claim sentences (found %d) to stay under $1.00",
            _MAX_CLAIM_SENTENCES,
            n_claim_sentences,
        )

    accumulated_cost: float = 0.0
    contradictions: list[dict] = []

    for sentence in claim_sentences[:effective_cap]:
        # Cost check before each pair of Sonnet calls
        if accumulated_cost >= _COST_CAP_USD:
            log.warning(
                "contradiction_radar: cost cap $%.2f reached; stopping early after %d sentences",
                _COST_CAP_USD,
                len(contradictions),
            )
            break

        # Step 1: extract subject + predicate
        subject, predicate = _extract_subject_predicate(sentence, client)
        # Estimate cost for this extract call (~150 in, ~60 out)
        accumulated_cost += _estimate_call_cost(150, 60)

        if not subject:
            # Can't retrieve without a subject; emit no_overlap entry
            contradictions.append({
                "draft_sentence": sentence,
                "extracted_subject": "",
                "extracted_predicate": "",
                "historical_claims": [],
                "verdict": "no_overlap",
                "confidence_band": "low",
                "rationale": "Could not extract a subject from this sentence.",
            })
            continue

        # Step 2: retrieve historical claims
        try:
            historical = retrieve_similar_claims(subject, predicate=predicate or None, k=5)
        except Exception as exc:
            log.warning("contradiction_radar: retrieve_similar_claims failed: %s", exc)
            historical = []

        # Filter to the fields we want to expose
        trimmed_historical = [
            {
                "paper_id": hc.get("paper_id", ""),
                "year": hc.get("year"),
                "subject": hc.get("subject", ""),
                "predicate": hc.get("predicate", ""),
                "object": hc.get("object", ""),
                "evidence_quote": hc.get("evidence_quote", ""),
                "similarity": hc.get("similarity"),
            }
            for hc in historical
        ]

        # Step 3: no historical hits → no_overlap, skip judge call
        if not trimmed_historical:
            contradictions.append({
                "draft_sentence": sentence,
                "extracted_subject": subject,
                "extracted_predicate": predicate,
                "historical_claims": [],
                "verdict": "no_overlap",
                "confidence_band": "low",
                "rationale": "No historical claims found for this subject in the knowledge graph.",
            })
            continue

        # Step 4: judge relationship
        if accumulated_cost >= _COST_CAP_USD:
            log.warning("contradiction_radar: cost cap reached before judge call.")
            break

        judgment = _judge_contradiction(sentence, subject, trimmed_historical, client)
        # Estimate cost for judge call (~300 in, ~80 out)
        accumulated_cost += _estimate_call_cost(300, 80)

        contradictions.append({
            "draft_sentence": sentence,
            "extracted_subject": subject,
            "extracted_predicate": predicate,
            "historical_claims": trimmed_historical,
            "verdict": judgment["verdict"],
            "confidence_band": judgment["confidence_band"],
            "rationale": judgment["rationale"],
        })

    n_overlapping = sum(
        1 for c in contradictions if c["verdict"] != "no_overlap"
    )

    # Build summary
    verdict_counts: dict[str, int] = {}
    for c in contradictions:
        v = c["verdict"]
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    parts = []
    if verdict_counts.get("contradicts"):
        parts.append(f"{verdict_counts['contradicts']} contradiction(s)")
    if verdict_counts.get("shifted_position"):
        parts.append(f"{verdict_counts['shifted_position']} position shift(s)")
    if verdict_counts.get("refines"):
        parts.append(f"{verdict_counts['refines']} refinement(s)")
    if verdict_counts.get("agrees"):
        parts.append(f"{verdict_counts['agrees']} agreement(s)")

    if parts:
        summary = (
            f"Scanned {n_claim_sentences} claim sentence(s); "
            f"{n_overlapping} overlapped with historical claims: "
            + ", ".join(parts) + "."
        )
    else:
        summary = (
            f"Scanned {n_claim_sentences} claim sentence(s); "
            "no overlapping historical claims found."
        )

    return {
        "contradictions": contradictions,
        "n_claim_sentences": n_claim_sentences,
        "n_overlapping": n_overlapping,
        "foundation_ready": ready,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Smoke tests (run directly)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=== Smoke test 1: empty input ===")
    r1 = detect_contradictions("")
    assert r1["contradictions"] == [], f"expected [], got {r1['contradictions']}"
    assert r1["n_claim_sentences"] == 0
    assert isinstance(r1["summary"], str)
    print("  ok — empty input returns graceful empty")

    print("\n=== Smoke test 2: foundation-not-ready check ===")
    r2 = detect_contradictions(
        "Sex chromosome turnover increases speciation rate in tetrapods."
    )
    print(f"  foundation_ready: {r2['foundation_ready']}")
    print(f"  n_claim_sentences: {r2['n_claim_sentences']}")
    print(f"  summary: {r2['summary']}")
    if not r2["foundation_ready"]:
        assert r2["contradictions"] == []
        print("  ok — foundation not ready, graceful empty returned")
    else:
        assert r2["n_claim_sentences"] >= 1
        print(f"  ok — foundation ready, {len(r2['contradictions'])} contradiction entries returned")
        for entry in r2["contradictions"]:
            print(
                f"    [{entry['verdict']} / {entry['confidence_band']}] "
                f"{entry['extracted_subject']!r}: {entry['rationale']}"
            )

    print("\n=== Smoke test 3: Sonnet API reachability ===")
    try:
        c = _get_client()
        msg = c.messages.create(
            model=_SONNET_MODEL,
            max_tokens=10,
            temperature=0,
            messages=[{"role": "user", "content": "Say ok"}],
        )
        print(f"  ok — Sonnet reachable, response: {msg.content[0].text!r}")
    except Exception as exc:
        print(f"  WARNING — Sonnet unreachable: {exc}")

    print("\nAll smoke tests complete.")
    sys.exit(0)
