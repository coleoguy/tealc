"""Paper-of-the-day job — runs daily at 6:00am Central via APScheduler.

Searches OpenAlex across Heath's keyword topics, picks the single most
relevant recent paper, has Sonnet write a 5-sentence "why this matters to
Heath" summary, stores it, and creates a briefing so morning_briefing surfaces it.

Run manually to test:
    python -m agent.jobs.paper_of_the_day
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.jobs.morning_briefing import KEYWORDS  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Sonnet system prompt for the 5-sentence summary
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You write a 5-sentence 'why this matters to Heath Blackmon' summary for one paper, "
    "given his research program: genome structure evolution, sex chromosome turnover, "
    "chromosome number / dysploidy, comparative phylogenetics across arthropods/mammals/fish/plants, "
    "the Fragile Y Hypothesis, the chromosomal stasis result. "
    "Heath's career goal is NAS membership; he cares about high-impact framings. "
    "Output ONLY the 5 sentences in markdown. No preamble. "
    "Each sentence: complete, specific, useful. "
    "First sentence: the paper's core claim. "
    "Sentences 2-3: how this connects to or challenges Heath's program. "
    "Sentence 4: a concrete next-step Heath could take (cite, contact author, build on). "
    "Sentence 5: the strategic NAS angle, or 'No immediate NAS angle.' if there isn't one."
)


# ---------------------------------------------------------------------------
# OpenAlex helpers
# ---------------------------------------------------------------------------

def _fetch_openalex(keyword: str, days_back: int = 30, per_page: int = 5) -> list[dict]:
    """Fetch recent works from OpenAlex for a keyword, sorted by citations desc.

    Uses title_and_abstract.search for reliable keyword matching.
    Default window is 30 days — Heath's keywords are specific enough that
    7 days often yields nothing.
    """
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


def _parse_work(work: dict, keyword: str) -> dict | None:
    """Extract the fields we care about from an OpenAlex work record."""
    try:
        title = work.get("title") or ""
        if not title:
            return None
        doi = work.get("doi") or ""
        # DOI may come as full URL; strip to canonical form
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        pubmed_id = None
        for ident in (work.get("ids") or {}).values():
            pass  # ids is a dict; we'll fish out pmid separately
        pmid_raw = (work.get("ids") or {}).get("pmid", "")
        if pmid_raw:
            pubmed_id = pmid_raw.replace("https://pubmed.ncbi.nlm.nih.gov/", "")
        authors_list = work.get("authorships") or []
        authors = "; ".join(
            (a.get("author") or {}).get("display_name") or ""
            for a in authors_list[:6]
        )
        if len(authors_list) > 6:
            authors += " et al."
        journal = (work.get("primary_location") or {}).get("source") or {}
        journal_name = journal.get("display_name") or ""
        pub_year = work.get("publication_year")
        citations = work.get("cited_by_count") or 0
        pub_date_str = work.get("publication_date") or ""
        oa_url = (work.get("open_access") or {}).get("oa_url") or ""
        abstract_inverted = work.get("abstract_inverted_index") or {}
        abstract = _reconstruct_abstract(abstract_inverted)
        return {
            "doi": doi,
            "pubmed_id": pubmed_id,
            "title": title,
            "authors": authors,
            "journal": journal_name,
            "publication_year": pub_year,
            "publication_date": pub_date_str,
            "open_access_url": oa_url,
            "citations_count": citations,
            "raw_abstract": abstract,
            "topic_matched": keyword,
        }
    except Exception:
        return None


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


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("paper_of_the_day")
def job() -> str:
    """Pick the single most relevant new paper from Heath's topics and summarise it."""
    today_iso = datetime.now(timezone.utc).date().isoformat()

    # 1. Check if we already have today's paper
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = conn.execute(
            "SELECT id FROM papers_of_the_day WHERE date_iso=?", (today_iso,)
        ).fetchone()
        conn.close()
        if existing:
            return "already_picked_today"
    except Exception as e:
        # If the table doesn't exist yet, _migrate() will create it below — keep going
        pass

    # 2. Search OpenAlex for each keyword (past 7 days, top 3 each)
    all_papers: list[dict] = []
    seen_dois: set[str] = set()
    for kw in KEYWORDS:
        try:
            works = _fetch_openalex(kw, days_back=30, per_page=5)
            for work in works:
                paper = _parse_work(work, kw)
                if paper is None:
                    continue
                dedup_key = paper["doi"] or paper["title"]
                if dedup_key in seen_dois:
                    continue
                seen_dois.add(dedup_key)
                all_papers.append(paper)
        except Exception:
            continue  # one bad keyword must not abort the job

    if not all_papers:
        return "no_papers_found — OpenAlex returned nothing for today's keywords"

    # 3. Drop papers already seen as paper-of-the-day (by DOI)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        seen_rows = conn.execute(
            "SELECT doi FROM papers_of_the_day WHERE doi IS NOT NULL"
        ).fetchall()
        conn.close()
        seen_potd_dois = {r[0] for r in seen_rows if r[0]}
    except Exception:
        seen_potd_dois = set()

    candidates = [p for p in all_papers if not (p["doi"] and p["doi"] in seen_potd_dois)]
    if not candidates:
        candidates = all_papers  # fallback if everything is already seen

    # 3b. Load project keywords and score candidates by token overlap
    project_keyword_tokens: set[str] = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        kw_rows = conn.execute(
            "SELECT keywords FROM research_projects WHERE status='active' AND keywords IS NOT NULL AND keywords != ''"
        ).fetchall()
        conn.close()
        for (kw_raw,) in kw_rows:
            for term in kw_raw.split(","):
                project_keyword_tokens.update(term.strip().lower().split())
    except Exception:
        pass  # keyword scoring is best-effort; don't break the job

    if project_keyword_tokens:
        def _kw_score(paper: dict) -> int:
            """Count how many project keyword tokens appear in title+abstract."""
            text = ((paper.get("title") or "") + " " + (paper.get("raw_abstract") or "")).lower()
            return sum(1 for tok in project_keyword_tokens if tok in text)
        candidates.sort(key=lambda p: (
            -_kw_score(p),
            -(p["citations_count"] or 0),
            "".join(reversed(p["publication_date"] or "")),
        ))
    # 4. Pick top candidate: highest citations; tiebreak by most recent pub_date
    # (skipped when project_keyword_tokens already sorted above)
    if not project_keyword_tokens:
        candidates.sort(key=lambda p: (
            -(p["citations_count"] or 0),
            "".join(reversed(p["publication_date"] or "")),
        ))
    top = candidates[0]

    # 5. Call Sonnet 4.6 for the 5-sentence summary
    client = Anthropic()
    user_content = (
        f"Title: {top['title']}\n"
        f"Authors: {top['authors']}\n"
        f"Journal: {top['journal']} ({top['publication_year']})\n\n"
        f"Abstract:\n{top['raw_abstract'] or '(no abstract available)'}"
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        why_it_matters_md = msg.content[0].text.strip()
    except Exception as e:
        why_it_matters_md = f"(Summary unavailable: {e})"

    # 6. Insert into papers_of_the_day
    now_iso = datetime.now(timezone.utc).isoformat()
    doi_url = f"https://doi.org/{top['doi']}" if top["doi"] else ""
    link = top["open_access_url"] or doi_url
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT OR IGNORE INTO papers_of_the_day
               (date_iso, doi, pubmed_id, title, authors, journal, publication_year,
                open_access_url, citations_count, raw_abstract, why_it_matters_md,
                topic_matched, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today_iso,
                top["doi"] or None,
                top["pubmed_id"] or None,
                top["title"],
                top["authors"] or None,
                top["journal"] or None,
                top["publication_year"],
                top["open_access_url"] or None,
                top["citations_count"],
                top["raw_abstract"] or None,
                why_it_matters_md,
                top["topic_matched"],
                now_iso,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return f"error inserting paper: {e}"

    # 7. Create a briefing row so morning_briefing surfaces it
    try:
        briefing_title = f"Today's paper: {top['title'][:80]}"
        briefing_content = (
            f"**{top['title']}**\n\n"
            f"Authors: {top['authors']}\n"
            f"Journal: {top['journal']} ({top['publication_year']}) | "
            f"Topic: {top['topic_matched']}\n\n"
            f"{why_it_matters_md}\n\n"
            + (f"[Open access link]({link})" if link else "")
        )
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('paper_of_the_day', 'info', ?, ?, ?)",
            (briefing_title, briefing_content, now_iso),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # briefing failure must never crash the job

    return f"picked: {top['title'][:60]} | journal={top['journal']}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
