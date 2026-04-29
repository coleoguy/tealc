"""Classify authorship role for every PDF in a Drive folder.

For each PDF, download + extract first-page text + ask Haiku whether a named
author ('Blackmon' by default) is the FIRST, LAST, MIDDLE, or NOT_AUTHOR.
Prints a classification report + an allow-list of file IDs matching
specified role(s). That allow-list is then fed to batch_ingest_folder via
--allow-ids-file.

Cost: ~$0.001 per paper (Haiku on 800-char first-page snippet).

Usage:
    PYTHONPATH=/path/to/00-Lab-Agent ~/.lab-agent-venv/bin/python \\
        -m agent.scripts.classify_drive_authorship <FOLDER_ID> \\
        [--author "Blackmon"] \\
        [--roles first,last] \\
        [--output-file /tmp/allow_ids.txt]

The --roles option controls which classifications end up in the allow-list
(comma-separated: first, last, first_or_last, middle, not_author). Default
is first_or_last.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.tools import _get_google_service  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.jobs.wiki_pipeline import (  # noqa: E402
    _resolve_drive_source, _extract_pdf_text,
)

_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
_CLASSIFIER_SYSTEM = """You are classifying the authorship role of a named
scientist on a scientific paper. You are given (a) the author's surname,
(b) the first ~800 characters of text extracted from the paper's first
page, which typically includes the title and author list.

Respond with EXACTLY ONE of these tokens — nothing else:
  FIRST         — the named author is the first listed author
  LAST          — the named author is the last listed author (senior author)
  MIDDLE        — the named author is in the author list but is neither first nor last
  NOT_AUTHOR    — the named author's surname does not appear in the author list
  UNCERTAIN     — the text is too garbled or the author list too ambiguous to classify

Rules:
- "First" means position 1 in the author list.
- "Last" means the final position (common convention for senior/PI author).
- If there are only two authors and the named author is one of them, they are
  either FIRST or LAST (never MIDDLE).
- If the surname appears in an affiliation line (e.g. "Blackmon Lab at TAMU"),
  that alone does NOT make them an author — they must be in the actual
  author list.
- For ambiguous initials like "H. Blackmon" vs "Heath Blackmon", either counts.
- Reply with just one token. No period, no explanation."""


@dataclass
class Classification:
    file_id: str
    name: str
    role: str                   # FIRST | LAST | MIDDLE | NOT_AUTHOR | UNCERTAIN | ERROR
    reason: str = ""            # short debug info
    cost_usd: float = 0.0
    text_preview: str = ""      # first 120 chars of extracted PDF text


def _list_pdfs(folder_id: str) -> list[dict]:
    svc, err = _get_google_service("drive", "v3")
    if err:
        raise RuntimeError(f"Drive auth: {err}")
    out: list[dict] = []
    page = None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            pageSize=200, pageToken=page,
            fields="nextPageToken, files(id,name,size)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        out.extend(resp.get("files", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def _classify_one(client: Anthropic, file_id: str, name: str,
                  author_surname: str) -> Classification:
    c = Classification(file_id=file_id, name=name, role="ERROR")
    try:
        _, meta = _resolve_drive_source(file_id)
    except Exception as e:
        c.reason = f"download failed: {type(e).__name__}: {e}"
        return c

    try:
        text = _extract_pdf_text(meta["local_pdf_path"]) or ""
    except Exception as e:
        c.reason = f"pdf text extract failed: {e}"
        return c

    if not text.strip():
        c.role = "UNCERTAIN"
        c.reason = "pdf produced empty text (scanned? image-only?)"
        return c

    head = text[:800]
    c.text_preview = head[:120].replace("\n", " ")

    user_msg = (
        f"Author surname to classify: {author_surname}\n\n"
        f"First ~800 chars of the paper's first page:\n\n{head}"
    )
    try:
        msg = client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=10,
            system=_CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        c.reason = f"anthropic api error: {type(e).__name__}: {e}"
        return c

    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    record_call(job_name="classify_drive_authorship",
                model=_CLASSIFIER_MODEL, usage=usage)
    c.cost_usd = (usage["input_tokens"] * 1.0 + usage["output_tokens"] * 5.0) / 1_000_000

    raw = (msg.content[0].text or "").strip().upper()
    # Tolerate a bit of model noise like a trailing period
    raw = raw.rstrip(".").strip()
    if raw in ("FIRST", "LAST", "MIDDLE", "NOT_AUTHOR", "UNCERTAIN"):
        c.role = raw
    else:
        c.role = "UNCERTAIN"
        c.reason = f"unexpected model response: {raw!r}"
    return c


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Classify authorship for PDFs in a Drive folder.")
    ap.add_argument("folder_id", help="Google Drive folder ID")
    ap.add_argument("--author", default="Blackmon",
                    help="Author surname to classify (default: Blackmon)")
    ap.add_argument("--roles", default="first_or_last",
                    help="Comma-separated roles to include in allow-list. "
                         "Options: first, last, first_or_last, middle, not_author, uncertain")
    ap.add_argument("--output-file", default=None,
                    help="Write allow-list of matching file IDs here (one per line, "
                         "with filename as '# comment'). Default: print to stdout.")
    args = ap.parse_args(argv)

    allow_roles = set()
    for tok in args.roles.split(","):
        tok = tok.strip().lower()
        if tok == "first_or_last":
            allow_roles.update({"FIRST", "LAST"})
        elif tok in ("first", "last", "middle", "not_author", "uncertain"):
            allow_roles.add(tok.upper())
        else:
            print(f"unknown role: {tok!r}", file=sys.stderr)
            return 1

    print(f"Listing PDFs in folder {args.folder_id}...")
    files = sorted(_list_pdfs(args.folder_id), key=lambda f: f["name"])
    print(f"Found {len(files)} PDF(s). Classifying authorship "
          f"(surname='{args.author}')...\n")

    client = Anthropic()
    results: list[Classification] = []
    total_cost = 0.0
    start = time.time()

    for i, f in enumerate(files, start=1):
        prefix = f"[{i:2d}/{len(files)}]"
        print(f"{prefix} {f['name']:<48s}", end="  ", flush=True)
        c = _classify_one(client, f["id"], f["name"], args.author)
        results.append(c)
        total_cost += c.cost_usd
        marker = "✓" if c.role in allow_roles else "·"
        extra = f"  — {c.reason[:60]}" if c.reason else ""
        print(f"{marker} {c.role:<12s}  ${c.cost_usd:.4f}{extra}")

    elapsed = time.time() - start

    # Summary
    role_counts: dict[str, int] = {}
    for c in results:
        role_counts[c.role] = role_counts.get(c.role, 0) + 1

    print(f"\n{'=' * 72}")
    print(f"Classified {len(results)} paper(s) in {elapsed:.0f}s "
          f"for ${total_cost:.3f}")
    for role in ("FIRST", "LAST", "MIDDLE", "NOT_AUTHOR", "UNCERTAIN", "ERROR"):
        if role in role_counts:
            mark = "✓" if role in allow_roles else " "
            print(f"  {mark} {role:<12s} {role_counts[role]:3d}")

    allow_list = [c for c in results if c.role in allow_roles]
    print(f"\nPapers matching allow-list ({args.roles}): {len(allow_list)}")

    # Write or print the allow-list
    if args.output_file:
        with open(args.output_file, "w") as f:
            for c in allow_list:
                f.write(f"{c.file_id}  # {c.name} [{c.role}]\n")
        print(f"Allow-list written to {args.output_file}")
    else:
        print("\nAllow-list (file_id  # name [role]):")
        for c in allow_list:
            print(f"  {c.file_id}  # {c.name} [{c.role}]")

    # Show the non-matches for review
    non_matches = [c for c in results if c.role not in allow_roles]
    if non_matches:
        print(f"\nNOT in allow-list ({len(non_matches)}) — eyeball for misclassifications:")
        for c in non_matches:
            print(f"  {c.name}  [{c.role}]"
                  + (f"  — {c.reason[:80]}" if c.reason else "")
                  + (f"  preview={c.text_preview!r}" if c.role in ("UNCERTAIN", "ERROR") else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
