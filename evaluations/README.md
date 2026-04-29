# Evaluations Harness

## Why this exists

This harness implements Proposal Claim 17.b: blinded external peer review of Tealc outputs across four biological domains. It is the single infrastructure component not yet deployed at the time of the Google.org Impact Challenge finalist review. The goal is to allow domain experts outside the lab to evaluate Tealc's outputs without knowing they came from an AI system or which lab produced them. Results feed directly into the grant's human-evaluation metrics reported to funders.

## The three-stage flow

**Stage 1 — Query the ledger.** `export_batch.py` calls `agent.ledger.query_outputs()` to retrieve stored outputs filtered by kind, domain, date window, and optional quality threshold. The ledger is the canonical store of everything Tealc has produced.

**Stage 2 — Blind.** Each entry passes through `blind.py`, which strips system and model names, replaces lab-member names with stable pseudonyms (Person_A, Person_B, ...), and replaces project identifiers. Timestamps are rounded to the Monday of the week to prevent temporal fingerprinting. A fresh UUID4 becomes the blinded_id for each item.

**Stage 3 — Export the batch.** Three files land in `evaluations/batches/`: the blinded JSONL sent to reviewers, a private answer key mapping blinded_ids back to ledger IDs, and a manifest with reviewer instructions and a rubric pointer.

## Generating a batch for a reviewer

```bash
cd "/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent"
python -m evaluations.export_batch \
    --kind hypothesis \
    --domain chromosomal_evolution \
    --since 2025-01-01 \
    --until 2025-06-30 \
    --min-score 3 \
    --out evaluations/batches/
```

This writes `batch_YYYYMMDDTHHMMSS.jsonl`, `batch_YYYYMMDDTHHMMSS_key.jsonl`, and `batch_YYYYMMDDTHHMMSS_manifest.json` to the output directory. Hand the reviewer only the `.jsonl` and `_manifest.json` files.

## How the answer key stays private

The `_key.jsonl` file is never distributed to reviewers. It maps blinded UUIDs back to original ledger IDs so scores can be reconciled after review. Store it locally, treat it like a codebook. The manifest explicitly names the key file but marks it as private. Do not include it in any email or shared folder given to reviewers.

## Rubrics

Domain-specific evaluation criteria live in `evaluations/rubrics/`:

- `chromosomal_evolution.md`
- `comparative_genomics.md`
- `sex_chromosome_evolution.md`
- `macroevolution.md`

Each rubric defines what counts as on-topic, field-typical standards for a testable hypothesis, what "grounding" means in that domain, and red flags to watch for. Heath will refine these; they are functional starting points.

## How blinded results come back

Reviewers fill in `evaluations/review_template.md` — one score block per entry, using the blinded_id printed at the top of each item. When complete, reviewers return the filled template by email to the PI. File completed review templates in `evaluations/completed_reviews/` (create as needed). Reconciliation against the answer key is a manual step: match blinded_id in the completed template to blinded_id in `_key.jsonl` to recover the original ledger entry for each score.
