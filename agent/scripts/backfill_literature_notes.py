"""Backfill literature_notes rows for every paper page already on disk.

Use after a pipeline run where DB writes were silently skipped (e.g. the
pre-dedup-guard era when _run_pipeline_core had `if meta.get("doi"):` gating
all DB writes; DOI-less Drive ingests produced paper pages but zero DB rows).

For each paper page .md file, parses YAML frontmatter, extracts (fingerprint,
doi, title, authors, year, journal), and inserts a literature_notes row using
'sha256:<fp>' as pseudo-DOI when no real DOI exists. Idempotent — existing
rows (matched by pdf_fingerprint) are left alone.

Does NOT backfill paper_findings (findings live in the markdown body and
re-parsing them is brittle; if you need findings in the DB, re-run the ingest
with --force on that paper).

Usage:
    cd /path/to/00-Lab-Agent
    PYTHONPATH="$PWD" ~/.lab-agent-venv/bin/python -m agent.scripts.backfill_literature_notes
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


def _parse_frontmatter(text: str) -> dict:
    """Return a flat dict of scalar frontmatter values. Ignores lists."""
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
        if val.startswith("[") or key in {"topics"}:
            continue  # skip list fields
        out[key] = val
    return out


def main() -> int:
    repo = website_repo_path()
    papers_dir = os.path.join(repo, "knowledge", "papers")
    if not os.path.isdir(papers_dir):
        print(f"No papers dir at {papers_dir}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # Preload existing fingerprints so we can skip without per-row query
    existing_fps = {
        r[0] for r in conn.execute(
            "SELECT pdf_fingerprint FROM literature_notes WHERE pdf_fingerprint IS NOT NULL"
        ).fetchall()
    }
    existing_doi_keys = {
        r[0] for r in conn.execute(
            "SELECT doi FROM literature_notes WHERE doi IS NOT NULL AND doi != ''"
        ).fetchall()
    }

    added = 0
    skipped_already = 0
    skipped_nokey = 0
    errors = 0

    for name in sorted(os.listdir(papers_dir)):
        if not name.endswith(".md") or name == "index.md":
            continue
        path = os.path.join(papers_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            fm = _parse_frontmatter(text)
        except Exception as e:
            print(f"  ! read failed for {name}: {e}")
            errors += 1
            continue

        fingerprint = fm.get("fingerprint_sha256") or ""
        doi = fm.get("doi") or ""
        if not fingerprint and not doi:
            print(f"  ! {name}: no fingerprint or doi in frontmatter — skipping")
            skipped_nokey += 1
            continue

        # Dedup key: prefer fingerprint, fall back to doi
        if fingerprint and fingerprint in existing_fps:
            skipped_already += 1
            continue
        if doi and doi in existing_doi_keys:
            skipped_already += 1
            continue

        # Use real DOI if present; otherwise sha256: pseudo-DOI
        doi_key = doi if doi else (f"sha256:{fingerprint}" if fingerprint else "")
        now = datetime.now(timezone.utc).isoformat()

        year_raw = fm.get("year") or ""
        try:
            year_int = int(year_raw) if year_raw else None
        except ValueError:
            year_int = None

        try:
            conn.execute(
                """INSERT INTO literature_notes
                   (project_id, doi, title, authors, journal, publication_year,
                    raw_abstract, extracted_findings_md, relevance_to_project,
                    pdf_fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    None,
                    doi_key,
                    fm.get("title") or "(unknown)",
                    fm.get("authors") or None,
                    fm.get("journal") or None,
                    year_int,
                    None,
                    "(backfilled from paper page; findings live in markdown body)",
                    None,
                    fingerprint or None,
                    now,
                ),
            )
            added += 1
            if fingerprint:
                existing_fps.add(fingerprint)
            if doi_key:
                existing_doi_keys.add(doi_key)
            print(f"  + {name}  →  doi_key={doi_key[:40]}{'…' if len(doi_key)>40 else ''}")
        except sqlite3.IntegrityError as e:
            # Likely UNIQUE(project_id, doi) conflict — another row already has
            # this pseudo-DOI. Shouldn't happen with our key scheme, but log it.
            print(f"  ~ {name}: integrity error ({e}); skipping")
            skipped_already += 1
        except Exception as e:
            print(f"  ! {name}: insert failed: {e}")
            errors += 1

    conn.commit()
    conn.close()

    print()
    print(f"Backfill summary:")
    print(f"  added:          {added}")
    print(f"  skipped (already in DB): {skipped_already}")
    print(f"  skipped (no key): {skipped_nokey}")
    print(f"  errors:         {errors}")

    # Final count
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM literature_notes").fetchone()[0]
    n_fp = conn.execute(
        "SELECT COUNT(*) FROM literature_notes WHERE pdf_fingerprint IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    print(f"  literature_notes total rows now: {n}  ({n_fp} with pdf_fingerprint)")

    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
