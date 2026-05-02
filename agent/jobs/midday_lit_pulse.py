"""Midday literature pulse — small daytime sibling of nightly_literature_synthesis.

Fires every 90 min during work hours. Each fire picks ONE active research
project (rotating via state file), queries OpenAlex for one keyword, picks the
top recent paper, and has Haiku write a brief literature_note. Cost ≈ $0.005
per fire, ~5 fires/workday ≈ $0.03/day.

Designed to make the public aquarium feed feel science-y during business hours
when nightly_literature_synthesis is dormant.

Run manually:
    python -m agent.jobs.midday_lit_pulse
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
from agent.config import is_job_enabled, should_run_this_cycle  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

_STATE_PATH = os.path.normpath(
    os.path.join(_PROJECT_ROOT, "data", "midday_lit_pulse.state.json")
)
_HAIKU = "claude-haiku-4-5-20251001"
_MAILTO = os.environ.get("OPENALEX_MAILTO", "blackmon@tamu.edu")


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
        return {"last_project_id": None, "seen_dois": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _STATE_PATH)


def _pick_next_project(conn: sqlite3.Connection, last_id: str | None) -> tuple[str, str, str] | None:
    rows = conn.execute(
        "SELECT id, name, keywords FROM research_projects "
        "WHERE status='active' AND keywords IS NOT NULL AND keywords != '' "
        "ORDER BY id"
    ).fetchall()
    if not rows:
        return None
    if last_id is None:
        return rows[0]
    ids = [r[0] for r in rows]
    if last_id not in ids:
        return rows[0]
    next_idx = (ids.index(last_id) + 1) % len(ids)
    return rows[next_idx]


def _fetch_recent_paper(keyword: str, seen_dois: list[str]) -> dict | None:
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "filter": f"from_publication_date:{since},title_and_abstract.search:{keyword}",
                "sort": "publication_date:desc",
                "per_page": 5,
                "mailto": _MAILTO,
            },
            timeout=15,
        )
        resp.raise_for_status()
        for work in resp.json().get("results", []):
            doi = (work.get("doi") or "").lower().replace("https://doi.org/", "")
            if not doi or doi in seen_dois:
                continue
            return work
    except Exception:
        return None
    return None


def _summarize_with_haiku(paper: dict, project_name: str, keywords: str) -> tuple[str, str]:
    """Return (findings_md, relevance) — short. ~$0.005 per call."""
    title = paper.get("title", "")
    abstract = paper.get("abstract_inverted_index")
    if isinstance(abstract, dict):
        words = [None] * (max((p for ps in abstract.values() for p in ps), default=-1) + 1)
        for w, positions in abstract.items():
            for p in positions:
                if 0 <= p < len(words):
                    words[p] = w
        abstract_text = " ".join(w for w in words if w)[:1500]
    else:
        abstract_text = ""

    client = Anthropic()
    sys_prompt = (
        "You read one scientific paper and return JSON with two short fields: "
        '"findings_md" (2-3 bullets of the paper\'s specific claims) and '
        '"relevance" (one sentence on relevance to the named project). '
        "Output JSON only."
    )
    user = (
        f"Project: {project_name}\nProject keywords: {keywords}\n\n"
        f"Paper title: {title}\nAbstract: {abstract_text}"
    )
    msg = client.messages.create(
        model=_HAIKU,
        max_tokens=500,
        system=sys_prompt,
        messages=[{"role": "user", "content": user}],
    )
    try:
        record_call("midday_lit_pulse", _HAIKU, msg.usage.__dict__)
    except Exception:
        pass
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        findings = data.get("findings_md", "")
        relevance = data.get("relevance", "")
        # Haiku sometimes returns lists for findings_md despite the prompt;
        # coerce to a markdown bullet string before SQLite binding.
        if isinstance(findings, list):
            findings = "\n".join(f"- {b}" for b in findings)
        if isinstance(relevance, list):
            relevance = " ".join(str(b) for b in relevance)
        return str(findings), str(relevance)
    except Exception:
        return text[:300], ""


@tracked("midday_lit_pulse")
def job() -> str:
    if not is_job_enabled("midday_lit_pulse"):
        return "skipped: disabled in config"
    if not should_run_this_cycle("midday_lit_pulse"):
        return "skipped: reduced-mode sample miss"
    if not _is_working_hours() and os.environ.get("FORCE_RUN") != "1":
        return "skipped: off-hours"

    state = _load_state()
    seen = list(state.get("seen_dois", []))[-200:]

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    pick = _pick_next_project(conn, state.get("last_project_id"))
    if pick is None:
        conn.close()
        return "skipped: no active projects with keywords"
    project_id, project_name, keywords_csv = pick
    keyword = keywords_csv.split(",")[0].strip()
    if not keyword:
        conn.close()
        return f"skipped: project {project_id} has empty first keyword"

    paper = _fetch_recent_paper(keyword, seen)
    if paper is None:
        state["last_project_id"] = project_id
        _save_state(state)
        conn.close()
        return f"no new papers for {project_id} keyword={keyword!r}"

    doi = (paper.get("doi") or "").lower().replace("https://doi.org/", "")
    findings, relevance = _summarize_with_haiku(paper, project_name, keywords_csv)

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO literature_notes "
            "(project_id, doi, title, authors, journal, publication_year, "
            " open_access_url, citations_count, raw_abstract, extracted_findings_md, "
            " relevance_to_project, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id, doi or None,
                paper.get("title", "")[:300],
                ", ".join(a.get("author", {}).get("display_name", "")
                          for a in (paper.get("authorships") or [])[:5]),
                (paper.get("primary_location") or {}).get("source", {}).get("display_name", ""),
                paper.get("publication_year"),
                (paper.get("primary_location") or {}).get("landing_page_url"),
                paper.get("cited_by_count", 0),
                "",  # raw abstract blob — keep light
                findings,
                relevance,
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    seen.append(doi)
    state["last_project_id"] = project_id
    state["seen_dois"] = seen[-200:]
    _save_state(state)
    return f"ok: project={project_id} doi={doi or 'none'}"


if __name__ == "__main__":
    os.environ["FORCE_RUN"] = "1"
    print(job())
