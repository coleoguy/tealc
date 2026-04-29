"""
export_batch.py — CLI and library for generating blinded evaluation batches.

Usage
-----
python -m evaluations.export_batch \\
    --kind hypothesis \\
    --domain chromosomal_evolution \\
    --since 2025-01-01 \\
    --until 2025-06-30 \\
    --min-score 3 \\
    --out evaluations/batches/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

VALID_KINDS = ["grant_draft", "hypothesis", "analysis", "literature_synthesis"]
VALID_DOMAINS = [
    "chromosomal_evolution",
    "comparative_genomics",
    "sex_chromosome_evolution",
    "macroevolution",
]


def _default_since() -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=30)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_until() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_batch_id() -> str:
    return "batch_" + datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


def build_batch(
    kind: str,
    domain: str,
    since: str,
    until: str,
    min_score: int | None,
    out_dir: str,
    batch_id: str,
) -> None:
    """
    Core logic: query ledger, blind entries, write three output files.
    """
    # ------------------------------------------------------------------
    # 1. Import ledger — hard fail if not available
    # ------------------------------------------------------------------
    try:
        from agent.ledger import query_outputs
    except ImportError:
        print("ERROR: agent.ledger not available — run Phase 1 Agent A first")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Query
    # ------------------------------------------------------------------
    raw_entries = query_outputs(
        kind=kind,
        since_iso=since,
        until_iso=until,
        min_score=min_score,
        limit=200,
    )

    if not raw_entries:
        print(f"No entries found for kind={kind!r}, domain={domain!r}, since={since}, until={until}")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 3. Blind entries
    # ------------------------------------------------------------------
    from evaluations.blind import blind_entry
    from evaluations.schema import AnswerKey, EvalBatch, EvalInput, to_jsonl

    pseudonym_map: dict[str, str] = {}
    blinded: list[EvalInput] = []
    key_mapping: dict[str, int] = {}

    for entry in raw_entries:
        # Filter by domain if the entry carries one
        entry_domain = entry.get("domain", domain)
        if entry_domain and entry_domain != domain:
            continue
        # Ensure domain is set on the entry before blinding
        entry.setdefault("domain", domain)

        ei = blind_entry(entry, pseudonym_map=pseudonym_map)
        # Force domain to the requested one if entry didn't carry it
        if not ei.domain:
            ei = EvalInput(
                blinded_id=ei.blinded_id,
                kind=ei.kind,
                content=ei.content,
                domain=domain,
                created_iso=ei.created_iso,
                context_hint=ei.context_hint,
            )
        blinded.append(ei)
        ledger_id = entry.get("ledger_id") or entry.get("id")
        key_mapping[ei.blinded_id] = int(ledger_id) if ledger_id is not None else -1

    if not blinded:
        print(f"No entries matched domain={domain!r} after filtering.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 4. Write output files
    # ------------------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)

    # -- 4a. Blinded JSONL (sent to reviewers) --
    blinded_path = os.path.join(out_dir, f"{batch_id}.jsonl")
    with open(blinded_path, "w", encoding="utf-8") as fh:
        fh.write(to_jsonl(blinded))
        fh.write("\n")

    # -- 4b. Answer key (PRIVATE) --
    answer_key = AnswerKey(batch_id=batch_id, mapping=key_mapping)
    key_path = os.path.join(out_dir, f"{batch_id}_key.jsonl")
    with open(key_path, "w", encoding="utf-8") as fh:
        fh.write(to_jsonl([answer_key]))
        fh.write("\n")

    # -- 4c. Manifest --
    created_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "batch_id": batch_id,
        "created_iso": created_iso,
        "domain": domain,
        "kind_filter": kind,
        "date_range": [since, until],
        "entry_count": len(blinded),
        "rubric_pointer": f"evaluations/rubrics/{domain}.md",
        "private_key_file": f"{batch_id}_key.jsonl",
        "note": (
            "IMPORTANT: The file listed in private_key_file is the answer key. "
            "Do NOT distribute it to reviewers."
        ),
        "instructions": (
            f"You are reviewing {len(blinded)} items in the domain of {domain.replace('_', ' ')}. "
            f"Each item is identified by a blinded_id. "
            f"For each item, score the five dimensions (rigor, novelty, grounding, clarity, feasibility) "
            f"on a 1-5 integer scale following the criteria in {domain}.md. "
            f"Return your completed review_template.md to the study coordinator by email."
        ),
    }
    manifest_path = os.path.join(out_dir, f"{batch_id}_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print(f"Batch {batch_id}: {len(blinded)} entries written to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a blinded evaluation batch from the Tealc ledger.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=VALID_KINDS,
        help="Output kind to include in this batch.",
    )
    parser.add_argument(
        "--domain",
        required=True,
        choices=VALID_DOMAINS,
        help="Biological domain for this batch.",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="ISO_DATE",
        help="Include entries on or after this ISO date (default: 30 days ago).",
    )
    parser.add_argument(
        "--until",
        default=None,
        metavar="ISO_DATE",
        help="Include entries on or before this ISO date (default: now).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        metavar="N",
        help="Only include entries with critic_score >= N.",
    )
    parser.add_argument(
        "--out",
        default="evaluations/batches/",
        metavar="DIR",
        help="Output directory for batch files.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        metavar="BATCH_ID",
        help="Override the generated batch ID.",
    )

    args = parser.parse_args()

    since = args.since or _default_since()
    until = args.until or _default_until()
    batch_id = args.batch_id or _default_batch_id()

    build_batch(
        kind=args.kind,
        domain=args.domain,
        since=since,
        until=until,
        min_score=args.min_score,
        out_dir=args.out,
        batch_id=batch_id,
    )


if __name__ == "__main__":
    main()
