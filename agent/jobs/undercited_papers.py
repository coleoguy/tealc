"""Monthly undercited-paper residual analysis.

For each of Heath's published papers:
  observed_citations = OpenAlex citation count
  expected_citations = bucket mean log(citations+1) for papers in the same
                       two-year publication window, optionally tilted by
                       journal h-index when available from OpenAlex.
  residual = log(observed+1) - log(expected+1)

Negative residuals → paper is drawing fewer citations than the age-matched
cohort average → undercited.

Top-5 most-negative residuals receive a single claude-opus-4-7 novelty
classification call ("incremental | consolidating | paradigm-shifting").

Results are persisted to `undercited_residuals` (snapshot per run).
The public helper `get_top_undercited()` is consumed by nas_case_packet.

Cost cap: $0.50/run (5 × ~$0.05 Opus calls + negligible OpenAlex traffic).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths / env
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.ledger import record_output  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_OPUS_MODEL = "claude-opus-4-7"
_MAILTO = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
_COST_CAP_USD = 0.50
_CURRENT_YEAR = datetime.now(timezone.utc).year

# OpenAlex author ID (set via OPENALEX_AUTHOR_ID env var)
_AUTHOR_OA_ID = os.environ.get("OPENALEX_AUTHOR_ID", "A5013786693")

# Authoritative DOI list lives in data/pdf_doi_map.json ("final" key).
_DOI_MAP_PATH = os.path.join(_PROJECT_ROOT, "data", "pdf_doi_map.json")

# ---------------------------------------------------------------------------
# Cost accumulator (per-run)
# ---------------------------------------------------------------------------
_run_cost_usd: float = 0.0

_OPUS_PRICING = {
    "in": 15.0,  # USD / 1M tokens
    "out": 75.0,
}


def _estimate_opus_cost(tokens_in: int, tokens_out: int) -> float:
    return (
        tokens_in * _OPUS_PRICING["in"] / 1_000_000
        + tokens_out * _OPUS_PRICING["out"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Data source: load authoritative DOI list
# ---------------------------------------------------------------------------

def _load_papers() -> list[dict]:
    """Return deduplicated paper records from pdf_doi_map.json 'final' section."""
    with open(_DOI_MAP_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)

    records = raw.get("final") or raw.get("mapping") or []
    seen_doi: set[str] = set()
    papers: list[dict] = []
    for rec in records:
        doi = (rec.get("doi") or "").strip()
        if not doi or doi in seen_doi:
            continue
        seen_doi.add(doi)
        papers.append({
            "paper_id": doi.replace("/", "_").replace(".", "_"),
            "doi": doi,
            "title": rec.get("title") or "",
            "year": int(rec.get("year") or 0),
        })
    return papers


# ---------------------------------------------------------------------------
# OpenAlex fetcher — one GET per DOI, polite 100 ms pause
# ---------------------------------------------------------------------------

def _fetch_oa_work(doi: str) -> dict | None:
    """Fetch OpenAlex work metadata for a DOI. Returns None on any failure."""
    import urllib.request
    import urllib.error

    url = f"https://api.openalex.org/works/doi:{doi}?mailto={_MAILTO}&select=id,title,publication_year,cited_by_count,primary_location"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"[undercited] OA HTTP {e.code} for {doi}")
        return None
    except Exception as e:
        print(f"[undercited] OA fetch error for {doi}: {e}")
        return None


def _fetch_journal_h_index(source_id: str) -> int | None:
    """Fetch h-index for an OpenAlex source (journal). Returns None on failure."""
    if not source_id:
        return None
    import urllib.request
    import urllib.error

    source_raw = source_id.replace("https://openalex.org/", "")
    url = f"https://api.openalex.org/sources/{source_raw}?mailto={_MAILTO}&select=h_index"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("h_index")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Enrich each paper record with OA data
# ---------------------------------------------------------------------------

def _enrich_papers(papers: list[dict], limit: int | None = None) -> list[dict]:
    """Add observed_citations, journal_h_index to each paper record."""
    subset = papers[:limit] if limit else papers
    enriched: list[dict] = []
    for i, p in enumerate(subset):
        if i > 0:
            time.sleep(0.12)  # polite crawl: ~8 req/s
        work = _fetch_oa_work(p["doi"])
        if work is None:
            obs = 0
            jhi = None
        else:
            obs = work.get("cited_by_count") or 0
            # Update title from OA if local is empty
            if not p["title"] and work.get("title"):
                p = dict(p, title=work.get("title") or "")
            # Update year from OA if local is 0
            if not p["year"] and work.get("publication_year"):
                p = dict(p, year=int(work.get("publication_year") or 0))
            # Attempt journal h-index
            loc = work.get("primary_location") or {}
            source = loc.get("source") or {}
            source_id = source.get("id") or ""
            jhi = _fetch_journal_h_index(source_id) if source_id else None

        enriched.append({
            **p,
            "observed_citations": obs,
            "journal_h_index": jhi,
        })
    return enriched


# ---------------------------------------------------------------------------
# Residual model — bucket log-linear baseline
# ---------------------------------------------------------------------------

def _compute_residuals(papers: list[dict]) -> list[dict]:
    """Add expected_citations and residual to each paper dict.

    Strategy:
      1. Group papers by 2-year publication bucket (floor to even year).
      2. expected[paper] = exp(mean_log_citations_in_bucket) - 1
         where log_citations = log(observed+1).
      3. Adjust by journal_h_index relative to bucket mean h-index when available:
         delta_h = (paper_h - bucket_mean_h) / max(bucket_std_h, 1)
         expected adjusted = expected * exp(0.05 * delta_h)
         (coefficient 0.05 keeps the h-index adjustment mild for N=63.)
      4. residual = log(observed+1) - log(expected+1)
    """
    # --- bucket assignment ---
    def _bucket(year: int) -> int:
        return (year // 2) * 2  # e.g. 2015, 2016 → 2014; 2017, 2018 → 2016

    # Build bucket statistics
    from collections import defaultdict
    bucket_log_cits: dict[int, list[float]] = defaultdict(list)
    bucket_h: dict[int, list[float]] = defaultdict(list)

    for p in papers:
        bk = _bucket(p.get("year") or _CURRENT_YEAR)
        bucket_log_cits[bk].append(math.log(p["observed_citations"] + 1))
        h = p.get("journal_h_index")
        if h is not None:
            bucket_h[bk].append(float(h))

    # Compute means/stds per bucket
    bucket_mean: dict[int, float] = {}
    bucket_mean_h: dict[int, float] = {}
    bucket_std_h: dict[int, float] = {}

    for bk, lcs in bucket_log_cits.items():
        bucket_mean[bk] = sum(lcs) / len(lcs)

    for bk, hs in bucket_h.items():
        bucket_mean_h[bk] = sum(hs) / len(hs)
        if len(hs) > 1:
            var = sum((x - bucket_mean_h[bk]) ** 2 for x in hs) / len(hs)
            bucket_std_h[bk] = math.sqrt(var)
        else:
            bucket_std_h[bk] = 1.0

    # Fall back to global mean if a bucket has <2 members
    global_mean_log = sum(v for vals in bucket_log_cits.values() for v in vals) / max(
        sum(len(v) for v in bucket_log_cits.values()), 1
    )

    result: list[dict] = []
    for p in papers:
        bk = _bucket(p.get("year") or _CURRENT_YEAR)
        bm = bucket_mean.get(bk, global_mean_log)

        # h-index tilt
        h_tilt = 0.0
        h = p.get("journal_h_index")
        if h is not None and bk in bucket_mean_h:
            delta_h = (h - bucket_mean_h[bk]) / max(bucket_std_h.get(bk, 1.0), 1.0)
            h_tilt = 0.05 * delta_h

        adjusted_expected_log = bm + h_tilt
        expected_cit = math.exp(adjusted_expected_log) - 1  # back-transform

        obs = p["observed_citations"]
        residual = math.log(obs + 1) - math.log(expected_cit + 1)

        result.append({
            **p,
            "expected_citations": round(expected_cit, 2),
            "residual": round(residual, 4),
        })

    # Sort ascending by residual (most negative first)
    result.sort(key=lambda x: x["residual"])
    return result


# ---------------------------------------------------------------------------
# Opus novelty classifier
# ---------------------------------------------------------------------------

_NOVELTY_SYSTEM = """\
You are a bibliometrician classifying the novelty of a scientific paper by its title and DOI.
Reply with EXACTLY one line in this format (no other text):

CLASS: <class> | RATIONALE: <one sentence>

Where <class> is one of:
  incremental      — extends existing methods/findings in a well-established framework
  consolidating    — synthesises, databases, or reviews an existing body of work
  paradigm-shifting — introduces a new framework, mechanism, or method that redirects a field

Base your judgment only on the paper title and what is known about the field from your training data."""


def _classify_novelty(paper: dict, client: Anthropic) -> tuple[str, str]:
    """Return (novelty_class, rationale). On parse or API failure returns ('unknown','')."""
    global _run_cost_usd

    prompt = f"Paper title: {paper['title']}\nDOI: {paper['doi']}\nYear: {paper.get('year','?')}"
    try:
        resp = client.messages.create(
            model=_OPUS_MODEL,
            max_tokens=120,
            system=_NOVELTY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()

        # Track cost
        call_cost = _estimate_opus_cost(
            resp.usage.input_tokens, resp.usage.output_tokens
        )
        _run_cost_usd += call_cost
        try:
            record_call(
                "undercited_papers",
                _OPUS_MODEL,
                {
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            )
        except Exception:
            pass

        # Parse "CLASS: X | RATIONALE: Y"
        if "CLASS:" in text and "RATIONALE:" in text:
            parts = text.split("|", 1)
            cls_raw = parts[0].replace("CLASS:", "").strip().lower()
            rat = parts[1].replace("RATIONALE:", "").strip() if len(parts) > 1 else ""
            known = {"incremental", "consolidating", "paradigm-shifting"}
            cls = cls_raw if cls_raw in known else "incremental"
            return cls, rat
        # Fallback: first word
        first = text.split()[0].lower() if text else "unknown"
        return first, text
    except Exception as e:
        print(f"[undercited] Opus classify error for {paper['doi']}: {e}")
        return "unknown", ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS undercited_residuals (
            snapshot_iso      TEXT,
            paper_id          TEXT,
            doi               TEXT,
            year              INT,
            observed_citations INT,
            expected_citations REAL,
            residual          REAL,
            novelty_class     TEXT,
            novelty_rationale TEXT,
            title             TEXT,
            PRIMARY KEY(snapshot_iso, paper_id)
        )
    """)
    conn.commit()


def _persist_snapshot(
    conn: sqlite3.Connection,
    snapshot_iso: str,
    papers: list[dict],
) -> None:
    rows = [
        (
            snapshot_iso,
            p["paper_id"],
            p["doi"],
            p.get("year") or 0,
            p["observed_citations"],
            p["expected_citations"],
            p["residual"],
            p.get("novelty_class") or "",
            p.get("novelty_rationale") or "",
            p.get("title") or "",
        )
        for p in papers
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO undercited_residuals
          (snapshot_iso, paper_id, doi, year, observed_citations,
           expected_citations, residual, novelty_class, novelty_rationale, title)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public query helper (for nas_case_packet and chat tools)
# ---------------------------------------------------------------------------

def get_top_undercited(
    snapshot_iso: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Return top N most-undercited papers from the most-recent (or specified) snapshot.

    Each dict: {paper_id, doi, year, observed, expected, residual,
                novelty_class, novelty_rationale, title}
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        if snapshot_iso:
            target_iso = snapshot_iso
        else:
            row = conn.execute(
                "SELECT snapshot_iso FROM undercited_residuals "
                "ORDER BY snapshot_iso DESC LIMIT 1"
            ).fetchone()
            if not row:
                conn.close()
                return []
            target_iso = row[0]

        rows = conn.execute(
            """
            SELECT paper_id, doi, year, observed_citations, expected_citations,
                   residual, novelty_class, novelty_rationale, title
            FROM undercited_residuals
            WHERE snapshot_iso = ?
            ORDER BY residual ASC
            LIMIT ?
            """,
            (target_iso, limit),
        ).fetchall()
        conn.close()

        return [
            {
                "paper_id": r[0],
                "doi": r[1],
                "year": r[2],
                "observed": r[3],
                "expected": r[4],
                "residual": r[5],
                "novelty_class": r[6],
                "novelty_rationale": r[7],
                "title": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[undercited] get_top_undercited error: {e}")
        return []


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("undercited_papers")
def job(
    _smoke_limit: int | None = None,
    _opus_cap: int = 5,
) -> str:
    """Monthly residual analysis — flag Heath's most-undercited papers.

    Args:
        _smoke_limit: If set, only fetch this many papers (smoke-test mode).
        _opus_cap: Max Opus novelty-classification calls (default 5).
    """
    global _run_cost_usd
    _run_cost_usd = 0.0

    snapshot_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # 1. Load paper list
    # ------------------------------------------------------------------
    try:
        papers = _load_papers()
    except Exception as e:
        return f"undercited_papers: failed to load DOI map: {e}"

    if not papers:
        return "undercited_papers: no papers found in DOI map"

    total_count = len(papers)

    # ------------------------------------------------------------------
    # 2. Enrich via OpenAlex
    # ------------------------------------------------------------------
    enriched = _enrich_papers(papers, limit=_smoke_limit)
    fetched = len(enriched)
    print(f"[undercited] fetched {fetched}/{total_count} papers from OpenAlex")

    # ------------------------------------------------------------------
    # 3. Compute residuals
    # ------------------------------------------------------------------
    with_residuals = _compute_residuals(enriched)

    # Print summary for smoke-test visibility
    print("[undercited] residuals (most negative first):")
    for p in with_residuals[:10]:
        print(
            f"  [{p['residual']:+.3f}] {p['title'][:60]!r} "
            f"({p.get('year','?')}) obs={p['observed_citations']} "
            f"exp={p['expected_citations']:.1f}"
        )

    # ------------------------------------------------------------------
    # 4. Opus novelty classification for top-N most negative
    # ------------------------------------------------------------------
    client = Anthropic()
    top_n = min(_opus_cap, 5)
    undercited_top = [p for p in with_residuals if p["residual"] < 0][:top_n]

    for i, p in enumerate(undercited_top):
        if _run_cost_usd >= _COST_CAP_USD:
            print(f"[undercited] cost cap ${_COST_CAP_USD} reached, stopping Opus calls")
            break
        if i > 0:
            time.sleep(0.3)
        cls, rat = _classify_novelty(p, client)
        p["novelty_class"] = cls
        p["novelty_rationale"] = rat
        print(f"[undercited] Opus: {p['title'][:50]!r} → {cls}")

    # Merge novelty back into full list (keyed by doi)
    novelty_map = {p["doi"]: (p.get("novelty_class", ""), p.get("novelty_rationale", ""))
                   for p in undercited_top}
    for p in with_residuals:
        if p["doi"] in novelty_map:
            p["novelty_class"], p["novelty_rationale"] = novelty_map[p["doi"]]
        else:
            p.setdefault("novelty_class", "")
            p.setdefault("novelty_rationale", "")

    # ------------------------------------------------------------------
    # 5. Persist to DB
    # ------------------------------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_table(conn)
        _persist_snapshot(conn, snapshot_iso, with_residuals)
        conn.close()
    except Exception as e:
        print(f"[undercited] DB persist error (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 6. Record to output_ledger (summary only, no narrative)
    # ------------------------------------------------------------------
    top5_text = "\n".join(
        f"- residual={p['residual']:+.3f}  {p['title'][:80]}  ({p.get('year','?')})"
        for p in with_residuals[:5]
    )
    summary_md = (
        f"# Undercited papers — {snapshot_iso[:10]}\n\n"
        f"Analysed {fetched} papers. "
        f"Total Opus cost: ${_run_cost_usd:.3f}\n\n"
        f"**Top 5 most undercited:**\n{top5_text}"
    )
    try:
        record_output(
            kind="undercited_papers",
            job_name="undercited_papers",
            model=_OPUS_MODEL,
            project_id=None,
            content_md=summary_md,
            tokens_in=0,
            tokens_out=0,
            provenance={"snapshot_iso": snapshot_iso, "papers_analysed": fetched},
        )
    except Exception as e:
        print(f"[undercited] ledger error (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 7. Briefing row
    # ------------------------------------------------------------------
    top1 = with_residuals[0] if with_residuals else {}
    briefing_title = (
        f"Undercited flagship: {top1.get('title','?')[:60]} "
        f"(residual={top1.get('residual',0):+.2f})"
        if top1 else "Undercited analysis complete"
    )
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('undercited_papers', 'info', ?, ?, ?)",
            (briefing_title, summary_md, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[undercited] briefing insert error (non-fatal): {e}")

    return (
        f"undercited_papers: {fetched} papers analysed, "
        f"snapshot={snapshot_iso}, "
        f"top_undercited={top1.get('title','?')[:40]!r} "
        f"(residual={top1.get('residual',0):+.3f}), "
        f"opus_cost=${_run_cost_usd:.3f}"
    )


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Limit to 5 papers, cap Opus at 2 calls
    result = job(_smoke_limit=5, _opus_cap=2)
    print("\n=== job() result ===")
    print(result)
    print("\n=== get_top_undercited(limit=3) ===")
    top = get_top_undercited(limit=3)
    for t in top:
        print(t)
