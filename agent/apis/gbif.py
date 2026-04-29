"""
GBIF API v1 client for Tealc.

Species occurrence data and taxonomic backbone.
Free, CC0, no auth required. Base: https://api.gbif.org/v1/
"""

from __future__ import annotations

import math
import time

import requests

_BASE = "https://api.gbif.org/v1"
_HEADERS = {"User-Agent": "Tealc/1.0 (blackmon@tamu.edu)"}
_TIMEOUT = 60  # facet/aggregation queries can be slow
_GBIF_MAX = 300  # GBIF hard per-request cap


def _get(endpoint: str, params: dict | None = None) -> dict:
    """GET with 20-second timeout and single retry on 5xx."""
    url = f"{_BASE}{endpoint}"
    for attempt in range(2):
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code < 500:
            resp.raise_for_status()
            return resp.json()
        if attempt == 0:
            time.sleep(1.0)
    resp.raise_for_status()
    return {}


def _taxon_key_params(scientific_name: str | None, gbif_key: int | None) -> dict:
    if gbif_key is not None:
        return {"taxonKey": gbif_key}
    if scientific_name is not None:
        return {"scientificName": scientific_name}
    return {}


def species_match(name: str, rank: str | None = None) -> dict | None:
    """Resolve a scientific name to GBIF taxonomy.

    Returns {gbif_key, scientific_name, canonical_name, rank, kingdom,
    phylum, class, order, family, genus, match_type, confidence,
    is_synonym, accepted_key}.
    """
    params: dict = {"name": name, "verbose": "false"}
    if rank:
        params["rank"] = rank.upper()

    data = _get("/species/match", params)
    if data.get("matchType") == "NONE":
        return None

    return {
        "gbif_key": data.get("usageKey"),
        "scientific_name": data.get("scientificName"),
        "canonical_name": data.get("canonicalName"),
        "rank": data.get("rank"),
        "kingdom": data.get("kingdom"),
        "phylum": data.get("phylum"),
        "class": data.get("class"),
        "order": data.get("order"),
        "family": data.get("family"),
        "genus": data.get("genus"),
        "match_type": data.get("matchType"),
        "confidence": data.get("confidence"),
        "is_synonym": data.get("synonym", False),
        "accepted_key": data.get("acceptedUsageKey"),
    }


def search_species(
    query: str,
    higher_taxon_key: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search GBIF's taxonomic backbone.

    Returns [{gbif_key, scientific_name, canonical_name, rank, kingdom,
    family, authorship}].
    """
    params: dict = {"q": query, "limit": min(limit, 100)}
    if higher_taxon_key is not None:
        params["highertaxonKey"] = higher_taxon_key

    data = _get("/species", params)
    results = []
    for item in data.get("results", []):
        results.append({
            "gbif_key": item.get("key"),
            "scientific_name": item.get("scientificName"),
            "canonical_name": item.get("canonicalName"),
            "rank": item.get("rank"),
            "kingdom": item.get("kingdom"),
            "family": item.get("family"),
            "authorship": item.get("authorship"),
        })
    return results


def occurrence_search(
    scientific_name: str | None = None,
    gbif_key: int | None = None,
    country: str | None = None,
    year_start: int | None = None,
    year_end: int | None = None,
    has_coordinate: bool = True,
    has_geospatial_issue: bool | None = False,
    basis_of_record: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Search occurrences with optional pagination for limit > 300.

    Returns [{key, scientific_name, gbif_key, country,
    decimal_latitude, decimal_longitude, event_date, year, month,
    locality, basis_of_record, institution_code, collection_code,
    catalog_number, recorded_by}].
    """
    base_params: dict = {"hasCoordinate": str(has_coordinate).lower()}
    if scientific_name:
        base_params["scientificName"] = scientific_name
    if gbif_key is not None:
        base_params["taxonKey"] = gbif_key
    if country:
        base_params["country"] = country
    if year_start or year_end:
        y0 = year_start or 1000
        y1 = year_end or 9999
        base_params["year"] = f"{y0},{y1}"
    if has_geospatial_issue is not None:
        base_params["hasGeospatialIssue"] = str(has_geospatial_issue).lower()
    if basis_of_record:
        base_params["basisOfRecord"] = basis_of_record

    records: list[dict] = []
    offset = 0
    remaining = limit

    while remaining > 0:
        batch = min(remaining, _GBIF_MAX)
        params = {**base_params, "limit": batch, "offset": offset}
        data = _get("/occurrence/search", params)
        raw = data.get("results", [])
        for item in raw:
            records.append({
                "key": item.get("key"),
                "scientific_name": item.get("scientificName"),
                "gbif_key": item.get("taxonKey"),
                "country": item.get("countryCode"),
                "decimal_latitude": item.get("decimalLatitude"),
                "decimal_longitude": item.get("decimalLongitude"),
                "event_date": item.get("eventDate"),
                "year": item.get("year"),
                "month": item.get("month"),
                "locality": item.get("locality"),
                "basis_of_record": item.get("basisOfRecord"),
                "institution_code": item.get("institutionCode"),
                "collection_code": item.get("collectionCode"),
                "catalog_number": item.get("catalogNumber"),
                "recorded_by": item.get("recordedBy"),
            })
        fetched = len(raw)
        offset += fetched
        remaining -= fetched
        if data.get("endOfRecords", True) or fetched == 0:
            break
        time.sleep(0.25)

    return records


def occurrence_counts_by_country(
    scientific_name: str | None = None,
    gbif_key: int | None = None,
) -> dict[str, int]:
    """Geographic distribution summary. Returns {country_code: count}."""
    params = {
        **_taxon_key_params(scientific_name, gbif_key),
        "limit": 0,
        "facet": "country",
        "facetLimit": 250,
    }
    data = _get("/occurrence/search", params)
    counts: dict[str, int] = {}
    for facet in data.get("facets", []):
        if facet.get("field") == "COUNTRY":
            for entry in facet.get("counts", []):
                counts[entry["name"]] = entry["count"]
    return counts


def occurrence_counts_by_year(
    scientific_name: str | None = None,
    gbif_key: int | None = None,
    year_start: int = 1900,
    year_end: int | None = None,
) -> dict[int, int]:
    """Temporal distribution summary. Returns {year: count}."""
    y1 = year_end or 9999
    params = {
        **_taxon_key_params(scientific_name, gbif_key),
        "limit": 0,
        "facet": "year",
        "facetLimit": 500,
        "year": f"{year_start},{y1}",
    }
    data = _get("/occurrence/search", params)
    counts: dict[int, int] = {}
    for facet in data.get("facets", []):
        if facet.get("field") == "YEAR":
            for entry in facet.get("counts", []):
                try:
                    counts[int(entry["name"])] = entry["count"]
                except (ValueError, KeyError):
                    pass
    return counts


def _sampling_bias_score(country_counts: dict[str, int]) -> float:
    """1 - entropy(dist)/log(n_countries). 1=single-country, 0=uniform."""
    n = len(country_counts)
    if n <= 1:
        return 1.0
    total = sum(country_counts.values())
    if total == 0:
        return 0.0
    entropy = -sum(
        (c / total) * math.log(c / total)
        for c in country_counts.values()
        if c > 0
    )
    max_entropy = math.log(n)
    score = 1.0 - (entropy / max_entropy)
    return max(0.0, min(1.0, score))


def geographic_summary(scientific_name: str) -> dict:
    """Combined geographic + temporal summary for a species."""
    match = species_match(scientific_name)
    if match is None:
        return {}

    gbif_key = match["gbif_key"]
    canonical = match.get("canonical_name") or scientific_name

    time.sleep(0.25)
    countries = occurrence_counts_by_country(gbif_key=gbif_key)

    time.sleep(0.25)
    years = occurrence_counts_by_year(gbif_key=gbif_key)

    time.sleep(0.25)
    basis_params = {
        "taxonKey": gbif_key,
        "limit": 0,
        "facet": "basisOfRecord",
        "facetLimit": 20,
    }
    basis_data = _get("/occurrence/search", basis_params)
    basis_summary: dict[str, int] = {}
    for facet in basis_data.get("facets", []):
        if facet.get("field") == "BASIS_OF_RECORD":
            for entry in facet.get("counts", []):
                basis_summary[entry["name"]] = entry["count"]

    total = basis_data.get("count", sum(countries.values()))
    top_countries = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:10]

    sorted_years = sorted(years.keys())
    year_range = (sorted_years[0], sorted_years[-1]) if sorted_years else (None, None)

    time.sleep(0.25)
    first_recs = _get("/occurrence/search", {
        "taxonKey": gbif_key, "limit": 1, "offset": 0,
        "hasCoordinate": "false",
    })
    most_recent_recs = _get("/occurrence/search", {
        "taxonKey": gbif_key, "limit": 1, "offset": 0,
        "hasCoordinate": "false",
    })
    first_recorded = None
    most_recent = None
    if first_recs.get("results"):
        first_recorded = first_recs["results"][0].get("eventDate")
    if most_recent_recs.get("results"):
        most_recent = most_recent_recs["results"][0].get("eventDate")

    return {
        "gbif_key": gbif_key,
        "canonical_name": canonical,
        "total_occurrences": total,
        "countries": countries,
        "top_countries": top_countries,
        "year_range": year_range,
        "first_recorded": first_recorded,
        "most_recent": most_recent,
        "basis_summary": basis_summary,
        "sampling_bias_score": _sampling_bias_score(countries),
    }


def bulk_occurrence_centroid(species_list: list[str]) -> dict[str, dict | None]:
    """Compute occurrence centroid for each species in *species_list*.

    For each Latin binomial:
      - Queries /v1/occurrence/search?scientificName=X&hasCoordinate=true&limit=300
      - Computes mean lat / lon, bounding box, and record count.
      - Species with < 5 georeferenced records get None + a reason string.
      - Results are cached in data/gbif_centroid_cache.json (keyed by species name).
      - Politeness sleep of 0.34 s between species.

    Returns {species: {"lat", "lon", "n_records", "bbox": [s, w, n, e]} | None}.
    Entries that are None carry {"reason": "..."} instead of coordinates.
    """
    import json as _json
    import os as _os

    _CACHE_PATH = "data/gbif_centroid_cache.json"
    _MIN_RECORDS = 5
    _POLITENESS  = 0.34

    # Load persistent cache
    cache: dict = {}
    if _os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH) as fh:
                cache = _json.load(fh)
        except (_json.JSONDecodeError, OSError):
            cache = {}

    out: dict[str, dict | None] = {}

    for i, species in enumerate(species_list):
        if species in cache:
            out[species] = cache[species]
            continue  # cache hit — no sleep needed

        if i > 0:
            time.sleep(_POLITENESS)

        try:
            data = _get("/occurrence/search", {
                "scientificName": species,
                "hasCoordinate": "true",
                "limit": _GBIF_MAX,
            })
        except Exception as exc:
            out[species] = {"reason": f"GBIF request failed: {exc}"}
            cache[species] = out[species]
            continue

        records = data.get("results", [])
        lats = [r["decimalLatitude"]  for r in records
                if r.get("decimalLatitude")  is not None]
        lons = [r["decimalLongitude"] for r in records
                if r.get("decimalLongitude") is not None]

        if len(lats) < _MIN_RECORDS:
            entry: dict | None = {"reason": f"only {len(lats)} georeferenced records (< {_MIN_RECORDS})"}
        else:
            n = len(lats)
            entry = {
                "lat":       sum(lats) / n,
                "lon":       sum(lons) / n,
                "n_records": n,
                "bbox":      [min(lats), min(lons), max(lats), max(lons)],  # [S, W, N, E]
            }

        out[species] = entry
        cache[species] = entry

    # Persist updated cache
    try:
        _os.makedirs("data", exist_ok=True)
        with open(_CACHE_PATH, "w") as fh:
            _json.dump(cache, fh, indent=2)
    except OSError:
        pass  # non-fatal

    return out


def check_invasive_records(
    scientific_name: str, native_countries: list[str]
) -> dict:
    """Check for occurrences outside a list of native countries.

    Returns {invasive_records, non_native_countries, earliest_outside_native}.
    """
    native_set = {c.upper() for c in native_countries}

    time.sleep(0.25)
    countries = occurrence_counts_by_country(scientific_name=scientific_name)

    non_native: dict[str, int] = {
        c: n for c, n in countries.items() if c.upper() not in native_set
    }
    invasive_total = sum(non_native.values())

    earliest_outside: int | None = None
    if non_native:
        time.sleep(0.25)
        match = species_match(scientific_name)
        gbif_key = match["gbif_key"] if match else None
        for country_code in sorted(non_native, key=lambda c: non_native[c], reverse=True)[:5]:
            time.sleep(0.25)
            params: dict = {
                "country": country_code,
                "limit": 1,
                "hasCoordinate": "false",
            }
            if gbif_key is not None:
                params["taxonKey"] = gbif_key
            else:
                params["scientificName"] = scientific_name
            data = _get("/occurrence/search", params)
            for rec in data.get("results", []):
                yr = rec.get("year")
                if yr and (earliest_outside is None or yr < earliest_outside):
                    earliest_outside = yr

    return {
        "invasive_records": invasive_total,
        "non_native_countries": non_native,
        "earliest_outside_native": earliest_outside,
    }
