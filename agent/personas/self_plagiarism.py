"""Self-Plagiarism Sentinel for Tealc.

Compares every sentence in a draft manuscript against the researcher's
published-paper corpus (Tier-2 sentence embeddings) and flags sentences
that are too similar to previously published text.

Deterministic — no LLM calls.  Cost: pure embedding cosine, ~1 s for a
200-sentence draft once the foundation corpus is loaded.

Public API
----------
check_self_plagiarism(draft_text, threshold=0.85, methods_threshold=0.92)
    -> dict
"""
from __future__ import annotations

import re
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Foundation interface — imported lazily to avoid hard-failing at import time
# if the foundation agent hasn't populated the corpus yet.
# ---------------------------------------------------------------------------

def _get_corpus():
    """Return (embeddings, metadata) or None if foundation not ready."""
    try:
        from agent.voice_index import get_corpus_embeddings  # type: ignore
        return get_corpus_embeddings()
    except (ImportError, AttributeError):
        return None


def _foundation_ready() -> bool:
    try:
        from agent.voice_index import is_foundation_ready  # type: ignore
        return bool(is_foundation_ready())
    except (ImportError, AttributeError):
        return False


def _embed(sentences: list[str]) -> Optional[np.ndarray]:
    """Embed a list of sentences; return None on failure."""
    if not sentences:
        return None
    try:
        from agent.voice_index import embed_sentences  # type: ignore
        result = embed_sentences(sentences)
        return np.asarray(result, dtype=np.float32)
    except (ImportError, AttributeError, Exception):
        return None


# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Split text into sentences via pysbd (with regex fallback).

    Filters to sentences between 40 and 400 chars (inclusive).
    """
    from agent.text_utils import segment_sentences  # noqa: PLC0415
    return segment_sentences(text, min_chars=40, max_chars=400)


# ---------------------------------------------------------------------------
# Section heuristic
# ---------------------------------------------------------------------------

# Patterns that indicate named sections in markdown or plain-text manuscripts.
_HEADER_RE = re.compile(
    r'^(?:#{1,4}\s+|(?=[A-Z][A-Za-z ]{2,30}:$))'  # ## Title or "Title:"
    r'(?P<name>.+)',
    re.MULTILINE,
)

# Canonical section buckets — map raw header text to a clean label.
_SECTION_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bintro(?:duction)?\b', re.I), 'intro'),
    (re.compile(r'\bmethod(?:s|ology)?\b', re.I), 'methods'),
    (re.compile(r'\bresult(?:s)?\b', re.I), 'results'),
    (re.compile(r'\bdiscussion\b', re.I), 'discussion'),
    (re.compile(r'\bconclusion(?:s)?\b', re.I), 'conclusion'),
    (re.compile(r'\babstract\b', re.I), 'abstract'),
    (re.compile(r'\breference(?:s)?\b', re.I), 'references'),
    (re.compile(r'\backnowledg', re.I), 'acknowledgements'),
]


def _canonical_section(raw_name: str) -> str:
    for pattern, label in _SECTION_MAP:
        if pattern.search(raw_name):
            return label
    return raw_name.strip().lower()[:40]


def _tag_sentences_with_sections(text: str, sentences: list[str]) -> list[Optional[str]]:
    """Assign each sentence the section label of the most recent header seen."""
    # Locate every header and its char offset in text.
    headers: list[tuple[int, str]] = []
    for m in _HEADER_RE.finditer(text):
        headers.append((m.start(), _canonical_section(m.group('name'))))

    # For each sentence, find its approximate offset (first occurrence).
    tags: list[Optional[str]] = []
    search_start = 0
    for sent in sentences:
        idx = text.find(sent, search_start)
        if idx == -1:
            idx = text.find(sent)  # fallback: search from beginning
        # Advance search_start to avoid matching the same sentence twice.
        if idx != -1:
            search_start = idx + len(sent)

        # Find the last header that appears before this sentence's position.
        section = 'intro'  # default when no header precedes
        if idx != -1:
            for hdr_pos, hdr_label in headers:
                if hdr_pos < idx:
                    section = hdr_label
                else:
                    break
        tags.append(section)

    return tags


# ---------------------------------------------------------------------------
# Cosine similarity (pure numpy, no sklearn)
# ---------------------------------------------------------------------------

def _cosine_similarity_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Return [N, M] cosine similarity matrix between row-vectors in a and b.

    a: [N, D], b: [M, D]  — float32 assumed.
    """
    # L2-normalise rows
    a_norm = np.linalg.norm(a, axis=1, keepdims=True)
    b_norm = np.linalg.norm(b, axis=1, keepdims=True)
    # Avoid division by zero
    a_norm = np.where(a_norm == 0, 1.0, a_norm)
    b_norm = np.where(b_norm == 0, 1.0, b_norm)
    a_hat = a / a_norm
    b_hat = b / b_norm
    return a_hat @ b_hat.T  # [N, M]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_self_plagiarism(
    draft_text: str,
    threshold: float = 0.85,
    methods_threshold: float = 0.92,
) -> dict:
    """Flag sentences in *draft_text* too similar to Heath's corpus.

    Parameters
    ----------
    draft_text : str
        Full manuscript or section text.
    threshold : float
        Cosine similarity above which non-methods sentences are flagged.
    methods_threshold : float
        Higher threshold for sentences in a methods section (boilerplate
        like "DNA was extracted using…" would otherwise dominate flags).

    Returns
    -------
    dict with keys:
        flags            : list[dict]
        n_draft_sentences: int
        n_flagged        : int
        foundation_ready : bool
        summary          : str
    """
    # ---- Handle empty input fast ----------------------------------------
    if not draft_text or not draft_text.strip():
        return {
            'flags': [],
            'n_draft_sentences': 0,
            'n_flagged': 0,
            'foundation_ready': _foundation_ready(),
            'summary': 'Draft is empty; nothing to check.',
        }

    # ---- Check foundation status ----------------------------------------
    corpus_result = _get_corpus()
    if corpus_result is None:
        return {
            'flags': [],
            'n_draft_sentences': 0,
            'n_flagged': 0,
            'foundation_ready': False,
            'summary': 'Foundation not populated yet.',
        }

    corpus_embeddings, corpus_meta = corpus_result  # ndarray [C, D], list[dict]
    corpus_embeddings = np.asarray(corpus_embeddings, dtype=np.float32)

    # ---- Sentence segmentation ------------------------------------------
    draft_sentences = _split_sentences(draft_text)
    n_draft = len(draft_sentences)

    if n_draft == 0:
        return {
            'flags': [],
            'n_draft_sentences': 0,
            'n_flagged': 0,
            'foundation_ready': True,
            'summary': 'No valid sentences found in draft (all too short/long).',
        }

    # ---- Section tagging ------------------------------------------------
    section_tags = _tag_sentences_with_sections(draft_text, draft_sentences)

    # ---- Embed draft sentences ------------------------------------------
    draft_embeddings = _embed(draft_sentences)
    if draft_embeddings is None or draft_embeddings.shape[0] == 0:
        return {
            'flags': [],
            'n_draft_sentences': n_draft,
            'n_flagged': 0,
            'foundation_ready': True,
            'summary': 'Embedding failed; cannot perform similarity check.',
        }

    # ---- Pairwise cosine similarity [N_draft, N_corpus] -----------------
    sim_matrix = _cosine_similarity_matrix(draft_embeddings, corpus_embeddings)
    # shape: [N_draft, N_corpus]

    # ---- Collect flags --------------------------------------------------
    flags: list[dict] = []

    for i, sent in enumerate(draft_sentences):
        section = section_tags[i]
        # Choose threshold based on section
        thr = methods_threshold if section == 'methods' else threshold

        # Top match in corpus for this draft sentence
        best_corpus_idx = int(np.argmax(sim_matrix[i]))
        best_sim = float(sim_matrix[i, best_corpus_idx])

        if best_sim < thr:
            continue  # below threshold — clean

        meta = corpus_meta[best_corpus_idx]
        kind = 'near_duplicate' if best_sim >= 0.95 else 'high_similarity'

        flags.append({
            'draft_sentence': sent,
            'draft_section': section,
            'matched_paper_id': meta.get('paper_id', ''),
            'matched_year': int(meta.get('year', 0)),
            'matched_sentence': meta.get('sentence', ''),
            'similarity': round(best_sim, 4),
            'kind': kind,
        })

    # ---- Summary --------------------------------------------------------
    n_flagged = len(flags)
    if n_flagged == 0:
        summary = (
            f'No self-plagiarism detected across {n_draft} draft sentences '
            f'(threshold {threshold}/{methods_threshold}).'
        )
    else:
        near_dup = sum(1 for f in flags if f['kind'] == 'near_duplicate')
        summary = (
            f'{n_flagged} of {n_draft} draft sentences flagged '
            f'({near_dup} near-duplicate, {n_flagged - near_dup} high-similarity). '
            f'Review before submission.'
        )

    return {
        'flags': flags,
        'n_draft_sentences': n_draft,
        'n_flagged': n_flagged,
        'foundation_ready': True,
        'summary': summary,
    }
