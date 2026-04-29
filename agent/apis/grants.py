"""Grants intelligence client for Tealc — NIH RePORTER v2 + NSF Award Search.

NIH RePORTER: https://api.reporter.nih.gov/v2/projects/search (POST, no auth)
NSF Awards:   https://api.nsf.gov/services/v1/awards.json    (GET,  no auth)
"""

from __future__ import annotations

import time
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Tealc/1.0 (blackmon@tamu.edu)"})
_TIMEOUT = 20

_NIH_SEARCH   = "https://api.reporter.nih.gov/v2/projects/search"
_NIH_PROJECT  = "https://api.reporter.nih.gov/v2/projects/{appl_id}"
_NIH_ABSTRACT = "https://api.reporter.nih.gov/v2/projects/abstracts/{appl_id}"

_NIH_FIELDS = [
    "ApplId", "ProjectNum", "ProjectTitle", "AbstractText",
    "PiNames", "FiscalYear", "Organization", "ActivityCode",
    "AwardAmount", "ProjectStartDate", "ProjectEndDate",
    "OpportunityNumber", "AgencyCode", "SubprojectId",
]

def _nih_request(method: str, url: str, **kwargs: Any) -> Any:
    """Single request to NIH with one retry on 5xx."""
    for attempt in range(2):
        try:
            resp = _SESSION.request(method, url, timeout=_TIMEOUT, **kwargs)
            if resp.status_code >= 500 and attempt == 0:
                time.sleep(1.0)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.warning("NIH request failed (%s): %s", exc.response.status_code, url)
            if attempt == 1:
                return None
        except requests.RequestException as exc:
            logger.warning("NIH request error: %s", exc)
            if attempt == 1:
                return None
    return None


def _nsf_request(url: str, params: dict) -> Any:
    """Single request to NSF with one retry on 5xx."""
    for attempt in range(2):
        try:
            resp = _SESSION.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code >= 500 and attempt == 0:
                time.sleep(0.5)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.warning("NSF request failed (%s): %s", exc.response.status_code, url)
            if attempt == 1:
                return None
        except requests.RequestException as exc:
            logger.warning("NSF request error: %s", exc)
            if attempt == 1:
                return None
    return None


def _flatten_nih(hit: dict) -> dict:
    """Normalise a raw NIH project hit into the standard output shape."""
    pi_names_raw = hit.get("principal_investigators") or hit.get("PiNames") or []
    if isinstance(pi_names_raw, list):
        pi_names = [
            " ".join(filter(None, [p.get("first_name", ""), p.get("last_name", "")]))
            if isinstance(p, dict)
            else str(p)
            for p in pi_names_raw
        ]
    else:
        pi_names = [str(pi_names_raw)]

    org_raw = hit.get("organization") or {}
    org = org_raw.get("org_name", "") if isinstance(org_raw, dict) else str(org_raw)

    return {
        "appl_id":            hit.get("appl_id") or hit.get("ApplId"),
        "project_num":        hit.get("project_num") or hit.get("ProjectNum"),
        "project_title":      hit.get("project_title") or hit.get("ProjectTitle", ""),
        "abstract_text":      hit.get("abstract_text") or hit.get("AbstractText") or "",
        "pi_names":           pi_names,
        "fiscal_year":        hit.get("fiscal_year") or hit.get("FiscalYear"),
        "organization":       org,
        "activity_code":      hit.get("activity_code") or hit.get("ActivityCode", ""),
        "award_amount":       hit.get("award_amount") or hit.get("AwardAmount"),
        "project_start_date": hit.get("project_start_date") or hit.get("ProjectStartDate"),
        "project_end_date":   hit.get("project_end_date") or hit.get("ProjectEndDate"),
    }

def nih_search_awards(
    query: str | None = None,
    pi_names: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    activity_codes: list[str] | None = None,
    organization_codes: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search NIH RePORTER.

    'query' searches abstracts + project titles.
    'activity_codes' e.g. ['R35', 'R01'].

    Returns a list of dicts with keys:
        appl_id, project_num, project_title, abstract_text, pi_names,
        fiscal_year, organization, activity_code, award_amount,
        project_start_date, project_end_date.
    """
    criteria: dict[str, Any] = {}

    if query:
        criteria["advanced_text_search"] = {
            "operator": "and",
            "search_field": "projecttitle,abstract",
            "search_text": query,
        }
    if pi_names:
        criteria["pi_names"] = [{"last_name": n.split()[-1], "first_name": n.split()[0] if " " in n else ""} for n in pi_names]
    if fiscal_years:
        criteria["fiscal_years"] = fiscal_years
    if activity_codes:
        criteria["activity_codes"] = activity_codes
    if organization_codes:
        criteria["org_names"] = organization_codes

    body: dict[str, Any] = {
        "criteria": criteria,
        "include_fields": _NIH_FIELDS,
        "limit": min(limit, 500),
        "offset": 0,
    }

    data = _nih_request("POST", _NIH_SEARCH, json=body)
    if not data:
        return []

    hits = data.get("results") or []
    time.sleep(1.0)
    return [_flatten_nih(h) for h in hits]


def nih_get_award(appl_id: int | str) -> dict | None:
    """Fetch one NIH award by appl_id.

    Returns the full project record merged with the abstract text.
    """
    # Project details
    data = _nih_request("GET", _NIH_PROJECT.format(appl_id=appl_id))
    time.sleep(1.0)

    if not data:
        return None

    # The endpoint may return a list or a single object
    if isinstance(data, list):
        record = data[0] if data else {}
    else:
        record = data.get("results", [{}])[0] if "results" in data else data

    result = _flatten_nih(record)

    # Fetch abstract separately if not already populated
    if not result.get("abstract_text"):
        abs_data = _nih_request("GET", _NIH_ABSTRACT.format(appl_id=appl_id))
        time.sleep(1.0)
        if abs_data:
            if isinstance(abs_data, list) and abs_data:
                result["abstract_text"] = abs_data[0].get("abstract_text", "")
            elif isinstance(abs_data, dict):
                items = abs_data.get("results") or []
                if items:
                    result["abstract_text"] = items[0].get("abstract_text", "")

    return result


def nih_get_abstracts(appl_ids: list[int | str]) -> dict[str, str]:
    """Fetch full abstract text for each appl_id.

    Returns {str(appl_id): abstract_text}.
    """
    out: dict[str, str] = {}
    for aid in appl_ids:
        data = _nih_request("GET", _NIH_ABSTRACT.format(appl_id=aid))
        time.sleep(1.0)
        if not data:
            out[str(aid)] = ""
            continue
        if isinstance(data, list):
            items = data
        else:
            items = data.get("results") or []
        text = items[0].get("abstract_text", "") if items else ""
        out[str(aid)] = text
    return out


def nih_search_by_topic_and_mechanism(
    topic: str,
    activity_code: str = "R35",
    fiscal_years: list[int] | None = None,
    limit: int = 30,
) -> list[dict]:
    """Convenience wrapper for the MIRA-renewal reference use-case.

    Returns recent awards matching *topic* under the given activity code,
    always including abstract_text (fetched individually when missing).
    """
    if fiscal_years is None:
        fiscal_years = [2022, 2023, 2024, 2025]

    awards = nih_search_awards(
        query=topic,
        activity_codes=[activity_code],
        fiscal_years=fiscal_years,
        limit=limit,
    )

    # Back-fill missing abstracts
    for award in awards:
        if not award.get("abstract_text") and award.get("appl_id"):
            abs_data = _nih_request(
                "GET", _NIH_ABSTRACT.format(appl_id=award["appl_id"])
            )
            time.sleep(1.0)
            if abs_data:
                if isinstance(abs_data, list) and abs_data:
                    award["abstract_text"] = abs_data[0].get("abstract_text", "")
                elif isinstance(abs_data, dict):
                    items = abs_data.get("results") or []
                    if items:
                        award["abstract_text"] = items[0].get("abstract_text", "")

    return awards

_NSF_BASE = "https://api.nsf.gov/services/v1/awards.json"
_NSF_FIELDS = "id,title,piFirstName,piLastName,awardee,amount,startDate,endDate,programElement,abstractText"


def _flatten_nsf(item: dict) -> dict:
    """Normalise a raw NSF award item into the standard output shape."""
    return {
        "award_id":        item.get("id", ""),
        "title":           item.get("title", ""),
        "pi_first_name":   item.get("piFirstName", ""),
        "pi_last_name":    item.get("piLastName", ""),
        "institution":     item.get("awardee", ""),
        "funding_amount":  item.get("amount", ""),
        "start_date":      item.get("startDate", ""),
        "end_date":        item.get("endDate", ""),
        "program_element": item.get("programElement", ""),
        "abstract_text":   item.get("abstractText", ""),
    }


def nsf_search_awards(
    query: str | None = None,
    program_element: str | None = None,
    pi_name: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search NSF awards.

    Returns a list of dicts with keys:
        award_id, title, pi_first_name, pi_last_name, institution,
        funding_amount, start_date, end_date, program_element, abstract_text.
    """
    params: dict[str, Any] = {
        "printFields": _NSF_FIELDS,
        "rpp": min(limit, 25),  # NSF max per page is 25
    }
    if query:
        params["keyword"] = query
    if program_element:
        params["programElement"] = program_element

    # Split pi_name into first / last heuristically
    if pi_name:
        parts = pi_name.strip().split()
        if len(parts) >= 2:
            params["piFirst"] = parts[0]
            params["piLast"]  = parts[-1]
        else:
            params["piLast"] = parts[0]

    if date_start:
        params["dateStart"] = date_start
    if date_end:
        params["dateEnd"] = date_end

    data = _nsf_request(_NSF_BASE, params)
    if not data:
        return []

    items = (
        data.get("response", {}).get("award") or []
    )
    return [_flatten_nsf(a) for a in items]


def nsf_get_award(award_id: str) -> dict | None:
    """Fetch one NSF award by ID, including abstract text."""
    params = {
        "id": award_id,
        "printFields": _NSF_FIELDS,
    }
    data = _nsf_request(_NSF_BASE, params)
    if not data:
        return None
    items = data.get("response", {}).get("award") or []
    if not items:
        return None
    return _flatten_nsf(items[0])

def search_grants_intelligence(
    query: str,
    mechanism: str | None = None,
    fiscal_years: list[int] | None = None,
) -> dict:
    """Search NIH + NSF simultaneously for any research topic.

    Returns:
        {
            nih: [...],
            nsf: [...],
            total_count: int,
            funded_language_samples: [<top-3 abstract excerpts matching the query>],
        }
    """
    # NIH search
    nih_kwargs: dict[str, Any] = {"query": query, "limit": 25}
    if mechanism:
        nih_kwargs["activity_codes"] = [mechanism]
    if fiscal_years:
        nih_kwargs["fiscal_years"] = fiscal_years
    nih_results = nih_search_awards(**nih_kwargs)

    # NSF search (runs after NIH to avoid hammering concurrently)
    nsf_results = nsf_search_awards(query=query, limit=25)

    # Extract funded-language samples: top-3 non-empty abstracts containing query terms
    query_terms = [t.lower() for t in query.split() if len(t) > 3]
    samples: list[str] = []
    for award in nih_results + nsf_results:
        text = award.get("abstract_text") or ""
        if not text:
            continue
        lower = text.lower()
        if any(term in lower for term in query_terms):
            # Grab a ~400-char excerpt around the first matching term
            idx = next((lower.find(t) for t in query_terms if t in lower), 0)
            start = max(0, idx - 100)
            excerpt = text[start : start + 400].strip()
            samples.append(excerpt)
            if len(samples) == 3:
                break

    return {
        "nih":                    nih_results,
        "nsf":                    nsf_results,
        "total_count":            len(nih_results) + len(nsf_results),
        "funded_language_samples": samples,
    }
