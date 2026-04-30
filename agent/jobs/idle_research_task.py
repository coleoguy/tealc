"""Idle research task — hourly opportunistic research during the researcher's downtime.

Runs every hour from 8am-7pm CT via APScheduler. An internal idle-gate skips
the run if the researcher is active or in-chat. When idle/deep_idle/away, picks ONE small
task from a five-item menu, executes it, and surfaces the result in the Inbox
as an 'idle_research' briefing + an output_ledger entry.

Menu (round-robin via data/idle_research_task_state.json):
  0  preprint_scout           ~$0.05   ~8 min
  1  self_citation_prospector ~$0.15  ~12 min
  2  hypothesis_stress_test   ~$0.20  ~10 min
  3  undercited_paper_memo    ~$0.05   ~8 min
  4  method_scout             ~$0.10  ~10 min

Per-run cost cap: $0.30 — task is skipped with a logged warning if its
cost estimate exceeds the cap.

Run manually / smoke-test:
    python -m agent.jobs.idle_research_task --force
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output, update_critic  # noqa: E402
from agent.critic import critic_pass  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

log = logging.getLogger("tealc.idle_research_task")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
_STATE_PATH = os.path.join(_DATA_DIR, "idle_research_task_state.json")
_COST_CAP_USD = 0.30

_TASK_MENU = [
    "preprint_scout",
    "self_citation_prospector",
    "hypothesis_stress_test",
    "undercited_paper_memo",
    "method_scout",
]

_TASK_COST_ESTIMATES = {
    "preprint_scout":           0.05,
    "self_citation_prospector": 0.15,
    "hypothesis_stress_test":   0.20,
    "undercited_paper_memo":    0.05,
    "method_scout":             0.10,
}

# Haiku is used for cheap scoring calls; Sonnet for memo drafting.
_HAIKU_MODEL  = "claude-haiku-4-5-20251001"
_SONNET_MODEL = "claude-sonnet-4-6"

# the researcher's keyword set for preprint scouting
_HEATH_KEYWORDS = [
    "sex chromosome",
    "karyotype evolution",
    "chromosome number evolution",
    "comparative genomics beetle",
    "fragile Y hypothesis",
    "sex chromosome turnover",
    "dysploidy",
    "chromosomal stasis",
    "Coleoptera genome",
    "AI for science phylogenetics",
]

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Load persisted round-robin state from disk."""
    if os.path.exists(_STATE_PATH):
        try:
            with open(_STATE_PATH) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"next_index": 0}


def _save_state(state: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Idle-gate
# ---------------------------------------------------------------------------

def _get_idle_class(conn: sqlite3.Connection) -> str:
    """Return the researcher's current idle_class from current_context.

    Falls back to 'idle' (permissive) when the table is empty so the job
    still runs if refresh_context hasn't fired yet.
    """
    try:
        row = conn.execute(
            "SELECT idle_class FROM current_context WHERE id=1"
        ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    # Fallback: derive from last_seen_state.json if DB not ready
    seen_path = os.path.join(_DATA_DIR, "last_seen_state.json")
    try:
        with open(seen_path) as fh:
            state = json.load(fh)
        # last_seen_state may carry a timestamp or class directly
        lsc = state.get("idle_class") or state.get("class")
        if lsc:
            return lsc
    except Exception:
        pass
    return "idle"  # safe default — permits execution


# ---------------------------------------------------------------------------
# Briefing writer
# ---------------------------------------------------------------------------

def _write_briefing(conn: sqlite3.Connection, title: str, content_md: str) -> int:
    """Insert an idle_research briefing row and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO briefings (kind, urgency, title, content_md, created_at)
           VALUES ('idle_research', 'low', ?, ?, ?)""",
        (title, content_md, now),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Task 0: preprint_scout
# ---------------------------------------------------------------------------

def _task_preprint_scout(client: Anthropic, conn: sqlite3.Connection) -> dict:
    """Search bioRxiv for recent preprints; score top-5 via Haiku; memo top-1.

    Tries a 7-day window first (bioRxiv often lags 1-2 days in indexing).
    Falls back to 30 days if the 7-day window is empty.
    """
    from datetime import timedelta
    import requests

    cost_estimate = _TASK_COST_ESTIMATES["preprint_scout"]

    def _fetch_window(days_back: int, max_pages: int = 10) -> list[dict]:
        """Return deduplicated keyword-matching papers for the given window.

        bioRxiv /details returns 30 papers per page; we paginate with cursor=0,30,60...
        to scan up to max_pages * 30 preprints from the window.
        """
        end_date  = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        end_str   = end_date.strftime("%Y-%m-%d")
        start_str = start_date.strftime("%Y-%m-%d")

        # Build a combined lowercase keyword set for one-pass matching
        kw_set = [kw.lower() for kw in _HEATH_KEYWORDS]

        papers: list[dict] = []
        seen: set[str] = set()

        for page_num in range(max_pages):
            cursor = page_num * 30
            try:
                resp = requests.get(
                    f"https://api.biorxiv.org/details/biorxiv/{start_str}/{end_str}/{cursor}/json",
                    timeout=15,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                collection = data.get("collection", [])
                if not collection:
                    break  # no more pages

                for p in collection:
                    doi = p.get("doi") or ""
                    if doi in seen:
                        continue
                    haystack = ((p.get("title") or "") + " " + (p.get("abstract") or "")).lower()
                    if any(kw in haystack for kw in kw_set):
                        seen.add(doi)
                        papers.append(p)

                # Check if more pages exist
                total = 0
                msgs = data.get("messages", [])
                if msgs and isinstance(msgs, list) and msgs:
                    total = int(msgs[0].get("total", 0) or 0)
                if cursor + 30 >= total:
                    break
            except Exception as exc:
                log.warning("preprint_scout: bioRxiv page %d failed: %s", page_num, exc)
                break
        return papers

    all_papers = _fetch_window(7)
    window_label = "7 days"
    if not all_papers:
        all_papers = _fetch_window(30, max_pages=20)
        window_label = "30 days"

    if not all_papers:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "preprint_scout: no matching preprints found (7d + 30d windows empty)",
            "cost_estimate": 0.0,
        }

    # Trim to top-5 by date (newest first), then score each via Haiku
    candidates = sorted(all_papers, key=lambda x: x.get("date", ""), reverse=True)[:5]

    scoring_prompt = (
        "You are helping the researcher, a biology PI specialising in sex-chromosome "
        "evolution, karyotype evolution, Coleoptera genomics, and AI for science.\n\n"
        "For each preprint below, reply with a JSON array of objects with fields:\n"
        '  doi, score (0-10 for "researcher should cite / track"), one_sentence_reason\n\n'
        "Preprints:\n"
    )
    for i, p in enumerate(candidates, 1):
        scoring_prompt += (
            f"\n{i}. Title: {p.get('title','N/A')}\n"
            f"   Authors: {p.get('authors','N/A')}\n"
            f"   Abstract (200 chars): {(p.get('abstract') or '')[:200]}\n"
            f"   DOI: {p.get('doi','N/A')}\n"
        )
    scoring_prompt += "\nReturn ONLY the JSON array."

    score_resp = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": scoring_prompt}],
    )
    usage = score_resp.usage
    record_call(
        job_name="idle_research_task.preprint_scout",
        model=_HAIKU_MODEL,
        usage=usage.model_dump() if hasattr(usage, "model_dump") else dict(usage),
    )

    raw_scores = score_resp.content[0].text.strip()
    if raw_scores.startswith("```"):
        raw_scores = "\n".join(
            l for l in raw_scores.splitlines() if not l.startswith("```")
        ).strip()

    try:
        scored = json.loads(raw_scores)
    except Exception:
        scored = []

    if not scored:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "preprint_scout: Haiku scoring returned no parseable JSON",
            "cost_estimate": cost_estimate,
        }

    top = max(scored, key=lambda x: x.get("score", 0) if isinstance(x, dict) else 0)
    top_doi = top.get("doi", "")
    top_score = top.get("score", 0)
    top_reason = top.get("one_sentence_reason", "")

    # Find full record for top-1
    top_paper = next(
        (p for p in candidates if p.get("doi") == top_doi), candidates[0]
    )

    # Write 200-word memo via Sonnet
    memo_prompt = (
        f"Write a concise 200-word research memo for the researcher summarising "
        f"the following preprint and why it is relevant to his work on sex chromosomes, "
        f"karyotype evolution, and Coleoptera genomics. Be factual, hedge appropriately, "
        f"no hype.\n\n"
        f"Title: {top_paper.get('title','N/A')}\n"
        f"Authors: {top_paper.get('authors','N/A')}\n"
        f"Date: {top_paper.get('date','N/A')}\n"
        f"DOI: {top_doi}\n"
        f"Abstract: {(top_paper.get('abstract') or '')[:600]}\n\n"
        f"Relevance score: {top_score}/10\n"
        f"Relevance reason: {top_reason}"
    )

    memo_resp = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": memo_prompt}],
    )
    memo_usage = memo_resp.usage
    record_call(
        job_name="idle_research_task.preprint_scout",
        model=_SONNET_MODEL,
        usage=memo_usage.model_dump() if hasattr(memo_usage, "model_dump") else dict(memo_usage),
    )
    memo_text = memo_resp.content[0].text.strip()

    content_md = (
        f"## Preprint Scout — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"**Top hit (score {top_score}/10):** {top_paper.get('title','N/A')}\n\n"
        f"**Authors:** {top_paper.get('authors','N/A')}\n\n"
        f"**DOI:** {top_doi}\n\n"
        f"**Memo:**\n\n{memo_text}\n\n"
        f"---\n*{len(candidates)} candidates scanned from bioRxiv (last {window_label})*"
    )

    ledger_id = record_output(
        kind="preprint_scout",
        job_name="idle_research_task",
        model=_SONNET_MODEL,
        project_id=None,
        content_md=content_md,
        tokens_in=getattr(memo_usage, "input_tokens", 0),
        tokens_out=getattr(memo_usage, "output_tokens", 0),
        provenance={"doi": top_doi, "score": top_score, "candidates_count": len(candidates)},
    )

    # Critic pass
    try:
        crit = critic_pass(memo_text, rubric_name="analysis")
        update_critic(ledger_id, crit.get("score", 0), crit.get("overall_notes", ""), crit.get("model", ""))
    except Exception as exc:
        log.warning("preprint_scout: critic pass failed: %s", exc)

    briefing_title = f"New preprint ({top_score}/10): {top_paper.get('title','')[:80]}"
    briefing_id = _write_briefing(conn, briefing_title, content_md)

    return {
        "ok": True,
        "ledger_id": ledger_id,
        "briefing_id": briefing_id,
        "summary": f"preprint_scout: {top_doi!r} score={top_score}",
        "cost_estimate": cost_estimate,
    }


# ---------------------------------------------------------------------------
# Task 1: self_citation_prospector
# ---------------------------------------------------------------------------

def _task_self_citation_prospector(client: Anthropic, conn: sqlite3.Connection) -> dict:
    """Scan most-recent draft via citation_suggester; write top-3 briefing."""
    cost_estimate = _TASK_COST_ESTIMATES["self_citation_prospector"]

    # Graceful degradation if Foundation not ready
    try:
        from agent.citation_suggester import suggest_citations, is_foundation_ready  # noqa: PLC0415
    except ImportError:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "self_citation_prospector: citation_suggester import failed",
            "cost_estimate": 0.0,
        }

    try:
        ready = is_foundation_ready()
    except Exception:
        ready = False

    if not ready:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "self_citation_prospector: Tier 2 Foundation not ready — skipped",
            "cost_estimate": 0.0,
        }

    # Find most recent draft from overnight_drafts or output_ledger
    draft_text: str | None = None
    draft_label = "unknown draft"

    try:
        row = conn.execute(
            "SELECT source_artifact_title, draft_doc_id FROM overnight_drafts "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row:
            draft_label = row[0] or "recent grant draft"
            # Try to read from output_ledger using grant_draft kind
    except Exception:
        pass

    # Fall back to most recent grant_draft in output_ledger
    if not draft_text:
        try:
            row = conn.execute(
                "SELECT content_md FROM output_ledger WHERE kind='grant_draft' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                draft_text = row[0]
                draft_label = "most recent grant draft"
        except Exception:
            pass

    if not draft_text:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "self_citation_prospector: no draft found in overnight_drafts or output_ledger",
            "cost_estimate": 0.0,
        }

    try:
        suggestions = suggest_citations(draft_text[:4000])
    except Exception as exc:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": f"self_citation_prospector: suggest_citations error: {exc}",
            "cost_estimate": 0.0,
        }

    # Flatten suggestions to a list
    items = []
    if isinstance(suggestions, dict):
        for sent, hits in list(suggestions.items())[:10]:
            for h in (hits or [])[:2]:
                items.append({"sentence": sent, "hit": h})
    elif isinstance(suggestions, list):
        items = suggestions[:6]

    if not items:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "self_citation_prospector: no self-cite suggestions found",
            "cost_estimate": 0.0,
        }

    top3 = items[:3]

    # Write briefing prose via Haiku
    briefing_input = (
        "Write a short briefing (3 bullet points, max 200 words total) for "
        "the researcher listing three places in his recent draft where he should "
        "consider citing his own prior work. Format as:\n"
        "- **Claim in draft**: ...\n  **Suggested self-cite**: ...\n  **Why**: ...\n\n"
        f"Draft label: {draft_label}\n\n"
        "Suggestions:\n" +
        "\n".join(
            f"- Draft sentence: {item.get('sentence', '')[:120]}\n"
            f"  Matching prior paper passage: {json.dumps(item.get('hit', {}))[:200]}"
            for item in top3
        )
    )

    prose_resp = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": briefing_input}],
    )
    prose_usage = prose_resp.usage
    record_call(
        job_name="idle_research_task.self_citation_prospector",
        model=_HAIKU_MODEL,
        usage=prose_usage.model_dump() if hasattr(prose_usage, "model_dump") else dict(prose_usage),
    )
    prose_text = prose_resp.content[0].text.strip()

    content_md = (
        f"## Self-Citation Prospector — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"**Draft:** {draft_label}\n\n"
        f"{prose_text}\n\n"
        f"---\n*{len(items)} suggestions scanned; top 3 shown*"
    )

    ledger_id = record_output(
        kind="self_citation_prospector",
        job_name="idle_research_task",
        model=_HAIKU_MODEL,
        project_id=None,
        content_md=content_md,
        tokens_in=getattr(prose_usage, "input_tokens", 0),
        tokens_out=getattr(prose_usage, "output_tokens", 0),
        provenance={"draft_label": draft_label, "suggestions_count": len(items)},
    )

    briefing_title = f"Self-cite opportunities in {draft_label[:60]}"
    briefing_id = _write_briefing(conn, briefing_title, content_md)

    return {
        "ok": True,
        "ledger_id": ledger_id,
        "briefing_id": briefing_id,
        "summary": f"self_citation_prospector: {len(top3)} suggestions for '{draft_label}'",
        "cost_estimate": cost_estimate,
    }


# ---------------------------------------------------------------------------
# Task 2: hypothesis_stress_test
# ---------------------------------------------------------------------------

def _task_hypothesis_stress_test(client: Anthropic, conn: sqlite3.Connection) -> dict:
    """Pick one unregistered hypothesis; stress-test against 2024+ literature."""
    cost_estimate = _TASK_COST_ESTIMATES["hypothesis_stress_test"]

    try:
        row = conn.execute(
            "SELECT id, project_id, hypothesis_md, rationale_md, novelty_score "
            "FROM hypothesis_proposals "
            "WHERE prereg_published_at IS NULL "
            "  AND (adopted_at IS NULL OR adopted_at = '') "
            "  AND novelty_score >= 3 "
            "ORDER BY proposed_iso DESC LIMIT 1"
        ).fetchone()
    except Exception as exc:
        # Table may lack adopted_at column yet — try simpler query
        try:
            row = conn.execute(
                "SELECT id, project_id, hypothesis_md, rationale_md, novelty_score "
                "FROM hypothesis_proposals "
                "WHERE prereg_published_at IS NULL AND novelty_score >= 3 "
                "ORDER BY proposed_iso DESC LIMIT 1"
            ).fetchone()
        except Exception as exc2:
            return {
                "ok": False,
                "ledger_id": None,
                "briefing_id": None,
                "summary": f"hypothesis_stress_test: DB query failed: {exc2}",
                "cost_estimate": 0.0,
            }

    if not row:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "hypothesis_stress_test: no eligible hypothesis found",
            "cost_estimate": 0.0,
        }

    hyp_id, project_id, hypothesis_md, rationale_md, novelty_score = row

    # Spawn subagent to search 2024+ literature
    try:
        from agent.subagents import run_subagent  # noqa: PLC0415
    except ImportError:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "hypothesis_stress_test: subagents module not available",
            "cost_estimate": 0.0,
        }

    subagent_task = (
        "Search 2024-2026 scientific literature and return a verdict on the following "
        "research hypothesis. Use web_search and search_pubmed. "
        "Return a structured answer with: VERDICT (supports / refutes / refines), "
        "KEY PAPERS (up to 3, with title + DOI), and REASONING (2-3 sentences).\n\n"
        f"HYPOTHESIS:\n{hypothesis_md[:800]}\n\n"
        f"RATIONALE:\n{(rationale_md or '')[:400]}"
    )

    subagent_result = run_subagent(
        subagent_task,
        model=_SONNET_MODEL,
        max_steps=6,
        allowed_tools=["web_search", "search_pubmed", "search_biorxiv", "fetch_url"],
    )

    # Write 200-word memo via Haiku
    memo_prompt = (
        "Write a concise 200-word 'stress-test memo' for the researcher. "
        "State whether this hypothesis still holds, was refined, or was refuted by "
        "recent 2024+ literature. Be scientific, hedge appropriately, no hype.\n\n"
        f"Original hypothesis: {hypothesis_md[:400]}\n\n"
        f"Literature search result:\n{subagent_result[:1200]}"
    )

    memo_resp = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": memo_prompt}],
    )
    memo_usage = memo_resp.usage
    record_call(
        job_name="idle_research_task.hypothesis_stress_test",
        model=_HAIKU_MODEL,
        usage=memo_usage.model_dump() if hasattr(memo_usage, "model_dump") else dict(memo_usage),
    )
    memo_text = memo_resp.content[0].text.strip()

    content_md = (
        f"## Hypothesis Stress-Test — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"**Hypothesis ID:** {hyp_id} | **Novelty score:** {novelty_score}\n\n"
        f"**Hypothesis:** {hypothesis_md[:300]}\n\n"
        f"**Verdict memo:**\n\n{memo_text}\n\n"
        f"<details><summary>Raw subagent search result</summary>\n\n"
        f"{subagent_result[:800]}\n\n</details>"
    )

    ledger_id = record_output(
        kind="stress_test",
        job_name="idle_research_task",
        model=_HAIKU_MODEL,
        project_id=project_id,
        content_md=content_md,
        tokens_in=getattr(memo_usage, "input_tokens", 0),
        tokens_out=getattr(memo_usage, "output_tokens", 0),
        provenance={"hypothesis_id": hyp_id, "novelty_score": novelty_score},
    )

    # Critic pass with hypothesis rubric
    try:
        crit = critic_pass(memo_text, rubric_name="hypothesis")
        update_critic(ledger_id, crit.get("score", 0), crit.get("overall_notes", ""), crit.get("model", ""))
    except Exception as exc:
        log.warning("hypothesis_stress_test: critic pass failed: %s", exc)

    briefing_title = f"Stress-test hyp #{hyp_id}: {hypothesis_md[:60]}..."
    briefing_id = _write_briefing(conn, briefing_title, content_md)

    return {
        "ok": True,
        "ledger_id": ledger_id,
        "briefing_id": briefing_id,
        "summary": f"hypothesis_stress_test: hyp_id={hyp_id}",
        "cost_estimate": cost_estimate,
    }


# ---------------------------------------------------------------------------
# Task 3: undercited_paper_memo
# ---------------------------------------------------------------------------

def _task_undercited_paper_memo(client: Anthropic, conn: sqlite3.Connection) -> dict:
    """Pull top undercited paper; write 200-word attention memo via Sonnet."""
    cost_estimate = _TASK_COST_ESTIMATES["undercited_paper_memo"]

    try:
        from agent.jobs.undercited_papers import get_top_undercited  # noqa: PLC0415
        papers = get_top_undercited(limit=1)
    except Exception as exc:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": f"undercited_paper_memo: get_top_undercited failed: {exc}",
            "cost_estimate": 0.0,
        }

    if not papers:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "undercited_paper_memo: no undercited papers found in DB",
            "cost_estimate": 0.0,
        }

    paper = papers[0]
    doi = paper.get("doi") or paper.get("paper_id") or "unknown"
    title = paper.get("title") or "Unknown title"
    year = paper.get("year") or paper.get("publication_year") or ""
    obs = paper.get("observed_citations") or paper.get("citations") or 0
    residual = paper.get("residual") or paper.get("citation_residual") or 0

    memo_prompt = (
        "Write a 200-word memo for the researcher explaining why this paper from "
        "his publication record deserves more attention. Describe the core finding, "
        "the likely reason it is undercited, and one practical step Heath could take "
        "to increase its visibility. Be concrete, hedge appropriately, no hype.\n\n"
        f"Title: {title}\n"
        f"DOI: {doi}\n"
        f"Year: {year}\n"
        f"Observed citations: {obs}\n"
        f"Citation residual (negative = undercited vs peers): {residual:.3f}\n"
    )

    resp = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": memo_prompt}],
    )
    usage = resp.usage
    record_call(
        job_name="idle_research_task.undercited_paper_memo",
        model=_SONNET_MODEL,
        usage=usage.model_dump() if hasattr(usage, "model_dump") else dict(usage),
    )
    memo_text = resp.content[0].text.strip()

    content_md = (
        f"## Undercited Paper Memo — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"**Paper:** {title}\n\n"
        f"**DOI:** {doi} | **Year:** {year} | **Citations:** {obs} "
        f"| **Residual:** {float(residual):.3f}\n\n"
        f"**Why it deserves more attention:**\n\n{memo_text}"
    )

    ledger_id = record_output(
        kind="undercited_memo",
        job_name="idle_research_task",
        model=_SONNET_MODEL,
        project_id=None,
        content_md=content_md,
        tokens_in=getattr(usage, "input_tokens", 0),
        tokens_out=getattr(usage, "output_tokens", 0),
        provenance={"doi": doi, "residual": float(residual), "citations": obs},
    )

    briefing_title = f"Undercited paper: {title[:70]}"
    briefing_id = _write_briefing(conn, briefing_title, content_md)

    return {
        "ok": True,
        "ledger_id": ledger_id,
        "briefing_id": briefing_id,
        "summary": f"undercited_paper_memo: {doi!r} residual={float(residual):.3f}",
        "cost_estimate": cost_estimate,
    }


# ---------------------------------------------------------------------------
# Task 4: method_scout
# ---------------------------------------------------------------------------

def _task_method_scout(client: Anthropic, conn: sqlite3.Connection) -> dict:
    """Find one new R/Python package (last 30 days) relevant to comparative phylogenetics."""
    cost_estimate = _TASK_COST_ESTIMATES["method_scout"]

    try:
        from agent.subagents import run_subagent  # noqa: PLC0415
    except ImportError:
        return {
            "ok": False,
            "ledger_id": None,
            "briefing_id": None,
            "summary": "method_scout: subagents module not available",
            "cost_estimate": 0.0,
        }

    subagent_task = (
        "Find ONE new R or Python package released or significantly updated in the last "
        "30 days that is relevant to comparative phylogenetics, chromosome evolution, "
        "karyotype analysis, or AI-for-science workflows in evolutionary biology. "
        "Search CRAN, Bioconductor, PyPI, GitHub, and bioRxiv preprints. "
        "Return: package name, language, release date, URL, and a 200-word evaluation "
        "of what it does and why the researcher (sex chromosome/karyotype evolution PI) "
        "might find it useful. Be specific about functionality, hedge about maturity."
    )

    eval_text = run_subagent(
        subagent_task,
        model=_SONNET_MODEL,
        max_steps=6,
        allowed_tools=["web_search", "fetch_url", "search_biorxiv", "search_pubmed"],
    )

    content_md = (
        f"## Method Scout — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"{eval_text}"
    )

    ledger_id = record_output(
        kind="method_scout",
        job_name="idle_research_task",
        model=_SONNET_MODEL,
        project_id=None,
        content_md=content_md,
        tokens_in=0,  # subagent tokens tracked internally via record_call
        tokens_out=0,
        provenance={"subagent_model": _SONNET_MODEL},
    )

    # Critic pass
    try:
        crit = critic_pass(eval_text[:1500], rubric_name="analysis")
        update_critic(ledger_id, crit.get("score", 0), crit.get("overall_notes", ""), crit.get("model", ""))
    except Exception as exc:
        log.warning("method_scout: critic pass failed: %s", exc)

    briefing_title = f"Method scout: new package for comparative phylogenetics"
    briefing_id = _write_briefing(conn, briefing_title, content_md)

    return {
        "ok": True,
        "ledger_id": ledger_id,
        "briefing_id": briefing_id,
        "summary": "method_scout: package evaluation written",
        "cost_estimate": cost_estimate,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_TASK_FUNCTIONS = {
    "preprint_scout":           _task_preprint_scout,
    "self_citation_prospector": _task_self_citation_prospector,
    "hypothesis_stress_test":   _task_hypothesis_stress_test,
    "undercited_paper_memo":    _task_undercited_paper_memo,
    "method_scout":             _task_method_scout,
}


def _pick_task() -> str:
    """Round-robin task selection via persisted state file."""
    state = _load_state()
    idx = state.get("next_index", 0) % len(_TASK_MENU)
    task_name = _TASK_MENU[idx]
    state["next_index"] = (idx + 1) % len(_TASK_MENU)
    _save_state(state)
    return task_name


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------

@tracked("idle_research_task")
def job(force: bool = False) -> str:
    """Hourly idle-research dispatcher.

    Args:
        force: bypass the idle gate (used for smoke-testing / manual runs).
    """
    _force = force or os.environ.get("FORCE_RUN") == "1"

    client = Anthropic()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # ------------------------------------------------------------------ #
        # 1. Idle gate
        # ------------------------------------------------------------------ #
        idle_class = _get_idle_class(conn)
        allowed_classes = {"idle", "deep_idle", "away", "engaged"}
        # "active" is blocked; anything else (including "engaged") is fine.
        # "engaged" means working but briefly away — still OK for low-urgency bg work.
        if not _force and idle_class == "active":
            conn.close()
            return f"idle_gate: skipped (idle_class={idle_class!r})"

        log.info("idle_research_task: running (idle_class=%r, force=%s)", idle_class, _force)

        # ------------------------------------------------------------------ #
        # 2. Pick task (force defaults to preprint_scout — cheapest)
        # ------------------------------------------------------------------ #
        if _force:
            task_name = "preprint_scout"
        else:
            task_name = _pick_task()

        cost_est = _TASK_COST_ESTIMATES.get(task_name, 0.0)
        if cost_est > _COST_CAP_USD:
            log.warning(
                "idle_research_task: %s estimated cost $%.2f exceeds cap $%.2f — skipping",
                task_name, cost_est, _COST_CAP_USD,
            )
            conn.close()
            return f"cost_cap: skipped {task_name!r} (est=${cost_est:.2f} > cap=${_COST_CAP_USD:.2f})"

        log.info("idle_research_task: selected task=%r est_cost=$%.2f", task_name, cost_est)

        # ------------------------------------------------------------------ #
        # 3. Execute task
        # ------------------------------------------------------------------ #
        task_fn = _TASK_FUNCTIONS[task_name]
        result = task_fn(client, conn)

        # ------------------------------------------------------------------ #
        # 4. Return summary
        # ------------------------------------------------------------------ #
        ok = result.get("ok", False)
        ledger_id = result.get("ledger_id")
        briefing_id = result.get("briefing_id")
        summary = result.get("summary", "")

        log.info(
            "idle_research_task: task=%r ok=%s ledger_id=%s briefing_id=%s summary=%r",
            task_name, ok, ledger_id, briefing_id, summary,
        )

        return (
            f"task={task_name!r} ok={ok} "
            f"ledger_id={ledger_id} briefing_id={briefing_id} "
            f"summary={summary!r}"
        )

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Idle research task smoke-test")
    parser.add_argument("--force", action="store_true", help="bypass idle gate")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print("Running idle_research_task (force={})...".format(args.force))
    result_str = job(force=args.force or True)
    print("\nResult:", result_str)

    # Parse and pretty-print key fields
    import re
    ledger_match  = re.search(r"ledger_id=(\d+)", result_str)
    briefing_match = re.search(r"briefing_id=(\d+)", result_str)
    task_match    = re.search(r"task='?([^' ]+)'?", result_str)
    cost_match    = re.search(r"est_cost=\$?([\d.]+)", result_str)

    print("\n--- Smoke-test summary ---")
    print("Task picked  :", task_match.group(1)  if task_match    else "N/A")
    print("Ledger ID    :", ledger_match.group(1) if ledger_match  else "None")
    print("Briefing ID  :", briefing_match.group(1) if briefing_match else "None")
