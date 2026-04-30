"""citation_suggester.py — Find Heath's own prior work that matches claim sentences.

For each claim-like sentence in a draft, retrieve semantically similar sentences
from Heath's 63 published papers (via the Tier 2 voice_index Foundation) and
emit a Markdown footnote block: "## Consider citing (Heath's prior work)".

Public API
----------
suggest_citations(draft_text, sections_to_scan=None) -> dict
build_footnote_block_md(suggestions) -> str
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Foundation import — gracefully degrade if Tier 2 not yet built
# ---------------------------------------------------------------------------

_foundation_available = False

try:
    from agent.voice_index import (  # type: ignore[attr-defined]
        embed_sentences,
        retrieve_similar_sentences,
        is_foundation_ready,
    )
    _foundation_available = True
except ImportError:
    # Tier 2 Foundation not yet built; cite suggester runs in degraded mode
    def embed_sentences(sentences: list[str]) -> list[Any]:  # type: ignore[misc]
        return []

    def retrieve_similar_sentences(  # type: ignore[misc]
        query: str, k: int = 12, min_cosine: float = 0.55
    ) -> list[dict]:
        return []

    def is_foundation_ready() -> bool:  # type: ignore[misc]
        return False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SECTIONS = ["approach", "results", "background", "discussion"]
_SKIP_SECTIONS = {"methods", "method", "materials and methods", "experimental procedures"}

# Verbs that signal a claim sentence
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
}

# Regex: ends with an inline citation like "(Smith et al., 2021)" or "(Smith et al. 2021)"
_CITATION_PATTERN = re.compile(r"\([A-Z][a-z]+ et al\.?,? \d{4}\)")

# Section header patterns (ATX and setext markdown, and plain uppercase/title headings)
_HEADER_RE = re.compile(
    r"^(?:#{1,6}\s+(.+)|([A-Z][A-Za-z ]{2,}):?\s*$)",
    re.MULTILINE,
)

# Minimum similarity threshold for inclusion in output
_MIN_COSINE = 0.70
# Top papers per claim sentence
_TOP_PAPERS = 3
# Min sentence length to be considered (skip very short fragments)
_MIN_SENTENCE_CHARS = 30


# ---------------------------------------------------------------------------
# Sentence splitter
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Sentence splitter via pysbd (handles 'et al.', 'Fig. 3a', citations)."""
    # Normalise line endings + strip markdown headers so they don't run on
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"^#{1,6}\s+", "\n", text, flags=re.MULTILINE)
    from agent.text_utils import segment_sentences  # noqa: PLC0415
    return segment_sentences(text, min_chars=_MIN_SENTENCE_CHARS)


# ---------------------------------------------------------------------------
# Section detector
# ---------------------------------------------------------------------------

def _tag_sentences_by_section(text: str) -> list[tuple[str, str]]:
    """Return [(sentence, section_name), ...].

    Walks the text, tracking the most recent header. Sentences before any
    header are tagged "preamble".
    """
    # Split into blocks separated by blank lines or headers
    lines = text.splitlines()
    current_section = "preamble"
    tagged: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush_buffer(section: str) -> None:
        chunk = " ".join(buffer).strip()
        if chunk:
            for sent in _split_sentences(chunk):
                if len(sent) >= _MIN_SENTENCE_CHARS:
                    tagged.append((sent, section))
        buffer.clear()

    for line in lines:
        # Detect ATX-style header
        atx = re.match(r"^(#{1,6})\s+(.*)", line)
        if atx:
            flush_buffer(current_section)
            current_section = atx.group(2).strip().lower()
            continue
        # Detect setext-style header (line of === or ---)
        if re.match(r"^[=\-]{3,}\s*$", line) and buffer:
            header_candidate = buffer[-1].strip()
            buffer.pop()
            flush_buffer(current_section)
            current_section = header_candidate.lower()
            continue
        buffer.append(line)

    flush_buffer(current_section)
    return tagged


# ---------------------------------------------------------------------------
# Claim-sentence filter
# ---------------------------------------------------------------------------

def _is_claim_sentence(sentence: str) -> bool:
    """Return True if the sentence looks like a scientific claim."""
    lower = sentence.lower()
    words = re.findall(r"\b[a-z]+\b", lower)
    if any(w in _CLAIM_VERBS for w in words):
        return True
    if _CITATION_PATTERN.search(sentence):
        return True
    return False


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def suggest_citations(
    draft_text: str,
    sections_to_scan: list[str] | None = None,
) -> dict:
    """For each claim-like sentence in draft_text, find Heath's own papers that
    semantically match. Used to inject "Consider citing your own prior work"
    footnote blocks into grant + manuscript drafts.

    Args:
        draft_text: full draft text (markdown or plain).
        sections_to_scan: e.g. ["approach", "results", "background"]. Default
                          ["approach", "results", "background", "discussion"].
                          Skip "methods" (too much boilerplate).

    Returns:
        {
          "suggestions": [
            {
              "draft_sentence": str,
              "draft_section": str,
              "candidates": [
                {"paper_id": str, "year": int, "matched_sentence": str, "similarity": float},
                ...  # top-3 per draft sentence, similarity >= 0.70
            ],
          ],
          "footnote_block_md": str,
          "n_claim_sentences": int,
          "foundation_ready": bool,
          "summary": str,
        }
    """
    if sections_to_scan is None:
        sections_to_scan = list(_DEFAULT_SECTIONS)
    scan_set = {s.lower() for s in sections_to_scan}

    # Check foundation readiness
    try:
        ready = is_foundation_ready()
    except Exception:
        ready = False

    empty_result: dict = {
        "suggestions": [],
        "footnote_block_md": "",
        "n_claim_sentences": 0,
        "foundation_ready": ready,
        "summary": "",
    }

    if not draft_text or not draft_text.strip():
        empty_result["summary"] = "Empty draft text."
        return empty_result

    # Tag every sentence with its section
    tagged = _tag_sentences_by_section(draft_text)

    # Filter to target sections (skip methods and non-scanned sections)
    def _section_in_scope(section: str) -> bool:
        sec = section.lower()
        # Skip methods sections regardless of scan_set
        for skip in _SKIP_SECTIONS:
            if skip in sec:
                return False
        # Include if section name contains any of the target tokens, or
        # if it is "preamble" (front matter before first header) and
        # "background" is in scan_set
        if sec == "preamble":
            return "background" in scan_set
        # Match if any scan_set word appears in the section name
        return any(tok in sec for tok in scan_set)

    in_scope = [(sent, sec) for sent, sec in tagged if _section_in_scope(sec)]

    # Identify claim sentences
    claim_sentences = [(sent, sec) for sent, sec in in_scope if _is_claim_sentence(sent)]

    n_claim_sentences = len(claim_sentences)

    if not ready:
        empty_result["n_claim_sentences"] = n_claim_sentences
        empty_result["summary"] = (
            f"Foundation index not ready; found {n_claim_sentences} claim sentence(s) "
            "but cannot retrieve matches."
        )
        return empty_result

    if not claim_sentences:
        empty_result["n_claim_sentences"] = 0
        empty_result["summary"] = "No claim sentences found in the specified sections."
        return empty_result

    # For each claim sentence, retrieve similar sentences from the index
    suggestions: list[dict] = []

    for sent, section in claim_sentences:
        try:
            hits = retrieve_similar_sentences(sent, k=10, min_cosine=_MIN_COSINE)
        except Exception as exc:
            log.warning("retrieve_similar_sentences failed for sentence: %s", exc)
            hits = []

        if not hits:
            continue

        # Group by paper_id, keep best (highest similarity) hit per paper
        by_paper: dict[str, dict] = {}
        for hit in hits:
            pid = hit.get("paper_id", "")
            if not pid:
                continue
            if pid not in by_paper or hit.get("similarity", 0) > by_paper[pid].get("similarity", 0):
                by_paper[pid] = hit

        # Sort papers by best similarity descending, take top _TOP_PAPERS
        top_papers = sorted(by_paper.values(), key=lambda h: h.get("similarity", 0), reverse=True)[
            :_TOP_PAPERS
        ]

        if not top_papers:
            continue

        candidates = [
            {
                "paper_id": h.get("paper_id", ""),
                "year": h.get("year", 0),
                "matched_sentence": h.get("sentence", ""),
                "similarity": round(float(h.get("similarity", 0)), 4),
            }
            for h in top_papers
        ]

        suggestions.append(
            {
                "draft_sentence": sent,
                "draft_section": section,
                "candidates": candidates,
            }
        )

    footnote_block = build_footnote_block_md(suggestions)

    n_with_candidates = len(suggestions)
    summary = (
        f"Scanned {n_claim_sentences} claim sentence(s) across sections "
        f"{sorted(scan_set)}; found {n_with_candidates} with candidate prior-work matches."
    )

    return {
        "suggestions": suggestions,
        "footnote_block_md": footnote_block,
        "n_claim_sentences": n_claim_sentences,
        "foundation_ready": ready,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Footnote formatter
# ---------------------------------------------------------------------------

def build_footnote_block_md(suggestions: list[dict]) -> str:
    """Pure formatter — Markdown comment block.

    Format:
        ## Consider citing (Heath's prior work)

        For "<draft sentence excerpt 1>":
          - (paperX_id, 2021): "<matched sentence excerpt>"
          - (paperY_id, 2019): "<matched sentence excerpt>"

        For "<draft sentence excerpt 2>":
          - ...
    """
    if not suggestions:
        return ""

    lines: list[str] = ["## Consider citing (Heath's prior work)", ""]

    for item in suggestions:
        draft_sent = item.get("draft_sentence", "")
        candidates = item.get("candidates", [])
        if not candidates:
            continue

        # Truncate long draft sentences for readability
        excerpt = draft_sent if len(draft_sent) <= 120 else draft_sent[:117] + "..."
        lines.append(f'For "{excerpt}":')

        for cand in candidates:
            pid = cand.get("paper_id", "?")
            year = cand.get("year", "?")
            matched = cand.get("matched_sentence", "")
            matched_excerpt = matched if len(matched) <= 100 else matched[:97] + "..."
            lines.append(f'  - ({pid}, {year}): "{matched_excerpt}"')

        lines.append("")

    # Remove trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manual smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = suggest_citations(
        "We show that sex chromosome turnover drives speciation in lizards."
    )
    print("foundation_ready:", result["foundation_ready"])
    print("n_claim_sentences:", result["n_claim_sentences"])
    print("suggestions:", len(result["suggestions"]))
    print("summary:", result["summary"])
    print()
    empty_fn = build_footnote_block_md([])
    print("build_footnote_block_md([]) ->", repr(empty_fn))
