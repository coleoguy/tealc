"""
NCBI Entrez E-utilities client for Tealc.

Covers: Taxonomy lookup/lineage, SRA run discovery, BioProject search,
and generic sequence fetch via esearch / esummary / efetch.

Rate limits:
  No API key : 3 req/sec  → sleep 0.35 s
  With API key: 10 req/sec → sleep 0.11 s

Set NCBI_API_KEY env var to raise the rate limit.
"""

from __future__ import annotations

import html
import os
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BASE     = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ESEARCH  = f"{_BASE}/esearch.fcgi"
_ESUMMARY = f"{_BASE}/esummary.fcgi"
_EFETCH   = f"{_BASE}/efetch.fcgi"
_ELINK    = f"{_BASE}/elink.fcgi"

_EMAIL   = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
_TOOL    = "Tealc"
_TIMEOUT = 20

_API_KEY = os.environ.get("NCBI_API_KEY", "")
_SLEEP   = 0.11 if _API_KEY else 0.35


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _base_params() -> dict:
    p = {"tool": _TOOL, "email": _EMAIL}
    if _API_KEY:
        p["api_key"] = _API_KEY
    return p


def _get(url: str, params: dict, *, timeout: int = _TIMEOUT) -> Optional[requests.Response]:
    """GET with one retry on transient 429/5xx."""
    for attempt in range(2):
        if attempt:
            time.sleep(2)
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                continue
            return None
        except requests.RequestException:
            if attempt == 0:
                continue
    return None


def _pause() -> None:
    time.sleep(_SLEEP)


def _txt(el: Optional[ET.Element], path: str, default: str = "") -> str:
    """Find a sub-element by path and return its text, or default."""
    if el is None:
        return default
    found = el.find(path)
    return (found.text or "").strip() if found is not None else default


def _esearch_ids(db: str, term: str, retmax: int = 100) -> list[str]:
    """Run esearch; return list of UIDs."""
    params = {**_base_params(), "db": db, "term": term,
              "retmax": retmax, "retmode": "xml", "usehistory": "n"}
    resp = _get(_ESEARCH, params)
    _pause()
    if not resp:
        return []
    root = ET.fromstring(resp.text)
    return [el.text for el in root.findall(".//Id") if el.text]


def _esummary_root(db: str, ids: list[str]) -> Optional[ET.Element]:
    """Fetch ESummary XML; return parsed root element or None."""
    if not ids:
        return None
    params = {**_base_params(), "db": db, "id": ",".join(ids),
              "retmode": "xml", "version": "2.0"}
    resp = _get(_ESUMMARY, params)
    _pause()
    if not resp:
        return None
    return ET.fromstring(resp.text)


def _efetch_xml(db: str, uid: str) -> Optional[ET.Element]:
    """Run efetch in XML mode; return parsed root or None."""
    params = {**_base_params(), "db": db, "id": uid,
              "rettype": "xml", "retmode": "xml"}
    resp = _get(_EFETCH, params)
    _pause()
    if not resp:
        return None
    try:
        return ET.fromstring(resp.text)
    except ET.ParseError:
        return None


# ---------------------------------------------------------------------------
# Taxonomy — uses efetch for rich data, esummary only for children list
# ---------------------------------------------------------------------------
def _parse_taxon(taxon_el: ET.Element) -> dict:
    """Extract fields from a <Taxon> efetch element."""
    # Authority: look for Name/ClassCDE == 'authority'
    authority = ""
    for name_el in taxon_el.findall(".//OtherNames/Name"):
        if _txt(name_el, "ClassCDE") == "authority":
            authority = _txt(name_el, "DispName")
            break

    lineage_str = _txt(taxon_el, "Lineage")
    gc   = _txt(taxon_el, "GeneticCode/GCName")
    mtgc = _txt(taxon_el, "MitoGeneticCode/MGCName")

    tax_id = _txt(taxon_el, "TaxId")

    return {
        "tax_id":             tax_id,
        "scientific_name":    _txt(taxon_el, "ScientificName"),
        "rank":               _txt(taxon_el, "Rank"),
        "lineage_str":        lineage_str,
        "authority":          authority,
        "genetic_code":       gc,
        "mitochondrial_code": mtgc,
        "division":           _txt(taxon_el, "Division"),
    }


def taxonomy_search(name: str) -> dict | None:
    """
    Resolve a species/genus name to canonical NCBI Taxonomy.

    Returns {tax_id, scientific_name, rank, lineage_str, authority,
             genetic_code, mitochondrial_code, division,
             is_synonym, accepted_tax_id}
    or None if not found.
    """
    ids = _esearch_ids("taxonomy", name, retmax=1)
    if not ids:
        return None
    tax_id = ids[0]

    root = _efetch_xml("taxonomy", tax_id)
    if root is None:
        return None

    taxon_el = root.find(".//Taxon")
    if taxon_el is None:
        return None

    result = _parse_taxon(taxon_el)

    # Synonym detection: esearch may return a redirected ID; compare with
    # what efetch reports.
    fetched_id = result["tax_id"]
    is_synonym      = bool(fetched_id and fetched_id != tax_id)
    accepted_tax_id = fetched_id if is_synonym else tax_id

    result["is_synonym"]      = is_synonym
    result["accepted_tax_id"] = accepted_tax_id
    # Normalise tax_id to what the caller searched for
    result["tax_id"] = tax_id
    return result


def taxonomy_get_children(tax_id: str | int, limit: int = 100) -> list[dict]:
    """
    Fetch immediate children of a taxon.

    Returns [{tax_id, name, rank}, ...].
    """
    term = f"txid{tax_id}[Organism] AND {tax_id}[Parent]"
    ids = _esearch_ids("taxonomy", term, retmax=limit)
    if not ids:
        return []

    root = _esummary_root("taxonomy", ids[:limit])
    if root is None:
        return []

    results = []
    for ds in root.findall(".//DocumentSummary"):
        results.append({
            "tax_id": ds.get("uid", ""),
            "name":   _txt(ds, "ScientificName"),
            "rank":   _txt(ds, "Rank"),
        })
    return results


def taxonomy_get_lineage(tax_id: str | int) -> list[dict]:
    """
    Full taxonomic lineage for a tax_id.

    Returns [{tax_id, name, rank}, ...] ordered root → target.
    """
    root = _efetch_xml("taxonomy", str(tax_id))
    if root is None:
        return []

    taxon_el = root.find(".//Taxon")
    if taxon_el is None:
        return []

    lineage_ex = taxon_el.find("LineageEx")
    if lineage_ex is not None:
        results = [
            {
                "tax_id": _txt(t, "TaxId"),
                "name":   _txt(t, "ScientificName"),
                "rank":   _txt(t, "Rank"),
            }
            for t in lineage_ex.findall("Taxon")
        ]
    else:
        # Fallback: parse flat lineage string
        lineage_str = _txt(taxon_el, "Lineage")
        results = [
            {"tax_id": "", "name": n.strip(), "rank": ""}
            for n in lineage_str.split(";") if n.strip()
        ]

    # Append the target itself
    results.append({
        "tax_id": str(tax_id),
        "name":   _txt(taxon_el, "ScientificName"),
        "rank":   _txt(taxon_el, "Rank"),
    })
    return results


# ---------------------------------------------------------------------------
# SRA — ESummary returns HTML-encoded XML blobs in ExpXml / Runs fields
# ---------------------------------------------------------------------------
def _sra_parse_docsum(ds: ET.Element) -> dict:
    """Parse an SRA ESummary DocumentSummary into a flat dict."""
    def child(tag: str) -> str:
        el = ds.find(tag)
        return (el.text or "").strip() if el is not None else ""

    expxml_raw = child("ExpXml")
    runs_raw   = child("Runs")

    run_id = exp_id = study_id = sample_id = organism = ""
    platform = instrument = library_strategy = library_source = layout = ""
    bases = spots = title = ""

    if expxml_raw:
        try:
            ex = ET.fromstring(f"<root>{html.unescape(expxml_raw)}</root>")
            title   = _txt(ex, ".//Summary/Title")
            pl_el   = ex.find(".//Platform")
            if pl_el is not None:
                platform   = (pl_el.text or "").strip()
                instrument = pl_el.get("instrument_model", "")
            stat_el = ex.find(".//Summary/Statistics")
            if stat_el is not None:
                bases = stat_el.get("total_bases", "")
                spots = stat_el.get("total_spots", "")
            lib_el = ex.find(".//Library_descriptor")
            if lib_el is not None:
                library_strategy = _txt(lib_el, "LIBRARY_STRATEGY")
                library_source   = _txt(lib_el, "LIBRARY_SOURCE")
                ly_el = lib_el.find("LIBRARY_LAYOUT")
                if ly_el is not None:
                    children = list(ly_el)
                    layout = children[0].tag if children else ""
            org_el = ex.find(".//Organism")
            if org_el is not None:
                organism = org_el.get("ScientificName", "") or (org_el.text or "").strip()
            exp_el = ex.find(".//Experiment")
            if exp_el is not None:
                exp_id = exp_el.get("acc", "")
            study_el = ex.find(".//Study")
            if study_el is not None:
                study_id = study_el.get("acc", "")
            sample_el = ex.find(".//Sample")
            if sample_el is not None:
                sample_id = sample_el.get("acc", "")
        except ET.ParseError:
            pass

    if runs_raw:
        try:
            ru = ET.fromstring(f"<root>{html.unescape(runs_raw)}</root>")
            run_el = ru.find(".//Run")
            if run_el is not None:
                run_id = run_el.get("acc", "")
                bases  = run_el.get("total_bases", bases)
                spots  = run_el.get("total_spots", spots)
        except ET.ParseError:
            pass

    return {
        "run_id":           run_id,
        "experiment_id":    exp_id,
        "study_id":         study_id,
        "sample_id":        sample_id,
        "organism":         organism,
        "platform":         platform,
        "instrument":       instrument,
        "bases":            bases,
        "bytes":            spots,
        "library_strategy": library_strategy,
        "library_source":   library_source,
        "layout":           layout,
        "title":            title,
    }


def sra_search(
    organism: str | None = None,
    query: str | None = None,
    platform: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Search SRA for runs.

    At least one of organism/query required.
    organism may be a species name or a TaxID prefixed with 'txid'.
    Returns list of run metadata dicts.
    """
    parts: list[str] = []
    if organism:
        if organism.startswith("txid"):
            parts.append(f"{organism}[Organism]")
        else:
            parts.append(f'"{organism}"[Organism]')
    if query:
        parts.append(query)
    if platform:
        parts.append(f'"{platform}"[Platform]')
    if not parts:
        raise ValueError("sra_search requires at least one of: organism, query")

    term = " AND ".join(parts)
    ids = _esearch_ids("sra", term, retmax=limit)
    if not ids:
        return []

    root = _esummary_root("sra", ids[:limit])
    if root is None:
        return []

    return [_sra_parse_docsum(ds) for ds in root.findall(".//DocumentSummary")]


def sra_run_detail(run_id: str) -> dict | None:
    """
    Deep fetch for a single SRA run accession (e.g. 'SRR123456').

    Returns all metadata + runinfo CSV, or None if not found.
    """
    ids = _esearch_ids("sra", f"{run_id}[Accession]", retmax=1)
    if not ids:
        return None

    root = _esummary_root("sra", [ids[0]])
    if root is None:
        return None

    ds = root.find(".//DocumentSummary")
    if ds is None:
        return None

    result = _sra_parse_docsum(ds)

    # Append runinfo CSV for download URLs
    params = {**_base_params(), "db": "sra", "id": ids[0],
              "rettype": "runinfo", "retmode": "text"}
    resp = _get(_EFETCH, params)
    _pause()
    if resp:
        result["runinfo_csv"] = resp.text

    return result


# ---------------------------------------------------------------------------
# BioProject — ESummary uses direct child elements (not Item/Name)
# ---------------------------------------------------------------------------
def _bp_parse_docsum(ds: ET.Element) -> dict:
    def child(tag: str) -> str:
        el = ds.find(tag)
        return (el.text or "").strip() if el is not None else ""

    return {
        "bioproject_id":    child("Project_Acc"),
        "title":            child("Project_Title"),
        "description":      child("Project_Description"),
        "organism":         child("Organism_Name"),
        "submission_date":  child("Registration_Date"),
        "linked_sra_count": child("Statistics_total"),
    }


def bioproject_search(query: str, limit: int = 20) -> list[dict]:
    """
    Search BioProject.

    Returns [{bioproject_id, title, description, organism,
              submission_date, linked_sra_count}, ...].
    """
    ids = _esearch_ids("bioproject", query, retmax=limit)
    if not ids:
        return []

    root = _esummary_root("bioproject", ids[:limit])
    if root is None:
        return []

    return [_bp_parse_docsum(ds) for ds in root.findall(".//DocumentSummary")]


def bioproject_get(bioproject_id: str) -> dict | None:
    """
    Fetch a BioProject by accession (e.g. 'PRJNA123456').

    Returns full metadata + list of linked SRA UIDs.
    """
    ids = _esearch_ids("bioproject", f"{bioproject_id}[Project Accession]", retmax=1)
    if not ids:
        return None

    root = _esummary_root("bioproject", [ids[0]])
    if root is None:
        return None

    ds = root.find(".//DocumentSummary")
    if ds is None:
        return None

    result = _bp_parse_docsum(ds)

    # Linked SRA entries via elink
    params = {**_base_params(), "dbfrom": "bioproject", "db": "sra",
              "id": ids[0], "retmode": "xml"}
    resp = _get(_ELINK, params)
    _pause()
    linked_sra: list[str] = []
    if resp:
        try:
            el_root = ET.fromstring(resp.text)
            linked_sra = [
                lid.text
                for lid in el_root.findall(".//LinkSetDb/Link/Id")
                if lid.text
            ]
        except ET.ParseError:
            pass

    result["linked_sra_uids"] = linked_sra
    return result


# ---------------------------------------------------------------------------
# PubMed batch fetch
# ---------------------------------------------------------------------------
def entrez_efetch_pubmed(pmids: list[str]) -> list[dict]:
    """Batch-fetch PubMed records by PMID list (db=pubmed, retmode=xml).

    Batches in groups of 200. Respects rate limits (_SLEEP between requests).

    Each returned dict:
      pmid, title, abstract, journal, year, authors (list of str),
      doi, mesh_terms (list of str).
    """
    _BATCH = 200
    out: list[dict] = []

    for start in range(0, len(pmids), _BATCH):
        batch = pmids[start : start + _BATCH]
        params = {
            **_base_params(),
            "db": "pubmed",
            "id": ",".join(batch),
            "rettype": "abstract",
            "retmode": "xml",
        }
        resp = _get(_EFETCH, params)
        _pause()
        if not resp:
            continue
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            continue

        for article in root.findall(".//PubmedArticle"):
            medline   = article.find("MedlineCitation")
            art_node  = medline.find("Article") if medline is not None else None

            # PMID
            pmid_el = medline.find("PMID") if medline is not None else None
            pmid    = (pmid_el.text or "").strip() if pmid_el is not None else ""

            # Title
            title_el = art_node.find("ArticleTitle") if art_node is not None else None
            title    = "".join(title_el.itertext()).strip() if title_el is not None else ""

            # Abstract (may have multiple AbstractText nodes with NlmCategory)
            abstract = ""
            if art_node is not None:
                abs_node = art_node.find("Abstract")
                if abs_node is not None:
                    parts = []
                    for at in abs_node.findall("AbstractText"):
                        label = at.get("Label", "")
                        text  = "".join(at.itertext()).strip()
                        if label:
                            parts.append(f"{label}: {text}")
                        else:
                            parts.append(text)
                    abstract = " ".join(parts)

            # Journal
            journal = ""
            if art_node is not None:
                j_node = art_node.find("Journal")
                if j_node is not None:
                    jt = j_node.find("Title")
                    if jt is None:
                        jt = j_node.find("ISOAbbreviation")
                    journal = (jt.text or "").strip() if jt is not None else ""

            # Year
            year = ""
            if art_node is not None:
                j_node = art_node.find("Journal")
                if j_node is not None:
                    ji = j_node.find("JournalIssue")
                    if ji is not None:
                        pd = ji.find("PubDate")
                        if pd is not None:
                            yr = pd.find("Year")
                            if yr is None:
                                yr = pd.find("MedlineDate")
                            year = (yr.text or "")[:4].strip() if yr is not None else ""

            # Authors
            authors: list[str] = []
            al = art_node.find("AuthorList") if art_node is not None else None
            if al is not None:
                for au in al.findall("Author"):
                    ln = au.find("LastName")
                    fn = au.find("ForeName")
                    if ln is not None:
                        name = (ln.text or "").strip()
                        if fn is not None:
                            name += f", {(fn.text or '').strip()}"
                        authors.append(name)

            # DOI
            doi = ""
            if art_node is not None:
                for eid in art_node.findall(".//ELocationID"):
                    if eid.get("EIdType", "").lower() == "doi":
                        doi = (eid.text or "").strip()
                        break

            # MeSH terms
            mesh_terms: list[str] = []
            if medline is not None:
                mhl = medline.find("MeshHeadingList")
                if mhl is not None:
                    for mh in mhl.findall("MeshHeading"):
                        desc = mh.find("DescriptorName")
                        if desc is not None:
                            mesh_terms.append((desc.text or "").strip())

            out.append({
                "pmid":       pmid,
                "title":      title,
                "abstract":   abstract,
                "journal":    journal,
                "year":       year,
                "authors":    authors,
                "doi":        doi,
                "mesh_terms": mesh_terms,
            })

    return out


# ---------------------------------------------------------------------------
# GenBank Assembly summary
# ---------------------------------------------------------------------------
def genbank_assembly_summary(taxon: str | int) -> list[dict]:
    """Search the NCBI Assembly db for *taxon* and return assembly metadata.

    Query: `txid{taxon}[Organism] OR {taxon}[Organism]`
    Each returned dict: accession, name, organism, level, seq_length_total,
    contig_n50, submission_date.
    Useful for Pseudoautosomal Region Atlas validation.
    """
    term = f"txid{taxon}[Organism] OR {taxon}[Organism]"
    ids  = _esearch_ids("assembly", term, retmax=200)
    if not ids:
        return []

    results: list[dict] = []
    # ESummary for assembly accepts up to 200 IDs at once
    _BATCH = 200
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        params = {
            **_base_params(),
            "db": "assembly",
            "id": ",".join(batch),
            "retmode": "xml",
            "version": "2.0",
        }
        resp = _get(_ESUMMARY, params)
        _pause()
        if not resp:
            continue
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            continue

        for ds in root.findall(".//DocumentSummary"):
            def _f(tag: str, default: str = "") -> str:  # noqa: E731
                el = ds.find(tag)
                return (el.text or "").strip() if el is not None else default

            # Assembly level (Complete/Chromosome/Scaffold/Contig)
            level_raw = _f("AssemblyStatus")
            level_map = {
                "Complete Genome": "Complete",
                "Chromosome": "Chromosome",
                "Scaffold": "Scaffold",
                "Contig": "Contig",
            }
            level = level_map.get(level_raw, level_raw)

            # total_length lives in the Meta XML blob under
            # <Stat category="total_length" sequence_tag="all">
            seq_length_total = ""
            meta_el = ds.find("Meta")
            if meta_el is not None and meta_el.text:
                try:
                    meta_root = ET.fromstring(f"<root>{meta_el.text}</root>")
                    for stat in meta_root.findall(".//Stat"):
                        if (stat.get("category") == "total_length"
                                and stat.get("sequence_tag") == "all"):
                            seq_length_total = (stat.text or "").strip()
                            break
                except ET.ParseError:
                    pass

            results.append({
                "accession":        _f("AssemblyAccession"),
                "name":             _f("AssemblyName"),
                "organism":         _f("Organism"),
                "level":            level,
                "seq_length_total": seq_length_total,
                "contig_n50":       _f("ContigN50"),
                "submission_date":  _f("SubmissionDate"),
            })

    return results


# ---------------------------------------------------------------------------
# Generic sequence fetch
# ---------------------------------------------------------------------------
def efetch_sequence(
    accession: str,
    db: str = "nucleotide",
    rettype: str = "fasta",
) -> str | None:
    """
    Fetch a sequence record by accession.

    Returns the record text (FASTA / GenBank / etc.) or None.
    """
    params = {**_base_params(), "db": db, "id": accession,
              "rettype": rettype, "retmode": "text"}
    resp = _get(_EFETCH, params)
    _pause()
    if resp and resp.text.strip():
        return resp.text
    return None
