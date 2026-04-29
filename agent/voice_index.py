"""voice_index.py — TF-IDF retrieval index of Heath Blackmon's published-paper text.

Provides stylistic exemplars for grant/hypothesis drafting without touching any
LLM or embedding model.  Pure retrieval: sklearn TF-IDF (falls back to a
hand-rolled implementation if sklearn is unavailable).

Public API
----------
build_index(refresh=False)  -> dict   # stats; caches to data/voice_index.pkl
retrieve_exemplars(query, k=3) -> list[dict]
voice_system_prompt_addendum(query, k=3) -> str
"""
from __future__ import annotations

import json
import math
import os
import pickle
import re
import time
from collections import Counter
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
_INDEX_PATH = os.path.join(_PROJECT_ROOT, "data", "voice_index.pkl")
_CURATED_PASSAGES_PATH = os.path.join(_PROJECT_ROOT, "data", "voice_passages.json")
_PUBS_JSON = os.path.join(
    os.path.dirname(os.path.dirname(_PROJECT_ROOT)),  # up to Desktop/GitHub
    # hard-coded absolute path so it works regardless of cwd
    "",
)
# Absolute path to the publications JSON on disk (read-only reference)
_PUBLICATIONS_JSON = (
    "/Users/blackmon/Desktop/GitHub/coleoguy.github.io/data/publications.json"
)

_OPENALEX_AUTHOR_ID = "A5054182121"  # Heath Blackmon
_OPENALEX_EMAIL = "blackmon@tamu.edu"

# ---------------------------------------------------------------------------
# TF-IDF: try sklearn; fall back to hand-rolled
# ---------------------------------------------------------------------------
_USE_SKLEARN: bool = False
try:
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
    import numpy as np  # type: ignore

    _USE_SKLEARN = True
except ImportError:
    pass


# ── Hand-rolled TF-IDF (used only when sklearn is absent) ──────────────────

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]{2,}", text.lower())


class _HandRolledTfidf:
    """Minimal TF-IDF implementation using Counter + log IDF."""

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._vocab: list[str] = []
        self._doc_vecs: list[dict[str, float]] = []

    def fit_transform(self, docs: list[str]) -> list[dict[str, float]]:
        N = len(docs)
        df: Counter = Counter()
        tokenized = [_tokenize(d) for d in docs]
        for toks in tokenized:
            for w in set(toks):
                df[w] += 1
        self._idf = {w: math.log((N + 1) / (c + 1)) + 1.0 for w, c in df.items()}
        self._vocab = list(self._idf)
        vecs: list[dict[str, float]] = []
        for toks in tokenized:
            tf = Counter(toks)
            total = max(len(toks), 1)
            vec = {w: (tf[w] / total) * self._idf[w] for w in tf if w in self._idf}
            # L2 normalise
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            vecs.append({w: v / norm for w, v in vec.items()})
        self._doc_vecs = vecs
        return vecs

    def transform_query(self, query: str) -> dict[str, float]:
        toks = _tokenize(query)
        tf = Counter(toks)
        total = max(len(toks), 1)
        vec = {w: (tf[w] / total) * self._idf[w] for w in tf if w in self._idf}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {w: v / norm for w, v in vec.items()}

    def cosine(self, qvec: dict[str, float], dvec: dict[str, float]) -> float:
        return sum(qvec.get(w, 0.0) * v for w, v in dvec.items())


# ---------------------------------------------------------------------------
# OpenAlex helpers (mirrors paper_of_the_day pattern)
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted: dict) -> str:
    """Reconstruct abstract from OpenAlex abstract_inverted_index."""
    if not inverted:
        return ""
    try:
        words: dict[int, str] = {}
        for word, positions in inverted.items():
            for pos in positions:
                words[pos] = word
        return " ".join(words[i] for i in sorted(words))
    except Exception:
        return ""


def _fetch_openalex_works(max_results: int = 60) -> list[dict]:
    """Return up to max_results works for Heath from OpenAlex, newest first."""
    url = "https://api.openalex.org/works"
    params = {
        "filter": f"author.id:{_OPENALEX_AUTHOR_ID}",
        "per_page": min(max_results, 200),
        "sort": "publication_date:desc",
        "mailto": _OPENALEX_EMAIL,
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception:
        return []


def _openalex_to_passages(works: list[dict]) -> list[dict]:
    """Convert OpenAlex work records to passage dicts."""
    passages: list[dict] = []
    for w in works:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index") or {})
        text = (title + ". " + abstract).strip()
        if len(text) < 100:
            continue
        doi = w.get("doi") or ""
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        year = w.get("publication_year") or 0
        passages.append(
            {
                "paper_id": doi or title[:40],
                "title": title,
                "year": year,
                "text": text,
            }
        )
    return passages


# ---------------------------------------------------------------------------
# Curated passages (primary source — Heath's Discussion sections, grant
# narratives, and lab-website prose — populated by data/voice_passages.json).
# When this file is present, the index is built from it; OpenAlex is fallback.
# ---------------------------------------------------------------------------

def _curated_passages() -> list[dict]:
    """Load paragraph-level passages from data/voice_passages.json.

    Each input passage has the shape:
      {"source": "...", "source_kind": "...", "register": "...",
       "purpose": "...", "context": "...", "year": int|null, "text": "..."}

    This function maps them into the passage dict shape used by the TF-IDF
    index (paper_id, title, year, text) and preserves the register/purpose
    as extra fields for downstream formatters.
    """
    passages: list[dict] = []
    if not os.path.exists(_CURATED_PASSAGES_PATH):
        return passages
    try:
        with open(_CURATED_PASSAGES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return passages
    for i, p in enumerate(data.get("passages", [])):
        text = (p.get("text") or "").strip()
        if len(text) < 80:
            continue
        source = (p.get("source") or "unknown").strip() or "unknown"
        passages.append({
            "paper_id": f"curated:{source}#{i}",
            "title": (p.get("context") or source)[:140],
            "year": p.get("year") or 0,
            "text": text,
            "register": p.get("register") or "",
            "purpose": p.get("purpose") or "",
            "source_kind": p.get("source_kind") or "",
        })
    return passages


# ---------------------------------------------------------------------------
# Pubs JSON fallback
# ---------------------------------------------------------------------------

def _pubs_json_passages() -> list[dict]:
    """Load title-only passages from the local publications.json as a fallback."""
    passages: list[dict] = []
    try:
        with open(_PUBLICATIONS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        for w in data.get("works", []):
            title = (w.get("title") or "").strip()
            if not title or len(title) < 50:
                continue
            doi = (w.get("doi") or "").strip()
            year_raw = w.get("year") or "0"
            try:
                year = int(year_raw)
            except (ValueError, TypeError):
                year = 0
            text = title  # only the title; abstract not available here
            passages.append(
                {
                    "paper_id": doi or title[:40],
                    "title": title,
                    "year": year,
                    "text": text,
                }
            )
    except Exception:
        pass
    return passages


# ---------------------------------------------------------------------------
# Index build / load
# ---------------------------------------------------------------------------

def _build_sklearn_index(passages: list[dict]) -> dict:
    """Build index using sklearn TfidfVectorizer."""
    texts = [p["text"] for p in passages]
    vectorizer = TfidfVectorizer(
        analyzer="word",
        token_pattern=r"[a-z]{2,}",
        lowercase=True,
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True,
    )
    matrix = vectorizer.fit_transform(texts)
    return {
        "backend": "sklearn",
        "vectorizer": vectorizer,
        "matrix": matrix,
        "paper_metadata": passages,
    }


def _build_handrolled_index(passages: list[dict]) -> dict:
    """Build index using hand-rolled TF-IDF."""
    texts = [p["text"] for p in passages]
    tfidf = _HandRolledTfidf()
    doc_vecs = tfidf.fit_transform(texts)
    return {
        "backend": "handrolled",
        "tfidf": tfidf,
        "doc_vecs": doc_vecs,
        "paper_metadata": passages,
    }


def build_index(refresh: bool = False) -> dict:
    """Build (or reload) the TF-IDF voice index.

    Caches to ``data/voice_index.pkl``.  Returns stats dict:
    ``{"papers_indexed": N, "total_chars": M, "index_path": "..."}``.
    """
    if not refresh and os.path.exists(_INDEX_PATH):
        # Return stats from cached index without re-building
        try:
            with open(_INDEX_PATH, "rb") as fh:
                cached = pickle.load(fh)
            passages = cached.get("paper_metadata", [])
            total_chars = sum(len(p["text"]) for p in passages)
            return {
                "papers_indexed": len(passages),
                "total_chars": total_chars,
                "index_path": _INDEX_PATH,
                "source": "cache",
            }
        except Exception:
            pass  # corrupt cache → rebuild

    # 1. PRIMARY SOURCE: curated passages file. Heath's Discussion sections,
    # grant narrative paragraphs, and lab-website prose live here. If present,
    # it is the canonical voice corpus — OpenAlex abstracts are fallback only.
    passages = _curated_passages()
    curated_count = len(passages)

    # 2. Supplement from OpenAlex (useful when the curated file is absent or
    # sparse for a topic) — only if curated yielded fewer than 40 passages.
    if curated_count < 40:
        works = _fetch_openalex_works(max_results=60)
        existing_ids = {p["paper_id"] for p in passages}
        for p in _openalex_to_passages(works):
            if p["paper_id"] not in existing_ids:
                passages.append(p)
                existing_ids.add(p["paper_id"])

    # 3. Last-resort fallback from pubs JSON titles — only if the corpus is
    # still sparse. Title-only entries pollute retrieval when real prose is
    # available, because a query matching the title's keywords outranks
    # Discussion-section prose that is semantically closer but lexically
    # further from the query.
    if len(passages) < 40:
        existing_ids = {p["paper_id"] for p in passages}
        for p in _pubs_json_passages():
            if p["paper_id"] not in existing_ids and len(p["text"]) >= 100:
                passages.append(p)
                existing_ids.add(p["paper_id"])

    # 3. Deduplicate by paper_id, keep passages ≥ 100 chars
    seen: set[str] = set()
    clean: list[dict] = []
    for p in passages:
        if p["paper_id"] not in seen and len(p["text"]) >= 100:
            seen.add(p["paper_id"])
            clean.append(p)

    if not clean:
        return {"papers_indexed": 0, "total_chars": 0, "index_path": _INDEX_PATH, "error": "no passages collected"}

    # 4. Build index
    if _USE_SKLEARN:
        index = _build_sklearn_index(clean)
    else:
        index = _build_handrolled_index(clean)

    # 5. Persist
    os.makedirs(os.path.dirname(_INDEX_PATH), exist_ok=True)
    with open(_INDEX_PATH, "wb") as fh:
        pickle.dump(index, fh, protocol=4)

    total_chars = sum(len(p["text"]) for p in clean)
    return {
        "papers_indexed": len(clean),
        "total_chars": total_chars,
        "index_path": _INDEX_PATH,
        "backend": index["backend"],
        "source": "rebuilt",
    }


def _load_index() -> dict | None:
    """Load the pickled index, or return None if missing/corrupt."""
    if not os.path.exists(_INDEX_PATH):
        return None
    try:
        with open(_INDEX_PATH, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _snippet(text: str, max_chars: int = 350) -> str:
    """Extract a 200-400 char snippet from passage text (prefer abstract body)."""
    # Skip the title portion (up to first ". ") for a more abstract-like snippet
    dot_idx = text.find(". ")
    body = text[dot_idx + 2:] if dot_idx != -1 and dot_idx < 120 else text
    if len(body) <= max_chars:
        return body.strip()
    # Truncate at a word boundary
    truncated = body[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        truncated = truncated[:last_space]
    return truncated.strip() + "…"


def retrieve_exemplars(query_text: str, k: int = 3) -> list[dict]:
    """Return top-k passages most similar to *query_text* by TF-IDF cosine.

    Returns ``[]`` if the index is missing or empty.
    """
    index = _load_index()
    if not index:
        # Attempt a silent build first
        try:
            build_index(refresh=False)
            index = _load_index()
        except Exception:
            pass
    if not index:
        return []

    passages: list[dict] = index.get("paper_metadata", [])
    if not passages:
        return []

    backend = index.get("backend", "unknown")

    if backend == "sklearn":
        vectorizer = index["vectorizer"]
        matrix = index["matrix"]
        try:
            import numpy as np
            from sklearn.metrics.pairwise import cosine_similarity  # type: ignore

            qvec = vectorizer.transform([query_text])
            sims = cosine_similarity(qvec, matrix).flatten()
            top_indices = sims.argsort()[::-1][:k]
            results = []
            for idx in top_indices:
                p = passages[idx]
                results.append(
                    {
                        "paper_id": p["paper_id"],
                        "title": p["title"],
                        "year": p["year"],
                        "passage": _snippet(p["text"]),
                        "similarity": float(sims[idx]),
                        "register": p.get("register", ""),
                        "purpose": p.get("purpose", ""),
                        "source_kind": p.get("source_kind", ""),
                    }
                )
            return results
        except Exception:
            return []

    elif backend == "handrolled":
        tfidf: _HandRolledTfidf = index["tfidf"]
        doc_vecs: list[dict[str, float]] = index["doc_vecs"]
        qvec = tfidf.transform_query(query_text)
        scored = [
            (tfidf.cosine(qvec, dv), i) for i, dv in enumerate(doc_vecs)
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, idx in scored[:k]:
            p = passages[idx]
            results.append(
                {
                    "paper_id": p["paper_id"],
                    "title": p["title"],
                    "year": p["year"],
                    "passage": _snippet(p["text"]),
                    "similarity": sim,
                    "register": p.get("register", ""),
                    "purpose": p.get("purpose", ""),
                    "source_kind": p.get("source_kind", ""),
                }
            )
        return results

    return []


# ---------------------------------------------------------------------------
# System-prompt helper
# ---------------------------------------------------------------------------

def voice_system_prompt_addendum(query_text: str, k: int = 3) -> str:
    """Return a formatted system-prompt snippet with stylistic exemplars.

    Returns empty string if no exemplars are found.
    """
    exemplars = retrieve_exemplars(query_text, k=k)
    if not exemplars:
        return ""
    lines = [
        "Recent examples of Heath's writing voice "
        "(for style reference only, do not quote directly):"
    ]
    for i, ex in enumerate(exemplars, 1):
        # Build a compact tag for the exemplar — register + year when present.
        tag_parts = []
        if ex.get("register"):
            tag_parts.append(ex["register"])
        if ex.get("year"):
            tag_parts.append(str(ex["year"]))
        tag = ", ".join(tag_parts) if tag_parts else "source"
        title_short = (ex.get("title") or "")[:80]
        lines.append(f'[{i}] ({tag}) "{ex["passage"]}" — {title_short}')
    lines.append(
        "Match the density, hedging level, and quantitative specificity "
        "of these passages."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manual run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    stats = build_index(refresh=True)
    print("Build stats:", stats)
    exs = retrieve_exemplars("chromosomal stasis in eukaryotic clades", k=3)
    for e in exs:
        print(f"  [{e['similarity']:.4f}] {e['title'][:70]}")
