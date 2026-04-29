"""Nightly literature synthesis job — runs daily at midnight Central via APScheduler.

For each active research project (up to 3 per run), queries OpenAlex for recent
papers matching the project's keywords, has Sonnet 4.6 extract findings + assess
relevance, and stores everything in literature_notes.

Run manually to test:
    python -m agent.jobs.nightly_literature_synthesis

Idle threshold: job only runs when idle_class='idle' OR idle_class='deep_idle'.
Cost estimate: 3 projects × 8 papers × 1 Sonnet call = max 24 calls/run ≈ $1.20/night ≈ $36/month.
Drive doc summary: deferred to v2 — SQLite only for v1.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Idle classes that permit this heavy job to run.
_ALLOWED_IDLE_CLASSES = {"idle", "deep_idle"}

# Max projects processed per run (cost bound).
_MAX_PROJECTS = 3

# Papers per keyword query (OpenAlex per_page).
_PAPERS_PER_KW = 3

# Target paper count per project per night (soft cap applied after aggregation).
_MIN_PAPERS = 5
_MAX_PAPERS = 8

# Abstract truncation for Sonnet user message.
_ABSTRACT_MAX_CHARS = 2000

# ---------------------------------------------------------------------------
# Sonnet system prompt for findings extraction
# ---------------------------------------------------------------------------
_EXTRACTION_SYSTEM = (
    "You extract findings from a scientific paper for a research project. "
    "Output JSON: "
    '{"findings_md": "<3-5 bullet points: the paper\'s specific empirical or theoretical claims>", '
    '"relevance": "<2 sentences explaining why this matters to the project\'s hypothesis>", '
    '"should_cite": true|false}. '
    "The project's current hypothesis and keywords are in the user message. "
    "If the paper is off-topic despite a keyword match, say so honestly: findings_md should still "
    "extract the paper's claims, but relevance should say "
    "'weak match — paper is about X, project is about Y'. Output JSON only."
)


# ---------------------------------------------------------------------------
# OpenAlex helpers (same pattern as paper_of_the_day.py)
# ---------------------------------------------------------------------------

def _fetch_openalex_for_kw(keyword: str, days_back: int = 7, per_page: int = 3) -> list[dict]:
    """Fetch recent works from OpenAlex for a single keyword."""
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://api.openalex.org/works"
    params = {
        "filter": f"from_publication_date:{since},title_and_abstract.search:{keyword}",
        "sort": "cited_by_count:desc",
        "per_page": per_page,
        "mailto": "blackmon@tamu.edu",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception:
        return []


def _reconstruct_abstract(inverted: dict) -> str:
    """Reconstruct abstract text from OpenAlex inverted index."""
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


def _parse_work(work: dict) -> dict | None:
    """Extract fields we care about from an OpenAlex work record."""
    try:
        title = work.get("title") or ""
        if not title:
            return None
        doi = work.get("doi") or ""
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        pmid_raw = (work.get("ids") or {}).get("pmid", "")
        pubmed_id = None
        if pmid_raw:
            pubmed_id = pmid_raw.replace("https://pubmed.ncbi.nlm.nih.gov/", "")
        authors_list = work.get("authorships") or []
        authors = "; ".join(
            (a.get("author") or {}).get("display_name") or ""
            for a in authors_list[:6]
        )
        if len(authors_list) > 6:
            authors += " et al."
        journal_src = (work.get("primary_location") or {}).get("source") or {}
        journal_name = journal_src.get("display_name") or ""
        pub_year = work.get("publication_year")
        citations = work.get("cited_by_count") or 0
        oa_url = (work.get("open_access") or {}).get("oa_url") or ""
        abstract = _reconstruct_abstract(work.get("abstract_inverted_index") or {})
        return {
            "doi": doi or None,
            "pubmed_id": pubmed_id,
            "title": title,
            "authors": authors or None,
            "journal": journal_name or None,
            "publication_year": pub_year,
            "open_access_url": oa_url or None,
            "citations_count": citations,
            "raw_abstract": abstract or None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Already-in-db check helpers
# ---------------------------------------------------------------------------

def _get_existing_dois(conn: sqlite3.Connection, project_id: str) -> set[str]:
    """Return set of DOIs already stored in literature_notes for this project."""
    try:
        rows = conn.execute(
            "SELECT doi FROM literature_notes WHERE project_id=? AND doi IS NOT NULL",
            (project_id,),
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Sonnet extraction call
# ---------------------------------------------------------------------------

def _extract_findings(
    client: Anthropic,
    project_name: str,
    hypothesis: str,
    keywords: list[str],
    paper: dict,
):
    """Call Sonnet 4.6 to extract findings and assess relevance for one paper.

    Returns (parsed_dict | None, msg_object | None).
    msg_object is the raw Anthropic response for cost/usage logging.
    """
    abstract_trunc = (paper["raw_abstract"] or "")[:_ABSTRACT_MAX_CHARS]
    user_msg = (
        f"Project: {project_name}\n"
        f"Hypothesis: {hypothesis or '(not specified)'}\n"
        f"Keywords: {', '.join(keywords)}\n\n"
        f"Paper title: {paper['title']}\n"
        f"Authors: {paper['authors'] or 'unknown'}\n"
        f"Abstract:\n{abstract_trunc or '(no abstract available)'}"
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if Sonnet wraps with ```json
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw), msg
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("nightly_literature_synthesis")
def job() -> str:
    """For each active research project, pull recent papers and extract findings."""

    # 1. Time guard only — scheduled midnight Central. Idle-class was too strict.
    from datetime import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = _dt.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22:
        return f"skipped: working-hours guard (hour={hour})"

    # 2. Pull active research projects.
    projects = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        # Check table exists
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='research_projects'"
        ).fetchone()
        if not tbl:
            conn.close()
            return "no_active_projects"
        rows = conn.execute(
            "SELECT id, name, current_hypothesis, keywords "
            "FROM research_projects WHERE status='active' "
            "ORDER BY last_touched_iso ASC NULLS FIRST "
            "LIMIT ?",
            (_MAX_PROJECTS,),
        ).fetchall()
        conn.close()
        projects = rows
    except Exception as e:
        return f"error loading projects: {e}"

    if not projects:
        return "no_active_projects"

    # 3. Process each project.
    client = Anthropic()
    total_papers_inserted = 0

    for proj_row in projects:
        proj_id, proj_name, hypothesis, kw_raw = proj_row
        if kw_raw and kw_raw.strip():
            # Prefer per-project keywords column for precise retrieval
            keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
        else:
            # Fallback: use hypothesis as a single broad query term
            keywords = [hypothesis.strip()] if hypothesis and hypothesis.strip() else []
        if not keywords:
            continue

        # 3a. Query OpenAlex for each keyword.
        candidate_papers: list[dict] = []
        seen_dois: set[str] = set()

        for kw in keywords:
            try:
                works = _fetch_openalex_for_kw(kw, days_back=7, per_page=_PAPERS_PER_KW)
                for work in works:
                    paper = _parse_work(work)
                    if paper is None:
                        continue
                    dedup_key = paper["doi"] or paper["title"]
                    if dedup_key in seen_dois:
                        continue
                    seen_dois.add(dedup_key)
                    candidate_papers.append(paper)
            except Exception:
                continue  # one bad keyword must not abort the project

        if not candidate_papers:
            continue

        # 3b. Drop papers already in literature_notes for this project.
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            existing_dois = _get_existing_dois(conn, proj_id)
            conn.close()
        except Exception:
            existing_dois = set()

        new_papers = [
            p for p in candidate_papers
            if not (p["doi"] and p["doi"] in existing_dois)
        ]

        # 3c. Cap at 5-8 papers per project per night.
        new_papers = new_papers[:_MAX_PAPERS]
        if not new_papers:
            continue

        # 3d. For each paper, call Sonnet and insert.
        papers_this_project = 0
        for paper in new_papers:
            try:
                extraction, extr_msg = _extract_findings(client, proj_name, hypothesis, keywords, paper)
                if extraction is None:
                    findings_md = "(extraction failed)"
                    relevance = "(extraction failed)"
                else:
                    findings_md = extraction.get("findings_md") or "(no findings)"
                    relevance = extraction.get("relevance") or "(no relevance)"

                now_iso = datetime.now(timezone.utc).isoformat()
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """INSERT OR IGNORE INTO literature_notes
                       (project_id, doi, pubmed_id, title, authors, journal,
                        publication_year, open_access_url, citations_count,
                        raw_abstract, extracted_findings_md, relevance_to_project,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        proj_id,
                        paper["doi"],
                        paper["pubmed_id"],
                        paper["title"],
                        paper["authors"],
                        paper["journal"],
                        paper["publication_year"],
                        paper["open_access_url"],
                        paper["citations_count"],
                        paper["raw_abstract"],
                        findings_md,
                        relevance,
                        now_iso,
                    ),
                )
                conn.commit()
                conn.close()
                papers_this_project += 1
                total_papers_inserted += 1

                # Record cost + ledger (no critic per spec)
                try:
                    if extr_msg is not None:
                        _extr_usage = {
                            "input_tokens": getattr(extr_msg.usage, "input_tokens", 0) or 0,
                            "output_tokens": getattr(extr_msg.usage, "output_tokens", 0) or 0,
                            "cache_creation_input_tokens": getattr(extr_msg.usage, "cache_creation_input_tokens", 0) or 0,
                            "cache_read_input_tokens": getattr(extr_msg.usage, "cache_read_input_tokens", 0) or 0,
                        }
                        record_call(
                            job_name="nightly_literature_synthesis",
                            model="claude-sonnet-4-6",
                            usage=_extr_usage,
                        )
                        record_output(
                            kind="literature_synthesis",
                            job_name="nightly_literature_synthesis",
                            model="claude-sonnet-4-6",
                            project_id=proj_id,
                            content_md=findings_md,
                            tokens_in=_extr_usage["input_tokens"],
                            tokens_out=_extr_usage["output_tokens"],
                            provenance={
                                "doi": paper.get("doi"),
                                "title": paper.get("title"),
                                "journal": paper.get("journal"),
                                "publication_year": paper.get("publication_year"),
                                "relevance_to_project": relevance,
                            },
                        )
                except Exception as _e:
                    print(f"[nightly_literature_synthesis] ledger/cost error: {_e}")

            except Exception:
                continue  # per-paper failure must never abort the project

    # 4. Drive doc summary deferred to v2 — SQLite only for v1.
    # (Per spec: not required for v1.)

    # 5. Return summary.
    return f"synthesized: projects={len(projects)} papers={total_papers_inserted}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
