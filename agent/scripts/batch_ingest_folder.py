"""Batch-ingest all PDFs in a Google Drive folder into the lab wiki.

Always runs in dry_run mode — every successful paper stages files under
the lab's GitHub Pages /knowledge/, but nothing is committed to git. At the end,
the user reviews the aggregate diff and commits + pushes once.

Dedup is handled automatically by the wiki_pipeline entry point: any PDF
whose DOI (in paper_findings) or SHA256 fingerprint (in literature_notes)
is already in the DB is skipped with zero API calls. Pass --force to
re-ingest everything regardless.

Skip list: --skip <filename_substring> can be repeated. Filename matching is
case-insensitive substring. Useful for excluding non-research PDFs (funding
reports, out-of-field papers, empty files).

Usage:
    PYTHONPATH=/path/to/00-Lab-Agent ~/.lab-agent-venv/bin/python \\
        -m agent.scripts.batch_ingest_folder <FOLDER_ID> \\
        [--skip "Funding"] [--skip "spine"] [--force] [--max N]

Example (Lab Papers folder, skipping obvious non-research):
    python -m agent.scripts.batch_ingest_folder 1RWYmpFJT6ApIfMmfTATDjR-250wi3ZZM \\
        --skip "Funding Distributions" --skip "circuitry injured spine" \\
        --skip "2022 spines" --skip "fourth report" --skip "DirectRepeateR"

After it finishes, commit with:
    cd ~/Desktop/GitHub/lab-pages && \\
      git add knowledge/ && git commit -m "[tealc] bulk ingest: ..." && git push
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs.wiki_pipeline import run_on_drive_pdf  # noqa: E402
from agent.tools import _get_google_service  # noqa: E402


@dataclass
class PerPaperOutcome:
    name: str
    file_id: str
    status: str = ""            # "success" | "already_ingested" | "error" | "skipped"
    findings_accepted: int = 0
    findings_revised: int = 0
    findings_rejected: int = 0
    cost_usd: float = 0.0
    paths_written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def _list_pdfs_in_folder(folder_id: str) -> list[dict]:
    svc, err = _get_google_service("drive", "v3")
    if err:
        raise RuntimeError(f"Drive auth error: {err}")
    all_files: list[dict] = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' "
              f"and trashed=false",
            pageSize=200, pageToken=page_token,
            fields="nextPageToken, files(id,name,size,modifiedTime)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        all_files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return all_files


def _should_skip(name: str, skip_patterns: list[str]) -> tuple[bool, str]:
    name_l = name.lower()
    for pat in skip_patterns:
        if pat.lower() in name_l:
            return (True, pat)
    return (False, "")


def _run_one(file_id: str, name: str, force: bool) -> PerPaperOutcome:
    start = time.time()
    outcome = PerPaperOutcome(name=name, file_id=file_id)
    try:
        result = run_on_drive_pdf(file_id=file_id, dry_run=True, force=force)
    except Exception as e:
        outcome.status = "error"
        outcome.errors.append(f"{type(e).__name__}: {e}")
        outcome.elapsed_s = time.time() - start
        return outcome

    outcome.status = result.status or "success"
    outcome.findings_accepted = getattr(result, "findings_accepted", 0)
    outcome.findings_revised = getattr(result, "findings_revised", 0)
    outcome.findings_rejected = getattr(result, "findings_rejected", 0)
    outcome.cost_usd = getattr(result, "cost_usd", 0.0)
    outcome.paths_written = list(getattr(result, "paths_written", []))
    outcome.errors = list(getattr(result, "errors", []))
    outcome.elapsed_s = time.time() - start
    return outcome


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Batch-ingest a Drive folder of PDFs.")
    ap.add_argument("folder_id", help="Google Drive folder ID")
    ap.add_argument("--skip", action="append", default=[],
                    help="Filename substring to skip (case-insensitive). Repeatable.")
    ap.add_argument("--force", action="store_true",
                    help="Re-ingest even if paper's DOI/fingerprint is already in DB.")
    ap.add_argument("--max", type=int, default=None,
                    help="Process at most N papers (after skips). Useful for test runs.")
    ap.add_argument("--allow-ids-file", default=None,
                    help="Path to a file of file IDs (one per line, '#'-comments ok). "
                         "Only IDs in this file will be processed; the skip "
                         "patterns still apply on top. Produced by "
                         "classify_drive_authorship.py.")
    args = ap.parse_args(argv)

    allow_ids: set[str] = set()
    if args.allow_ids_file:
        with open(args.allow_ids_file) as f:
            for raw in f:
                fid = raw.split("#", 1)[0].strip()
                if fid:
                    allow_ids.add(fid)
        print(f"Loaded allow-list of {len(allow_ids)} file ID(s) from {args.allow_ids_file}")

    print(f"Listing PDFs in folder {args.folder_id}...")
    files = _list_pdfs_in_folder(args.folder_id)
    print(f"Found {len(files)} PDF file(s).")
    if args.skip:
        print(f"Skip patterns: {args.skip}")

    # Sort by name for deterministic progress
    files.sort(key=lambda f: f["name"])

    to_process: list[tuple[str, str]] = []
    skipped_by_filter: list[str] = []
    skipped_not_allowed: list[str] = []
    for f in files:
        if allow_ids and f["id"] not in allow_ids:
            skipped_not_allowed.append(f["name"])
            continue
        should_skip, pat = _should_skip(f["name"], args.skip)
        if should_skip:
            skipped_by_filter.append(f"{f['name']}  (matches {pat!r})")
            continue
        to_process.append((f["id"], f["name"]))

    if args.max and len(to_process) > args.max:
        print(f"--max={args.max} — truncating from {len(to_process)} to {args.max}")
        to_process = to_process[:args.max]

    if skipped_by_filter:
        print(f"\nSkipped by filter ({len(skipped_by_filter)}):")
        for s in skipped_by_filter:
            print(f"  · {s}")

    print(f"\nWill attempt: {len(to_process)} paper(s)\n")
    if not to_process:
        return 0

    outcomes: list[PerPaperOutcome] = []
    total_start = time.time()

    for i, (fid, name) in enumerate(to_process, start=1):
        prefix = f"[{i:2d}/{len(to_process)}]"
        print(f"{prefix} {name}  ... ", end="", flush=True)
        oc = _run_one(fid, name, args.force)
        outcomes.append(oc)
        status_emoji = {
            "success": "✓",
            "already_ingested": "⟲ skipped (already in DB)",
            "dry_run": "✓",
            "error": "✗ ERROR",
            "partial": "⚠ partial",
        }.get(oc.status, oc.status)
        cost_str = f"${oc.cost_usd:.3f}" if oc.cost_usd > 0 else "$0.000"
        print(f"{status_emoji}  {cost_str}  ({oc.elapsed_s:.0f}s)")
        if oc.errors and oc.status not in ("already_ingested",):
            for e in oc.errors[:2]:
                print(f"     └─ {e[:200]}")

    total_elapsed = time.time() - total_start

    # --- Summary ---
    n_success = sum(1 for o in outcomes if o.status in ("success", "dry_run"))
    n_already = sum(1 for o in outcomes if o.status == "already_ingested")
    n_error = sum(1 for o in outcomes if o.status == "error")
    n_partial = sum(1 for o in outcomes if o.status == "partial")
    total_cost = sum(o.cost_usd for o in outcomes)
    total_findings = sum(o.findings_accepted + o.findings_revised for o in outcomes)
    all_paths = sorted({p for o in outcomes for p in o.paths_written})

    print(f"\n{'=' * 70}")
    print(f"Batch complete in {total_elapsed:.0f}s "
          f"({total_elapsed / 60:.1f} min)")
    print(f"  ✓ success:          {n_success}")
    print(f"  ⟲ already ingested: {n_already}")
    print(f"  ⚠ partial:          {n_partial}")
    print(f"  ✗ error:            {n_error}")
    print(f"  Findings (accepted+revised): {total_findings}")
    print(f"  Files staged:       {len(all_paths)}")
    print(f"  Total API cost:     ${total_cost:.2f}")

    if n_error > 0:
        print(f"\nErrors:")
        for o in outcomes:
            if o.status == "error":
                print(f"  ✗ {o.name}")
                for e in o.errors[:3]:
                    print(f"      {e[:300]}")

    print(f"\nNext step — review the diff, then commit + push once:")
    print(f"  cd ~/Desktop/GitHub/lab-pages")
    print(f"  git diff --stat knowledge/")
    print(f"  git add knowledge/ && git commit -m '[tealc] bulk ingest' && git push")

    return 0 if n_error == 0 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
