"""Surgical retry for a single topic page that failed during a wiki_pipeline run.

When one of N topic writers fails (typically a JSON parse error on a rich
topic), use this script to re-run JUST that one topic writer using the
findings already persisted to paper_findings. Saves the cost of re-running
the extractor + verifier (the expensive parts).

Usage:
    cd /path/to/00-Lab-Agent
    PYTHONPATH="$PWD" ~/.lab-agent-venv/bin/python -m agent.scripts.retry_topic \\
        --doi 10.1534/genetics.117.300382 \\
        --slug fragile_y_hypothesis

The script always stages the result as a dry-run write (file on disk, no git
commit). Review the diff it prints, then if you like it, commit via your
normal flow (Tealc's ingest_paper_to_wiki tool or git from the website repo).
"""
from __future__ import annotations

import argparse
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


def _load_paper_findings_for(doi: str, slug: str) -> list[dict]:
    """Load paper_findings rows for a DOI filtered to those tagged with slug."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM paper_findings WHERE doi=? ORDER BY finding_idx",
            (doi,),
        ).fetchall()
    finally:
        conn.close()

    matching: list[dict] = []
    for r in rows:
        d = dict(r)
        tags_raw = d.get("topic_tags") or ""
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        if slug in tags:
            d["topic_tags"] = tags  # normalize to list for the prompt
            matching.append(d)
    return matching


def _paper_meta_from_page(paper_slug: str, doi: str) -> dict:
    """Read the existing paper page's YAML frontmatter for title + fingerprint."""
    repo = website_repo_path()
    path = os.path.join(repo, "knowledge", "papers", f"{paper_slug}.md")
    meta = {
        "doi": doi,
        "title": f"Paper {doi}",
        "fingerprint_sha256": "",
        "paper_slug": paper_slug,
        "paper_permalink": f"/knowledge/papers/{paper_slug}/",
    }
    if not os.path.exists(path):
        return meta
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        return meta
    fm_end = text.find("\n---", 3)
    if fm_end < 0:
        return meta
    for line in text[3:fm_end].splitlines():
        line = line.strip()
        if line.startswith("title:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                meta["title"] = val
        elif line.startswith("fingerprint_sha256:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                meta["fingerprint_sha256"] = val
    return meta


# Removed _existing_topic_body — use _read_existing_topic_page from wiki_pipeline
# which also returns the existing papers_supporting list for proper unioning.


def _topic_title_from_registry(slug: str) -> str:
    topics = _load_research_topics().get("topics", [])
    entry = next((t for t in topics if t.get("slug") == slug), {})
    return entry.get("title") or slug.replace("_", " ").title()


def main() -> int:
    ap = argparse.ArgumentParser(description="Retry one topic writer for one DOI.")
    ap.add_argument("--doi", required=True, help="Paper DOI (e.g. 10.1534/genetics.117.300382)")
    ap.add_argument("--slug", required=True, help="Topic slug (e.g. fragile_y_hypothesis)")
    args = ap.parse_args()

    findings = _load_paper_findings_for(args.doi, args.slug)
    if not findings:
        print(f"No findings for doi={args.doi!r} tagged with slug={args.slug!r}.")
        print("Options: check the DB contents with:")
        print(
            f"  sqlite3 {DB_PATH!r} \"SELECT finding_idx, topic_tags FROM "
            f"paper_findings WHERE doi='{args.doi}';\""
        )
        return 1

    print(f"Found {len(findings)} finding(s) for this paper+topic.")

    paper_slug = doi_to_slug(args.doi)
    meta = _paper_meta_from_page(paper_slug, args.doi)
    existing_body, existing_papers, existing_category = _read_existing_topic_page(
        website_repo_path(), args.slug,
    )
    # Pass only the auto region to the writer (preserves before/after content)
    _before, old_auto, _after, _had = _split_body_by_markers(existing_body)
    topic_title = _topic_title_from_registry(args.slug)

    client = Anthropic()
    try:
        writer_result, cost = _run_topic_writer(
            client, args.slug, topic_title, old_auto, findings, meta,
        )
    except Exception as e:
        print(f"topic writer STILL failed: {e}")
        return 2

    print(f"topic writer succeeded. cost≈${cost:.3f}")

    new_auto_body = writer_result.get("body_md") or ""
    edit_note = writer_result.get("edit_note") or {}
    if not new_auto_body.strip():
        print("writer returned empty body_md — aborting (would produce a blank page)")
        return 3

    # Splice the writer's output back into the auto region, preserving any
    # human-added content outside tealc:auto markers.
    body_md = _splice_auto_region(new_auto_body, existing_body)

    # Union the existing papers_supporting with this paper's DOI so the list
    # accumulates correctly across retries. Preserve the existing category so
    # we don't drop it on a topic-page rewrite (WIKI_HANDOFF invariant).
    papers_supporting = sorted(set(existing_papers) | {args.doi})
    page_md = _compose_topic_page(
        args.slug, body_md, papers_supporting, existing_category,
    )
    rel_path = f"knowledge/topics/{args.slug}.md"
    stage_result = stage_and_diff(rel_path, page_md)

    print(f"\n--- staged: {rel_path} ---")
    print(f"edit_note.what_changed: {edit_note.get('what_changed', '')[:300]}")
    print(f"edit_note.why_changed:  {edit_note.get('why_changed', '')[:300]}")
    print(f"edit_note.counter:      {edit_note.get('counter_argument', '')[:300]}")
    print("\n--- diff preview (first 80 lines) ---")
    diff_lines = stage_result.diff.splitlines()
    for ln in diff_lines[:80]:
        print(ln)
    if len(diff_lines) > 80:
        print(f"[diff truncated — {len(diff_lines) - 80} more lines]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
