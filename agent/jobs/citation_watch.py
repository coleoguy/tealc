"""Citation watch — every 4 hours, polls OpenAlex for new citations to Heath's
papers. If the cited_by_count jumped on any paper since last check, log a row
to the output_ledger with kind='citation_alert'. No LLM, ~free.

State stored in data/citation_watch.state.json (last_seen[work_id] = count).

Designed to keep the public aquarium feed showing science activity during work
hours via a "Checked citations of lab work" event whenever it fires.

Run manually:
    python -m agent.jobs.citation_watch
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.config import is_job_enabled, should_run_this_cycle  # noqa: E402
from agent.ledger import record_output  # noqa: E402

_STATE_PATH = os.path.normpath(
    os.path.join(_PROJECT_ROOT, "data", "citation_watch.state.json")
)
_MAILTO = os.environ.get("OPENALEX_MAILTO", "blackmon@tamu.edu")
_AUTHOR_NAME = os.environ.get("RESEARCHER_FULL_NAME", "Heath Blackmon")
_ORCID = os.environ.get("RESEARCHER_ORCID", "0000-0002-5433-4036")


def _load_state() -> dict:
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"last_seen": {}}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _STATE_PATH)


def _resolve_author_id() -> str | None:
    try:
        resp = requests.get(
            "https://api.openalex.org/authors",
            params={"filter": f"orcid:{_ORCID}", "mailto": _MAILTO},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            resp = requests.get(
                "https://api.openalex.org/authors",
                params={"search": _AUTHOR_NAME, "per_page": 1, "mailto": _MAILTO},
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        if not results:
            return None
        return results[0].get("id", "").rsplit("/", 1)[-1]
    except Exception:
        return None


def _fetch_works(author_id: str) -> list[dict]:
    out: list[dict] = []
    cursor = "*"
    while cursor and len(out) < 200:
        try:
            resp = requests.get(
                "https://api.openalex.org/works",
                params={
                    "filter": f"author.id:{author_id}",
                    "select": "id,title,cited_by_count,publication_year,doi",
                    "per-page": 100,
                    "cursor": cursor,
                    "mailto": _MAILTO,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            out.extend(data.get("results", []))
            cursor = (data.get("meta") or {}).get("next_cursor")
        except Exception:
            break
    return out


@tracked("citation_watch")
def job() -> str:
    if not is_job_enabled("citation_watch"):
        return "skipped: disabled in config"
    if not should_run_this_cycle("citation_watch"):
        return "skipped: reduced-mode sample miss"

    author_id = _resolve_author_id()
    if not author_id:
        return "skipped: could not resolve author id from OpenAlex"

    works = _fetch_works(author_id)
    if not works:
        return "skipped: OpenAlex returned no works"

    state = _load_state()
    last_seen: dict[str, int] = state.get("last_seen", {})
    new_alerts: list[tuple[str, str, int, int]] = []
    for w in works:
        wid = w.get("id", "")
        if not wid:
            continue
        cur = int(w.get("cited_by_count") or 0)
        prev = int(last_seen.get(wid, -1))
        # Skip first-ever observation — establish baseline silently.
        if prev >= 0 and cur > prev:
            new_alerts.append((wid, w.get("title", "")[:200], prev, cur))
        last_seen[wid] = cur

    state["last_seen"] = last_seen
    _save_state(state)

    if not new_alerts:
        return f"ok: baseline {len(last_seen)} works, no new citations"

    # Log to output_ledger so it surfaces in the chat tools (list_output_ledger)
    # and in any UIs that read kind='citation_alert'.
    now_iso = datetime.now(timezone.utc).isoformat()
    summary_lines = [
        f"- {title}: {prev} → {cur} (+{cur - prev})"
        for _wid, title, prev, cur in new_alerts
    ]
    content_md = "**New citations detected:**\n" + "\n".join(summary_lines)
    try:
        record_output(
            kind="citation_alert",
            content_md=content_md,
            project_id=None,
            doc_id=None,
            cited_dois=[],
            notes=f"{len(new_alerts)} works gained citations",
            decided_by="auto",
            critic_pass=True,
        )
    except TypeError:
        # ledger.record_output signature varies — fall back to direct insert.
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO output_ledger (kind, content_md, created_at, decided_by) "
                "VALUES (?, ?, ?, ?)",
                ("citation_alert", content_md, now_iso, "auto"),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    return f"ok: {len(new_alerts)} works gained citations"


if __name__ == "__main__":
    print(job())
