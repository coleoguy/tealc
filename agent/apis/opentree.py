"""
Open Tree of Life + TimeTree phylogenetic access layer for Tealc.

OToL v3 (https://api.opentreeoflife.org/v3): TNRS, induced subtree.
TimeTree v5 — three-layer reliability:
  1. NCBI eutils esearch → taxon IDs (names → numeric IDs).
  2. timetree.temple.edu/api/pairwise/{id_a}/{id_b}/json (stable endpoint).
  3. HTML scrape of timetree.org/search/pairwise as last resort.
Note: timetree.org/api rejects plain names; the real API is at temple.edu.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import warnings
from typing import Optional

import requests

try:
    from bs4 import BeautifulSoup as _BS
    _HAVE_BS4 = True
except ImportError:
    _HAVE_BS4 = False

_OTOL_BASE = "https://api.opentreeoflife.org/v3"
_TT_API_BASE = "http://timetree.temple.edu/api"
_NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_RESEARCHER_EMAIL = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
_HEADERS = {"User-Agent": f"Tealc/1.0 ({_RESEARCHER_EMAIL})"}
_TIMEOUT = 15


def tnrs_match(names: list[str]) -> dict[str, dict]:
    """Resolve a list of species/genus names to Open Tree Taxonomy IDs.

    Returns {input_name: {accepted_name, ott_id, match_score,
    is_synonym, unique_name}}.  For unresolved names the value is
    {'ott_id': None, 'accepted_name': None, 'match_score': 0.0}.
    """
    empty = {"ott_id": None, "accepted_name": None,
             "match_score": 0.0, "is_synonym": False, "unique_name": None}
    result: dict[str, dict] = {n: dict(empty) for n in names}
    if not names:
        return result

    url = f"{_OTOL_BASE}/tnrs/match_names"
    try:
        resp = requests.post(
            url,
            json={"names": names, "do_approximate_matching": True},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"tnrs_match failed: {exc}")
        return result

    for match_block in data.get("results", []):
        query_name = match_block.get("name", "")
        matches = match_block.get("matches", [])
        if not matches:
            continue
        best = matches[0]
        taxon = best.get("taxon", {})
        ott_id_raw = taxon.get("ott_id")
        result[query_name] = {
            "ott_id": int(ott_id_raw) if ott_id_raw is not None else None,
            "accepted_name": taxon.get("name"),
            "match_score": float(best.get("score", 0.0)),
            "is_synonym": best.get("is_synonym", False),
            "unique_name": taxon.get("unique_name"),
        }
    return result



def get_induced_subtree(ott_ids: list[int]) -> Optional[str]:
    """Fetch Newick induced subtree for OTT IDs. Retries once after removing broken IDs."""
    if not ott_ids:
        return None

    def _fetch(ids: list[int]) -> tuple[Optional[str], list[int]]:
        try:
            resp = requests.post(
                f"{_OTOL_BASE}/tree_of_life/induced_subtree",
                json={"ott_ids": ids, "label_format": "name_and_id"},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            bad: list[int] = []
            for k in ("unknown", "broken"):
                bad.extend(int(x) for x in body.get(k, []))
            return body.get("newick") or body.get("subtree"), bad
        except requests.HTTPError as exc:
            try:
                body = exc.response.json()
                bad = []
                for k in ("unknown", "broken"):
                    bad.extend(int(x) for x in body.get(k, []))
                return None, bad
            except Exception:
                return None, []
        except Exception as exc2:  # noqa: BLE001
            warnings.warn(f"get_induced_subtree: {exc2}")
            return None, []

    newick, bad = _fetch(ott_ids)
    if newick:
        return newick
    if bad:
        cleaned = [i for i in ott_ids if i not in bad]
        if len(cleaned) >= 3:
            newick, _ = _fetch(cleaned)
            return newick
    return None



def get_induced_subtree_by_names(names: list[str]) -> dict:
    """Convenience: tnrs_match + get_induced_subtree in one call.

    Returns {'newick': str | None, 'resolved': {name: ott_id},
             'unresolved': [names]}.
    newick is None when fewer than 3 names resolved.
    """
    resolved_map = tnrs_match(names)
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for name, info in resolved_map.items():
        if info["ott_id"] is not None:
            resolved[name] = info["ott_id"]
        else:
            unresolved.append(name)

    newick: Optional[str] = None
    if len(resolved) >= 3:
        newick = get_induced_subtree(list(resolved.values()))

    return {"newick": newick, "resolved": resolved, "unresolved": unresolved}



def _ncbi_taxon_id(name: str) -> Optional[int]:
    """Resolve a species/genus name to its NCBI Taxonomy integer ID via eutils."""
    try:
        resp = requests.get(
            _NCBI_ESEARCH,
            params={"db": "taxonomy", "term": name, "retmode": "json"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        return int(ids[0]) if ids else None
    except Exception:  # noqa: BLE001
        return None


def _tt_api(ncbi_a: int, ncbi_b: int) -> Optional[dict]:
    """GET timetree.temple.edu/api/pairwise/{a}/{b}/json → precomputed age + CI."""
    url = f"{_TT_API_BASE}/pairwise/{ncbi_a}/{ncbi_b}/json"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        studies = data.get("studies", {})
        median = studies.get("precomputed_age") or data.get("sum_median_time")
        if median is None:
            return None
        ci_low = studies.get("precomputed_ci_low", 0.0)
        ci_high = studies.get("precomputed_ci_high", 0.0)
        n = data.get("all_total", 0)
        return {
            "mya_median": float(median),
            "mya_min": float(ci_low),
            "mya_max": float(ci_high),
            "study_count": int(n),
            "source": "timetree",
        }
    except Exception:  # noqa: BLE001
        return None


def _tt_fallback_html(taxon_a: str, taxon_b: str) -> Optional[dict]:
    """Scrape timetree.org/search/pairwise HTML (last resort). BS4 preferred, else regex."""
    a_enc = requests.utils.quote(taxon_a.replace(" ", "_"))
    b_enc = requests.utils.quote(taxon_b.replace(" ", "_"))
    url = f"http://www.timetree.org/search/pairwise/{a_enc}/{b_enc}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception:  # noqa: BLE001
        return None

    if "Page Not Found" in html:
        return None

    median = min_t = max_t = None
    n_studies = 0

    if _HAVE_BS4:
        for row in _BS(html, "html.parser").find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label, raw = cells[0].lower(), cells[1].replace(",", "")
            try:
                val = float(re.sub(r"[^\d.]", "", raw))
            except ValueError:
                continue
            if "median" in label:
                median = val
            elif "min" in label:
                min_t = val
            elif "max" in label:
                max_t = val
    else:
        for dest, pat in [
            ("median", r"Median[^<]*?</t[dh]>\s*<t[dh][^>]*?>\s*([\d.]+)"),
            ("min",    r"Min[^<]*?</t[dh]>\s*<t[dh][^>]*?>\s*([\d.]+)"),
            ("max",    r"Max[^<]*?</t[dh]>\s*<t[dh][^>]*?>\s*([\d.]+)"),
        ]:
            m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
            if m:
                v = float(m.group(1))
                if dest == "median": median = v
                elif dest == "min":  min_t = v
                else:                max_t = v
    m = re.search(r"(\d+)\s+stud", html, re.IGNORECASE)
    if m:
        n_studies = int(m.group(1))

    if median is None:
        return None
    return {
        "mya_median": median,
        "mya_min": float(min_t or 0.0),
        "mya_max": float(max_t or 0.0),
        "study_count": n_studies,
        "source": "timetree",
    }


def get_age_distribution(taxon_a: str, taxon_b: str) -> dict:
    """Return the FULL distribution of divergence-time estimates from TimeTree.

    Resolves names to NCBI taxon IDs, then queries
    timetree.temple.edu/api/pairwise/{a}/{b}/json for the complete study list.

    Returns:
      {
        "median_mya":   float,
        "ci_low":       float,
        "ci_high":      float,
        "n_studies":    int,
        "estimates":    [{"mya": float, "study_doi": str, "method": str}, ...],
        "consensus_url": str,
      }

    Falls back to HTML scrape (no per-study estimates) when the JSON API fails.
    Raises RuntimeError if neither source returns data.
    """
    # Resolve both names to NCBI taxon IDs
    id_a = _ncbi_taxon_id(taxon_a)
    time.sleep(0.3)
    id_b = _ncbi_taxon_id(taxon_b) if id_a is not None else None

    consensus_url = (
        f"http://www.timetree.org/search/pairwise/"
        f"{requests.utils.quote(taxon_a.replace(' ', '_'))}/"
        f"{requests.utils.quote(taxon_b.replace(' ', '_'))}"
    )

    if id_a is not None and id_b is not None:
        url = f"{_TT_API_BASE}/pairwise/{id_a}/{id_b}/json"
        try:
            time.sleep(0.3)
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            data = {}

        if data:
            studies_blob = data.get("studies", {})
            # Per-study estimates are in data["hit_records"] (list of dicts with
            # keys: pubmed_id, author, title, year, time, ref_id, citation_num).
            hit_records = data.get("hit_records", [])

            estimates: list[dict] = []
            for s in hit_records:
                try:
                    mya = float(s.get("time") or 0)
                except (TypeError, ValueError):
                    mya = 0.0
                # Build a DOI-style reference: use pubmed_id as PMID or ref_id
                pmid = s.get("pubmed_id")
                doi  = f"PMID:{pmid}" if pmid else (s.get("ref_id") or "")
                estimates.append({
                    "mya":       mya,
                    "study_doi": doi,
                    "method":    "",  # not returned by this endpoint
                })

            # Aggregate stats — prefer precomputed fields
            median = (
                studies_blob.get("precomputed_age")
                or data.get("sum_median_time")
            )
            ci_low  = studies_blob.get("precomputed_ci_low",  0.0)
            ci_high = studies_blob.get("precomputed_ci_high", 0.0)
            n       = data.get("all_total", len(estimates))

            # If API returned nothing useful, fall through
            if median is not None:
                return {
                    "median_mya":    float(median),
                    "ci_low":        float(ci_low),
                    "ci_high":       float(ci_high),
                    "n_studies":     int(n),
                    "estimates":     estimates,
                    "consensus_url": consensus_url,
                }

    # --- HTML fallback (no per-study breakdown available) ---
    time.sleep(0.3)
    scraped = _tt_fallback_html(taxon_a, taxon_b)
    if scraped is None:
        raise RuntimeError(
            f"get_age_distribution: TimeTree returned no data for "
            f"{taxon_a!r} / {taxon_b!r}. "
            "The temple.edu API may require a session cookie for some pairs."
        )
    return {
        "median_mya":    scraped["mya_median"],
        "ci_low":        scraped["mya_min"],
        "ci_high":       scraped["mya_max"],
        "n_studies":     scraped["study_count"],
        "estimates":     [],   # HTML scrape does not expose per-study data
        "consensus_url": consensus_url,
    }


def get_divergence_time(taxon_a: str, taxon_b: str) -> Optional[dict]:
    """Query TimeTree for pairwise divergence time in MYA between two taxa.

    Workflow (three layers for reliability):
      1. Resolve both names to NCBI taxon IDs via NCBI eutils.
      2. Call the TimeTree temple.edu JSON API with numeric IDs.
      3. If step 1 or 2 fails, fall back to scraping timetree.org HTML.

    Returns {'mya_median', 'mya_min', 'mya_max', 'study_count', 'source'}
    or None on failure.
    """
    # Layer 1+2: resolve names → NCBI IDs → temple.edu API
    id_a = _ncbi_taxon_id(taxon_a)
    time.sleep(0.3)
    id_b = _ncbi_taxon_id(taxon_b) if id_a is not None else None
    if id_a is not None and id_b is not None:
        time.sleep(0.3)
        result = _tt_api(id_a, id_b)
        if result is not None:
            return result

    # Layer 3: HTML scraping fallback
    time.sleep(0.3)
    return _tt_fallback_html(taxon_a, taxon_b)



def _equal_branch_lengths(newick: str) -> str:
    """Set every branch to 1.0; topology preserved."""
    cleaned = re.sub(r":[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?", "", newick)
    result = re.sub(r"(?<=[^(,\s])([,)])", r":1.0\1", cleaned)
    result = re.sub(r"\)([^:;])", r"):1.0\1", result)
    return result


def _timetree_calibrated(newick: str) -> Optional[str]:
    """Scale all branches uniformly so root height = TimeTree first/last leaf pair."""
    leaf_names = re.findall(r"([A-Za-z][A-Za-z0-9_ ]+)(?:_ott\d+)?(?::[0-9.eE+-]+)?[,)]",
                            newick)
    leaf_names = [n.strip().replace("_", " ") for n in leaf_names if n.strip()]
    if len(leaf_names) < 2:
        return None
    time.sleep(0.3)
    dt = get_divergence_time(leaf_names[0], leaf_names[-1])
    if dt is None or dt["mya_median"] == 0:
        return None
    root_age = dt["mya_median"]
    equal = _equal_branch_lengths(newick)
    max_depth = max(
        (len(re.findall(r"\(", equal[:m.start()])) for m in re.finditer(r"[,)]", equal)),
        default=1,
    ) or 1
    branch_len = root_age / max_depth
    return re.sub(r":[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?",
                  lambda _: f":{branch_len:.4f}", equal)


def ultrametricize_newick(newick: str, method: str = "penalized_likelihood") -> Optional[str]:
    """Make Newick ultrametric. Methods: 'equal_branch_lengths', 'timetree_calibrated',
    'penalized_likelihood' (Rscript+ape::chronos; falls back to equal_branch_lengths)."""
    if method == "equal_branch_lengths":
        return _equal_branch_lengths(newick)

    if method == "timetree_calibrated":
        result = _timetree_calibrated(newick)
        if result is not None:
            return result
        warnings.warn(
            "timetree_calibrated: could not get calibration point; "
            "falling back to equal_branch_lengths"
        )
        return _equal_branch_lengths(newick)

    if method == "penalized_likelihood":
        rscript = shutil.which("Rscript")
        if rscript:
            with tempfile.NamedTemporaryFile(suffix=".nwk", mode="w", delete=False) as fh:
                fh.write(newick); tmp_in = fh.name
            tmp_out = tmp_in + ".out.nwk"
            r_code = (f'library(ape);tree<-read.tree("{tmp_in}");'
                      f'tree<-chronos(tree);write.tree(tree,file="{tmp_out}")')
            try:
                proc = subprocess.run([rscript, "-e", r_code],
                                      capture_output=True, text=True, timeout=60)
                if proc.returncode == 0 and os.path.exists(tmp_out):
                    with open(tmp_out) as f:
                        return f.read().strip()
            except Exception as exc:  # noqa: BLE001
                warnings.warn(f"Rscript chronos failed: {exc}")
            finally:
                for p in (tmp_in, tmp_out):
                    try: os.unlink(p)
                    except OSError: pass
        warnings.warn("penalized_likelihood unavailable; using equal_branch_lengths")
        return _equal_branch_lengths(newick)

    raise ValueError(f"Unknown method: {method!r}. Choose from "
                     "'equal_branch_lengths', 'timetree_calibrated', "
                     "'penalized_likelihood'.")



def save_tree(newick: str, path: str) -> str:
    """Write newick to disk. Returns absolute path."""
    abs_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
    with open(abs_path, "w") as fh:
        fh.write(newick)
        if not newick.endswith("\n"):
            fh.write("\n")
    return abs_path
