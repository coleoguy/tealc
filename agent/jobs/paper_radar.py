"""Paper radar — every 2 hours during work hours, scans bioRxiv's recent
posts in evolutionary-biology-adjacent categories and matches against active
research project keywords. Hits get queued in a paper_radar_queue table for
inclusion in the next morning briefing.

No LLM in this version — pure keyword match against title + abstract — to keep
cost ≈ $0. Upgrade path: add a Haiku relevance pass once we know the keyword
match alone is too noisy or too sparse.

Run manually:
    python -m agent.jobs.paper_radar
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.config import is_job_enabled, should_run_this_cycle  # noqa: E402

_STATE_PATH = os.path.normpath(
    os.path.join(_PROJECT_ROOT, "data", "paper_radar.state.json")
)


def _is_working_hours() -> bool:
    try:
        import zoneinfo
        central = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        return True
    h = datetime.now(central).hour
    return 9 <= h < 17


def _load_state() -> dict:
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"seen_dois": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _STATE_PATH)


def _ensure_queue_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_radar_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doi TEXT UNIQUE,
            title TEXT NOT NULL,
            authors TEXT,
            posted_iso TEXT,
            source TEXT,
            matched_project_ids TEXT,
            matched_keywords TEXT,
            queued_at TEXT NOT NULL,
            surfaced_at TEXT,
            dismissed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


def _fetch_biorxiv_recent(days_back: int = 1) -> list[dict]:
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    out: list[dict] = []
    for server in ("biorxiv", "medrxiv"):
        try:
            resp = requests.get(
                f"https://api.biorxiv.org/details/{server}/{start}/{end}/0/json",
                timeout=20,
            )
            resp.raise_for_status()
            out.extend(resp.json().get("collection", []) or [])
        except Exception:
            continue
    return out


def _project_keywords(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    rows = conn.execute(
        "SELECT id, keywords FROM research_projects "
        "WHERE status='active' AND keywords IS NOT NULL AND keywords != ''"
    ).fetchall()
    out: list[tuple[str, list[str]]] = []
    for pid, csv in rows:
        kws = [k.strip().lower() for k in (csv or "").split(",") if k.strip()]
        if kws:
            out.append((pid, kws))
    return out


def _match(text: str, keywords: list[str]) -> list[str]:
    text_l = (text or "").lower()
    return [k for k in keywords if re.search(rf"\b{re.escape(k)}\b", text_l)]


@tracked("paper_radar")
def job() -> str:
    if not is_job_enabled("paper_radar"):
        return "skipped: disabled in config"
    if not should_run_this_cycle("paper_radar"):
        return "skipped: reduced-mode sample miss"
    if not _is_working_hours() and os.environ.get("FORCE_RUN") != "1":
        return "skipped: off-hours"

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_queue_table(conn)

    project_kws = _project_keywords(conn)
    if not project_kws:
        conn.close()
        return "skipped: no active projects with keywords"

    state = _load_state()
    raw_seen = state.get("seen_dois", [])
    seen: set[str] = set(raw_seen[-1000:]) if isinstance(raw_seen, list) else set()

    papers = _fetch_biorxiv_recent(days_back=1)
    if not papers:
        conn.close()
        return "skipped: bioRxiv API returned no papers"

    now_iso = datetime.now(timezone.utc).isoformat()
    queued = 0
    for p in papers:
        doi = (p.get("doi") or "").lower()
        if not doi or doi in seen:
            continue
        seen.add(doi)
        title = p.get("title") or ""
        abstract = p.get("abstract") or ""
        text = f"{title} {abstract}"
        matched_pairs: list[tuple[str, list[str]]] = []
        for pid, kws in project_kws:
            hits = _match(text, kws)
            if hits:
                matched_pairs.append((pid, hits))
        if not matched_pairs:
            continue
        matched_pids = ",".join(p for p, _ in matched_pairs)
        matched_kws = ",".join(sorted({k for _, ks in matched_pairs for k in ks}))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO paper_radar_queue "
                "(doi, title, authors, posted_iso, source, matched_project_ids, "
                " matched_keywords, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doi,
                    title[:300],
                    (p.get("authors") or "")[:300],
                    p.get("date"),
                    "biorxiv",
                    matched_pids,
                    matched_kws,
                    now_iso,
                ),
            )
            if conn.total_changes:
                queued += 1
        except Exception:
            continue

    conn.commit()
    conn.close()

    state["seen_dois"] = list(seen)[-1000:]
    _save_state(state)

    return f"ok: scanned {len(papers)} preprints, queued {queued} matches"


if __name__ == "__main__":
    os.environ["FORCE_RUN"] = "1"
    print(job())
