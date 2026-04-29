"""Backfill paper_findings rows from paper-page markdown bodies.

The pipeline writes findings to BOTH the paper-page .md file AND the
paper_findings DB table. If the DB write was skipped (pre-fix era, when the
`if meta.get("doi"):` guard gated DOI-less papers) the markdown is the only
source of truth. This script round-trips the structured markdown format back
into DB rows so retry_topic.py, rebuild_topic_pages.py, and the dedup guard
have a full picture.

Deterministic parse — no LLM needed. The format is what _compose_paper_page
emits, regex-parseable. Idempotent — skips papers that already have rows.

Usage:
    PYTHONPATH=/path/to/00-Lab-Agent ~/.lab-agent-venv/bin/python \\
        -m agent.scripts.backfill_paper_findings
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.scheduler import DB_PATH  # noqa: E402
from agent.jobs.website_git import website_repo_path  # noqa: E402


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_ANCHOR_RE = re.compile(r'<a id="finding-(\d+)"></a>')
_FINDING_TITLE_RE = re.compile(r'^### Finding \d+ — (.+?)$', re.MULTILINE)
_QUOTE_RE = re.compile(r'^> (.+?)$', re.MULTILINE)
_PAGE_RE = re.compile(r'^— p\. (.+?)$', re.MULTILINE)
# Reasoning / counter: allow multi-line, stop at next starred field or end of block
_REASONING_RE = re.compile(
    r'\*Why this is citable:\* (.+?)(?=\n\n\*|\n<a id=|\Z)', re.DOTALL,
)
_COUNTER_RE = re.compile(
    r'\*Counter / limitation:\* (.+?)(?=\n\n\*|\n<a id=|\Z)', re.DOTALL,
)
_TOPICS_LINE_RE = re.compile(r'^\*Topics:\*\s*(.+?)$', re.MULTILINE)
_TOPIC_LINK_RE = re.compile(r'/knowledge/topics/([A-Za-z0-9_]+)/')


def parse_findings_from_body(body: str) -> list[dict]:
    """Return a list of finding dicts parsed from a paper-page body."""
    anchors = list(_ANCHOR_RE.finditer(body))
    findings: list[dict] = []
    for i, m in enumerate(anchors):
        block_start = m.start()
        block_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(body)
        block = body[block_start:block_end]
        idx = int(m.group(1))

        t = _FINDING_TITLE_RE.search(block)
        q = _QUOTE_RE.search(block)
        p = _PAGE_RE.search(block)
        r = _REASONING_RE.search(block)
        c = _COUNTER_RE.search(block)
        tl = _TOPICS_LINE_RE.search(block)

        finding_text = t.group(1).strip() if t else ""
        quote = q.group(1).strip() if q else ""
        if not finding_text or not quote:
            continue

        page = p.group(1).strip() if p else None
        reasoning = r.group(1).strip() if r else ""
        counter = c.group(1).strip() if c else ""
        topic_tags = _TOPIC_LINK_RE.findall(tl.group(1)) if tl else []

        findings.append({
            "idx": idx,
            "finding_text": finding_text,
            "quote": quote,
            "page": page,
            "reasoning": reasoning,
            "counter": counter,
            "topic_tags": topic_tags,
        })
    return findings


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fm = text[3:end]
    out: dict = {}
    for raw in fm.splitlines():
        line = raw.strip()
        if ":" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if val.startswith("[") and key != "topics":
            continue
        out[key] = val
    return out


def paper_key(fm: dict) -> str:
    """Canonical paper identity: real DOI > sha256:<fp>."""
    doi = (fm.get("doi") or "").strip()
    if doi:
        return doi
    fp = (fm.get("fingerprint_sha256") or "").strip()
    if fp:
        return f"sha256:{fp}"
    return ""


# ---------------------------------------------------------------------------
# DB ops
# ---------------------------------------------------------------------------

def existing_keys() -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT DISTINCT doi FROM paper_findings").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def insert_findings(key: str, findings: list[dict], fingerprint: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    n = 0
    try:
        # Remove any pre-existing stragglers before insert to keep idempotent
        conn.execute("DELETE FROM paper_findings WHERE doi=?", (key,))
        for f in findings:
            try:
                conn.execute(
                    """INSERT INTO paper_findings
                       (doi, finding_idx, finding_text, quote, page, reasoning,
                        counter, topic_tags, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        f["idx"],
                        f["finding_text"],
                        f["quote"],
                        f["page"],
                        f["reasoning"],
                        f["counter"],
                        ",".join(f["topic_tags"]),
                        now,
                    ),
                )
                n += 1
            except sqlite3.IntegrityError as e:
                print(f"    ! insert conflict on finding {f['idx']}: {e}")
        conn.commit()
    finally:
        conn.close()
    return n


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    repo = website_repo_path()
    papers_dir = os.path.join(repo, "knowledge", "papers")
    already = existing_keys()

    summary = {
        "processed": 0,
        "already_in_db": 0,
        "no_findings_in_body": 0,
        "newly_inserted_keys": 0,
        "newly_inserted_rows": 0,
        "errors": 0,
    }

    for name in sorted(os.listdir(papers_dir)):
        if not name.endswith(".md") or name == "index.md":
            continue
        path = os.path.join(papers_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            fm = parse_frontmatter(text)
        except Exception as e:
            print(f"  ! {name}: read failed: {e}")
            summary["errors"] += 1
            continue

        key = paper_key(fm)
        if not key:
            print(f"  ! {name}: no paper key (no doi or fingerprint) — skipping")
            summary["errors"] += 1
            continue

        # Body comes after the second `---`
        fm_end = text.find("\n---", 3)
        body = text[fm_end + 4:] if fm_end >= 0 else text

        findings = parse_findings_from_body(body)
        summary["processed"] += 1

        if key in already:
            summary["already_in_db"] += 1
            continue

        if not findings:
            print(f"  · {name}: no finding blocks in body — skipping")
            summary["no_findings_in_body"] += 1
            continue

        inserted = insert_findings(key, findings, fm.get("fingerprint_sha256") or "")
        summary["newly_inserted_keys"] += 1
        summary["newly_inserted_rows"] += inserted
        already.add(key)
        print(f"  + {name}: {inserted} finding(s) inserted under key={key[:40]}{'…' if len(key)>40 else ''}")

    # Final stats
    conn = sqlite3.connect(DB_PATH)
    try:
        unique = conn.execute("SELECT COUNT(DISTINCT doi) FROM paper_findings").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM paper_findings").fetchone()[0]
    finally:
        conn.close()

    print()
    print("Backfill summary:")
    print(f"  paper files processed:  {summary['processed']}")
    print(f"  already in paper_findings: {summary['already_in_db']}")
    print(f"  no finding blocks found:   {summary['no_findings_in_body']}")
    print(f"  newly inserted papers:     {summary['newly_inserted_keys']}")
    print(f"  newly inserted rows:       {summary['newly_inserted_rows']}")
    print(f"  errors:                    {summary['errors']}")
    print()
    print(f"paper_findings now holds {unique} distinct paper key(s) / {total} total rows")

    return 0 if summary["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
