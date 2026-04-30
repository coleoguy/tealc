"""CrossRef public API client for Tealc.

Resolves free-text bibliographic citation strings to DOIs using the CrossRef
works endpoint.  No API key required, but setting CROSSREF_EMAIL unlocks the
polite pool for substantially higher rate-limits and more reliable service.

Rate limits:
    - Anonymous pool:  ~3 req/sec (shared, throttled)
    - Polite pool:     ~50 req/sec (unlocked by mailto header)

Usage:
    from agent.apis.crossref import resolve_citation_to_doi, get_work_metadata, batch_resolve
"""
from __future__ import annotations

import concurrent.futures as _futures
import logging
import os
import threading
import time
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.crossref.org"
_TIMEOUT = 15

# -------------------------------------------------------------------
# Polite-pool rate-limiting: 50 req/sec max via a token bucket
# -------------------------------------------------------------------
_RATE_LIMIT = 50          # requests per second
_BUCKET_LOCK = threading.Lock()
_TOKENS: float = _RATE_LIMIT
_LAST_REFILL: float = time.monotonic()


def _acquire_token() -> None:
    """Block until a rate-limit token is available (token bucket, 50 rps)."""
    global _TOKENS, _LAST_REFILL
    while True:
        with _BUCKET_LOCK:
            now = time.monotonic()
            elapsed = now - _LAST_REFILL
            _TOKENS = min(_RATE_LIMIT, _TOKENS + elapsed * _RATE_LIMIT)
            _LAST_REFILL = now
            if _TOKENS >= 1.0:
                _TOKENS -= 1.0
                return
        time.sleep(0.02)


def _session() -> requests.Session:
    s = requests.Session()
    email = os.environ.get("CROSSREF_EMAIL", "")
    if email:
        ua = f"Tealc/1.0 (mailto:{email})"
    else:
        ua = "Tealc/1.0"
    s.headers.update({"User-Agent": ua})
    return s


def _get(url: str, params: dict | None = None) -> requests.Response | None:
    """GET with rate-limit token, backoff on 429, single retry on 5xx."""
    _acquire_token()
    session = _session()
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("CrossRef request error: %s", exc)
            return None

        if resp.status_code == 200:
            return resp
        if resp.status_code == 404:
            return resp
        if resp.status_code == 429:
            delay = 2 ** attempt
            logger.warning("CrossRef 429; sleeping %ds (attempt %d)", delay, attempt + 1)
            time.sleep(delay)
            continue
        if resp.status_code >= 500 and attempt == 0:
            logger.warning("CrossRef 5xx (%d); retrying once", resp.status_code)
            time.sleep(1)
            continue
        logger.error("CrossRef unexpected status %d for %s", resp.status_code, url)
        return None
    return None


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------

def resolve_citation_to_doi(citation: str, min_score: float = 40.0) -> dict:
    """Given a free-text citation string, search CrossRef and return the
    best-match DOI + match score.

    Returns: {doi: str|None, title: str, score: float, raw_match: dict}
    `score` is CrossRef's relevance score; we accept the top hit if score
    >= min_score, else return doi=None.
    Politeness: rate-limit to 50 req/sec (CrossRef public API limit).
    Set `mailto` header with CROSSREF_EMAIL env var (CrossRef's etiquette;
    grants better service).

    Note: min_score=40.0 (not 50.0) is the right floor for the anonymous pool.
    The polite pool (CROSSREF_EMAIL set) returns higher scores; 50.0 is safe there.
    """
    empty: dict = {"doi": None, "title": "", "score": 0.0, "raw_match": {}}
    if not citation or not citation.strip():
        return empty

    params = {
        "query.bibliographic": citation,
        "rows": 3,
        "select": "DOI,title,score",
    }
    resp = _get(f"{_BASE}/works", params=params)
    if resp is None or resp.status_code != 200:
        return empty

    try:
        data = resp.json()
        items = data.get("message", {}).get("items", [])
    except Exception as exc:
        logger.warning("CrossRef JSON parse error: %s", exc)
        return empty

    if not items:
        return empty

    top = items[0]
    score: float = float(top.get("score", 0.0))
    doi: str | None = top.get("DOI")
    raw_title = top.get("title", [])
    title = raw_title[0] if isinstance(raw_title, list) and raw_title else str(raw_title)

    if score < min_score:
        doi = None

    return {"doi": doi, "title": title, "score": score, "raw_match": top}


def get_work_metadata(doi: str) -> dict:
    """Fetch full CrossRef metadata for a known DOI.

    Returns: {doi, title, authors, year, journal, abstract, type, references_count}.
    Returns {} if not found.
    """
    if not doi or not doi.strip():
        return {}

    # CrossRef encodes DOIs in the path; special chars must be percent-encoded
    encoded = quote(doi.strip(), safe="")
    resp = _get(f"{_BASE}/works/{encoded}")
    if resp is None:
        return {}
    if resp.status_code == 404:
        logger.info("CrossRef: DOI not found: %s", doi)
        return {}
    if resp.status_code != 200:
        return {}

    try:
        msg = resp.json().get("message", {})
    except Exception as exc:
        logger.warning("CrossRef metadata JSON parse error: %s", exc)
        return {}

    raw_title = msg.get("title", [])
    title = raw_title[0] if isinstance(raw_title, list) and raw_title else ""

    raw_authors = msg.get("author", [])
    authors: list[str] = []
    for a in raw_authors:
        given = a.get("given", "")
        family = a.get("family", "")
        name = f"{given} {family}".strip()
        if name:
            authors.append(name)

    # Year: prefer published-print, fall back to published-online or issued
    year: int | None = None
    for date_field in ("published-print", "published-online", "issued"):
        date_parts = msg.get(date_field, {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            try:
                year = int(date_parts[0][0])
                break
            except (TypeError, ValueError):
                pass

    container = msg.get("container-title", [])
    journal = container[0] if isinstance(container, list) and container else ""

    return {
        "doi": msg.get("DOI", doi),
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "abstract": msg.get("abstract", ""),
        "type": msg.get("type", ""),
        "references_count": msg.get("references-count", 0),
    }


def batch_resolve(citations: list[str], max_workers: int = 8) -> list[dict]:
    """Batch version of resolve_citation_to_doi via ThreadPoolExecutor.

    Same shape as resolve_citation_to_doi per item; politeness pool capped.
    Returns list in the same order as input.
    """
    if not citations:
        return []

    n_workers = min(max(1, max_workers), len(citations), 8)
    results: list[dict] = [{}] * len(citations)

    with _futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        future_to_idx = {
            pool.submit(resolve_citation_to_doi, cit): i
            for i, cit in enumerate(citations)
        }
        for fut in _futures.as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:
                logger.warning("batch_resolve item %d failed: %s", i, exc)
                results[i] = {"doi": None, "title": "", "score": 0.0, "raw_match": {}}

    return results


def is_configured() -> bool:
    """No API key required, but CROSSREF_EMAIL header is recommended.

    Returns True (this client always works; just slower if no email).
    """
    email = os.environ.get("CROSSREF_EMAIL", "")
    if not email:
        logger.info(
            "CROSSREF_EMAIL not set; using anonymous pool (slower, lower rate-limit). "
            "Set CROSSREF_EMAIL in .env for the polite pool."
        )
    return True
