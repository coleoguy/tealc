"""Europe PMC full-text client for Tealc.

Exports: search_full_text, fetch_full_text_xml, extract_sections,
         fetch_and_extract, bulk_fetch, cache_full_text
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

_BASE_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_BASE_FT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
_RESEARCHER_EMAIL = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
_HEADERS = {"User-Agent": f"Tealc/1.0 ({_RESEARCHER_EMAIL})"}
_EMAIL = _RESEARCHER_EMAIL
_RATE_SLEEP = 0.1
_TIMEOUT = 2
_RETRY_DELAYS = [2, 4]

_SECTION_KEYWORDS: dict[str, list[str]] = {
    "introduction": ["introduction", "background", "overview"],
    "methods": ["method", "material", "experimental", "procedure", "protocol",
                "approach", "statistical", "data collection", "study design"],
    "results": ["result", "finding", "observation"],
    "discussion": ["discussion", "interpretation"],
    "conclusions": ["conclusion", "summary", "implication"],
}


# --- Internal helpers ---

def _get_with_retry(url: str, params: Optional[dict] = None,
                    timeout: int = _TIMEOUT) -> Optional[requests.Response]:
    """GET with one retry on transient 429/5xx."""
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < len(_RETRY_DELAYS):
                    continue
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            if attempt >= len(_RETRY_DELAYS):
                return None
        except requests.exceptions.RequestException:
            return None
    return None


def _decode_inverted_index(inv: dict) -> str:
    """Reconstruct abstract from abstractInvertedIndex."""
    if not inv:
        return ""
    max_pos = max(pos for positions in inv.values() for pos in positions)
    tokens: list[str] = [""] * (max_pos + 1)
    for token, positions in inv.items():
        for pos in positions:
            tokens[pos] = token
    return " ".join(t for t in tokens if t)


def _text(elem: ET.Element) -> str:
    """Recursively collect all text under an XML element."""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text.strip())
    for child in elem:
        parts.append(_text(child))
        if child.tail:
            parts.append(child.tail.strip())
    return " ".join(p for p in parts if p)


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _classify_title(title: str) -> Optional[str]:
    low = title.lower()
    for section, keywords in _SECTION_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            return section
    return None


def _sec_body(sec: ET.Element) -> str:
    """Text of a <sec> element, skipping its <title> child."""
    parts = [_text(child) for child in sec if _local(child.tag) != "title"]
    return " ".join(p for p in parts if p)


# --- Public API ---

def search_full_text(query: str, since_iso: Optional[str] = None,
                     limit: int = 20) -> list[dict]:
    """Search Europe PMC for open-access papers with full-text XML available.

    Returns [{pmcid, pmid, doi, title, authors, journal, pub_year,
              has_full_text, is_open_access, abstract}, ...]
    Only returns results where is_open_access=True AND has_full_text=True.
    since_iso: optional YYYY-MM-DD filter.
    """
    if since_iso:
        query = f"({query}) AND FIRST_PDATE:[{since_iso} TO *]"
    params: dict = {
        "query": query, "resultType": "core", "format": "json",
        "pageSize": min(limit * 3, 100), "email": _EMAIL,
    }
    resp = _get_with_retry(_BASE_SEARCH, params=params)
    if resp is None:
        return []

    out: list[dict] = []
    for r in resp.json().get("resultList", {}).get("result", []):
        is_oa = str(r.get("isOpenAccess", "")).upper() == "Y"
        # inEPMC=Y signals full-text XML is deposited; hasFullText absent in core results
        has_ft = (str(r.get("inEPMC", "")).upper() == "Y"
                  or str(r.get("hasFullText", "")).upper() == "Y")
        if not (is_oa and has_ft and r.get("pmcid")):
            continue

        abstract = r.get("abstractText", "")
        if not abstract:
            try:
                abstract = _decode_inverted_index(r.get("abstractInvertedIndex") or {})
            except Exception:
                abstract = ""

        author_list = r.get("authorList", {}).get("author", [])
        authors = ", ".join(
            a.get("fullName", a.get("lastName", ""))
            for a in (author_list if isinstance(author_list, list) else [])
        )
        out.append({
            "pmcid": r.get("pmcid", ""), "pmid": r.get("pmid", ""),
            "doi": r.get("doi", ""), "title": r.get("title", ""),
            "authors": authors, "journal": r.get("journalTitle", ""),
            "pub_year": r.get("pubYear", ""), "has_full_text": has_ft,
            "is_open_access": is_oa, "abstract": abstract,
        })
        if len(out) >= limit:
            break

    time.sleep(_RATE_SLEEP)
    return out


def fetch_full_text_xml(pmcid: str) -> Optional[str]:
    """Fetch raw JATS XML for a PMCID (e.g. 'PMC1790863').

    Returns None on 404/unavailable. 2-second timeout, one retry on 5xx.
    """
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    resp = _get_with_retry(_BASE_FT.format(pmcid=pmcid),
                           params={"email": _EMAIL}, timeout=_TIMEOUT)
    time.sleep(_RATE_SLEEP)
    return resp.text if resp is not None else None


def extract_sections(xml: str) -> dict:
    """Parse JATS XML into named sections.

    Returns {abstract, introduction, methods, results, discussion,
             conclusions, references, full_text}.
    Sections matched via sec-type attribute first, then <title> text keywords.
    Multiple occurrences of the same section are concatenated.
    Missing sections map to empty string. full_text is all visible text
    with == Section == headers.
    """
    sections: dict[str, str] = {k: "" for k in
        ("abstract", "introduction", "methods", "results",
         "discussion", "conclusions", "references", "full_text")}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return sections

    for elem in root.iter():
        if _local(elem.tag) == "abstract":
            sections["abstract"] = _text(elem)
            break

    full_parts: list[str] = []
    if sections["abstract"]:
        full_parts.append("== Abstract ==\n" + sections["abstract"])

    for elem in root.iter():
        if _local(elem.tag) != "sec":
            continue
        sec_type = elem.get("sec-type", "").lower()
        classified: Optional[str] = next(
            (k for k, kws in _SECTION_KEYWORDS.items() if any(kw in sec_type for kw in kws)),
            None)
        title_text = ""
        if classified is None:
            title_elem = next((c for c in elem if _local(c.tag) == "title"), None)
            if title_elem is not None:
                title_text = _text(title_elem)
                classified = _classify_title(title_text)
        else:
            title_elem = next((c for c in elem if _local(c.tag) == "title"), None)
            title_text = _text(title_elem) if title_elem is not None else classified.title()

        if classified and classified in sections:
            if title_text:
                full_parts.append(f"== {title_text} ==")
            body = _sec_body(elem)
            if body:
                sections[classified] = (sections[classified] + " " + body).strip()
                full_parts.append(body)

    for elem in root.iter():
        if _local(elem.tag) in ("ref-list", "back"):
            ref = _text(elem)
            if ref:
                sections["references"] = ref
                full_parts.append("== References ==\n" + ref)
            break

    sections["full_text"] = "\n\n".join(full_parts)
    return sections


def fetch_and_extract(pmcid: str) -> Optional[dict]:
    """Fetch full-text XML and extract sections. Returns None on fetch failure.

    Result dict includes 'pmcid' plus all section keys from extract_sections.
    """
    xml = fetch_full_text_xml(pmcid)
    if xml is None:
        return None
    result = extract_sections(xml)
    result["pmcid"] = pmcid.upper() if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    return result


def cache_full_text(pmcid: str, dest_dir: str | Path) -> dict:
    """Fetch JATS XML for *pmcid*, store raw XML and extracted-section JSON under *dest_dir*.

    Files written:
      {dest_dir}/{pmcid}.xml  — raw JATS XML
      {dest_dir}/{pmcid}.json — {pmcid, title, sections, fetched_at}

    Idempotent: if both files already exist **and** the stored XML hash matches the
    freshly-fetched bytes, the cached JSON is returned immediately without a network
    call (the hash comparison skips the re-fetch entirely when possible).

    Returns the JSON dict on success; raises RuntimeError on network/parse failure.
    """
    import hashlib
    import json
    from datetime import datetime, timezone

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    pid = pmcid.strip().upper()
    if not pid.startswith("PMC"):
        pid = f"PMC{pid}"

    xml_path  = dest / f"{pid}.xml"
    json_path = dest / f"{pid}.json"

    # --- cache hit: both files exist; check XML hash ---
    if xml_path.exists() and json_path.exists():
        cached_xml = xml_path.read_bytes()
        cached_hash = hashlib.sha256(cached_xml).hexdigest()
        try:
            cached_json = json.loads(json_path.read_text())
            if cached_json.get("xml_sha256") == cached_hash:
                return cached_json
        except (json.JSONDecodeError, KeyError):
            pass  # fall through to re-fetch

    # --- fetch ---
    xml_text = fetch_full_text_xml(pid)
    if xml_text is None:
        raise RuntimeError(f"cache_full_text: full-text XML unavailable for {pid}")

    xml_bytes = xml_text.encode("utf-8")
    xml_hash  = hashlib.sha256(xml_bytes).hexdigest()
    xml_path.write_bytes(xml_bytes)

    # --- parse sections ---
    sections_full = extract_sections(xml_text)
    kept = {k: sections_full.get(k, "") for k in
            ("introduction", "methods", "results", "discussion")}

    # --- extract title from XML ---
    import xml.etree.ElementTree as ET
    title = ""
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            if _local(elem.tag) == "article-title":
                title = _text(elem)
                break
    except ET.ParseError:
        pass

    out = {
        "pmcid":      pid,
        "title":      title,
        "sections":   kept,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "xml_sha256": xml_hash,
    }
    json_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def bulk_fetch(pmcids: list[str], concurrent: int = 4) -> dict[str, dict]:
    """Fetch + extract multiple PMCIDs in parallel (max 4 workers).

    Returns {pmcid: extracted_dict}. Failures silently skipped.
    """
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(concurrent, 4)) as executor:
        futures = {executor.submit(fetch_and_extract, p): p for p in pmcids}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    out[futures[future]] = result
            except Exception:
                pass
    return out
