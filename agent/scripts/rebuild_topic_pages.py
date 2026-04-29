"""One-shot: wipe all topic pages and regenerate them from paper_findings.

Use this after fixing a bug in the pipeline that produced corrupt topic pages,
or when the existing topic pages have drifted from what the DB says they
should contain.

Algorithm:
  1. Load every (doi, topic_tag, created_at_first) tuple from paper_findings.
  2. Group by DOI; sort DOIs by first-ingest timestamp (oldest first).
  3. For each DOI, extract its unique topic_tags.
  4. Delete all files under knowledge/topics/ except .gitkeep and index.md.
  5. For each (doi, slug) pair in chronological order: call retry_topic's
     core logic (run topic writer → compose page → stage).

Usage:
    PYTHONPATH=/path/to/00-Lab-Agent ~/.lab-agent-venv/bin/python \\
        -m agent.scripts.rebuild_topic_pages

Dry-run only — every call stages the page to disk but does NOT commit or push.
Review the result under coleoguy.github.io/knowledge/topics/ and commit yourself.
"""
from __future__ import annotations

import os
import sqlite3
import sys

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.scheduler import DB_PATH  # noqa: E402
from agent.jobs.website_git import (  # noqa: E402
    website_repo_path, stage_and_diff,
)
from agent.jobs.wiki_pipeline import (  # noqa: E402
    _run_topic_writer, _compose_topic_page, _load_research_topics,
    _read_existing_topic_page, _split_body_by_markers, _splice_auto_region,
    doi_to_slug,
)
from agent.scripts.retry_topic import (  # noqa: E402
    _load_paper_findings_for, _paper_meta_from_page, _topic_title_from_registry,
)


def _collect_ingest_plan() -> list[tuple[str, list[str]]]:
    """Return [(doi, [slugs])] ordered by first-ingest timestamp."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT doi, topic_tags, MIN(created_at) as first_at "
            "FROM paper_findings GROUP BY doi, topic_tags ORDER BY first_at"
        ).fetchall()
    finally:
        conn.close()

    # Fold tag groups into per-DOI slug sets, preserving DOI order by first_at
    seen_dois: dict[str, tuple[str, set[str]]] = {}
    for r in rows:
        doi = r["doi"]
        tags_raw = r["topic_tags"] or ""
        slugs = {t.strip() for t in tags_raw.split(",") if t.strip()}
        if doi not in seen_dois:
            seen_dois[doi] = (r["first_at"], set())
        seen_dois[doi][1].update(slugs)

    ordered = sorted(seen_dois.items(), key=lambda kv: kv[1][0])
    return [(doi, sorted(slugs)) for doi, (_, slugs) in ordered]


def _wipe_topic_pages(repo: str) -> int:
    """Delete every .md under knowledge/topics/ except index.md. Return count removed."""
    topics_dir = os.path.join(repo, "knowledge", "topics")
    removed = 0
    for name in sorted(os.listdir(topics_dir)):
        if not name.endswith(".md"):
            continue
        if name == "index.md":
            continue
        os.remove(os.path.join(topics_dir, name))
        removed += 1
    return removed


def _run_one(client: Anthropic, doi: str, slug: str, repo: str) -> tuple[bool, str, float]:
    """Run one (doi, slug) through the topic writer. Returns (ok, message, cost)."""
    findings = _load_paper_findings_for(doi, slug)
    if not findings:
        return (True, f"  {slug}: no findings for this DOI (skipped)", 0.0)

    paper_slug = doi_to_slug(doi)
    meta = _paper_meta_from_page(paper_slug, doi)
    existing_body, existing_papers, existing_category = _read_existing_topic_page(repo, slug)
    # Preserve content outside tealc:auto markers; writer only sees auto region.
    _before, old_auto, _after, _had = _split_body_by_markers(existing_body)
    topic_title = _topic_title_from_registry(slug)

    try:
        writer_result, cost = _run_topic_writer(
            client, slug, topic_title, old_auto, findings, meta,
        )
    except Exception as e:
        return (False, f"  {slug}: writer failed: {e}", 0.0)

    new_auto_body = (writer_result.get("body_md") or "").strip()
    if not new_auto_body:
        return (False, f"  {slug}: writer returned empty body_md", cost)

    # Splice writer output back into auto region, preserving content outside markers.
    body_md = _splice_auto_region(new_auto_body, existing_body)

    papers_supporting = sorted(set(existing_papers) | {doi})
    page_md = _compose_topic_page(slug, body_md, papers_supporting, existing_category)
    rel_path = f"knowledge/topics/{slug}.md"
    stage_and_diff(rel_path, page_md)
    return (True, f"  {slug}: {len(findings)} finding(s) folded in  (${cost:.3f})", cost)


def main() -> int:
    repo = website_repo_path()
    plan = _collect_ingest_plan()
    if not plan:
        print("No findings in paper_findings. Nothing to rebuild.")
        return 0

    print(f"Rebuild plan: {len(plan)} paper(s), in chronological order of ingest:")
    for doi, slugs in plan:
        print(f"  - {doi}  ({len(slugs)} topic(s))")
    print()

    removed = _wipe_topic_pages(repo)
    print(f"Wiped {removed} topic page file(s).\n")

    client = Anthropic()
    total_cost = 0.0
    total_ok = 0
    total_fail = 0

    for doi, slugs in plan:
        print(f"--- {doi} ---")
        for slug in slugs:
            ok, msg, cost = _run_one(client, doi, slug, repo)
            total_cost += cost
            if ok:
                total_ok += 1
            else:
                total_fail += 1
            print(msg)
        print()

    print(f"=== done: {total_ok} ok, {total_fail} failed, total ≈ ${total_cost:.2f} ===")
    return 0 if total_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
