"""
run_reviewer_circle.py — One-shot orchestrator for the Tealc Live Reviewer Circle.

All phases are idempotent: re-running is always safe.

Usage
-----
# Run all three phases in sequence:
python -m evaluations.run_reviewer_circle all

# Individual phases:
python -m evaluations.run_reviewer_circle phase_a   # backfill ledger + build manifest
python -m evaluations.run_reviewer_circle phase_b   # draft reviewer invitation emails
python -m evaluations.run_reviewer_circle phase_c   # ingest replies + compute correlations

# Dry-run (no writes, no API calls):
python -m evaluations.run_reviewer_circle all --dry-run

Notes
-----
- This is NOT a scheduled job. Heath fires it manually.
- Phase A backfills from existing tables; it does NOT generate new artifacts.
- Phase B creates Gmail DRAFTS only — Heath must open Gmail and click Send.
- Phase C polls the 'reviewer-circle-replies/' Gmail label for returned templates.
"""
from __future__ import annotations

import argparse
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)


def _phase_a(dry_run: bool) -> None:
    print("=" * 60)
    print("PHASE A — Backfill output_ledger + build manifest")
    print("=" * 60)
    from agent.scripts.backfill_output_ledger import backfill  # noqa: PLC0415
    result = backfill(dry_run=dry_run)
    print(f"\nPhase A summary: {result}")


def _phase_b(dry_run: bool) -> None:
    print("=" * 60)
    print("PHASE B — Draft reviewer invitation emails")
    print("=" * 60)
    print(
        "\nNote: drafts are saved to Gmail — open Gmail to confirm and send each one.\n"
    )
    from evaluations.send_reviewer_invitations import send_invitations  # noqa: PLC0415
    send_invitations(dry_run=dry_run)


def _phase_c(dry_run: bool) -> None:
    print("=" * 60)
    print("PHASE C — Ingest replies + compute correlations")
    print("=" * 60)
    from evaluations.ingest_reviews import ingest_reviews  # noqa: PLC0415
    ingest_reviews(dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tealc Live Reviewer Circle — one-shot orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "phase",
        choices=["phase_a", "phase_b", "phase_c", "all"],
        help="Which phase to run (or 'all' for all three in sequence).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan / plan without writing to DB, Gmail, or disk.",
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        print("\n[DRY RUN MODE — no writes, no emails, no DB changes]\n")

    if args.phase in ("phase_a", "all"):
        _phase_a(dry_run=dry_run)

    if args.phase in ("phase_b", "all"):
        _phase_b(dry_run=dry_run)

    if args.phase in ("phase_c", "all"):
        _phase_c(dry_run=dry_run)

    print("\nReviewer circle run complete.")


if __name__ == "__main__":
    main()
