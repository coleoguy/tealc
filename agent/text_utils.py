"""Centralized text utilities for the Tealc corpus pipeline.

Single source of truth for sentence segmentation. Used by:
  - agent/jobs/build_heath_corpus.py        (corpus indexing)
  - agent/personas/self_plagiarism.py        (draft scanning)
  - agent/citation_suggester.py              (claim extraction from drafts)
  - agent/contradiction_radar.py             (claim extraction)

We use pysbd (Python Sentence Boundary Disambiguation):
  - Pure Python — no native binaries, runs on Apple Silicon and x86_64
  - Rule-based, ~1000 sentences/sec
  - ~94% accuracy on Genia (scientific) corpus
  - Correctly handles "et al.", "Fig. 3a", "p < 0.05", "Drs.", citations,
    abbreviations — edge cases that destroy regex-based splitters.

Falls back to a naive regex if pysbd is unavailable for any reason, so the
pipeline is never bricked.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("tealc.text_utils")

_SEGMENTER = None
_SEGMENTER_KIND = None  # 'pysbd' | 'regex'


def _get_segmenter():
    """Lazy-load pysbd segmenter (cached). Returns the segmenter or None."""
    global _SEGMENTER, _SEGMENTER_KIND
    if _SEGMENTER is not None or _SEGMENTER_KIND == "regex":
        return _SEGMENTER
    try:
        import pysbd  # type: ignore
        _SEGMENTER = pysbd.Segmenter(language="en", clean=False)
        _SEGMENTER_KIND = "pysbd"
        return _SEGMENTER
    except Exception as exc:
        log.warning("pysbd unavailable, using regex fallback: %s", exc)
        _SEGMENTER_KIND = "regex"
        return None


def segment_sentences(
    text: str,
    min_chars: int = 0,
    max_chars: int | None = None,
) -> list[str]:
    """Segment text into sentences.

    Args:
        text: input text. Can contain newlines, equations, citations.
        min_chars: drop sentences shorter than this (defaults to 0 = keep all).
                   Use ~40 to filter out fragmentary headings/captions.
        max_chars: drop sentences longer than this. Use ~400 to filter out
                   paragraph-blob false positives.

    Returns:
        list of stripped, non-empty sentence strings.
    """
    if not text or not text.strip():
        return []

    seg = _get_segmenter()
    if seg is not None:
        try:
            raw = seg.segment(text)
        except Exception as exc:
            log.warning("pysbd segment failed: %s — falling back to regex", exc)
            raw = re.split(r"(?<=[.!?])\s+", text)
    else:
        raw = re.split(r"(?<=[.!?])\s+", text)

    out: list[str] = []
    for s in raw:
        s = s.strip()
        if not s:
            continue
        if len(s) < min_chars:
            continue
        if max_chars is not None and len(s) > max_chars:
            continue
        out.append(s)
    return out


def segmenter_kind() -> str:
    """For diagnostics — returns 'pysbd' or 'regex' depending on what's loaded."""
    _get_segmenter()
    return _SEGMENTER_KIND or "uninitialized"
