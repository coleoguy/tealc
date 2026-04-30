"""build_heath_corpus.py — Extract text from Heath's papers, embed sentences.

Produces:
  data/voice_index_st.npz   — numpy array [N, 384] of sentence embeddings
  data/voice_sentences.jsonl — one JSON line per sentence with metadata
  heath_sentences SQLite table

Idempotent: skips papers already recorded in heath_sentences.

Manual run:
    python -m agent.jobs.build_heath_corpus
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any

import numpy as np
import requests

from agent.scheduler import DB_PATH
from agent.jobs import tracked

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
_NPZ_PATH = os.path.join(_DATA_DIR, "voice_index_st.npz")
_JSONL_PATH = os.path.join(_DATA_DIR, "voice_sentences.jsonl")

_PUBLICATIONS_JSON = os.environ.get(
    "PUBLICATIONS_JSON",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/data/publications.json"),
)
_PDF_DIR = os.environ.get(
    "PUBLICATIONS_PDF_DIR",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/pdfs/"),
)

_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
_BATCH_SIZE = 64
_MIN_CHARS = 40
_MAX_CHARS = 400

# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _ensure_tables() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS heath_sentences (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sentence_id  TEXT NOT NULL UNIQUE,
            paper_id     TEXT NOT NULL,
            year         INTEGER,
            section      TEXT,
            sentence     TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hs_paper_id ON heath_sentences(paper_id);
    """)
    conn.commit()
    conn.close()


def _already_processed_papers() -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute("SELECT DISTINCT paper_id FROM heath_sentences").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _insert_sentences(rows: list[dict]) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executemany(
        """INSERT OR IGNORE INTO heath_sentences
           (sentence_id, paper_id, year, section, sentence, created_at)
           VALUES (:sentence_id, :paper_id, :year, :section, :sentence, :created_at)""",
        [{**r, "created_at": now} for r in rows],
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------

def _segment_sentences(text: str) -> list[str]:
    """Split text into sentences via pysbd (with regex fallback)."""
    from agent.text_utils import segment_sentences  # noqa: PLC0415
    return segment_sentences(text, min_chars=_MIN_CHARS, max_chars=_MAX_CHARS)


# ---------------------------------------------------------------------------
# Section tagging
# ---------------------------------------------------------------------------

_SECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\babstract\b", re.I), "abstract"),
    (re.compile(r"\bintroduction\b", re.I), "intro"),
    (re.compile(r"\bmethods?\b|\bmaterials?\b", re.I), "methods"),
    (re.compile(r"\bresults?\b", re.I), "results"),
    (re.compile(r"\bdiscussion\b", re.I), "discussion"),
    (re.compile(r"\bconclusion\b", re.I), "discussion"),
    (re.compile(r"\backnowledg", re.I), "acknowledgments"),
    (re.compile(r"\breferences?\b|\bliterature cited\b", re.I), "references"),
]


def _tag_section(heading: str) -> str:
    for pat, label in _SECTION_PATTERNS:
        if pat.search(heading):
            return label
    return "body"


def _assign_sections(full_text: str, sentences: list[str]) -> list[str]:
    """Return a section label for each sentence using heading heuristics."""
    # Build a list of (char_offset, section) tuples
    heading_re = re.compile(
        r"^(?:(?:[A-Z][A-Z\s]{0,40})\n|(?:\d+\.\s+)?(?:Abstract|Introduction|Methods?|Materials?|Results?|Discussion|Conclusions?|Acknowledgments?|References?)[^\n]{0,60}\n)",
        re.MULTILINE,
    )
    offsets: list[tuple[int, str]] = [(0, "body")]
    for m in heading_re.finditer(full_text):
        section = _tag_section(m.group())
        offsets.append((m.start(), section))

    # For each sentence, find its approximate position in full_text
    labels: list[str] = []
    pos = 0
    for sent in sentences:
        idx = full_text.find(sent[:40], pos)
        if idx == -1:
            idx = pos
        else:
            pos = idx
        # Pick last heading before idx
        sect = "body"
        for off, label in offsets:
            if off <= idx:
                sect = label
            else:
                break
        labels.append(sect)
    return labels


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_path: str) -> str:
    from pdfminer.high_level import extract_text as _extract
    try:
        text = _extract(pdf_path)
        return text or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# OpenAlex abstract fetch
# ---------------------------------------------------------------------------

def _fetch_openalex_abstract(doi: str) -> str:
    url = f"https://api.openalex.org/works/doi:{doi}"
    try:
        resp = requests.get(url, params={"mailto": os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        inv = data.get("abstract_inverted_index") or {}
        if not inv:
            return ""
        words: dict[int, str] = {}
        for word, positions in inv.items():
            for p in positions:
                words[p] = word
        return " ".join(words[i] for i in sorted(words))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# DOI → PDF path
# ---------------------------------------------------------------------------

def _doi_to_pdf_path(doi: str) -> str | None:
    fname = doi.replace("/", "_").replace(".", "_") + ".pdf"
    path = os.path.join(_PDF_DIR, fname)
    return path if os.path.exists(path) else None


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_EMBED_MODEL)
    return _model


def embed_sentences_batch(sentences: list[str]) -> np.ndarray:
    """Embed a list of sentences in batches, return [N, 384] array."""
    model = _get_model()
    if not sentences:
        return np.zeros((0, 384), dtype=np.float32)
    all_vecs: list[np.ndarray] = []
    for i in range(0, len(sentences), _BATCH_SIZE):
        batch = sentences[i : i + _BATCH_SIZE]
        vecs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        all_vecs.append(vecs.astype(np.float32))
    return np.vstack(all_vecs)


# ---------------------------------------------------------------------------
# Persist helpers
# ---------------------------------------------------------------------------

def _load_existing_npz() -> tuple[np.ndarray, list[dict]]:
    """Load existing embeddings + metadata, or return empty defaults."""
    if not os.path.exists(_NPZ_PATH) or not os.path.exists(_JSONL_PATH):
        return np.zeros((0, 384), dtype=np.float32), []
    try:
        data = np.load(_NPZ_PATH)
        embs = data["embeddings"].astype(np.float32)
        meta: list[dict] = []
        with open(_JSONL_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    meta.append(json.loads(line))
        if len(embs) != len(meta):
            return np.zeros((0, 384), dtype=np.float32), []
        return embs, meta
    except Exception:
        return np.zeros((0, 384), dtype=np.float32), []


def _persist(embeddings: np.ndarray, metadata: list[dict]) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    np.savez_compressed(_NPZ_PATH, embeddings=embeddings)
    with open(_JSONL_PATH, "w", encoding="utf-8") as fh:
        for m in metadata:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Core processing per paper
# ---------------------------------------------------------------------------

def _process_paper(
    paper_id: str,
    year: int,
    text: str,
    source: str,
) -> list[dict]:
    """Segment text into sentences and return metadata rows (no embeddings yet)."""
    sentences = _segment_sentences(text)
    if not sentences:
        return []
    sections = _assign_sections(text, sentences)
    rows: list[dict] = []
    for i, (sent, sect) in enumerate(zip(sentences, sections)):
        sid = f"{paper_id}:{i}"
        rows.append({
            "sentence_id": sid,
            "paper_id": paper_id,
            "year": year,
            "section": sect,
            "sentence": sent,
        })
    return rows


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("build_heath_corpus")
def job(limit: int | None = None) -> str:
    """Build sentence corpus from Heath's papers.

    Args:
        limit: if set, process at most this many papers (for smoke tests).
    """
    _ensure_tables()

    # Load publications
    try:
        with open(_PUBLICATIONS_JSON, encoding="utf-8") as fh:
            pubs_data = json.load(fh)
        works = pubs_data.get("works", [])
    except Exception as exc:
        return f"error loading publications.json: {exc}"

    already_done = _already_processed_papers()

    # Load existing npz + jsonl so we can append
    all_embeddings, all_meta = _load_existing_npz()

    existing_sids = {m["sentence_id"] for m in all_meta}

    papers_processed = 0
    total_new_sentences = 0

    for work in works:
        if limit is not None and papers_processed >= limit:
            break

        doi = (work.get("doi") or "").strip()
        if not doi:
            continue

        paper_id = doi
        year_raw = work.get("year") or "0"
        try:
            year = int(year_raw)
        except (ValueError, TypeError):
            year = 0

        if paper_id in already_done:
            continue

        # Try PDF first
        pdf_path = _doi_to_pdf_path(doi)
        if pdf_path:
            text = _extract_pdf_text(pdf_path)
            source = "pdf"
        else:
            text = _fetch_openalex_abstract(doi)
            source = "abstract"
            # Rate-limit OpenAlex
            time.sleep(0.3)

        if not text or len(text.strip()) < 100:
            # Insert a sentinel so we don't re-try on next run
            _insert_sentences([{
                "sentence_id": f"{paper_id}:__empty__",
                "paper_id": paper_id,
                "year": year,
                "section": "none",
                "sentence": "__no_text__",
            }])
            already_done.add(paper_id)
            continue

        rows = _process_paper(paper_id, year, text, source)
        new_rows = [r for r in rows if r["sentence_id"] not in existing_sids]
        if not new_rows:
            already_done.add(paper_id)
            papers_processed += 1
            continue

        # Embed new sentences
        new_sentences = [r["sentence"] for r in new_rows]
        new_vecs = embed_sentences_batch(new_sentences)

        # Accumulate
        if all_embeddings.shape[0] == 0:
            all_embeddings = new_vecs
        else:
            all_embeddings = np.vstack([all_embeddings, new_vecs])

        for r in new_rows:
            m = {k: r[k] for k in ("sentence_id", "paper_id", "year", "section", "sentence")}
            all_meta.append(m)
            existing_sids.add(r["sentence_id"])

        # Insert into SQLite (real sentences only, not sentinel)
        _insert_sentences(new_rows)
        already_done.add(paper_id)

        total_new_sentences += len(new_rows)
        papers_processed += 1

    # Persist updated npz + jsonl
    if total_new_sentences > 0:
        _persist(all_embeddings, all_meta)

    return (
        f"processed {papers_processed} papers, "
        f"{total_new_sentences} new sentences embedded, "
        f"{all_embeddings.shape[0]} total in corpus"
    )


if __name__ == "__main__":
    print(job())
