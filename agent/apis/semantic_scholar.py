"""Semantic Scholar Graph API v1 client for Tealc.

Base: https://api.semanticscholar.org/graph/v1
Recommendations: https://api.semanticscholar.org/recommendations/v1

Set SEMANTIC_SCHOLAR_API_KEY env var for higher rate limits (100 rps keyed vs. shared pool).
"""

from __future__ import annotations

import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.semanticscholar.org/graph/v1"
_RECS_BASE = "https://api.semanticscholar.org/recommendations/v1"
_TIMEOUT = 15

_DEFAULT_PAPER_FIELDS = (
    "paperId,title,abstract,year,authors,venue,"
    "openAccessPdf,tldr,citationCount,influentialCitationCount"
)
_CITATION_FIELDS = (
    "contexts,intents,isInfluential,"
    "citingPaper.paperId,citingPaper.title,citingPaper.year,"
    "citingPaper.authors,citingPaper.venue"
)
_REFERENCE_FIELDS = (
    "contexts,intents,isInfluential,"
    "citedPaper.paperId,citedPaper.title,citedPaper.year,"
    "citedPaper.authors,citedPaper.venue"
)
_DEFAULT_AUTHOR_FIELDS = "authorId,name,papers,hIndex,citationCount,affiliations"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Tealc/1.0 (blackmon@tamu.edu)"})
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if key:
        s.headers.update({"x-api-key": key})
    return s


def _get(url: str, params: dict | None = None) -> requests.Response | None:
    """GET with exponential backoff on 429 and single retry on 5xx."""
    session = _session()
    delays = [1, 2, 4]
    for attempt, delay in enumerate(delays + [None]):
        try:
            resp = session.get(url, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Request error: %s", exc)
            return None

        if resp.status_code == 200:
            return resp
        if resp.status_code == 404:
            return resp  # caller handles None
        if resp.status_code == 429:
            if delay is None:
                logger.error("Rate-limited after retries; giving up.")
                return None
            logger.warning("429 rate limit; sleeping %ds (attempt %d)", delay, attempt + 1)
            time.sleep(delay)
            continue
        if resp.status_code >= 500:
            if attempt == 0:
                logger.warning("5xx (%d); retrying once.", resp.status_code)
                time.sleep(1)
                continue
            logger.error("5xx (%d) on retry; giving up.", resp.status_code)
            return None
        logger.error("Unexpected status %d for %s", resp.status_code, url)
        return None
    return None


def _post(url: str, json_body: dict) -> requests.Response | None:
    """POST with same backoff logic."""
    session = _session()
    delays = [1, 2, 4]
    for attempt, delay in enumerate(delays + [None]):
        try:
            resp = session.post(url, json=json_body, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Request error: %s", exc)
            return None

        if resp.status_code in (200, 201):
            return resp
        if resp.status_code == 429:
            if delay is None:
                logger.error("Rate-limited after retries; giving up.")
                return None
            logger.warning("429 rate limit; sleeping %ds (attempt %d)", delay, attempt + 1)
            time.sleep(delay)
            continue
        if resp.status_code >= 500:
            if attempt == 0:
                logger.warning("5xx (%d); retrying once.", resp.status_code)
                time.sleep(1)
                continue
            logger.error("5xx (%d) on retry; giving up.", resp.status_code)
            return None
        logger.error("Unexpected POST status %d for %s", resp.status_code, url)
        return None
    return None


def _paginate(url: str, params: dict, limit: int) -> list[dict]:
    """Walk offset-paginated endpoints, collecting up to `limit` items."""
    results: list[dict] = []
    page_size = min(limit, 100)
    params = {**params, "limit": page_size, "offset": 0}
    while len(results) < limit:
        resp = _get(url, params)
        if resp is None or resp.status_code != 200:
            break
        data = resp.json()
        batch = data.get("data", [])
        results.extend(batch)
        if len(batch) < page_size or not data.get("next"):
            break
        params["offset"] += page_size
    return results[:limit]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(query: str, limit: int = 20, fields: list[str] | None = None) -> list[dict]:
    """Search papers by keyword."""
    field_str = ",".join(fields) if fields else _DEFAULT_PAPER_FIELDS
    url = f"{_BASE}/paper/search"
    return _paginate(url, {"query": query, "fields": field_str}, limit)


def search_papers(
    query: str,
    year_min: int | None = None,
    year_max: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Search /graph/v1/paper/search with optional year filters; paginate up to 1000.

    Each result dict contains:
      paperId, title, abstract, year, authors (list of {name}), externalIds.DOI.

    Pagination uses offset in steps of 100 (S2's per-page cap). Hard ceiling 1000.
    Respects existing _get / _paginate rate-limit handling.
    """
    _HARD_CAP = 1000
    cap = min(limit, _HARD_CAP)

    # Build year-range filter (S2 uses "year" param as "YYYY" or "YYYY-YYYY")
    year_filter: str | None = None
    if year_min is not None and year_max is not None:
        year_filter = f"{year_min}-{year_max}"
    elif year_min is not None:
        year_filter = f"{year_min}-"
    elif year_max is not None:
        year_filter = f"-{year_max}"

    fields = "paperId,title,abstract,year,authors,externalIds"
    params: dict = {"query": query, "fields": fields}
    if year_filter:
        params["year"] = year_filter

    raw = _paginate(f"{_BASE}/paper/search", params, cap)

    # Normalise to a stable schema so callers never see missing keys.
    out: list[dict] = []
    for item in raw:
        ext = item.get("externalIds") or {}
        out.append({
            "paperId":  item.get("paperId", ""),
            "title":    item.get("title", ""),
            "abstract": item.get("abstract", ""),
            "year":     item.get("year"),
            "authors":  [{"name": a.get("name", "")}
                         for a in (item.get("authors") or [])],
            "doi":      ext.get("DOI", ""),
        })
    return out


def get_paper(paper_id: str, fields: list[str] | None = None) -> dict | None:
    """Fetch a single paper by any supported ID format.

    Accepts: S2 ID, DOI (raw or 'DOI:' prefix), 'ARXIV:', 'MAG:', 'ACL:',
    'PMID:', 'PMCID:', 'CorpusId:'.
    """
    field_str = ",".join(fields) if fields else _DEFAULT_PAPER_FIELDS
    url = f"{_BASE}/paper/{paper_id}"
    resp = _get(url, {"fields": field_str})
    if resp is None or resp.status_code == 404:
        return None
    return resp.json()


def get_citing_papers(paper_id: str, limit: int = 20) -> list[dict]:
    """Papers that cite the given paper, with citation-context sentences."""
    url = f"{_BASE}/paper/{paper_id}/citations"
    return _paginate(url, {"fields": _CITATION_FIELDS}, limit)


def get_cited_papers(paper_id: str, limit: int = 20) -> list[dict]:
    """References of the given paper (papers it cites)."""
    url = f"{_BASE}/paper/{paper_id}/references"
    return _paginate(url, {"fields": _REFERENCE_FIELDS}, limit)


def get_recommendations(
    positive_paper_ids: list[str],
    negative_paper_ids: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Get paper recommendations anchored to positive IDs."""
    body: dict = {
        "positivePaperIds": positive_paper_ids,
        "negativePaperIds": negative_paper_ids or [],
    }
    url = f"{_RECS_BASE}/papers"
    resp = _post(url, body)
    if resp is None:
        return []
    data = resp.json()
    papers = data.get("recommendedPapers", [])
    return papers[:limit]


def get_tldr(paper_id: str) -> str | None:
    """Fetch just the TL;DR string for a paper."""
    resp = _get(f"{_BASE}/paper/{paper_id}", {"fields": "tldr"})
    if resp is None or resp.status_code == 404:
        return None
    tldr = resp.json().get("tldr")
    if isinstance(tldr, dict):
        return tldr.get("text")
    return tldr


def get_author(author_id_or_orcid: str, fields: list[str] | None = None) -> dict | None:
    """Fetch an author profile.

    Accepts a Semantic Scholar author ID (numeric string).
    Note: the S2 author endpoint does NOT accept ORCID prefixes directly; to look
    up by ORCID call author_search() or use a known S2 authorId.
    The 'ORCID:' prefix form is kept in the signature for forward-compatibility.
    """
    field_str = ",".join(fields) if fields else _DEFAULT_AUTHOR_FIELDS
    url = f"{_BASE}/author/{author_id_or_orcid}"
    resp = _get(url, {"fields": field_str})
    if resp is None or resp.status_code == 404:
        return None
    return resp.json()


def get_author_papers(author_id: str, limit: int = 100) -> list[dict]:
    """All papers by a given author (requires S2 numeric author ID)."""
    url = f"{_BASE}/author/{author_id}/papers"
    return _paginate(url, {"fields": _DEFAULT_PAPER_FIELDS}, limit)


def author_search(query: str, limit: int = 5, fields: list[str] | None = None) -> list[dict]:
    """Search for authors by name. Useful for resolving a name or ORCID to an S2 authorId.

    Example: author_search('Heath Blackmon') → [{authorId, name, hIndex, ...}, ...]
    """
    field_str = ",".join(fields) if fields else _DEFAULT_AUTHOR_FIELDS
    url = f"{_BASE}/author/search"
    return _paginate(url, {"query": query, "fields": field_str}, limit)
