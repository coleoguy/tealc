"""NAS-metric tracker job — runs daily at 5:30am Central via APScheduler.

Pulls Heath's citation metrics from OpenAlex, persists a daily snapshot, so
Tealc can surface trends over time rather than just point-in-time numbers.

# Recommended schedule (the final-wave agent updates scheduler.py): CronTrigger(hour=5, minute=30, timezone="America/Chicago")

Run manually to test:
    python -m agent.jobs.track_nas_metrics
"""
import json
import os
import sqlite3
from datetime import date, timedelta, timezone, datetime

import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402 (after load_dotenv)

from agent.jobs import tracked
from agent.scheduler import DB_PATH
import agent.cost_tracking as cost_tracking
from agent.ledger import record_output


# ---------------------------------------------------------------------------
# OpenAlex helper
# ---------------------------------------------------------------------------

_MAILTO = "blackmon@tamu.edu"


def _openalex_get(url: str, params: dict) -> dict:
    """GET an OpenAlex endpoint; raises on HTTP error; returns parsed JSON."""
    params.setdefault("mailto", _MAILTO)
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("track_nas_metrics")
def job() -> str:
    """Pull Heath's OpenAlex metrics and persist a daily snapshot."""

    # 1. Compute today's date (snapshot key)
    today = date.today()
    today_iso = today.isoformat()

    # 2. Check idempotency — one row per day
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = conn.execute(
            "SELECT id FROM nas_metrics WHERE snapshot_iso=?", (today_iso,)
        ).fetchone()
        conn.close()
        if existing:
            return f"already_snapped: {today_iso}"
    except Exception as e:
        return f"error checking existing snapshot: {e}"

    # 3. Pull Heath's OpenAlex author record
    try:
        data = _openalex_get(
            "https://api.openalex.org/authors",
            {"search": "Heath Blackmon", "per_page": 1},
        )
        results = data.get("results", [])
        if not results:
            return "error: OpenAlex returned no author results for Heath Blackmon"
        author = results[0]
    except Exception as e:
        return f"error fetching author from OpenAlex: {e}"

    try:
        author_id = author.get("id", "")  # e.g. "https://openalex.org/A123456"
        total_citations = author.get("cited_by_count") or 0
        summary = author.get("summary_stats") or {}
        h_index = summary.get("h_index")
        i10_index = summary.get("i10_index")
        works_count = author.get("works_count")

        # Sum citations for years 2021+
        counts_by_year = author.get("counts_by_year") or []
        citations_since_2021 = sum(
            row.get("cited_by_count", 0)
            for row in counts_by_year
            if (row.get("year") or 0) >= 2021
        )
    except Exception as e:
        return f"error parsing author record: {e}"

    # 4. Pull top 3 most-cited papers from past 3 years
    top_3_papers = []
    try:
        three_years_ago = (date.today() - timedelta(days=3 * 365)).isoformat()
        # Strip URL prefix from author_id for filter
        raw_id = author_id.replace("https://openalex.org/", "")
        works_data = _openalex_get(
            "https://api.openalex.org/works",
            {
                "filter": f"author.id:{raw_id},from_publication_date:{three_years_ago}",
                "sort": "cited_by_count:desc",
                "per_page": 3,
            },
        )
        for work in works_data.get("results", []):
            top_3_papers.append({
                "title": (work.get("title") or "")[:200],
                "year": work.get("publication_year"),
                "citations": work.get("cited_by_count") or 0,
            })
    except Exception as e:
        top_3_papers = []  # non-fatal — still save the author-level metrics

    # 5. Insert row into nas_metrics
    try:
        top_3_json = json.dumps(top_3_papers)
        raw_author_json = json.dumps(author)

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT OR IGNORE INTO nas_metrics
               (snapshot_iso, total_citations, citations_since_2021,
                h_index, i10_index, works_count,
                top_3_recent_papers_json, raw_author_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today_iso,
                total_citations,
                citations_since_2021,
                h_index,
                i10_index,
                works_count,
                top_3_json,
                raw_author_json,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return f"error inserting snapshot: {e}"

    # 6. Citation-delta detection — compare to previous snapshot
    delta = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT total_citations, snapshot_iso FROM nas_metrics "
            "ORDER BY snapshot_iso DESC LIMIT 2"
        ).fetchall()
        conn.close()

        if len(rows) >= 2:
            current_citations, current_snap = rows[0]
            prev_citations, prev_snap = rows[1]
            delta = (current_citations or 0) - (prev_citations or 0)

            if delta > 0:
                # Fetch new citing papers published since previous snapshot
                citing_data = _openalex_get(
                    "https://api.openalex.org/works",
                    {
                        "filter": (
                            f"cites:{raw_id},"
                            f"from_publication_date:{prev_snap}"
                        ),
                        "sort": "publication_date:desc",
                        "per_page": 20,
                        "select": (
                            "id,title,authorships,primary_location,"
                            "publication_year,referenced_works,abstract_inverted_index"
                        ),
                    },
                )

                # Fetch Heath's own work IDs for intersection
                heath_works_data = _openalex_get(
                    "https://api.openalex.org/works",
                    {
                        "filter": f"author.id:{raw_id}",
                        "per_page": 50,
                        "select": "id,title",
                    },
                )
                heath_work_map = {
                    w.get("id", ""): (w.get("title") or "Untitled")
                    for w in heath_works_data.get("results", [])
                }
                heath_work_ids = set(heath_work_map.keys())

                new_papers = citing_data.get("results", [])
                n_papers = len(new_papers)

                # ----------------------------------------------------------
                # Build per-paper metadata and reconstruct abstracts
                # ----------------------------------------------------------
                paper_meta = []  # parallel list to new_papers
                for paper in new_papers:
                    p_title = paper.get("title") or "Untitled"
                    p_year = paper.get("publication_year") or ""
                    p_id = paper.get("id") or ""
                    p_url = p_id if p_id.startswith("http") else f"https://openalex.org/{p_id}"

                    # Authors (first 3 + et al)
                    authorships = paper.get("authorships") or []
                    author_names = [
                        (a.get("author") or {}).get("display_name", "")
                        for a in authorships[:3]
                        if a.get("author")
                    ]
                    author_str = ", ".join(filter(None, author_names))
                    if len(authorships) > 3:
                        author_str += " et al."

                    # Journal
                    loc = paper.get("primary_location") or {}
                    source = loc.get("source") or {}
                    journal = source.get("display_name") or "Unknown journal"

                    # Which of Heath's papers it cited
                    ref_works = set(paper.get("referenced_works") or [])
                    cited_heath = heath_work_ids & ref_works
                    cited_titles = [
                        heath_work_map.get(wid, wid) for wid in cited_heath
                    ] or ["(could not determine)"]

                    # Reconstruct abstract from inverted index (truncated to 1500 chars)
                    abstract_inv = paper.get("abstract_inverted_index") or {}
                    abstract_text = ""
                    if abstract_inv:
                        word_pos = []
                        for word, positions in abstract_inv.items():
                            for pos in positions:
                                word_pos.append((pos, word))
                        word_pos.sort()
                        abstract_text = " ".join(w for _, w in word_pos)[:1500]

                    paper_meta.append({
                        "title": p_title,
                        "authors": author_str or "Unknown",
                        "journal": journal,
                        "year": p_year,
                        "url": p_url,
                        "abstract": abstract_text,
                        "cited_heath_titles": cited_titles,
                    })

                # ----------------------------------------------------------
                # Sonnet framing pass — one batched call for all new papers
                # ----------------------------------------------------------
                _FRAMING_MODEL = "claude-sonnet-4-6"
                framing_results = []  # list of dicts with frame, one_line, why
                framing_tokens_in = 0
                framing_tokens_out = 0

                try:
                    # Build user message from all mini-contexts
                    batch_items = []
                    for i, pm in enumerate(paper_meta):
                        item = {
                            "index": i,
                            "citing_paper_title": pm["title"],
                            "authors": pm["authors"],
                            "journal_year": f"{pm['journal']} {pm['year']}",
                            "abstract_excerpt": pm["abstract"],
                            "heath_papers_cited": pm["cited_heath_titles"],
                        }
                        batch_items.append(item)

                    system_prompt = (
                        "You are Heath Blackmon's NAS-case strategist. You receive a batch of new "
                        "citations of Heath's work and classify each: \"confirmation\" (another group "
                        "validated Heath's finding), \"extension\" (built on top of it), "
                        "\"contradiction\" (challenged it), \"methodological\" (used his "
                        "method/dataset), or \"incidental\" (cited in passing, low narrative value).\n\n"
                        "For each citation, output JSON:\n"
                        "{\n"
                        "  \"citing_paper_title\": \"<echoed>\",\n"
                        "  \"frame\": \"confirmation|extension|contradiction|methodological|incidental\",\n"
                        "  \"one_line_for_narrative\": \"<one sentence Heath could use in an NAS "
                        "narrative, or null if incidental>\",\n"
                        "  \"why_it_matters\": \"<1-2 sentences on significance for NAS trajectory>\"\n"
                        "}\n\n"
                        "Output a JSON array of these objects, one per citing paper. Be honest — "
                        "flag \"incidental\" when it's just a background-reference citation."
                    )

                    user_msg = json.dumps(batch_items, indent=2)

                    client = Anthropic()
                    resp = client.messages.create(
                        model=_FRAMING_MODEL,
                        max_tokens=800,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_msg}],
                    )

                    framing_tokens_in = resp.usage.input_tokens
                    framing_tokens_out = resp.usage.output_tokens

                    # Record cost (best-effort)
                    try:
                        cost_tracking.record_call(
                            "track_nas_metrics",
                            _FRAMING_MODEL,
                            {
                                "input_tokens": framing_tokens_in,
                                "output_tokens": framing_tokens_out,
                            },
                        )
                    except Exception:
                        pass

                    # Parse response — strip markdown fences, tolerate partial failures
                    raw_text = resp.content[0].text.strip()
                    if raw_text.startswith("```"):
                        lines_raw = raw_text.splitlines()
                        # Drop first and last fence lines
                        raw_text = "\n".join(
                            l for l in lines_raw
                            if not l.strip().startswith("```")
                        )

                    parsed_batch = json.loads(raw_text)
                    if isinstance(parsed_batch, list):
                        framing_results = parsed_batch
                    else:
                        framing_results = []

                except Exception:
                    framing_results = []

                # Build a lookup by title for quick access (case-insensitive key)
                framing_by_title = {}
                for fr in framing_results:
                    if isinstance(fr, dict):
                        key = (fr.get("citing_paper_title") or "").strip().lower()
                        framing_by_title[key] = fr

                def _get_framing(title: str) -> dict:
                    """Return framing dict for a paper title, with safe fallback."""
                    fr = framing_by_title.get(title.strip().lower(), {})
                    return {
                        "frame": fr.get("frame") or "unknown",
                        "one_line_for_narrative": fr.get("one_line_for_narrative"),
                        "why_it_matters": fr.get("why_it_matters") or "",
                    }

                # ----------------------------------------------------------
                # Build briefing content with NAS-narrative structure
                # ----------------------------------------------------------
                _NARRATIVE_FRAMES = {"confirmation", "extension", "contradiction", "methodological"}

                narrative_papers = []
                incidental_papers = []
                has_contradiction = False

                for pm in paper_meta:
                    fr = _get_framing(pm["title"])
                    entry = {**pm, **fr}
                    frame = fr["frame"]
                    if frame == "contradiction":
                        has_contradiction = True
                    if frame in _NARRATIVE_FRAMES:
                        narrative_papers.append(entry)
                    else:
                        incidental_papers.append(entry)

                # Build markdown
                md_lines = [
                    f"# Citation delta: +{delta} new citation(s), {n_papers} new citing paper(s)\n",
                ]

                if narrative_papers:
                    md_lines.append("## Narrative-relevant (confirmations / extensions / contradictions)\n")
                    for entry in narrative_papers:
                        cited_str = "; ".join(entry["cited_heath_titles"][:3])
                        md_lines.append(
                            f"**{entry['title']}** — {entry['authors']}, "
                            f"{entry['journal']} {entry['year']}"
                        )
                        md_lines.append(f"_{entry['frame']}_ — cited: {cited_str}")
                        if entry.get("one_line_for_narrative"):
                            md_lines.append(
                                f"**For NAS narrative:** \"{entry['one_line_for_narrative']}\""
                            )
                        if entry.get("why_it_matters"):
                            md_lines.append(entry["why_it_matters"])
                        md_lines.append(f"[OpenAlex link]({entry['url']})")
                        md_lines.append("")
                else:
                    md_lines.append("## Narrative-relevant (confirmations / extensions / contradictions)\n")
                    md_lines.append("_None of the new citing papers were classified as narrative-relevant._\n")

                if incidental_papers:
                    md_lines.append("## Incidental (lower priority)\n")
                    for entry in incidental_papers:
                        md_lines.append(
                            f"- **{entry['title']}** ({entry['year']}) — "
                            f"{entry['authors']} — [{entry['journal']}]({entry['url']})"
                        )
                    md_lines.append("")

                content_md = "\n".join(md_lines)
                briefing_title = (
                    f"+{delta} new citation(s) — {n_papers} new citing paper(s)"
                )
                urgency = "warn" if has_contradiction else "info"

                # Insert briefing
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """INSERT INTO briefings
                       (kind, urgency, title, content_md, metadata_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "citation_delta",
                        urgency,
                        briefing_title,
                        content_md,
                        json.dumps({"delta": delta, "n_papers": n_papers,
                                    "prev_snap": prev_snap, "curr_snap": today_iso}),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
                conn.close()

                # Write framing output to ledger (best-effort)
                try:
                    all_frames = [_get_framing(pm["title"])["frame"] for pm in paper_meta]
                    record_output(
                        kind="citation_framing",
                        job_name="track_nas_metrics",
                        model=_FRAMING_MODEL,
                        project_id=None,
                        content_md=content_md,
                        tokens_in=framing_tokens_in,
                        tokens_out=framing_tokens_out,
                        provenance={
                            "delta": delta,
                            "citing_paper_count": n_papers,
                            "frames": all_frames,
                        },
                    )
                except Exception:
                    pass

    except Exception as e:
        # Citation-delta detection failure must NOT crash the snapshot insert
        delta = delta  # preserve any computed delta

    return (
        f"snapped {today_iso}: "
        f"citations={total_citations} "
        f"h={h_index} "
        f"i10={i10_index} "
        f"works={works_count} "
        f"delta=+{delta} (from last snapshot)"
    )


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
