"""
send_reviewer_invitations.py — Phase B of the Live Reviewer Circle pipeline.

Reads data/reviewer_circle/reviewers.json and data/reviewer_circle/manifest.json.
For each reviewer, selects the matching domain batch, builds a blinded JSONL
via export_batch.build_batch(), composes a draft email with attachments, and
saves it via Gmail draft API. Inserts a row into reviewer_invitations.

Draft mode is the ONLY mode — Tealc never sends automatically. Heath confirms
in Gmail before sending.

Usage
-----
python -m evaluations.send_reviewer_invitations [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)

from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import query_outputs  # noqa: E402
from evaluations.blind import blind_entry  # noqa: E402
from evaluations.schema import to_jsonl, EvalInput  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REVIEWERS_PATH = os.path.join(_ROOT, "data", "reviewer_circle", "reviewers.json")
MANIFEST_PATH = os.path.join(_ROOT, "data", "reviewer_circle", "manifest.json")
REVIEW_TEMPLATE_PATH = os.path.join(_ROOT, "data", "reviewer_circle", "review_template.md")
BATCHES_DIR = os.path.join(_ROOT, "data", "reviewer_circle", "batches")

EMAIL_SUBJECT = (
    "Tealc Reviewer Circle: 7-day blind peer-review pilot — 10 min skim"
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def _ensure_reviewer_invitations_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviewer_invitations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer_pseudonym TEXT NOT NULL,
            domain            TEXT NOT NULL,
            batch_id          TEXT NOT NULL,
            draft_id          TEXT,
            status            TEXT NOT NULL DEFAULT 'draft',
            sla_iso           TEXT NOT NULL,
            sent_at           TEXT,
            created_at        TEXT NOT NULL,
            UNIQUE(reviewer_pseudonym, domain)
        )
    """)
    conn.commit()


def _pseudonymize_email(email: str) -> str:
    """Return a stable SHA-256-based pseudonym for an email address."""
    return "reviewer_" + hashlib.sha256(email.lower().encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Build JSONL batch from ledger IDs
# ---------------------------------------------------------------------------

def _build_batch_from_ids(domain: str, ledger_ids: list[int]) -> tuple[list[EvalInput], dict[str, int]]:
    """
    Blind ledger rows for the given IDs and return (blinded_inputs, key_mapping).
    key_mapping: blinded_id -> original ledger_id.
    """
    from agent.ledger import get_entry  # noqa: PLC0415
    pseudonym_map: dict[str, str] = {}
    blinded: list[EvalInput] = []
    key_mapping: dict[str, int] = {}

    for lid in ledger_ids:
        row = get_entry(lid)
        if row is None:
            continue
        row.setdefault("domain", domain)
        # Remap content key
        if not row.get("content") and row.get("content_md"):
            row["content"] = row["content_md"]
        ei = blind_entry(row, pseudonym_map=pseudonym_map)
        if not ei.domain:
            from evaluations.schema import EvalInput as EI  # noqa: PLC0415
            ei = EI(
                blinded_id=ei.blinded_id,
                kind=ei.kind,
                content=ei.content,
                domain=domain,
                created_iso=ei.created_iso,
                context_hint=ei.context_hint,
            )
        blinded.append(ei)
        key_mapping[ei.blinded_id] = lid

    return blinded, key_mapping


# ---------------------------------------------------------------------------
# Gmail draft creation
# ---------------------------------------------------------------------------

def _create_gmail_draft(to_email: str, subject: str, body: str,
                        attachments: list[tuple[str, str, bytes]]) -> str | None:
    """
    Create a Gmail draft with optional attachments.

    attachments: list of (filename, mimetype, bytes_content)
    Returns draft_id string, or None on failure.
    """
    try:
        # Build MIME message
        if attachments:
            msg = MIMEMultipart()
        else:
            msg = MIMEMultipart()

        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        for filename, mimetype, content_bytes in attachments:
            part = MIMEBase(*mimetype.split("/", 1))
            part.set_payload(content_bytes)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=filename,
            )
            msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        # Get Gmail service
        import importlib
        tools_mod = importlib.import_module("agent.tools")
        svc_fn = getattr(tools_mod, "_get_google_service", None)
        if svc_fn is None:
            print("  [warn] _get_google_service not found in agent.tools — skip draft creation")
            return None
        service, err = svc_fn("gmail", "v1")
        if err:
            print(f"  [warn] Gmail service unavailable: {err}")
            return None

        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
        return draft["id"]
    except Exception as e:
        print(f"  [error] Gmail draft creation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Compose email body
# ---------------------------------------------------------------------------

def _compose_body(reviewer: dict, domain: str, item_count: int) -> str:
    pseudonym = reviewer.get("pseudonym", "Colleague")
    return f"""Dear {pseudonym},

Thank you for agreeing to participate in the Tealc Reviewer Circle, a blind peer-review pilot.

You have been matched to the domain: {domain.replace('_', ' ').title()}

What we are asking:
  - Review {item_count} blinded research output(s) — about 10 minutes total.
  - Score each item on 5 dimensions (rigor, novelty, grounding, clarity, feasibility) using the attached review_template.md.
  - Return your completed template to this email thread within 7 days.

Attachments:
  - items.jsonl: the blinded outputs (one JSON object per line, field "content")
  - rubric_{domain}.md: scoring criteria for this domain
  - review_template.md: the form to complete and return

The outputs are fully de-identified. Please do not attempt to identify the author or system.

Questions? Reply to this email.

Thank you for your time.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def send_invitations(dry_run: bool = False) -> None:
    # Load reviewers
    if not os.path.exists(REVIEWERS_PATH):
        print(f"ERROR: {REVIEWERS_PATH} not found. Run Phase A first and fill in reviewers.json.")
        sys.exit(1)
    with open(REVIEWERS_PATH, encoding="utf-8") as fh:
        reviewers_data = json.load(fh)
    reviewers: list[dict] = reviewers_data.get("reviewers", [])

    # Load manifest
    if not os.path.exists(MANIFEST_PATH):
        print(f"ERROR: {MANIFEST_PATH} not found. Run Phase A (backfill) first.")
        sys.exit(1)
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        manifest = json.load(fh)
    domain_ids: dict[str, list[int]] = manifest.get("domains", {})

    # Load review template bytes
    template_bytes = b""
    if os.path.exists(REVIEW_TEMPLATE_PATH):
        with open(REVIEW_TEMPLATE_PATH, "rb") as fh:
            template_bytes = fh.read()

    os.makedirs(BATCHES_DIR, exist_ok=True)

    conn = _conn()
    _ensure_reviewer_invitations_table(conn)

    now_utc = datetime.now(timezone.utc)
    sla_iso = (now_utc + timedelta(days=7)).isoformat()
    batch_ts = now_utc.strftime("%Y%m%dT%H%M%S")

    sent_count = 0
    skipped_count = 0

    for reviewer in reviewers:
        email = reviewer.get("email", "")
        pseudonym = reviewer.get("pseudonym", "")
        expertise = reviewer.get("expertise_tags", [])
        status = reviewer.get("status", "active")

        if not pseudonym:
            print(f"  [skip] Reviewer missing pseudonym: {reviewer}")
            continue
        if not email or "TODO" in email:
            print(f"  [skip] {pseudonym}: email not filled in (TODO placeholder)")
            skipped_count += 1
            continue
        if status != "active":
            print(f"  [skip] {pseudonym}: status={status}")
            skipped_count += 1
            continue

        # Pick domain matching expertise
        domain = _pick_domain(expertise, domain_ids)
        ledger_ids = domain_ids.get(domain, [])
        if not ledger_ids:
            print(f"  [skip] {pseudonym}: no ledger items for domain={domain}")
            skipped_count += 1
            continue

        # Check idempotency
        existing = conn.execute(
            "SELECT id FROM reviewer_invitations WHERE reviewer_pseudonym=? AND domain=?",
            (pseudonym, domain),
        ).fetchone()
        if existing:
            print(f"  [skip] {pseudonym}: invitation for {domain} already exists")
            skipped_count += 1
            continue

        # Build blinded batch
        blinded, key_mapping = _build_batch_from_ids(domain, ledger_ids)
        if not blinded:
            print(f"  [skip] {pseudonym}: no blinded items produced for domain={domain}")
            skipped_count += 1
            continue

        # Write batch files
        batch_id = f"batch_{batch_ts}_{pseudonym}_{domain}"
        jsonl_path = os.path.join(BATCHES_DIR, f"{batch_id}.jsonl")
        key_path = os.path.join(BATCHES_DIR, f"{batch_id}_key.jsonl")
        if not dry_run:
            with open(jsonl_path, "w", encoding="utf-8") as fh:
                fh.write(to_jsonl(blinded) + "\n")
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"batch_id": batch_id, "mapping": key_mapping}) + "\n")

        # Load rubric
        rubric_path = os.path.join(_ROOT, "evaluations", "rubrics", f"{domain}.md")
        rubric_bytes = b""
        if os.path.exists(rubric_path):
            with open(rubric_path, "rb") as fh:
                rubric_bytes = fh.read()

        # Compose email
        body = _compose_body(reviewer, domain, len(blinded))
        jsonl_bytes = to_jsonl(blinded).encode("utf-8")
        attachments = [
            ("items.jsonl", "application/jsonl", jsonl_bytes),
            (f"rubric_{domain}.md", "text/markdown", rubric_bytes),
            ("review_template.md", "text/markdown", template_bytes),
        ]

        draft_id = None
        invitation_status = "draft"
        if not dry_run:
            print(f"  Creating Gmail draft for {pseudonym} ({domain}, {len(blinded)} items)...")
            draft_id = _create_gmail_draft(email, EMAIL_SUBJECT, body, attachments)
            if draft_id:
                print(f"    Draft ID: {draft_id} — open Gmail to confirm and send")
            else:
                print(f"    Draft creation failed — logged as 'draft_failed'")
                invitation_status = "draft_failed"
        else:
            print(f"  [dry-run] Would draft to {pseudonym} <{email}> domain={domain} items={len(blinded)}")

        # Insert invitation row
        reviewer_id = _pseudonymize_email(email)
        created_at = now_utc.isoformat()
        if not dry_run:
            conn.execute(
                """INSERT OR IGNORE INTO reviewer_invitations
                   (reviewer_pseudonym, domain, batch_id, draft_id, status, sla_iso, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (pseudonym, domain, batch_id, draft_id or "", invitation_status, sla_iso, created_at),
            )
            conn.commit()
        sent_count += 1

    conn.close()
    print(f"\nPhase B complete: {sent_count} drafts created, {skipped_count} skipped.")
    if dry_run:
        print("(DRY RUN — nothing written or sent)")


def _pick_domain(expertise_tags: list[str], domain_ids: dict[str, list[int]]) -> str:
    """Return the domain most relevant to the reviewer's expertise tags."""
    _TAG_DOMAIN_MAP = {
        "chromosome": "chromosomal_evolution",
        "karyotype": "chromosomal_evolution",
        "holocentric": "chromosomal_evolution",
        "sex chromosome": "sex_chromosome_evolution",
        "sex determination": "sex_chromosome_evolution",
        "dosage compensation": "sex_chromosome_evolution",
        "genome": "comparative_genomics",
        "synteny": "comparative_genomics",
        "genomics": "comparative_genomics",
        "phylogenomics": "comparative_genomics",
        "diversification": "macroevolution",
        "speciation": "macroevolution",
        "macroevolution": "macroevolution",
        "evolution": "macroevolution",
    }
    for tag in expertise_tags:
        tag_lower = tag.lower()
        for keyword, domain in _TAG_DOMAIN_MAP.items():
            if keyword in tag_lower:
                if domain in domain_ids and domain_ids[domain]:
                    return domain
    # Fallback: first domain that has items
    for domain, ids in domain_ids.items():
        if ids:
            return domain
    return "macroevolution"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send blinded reviewer invitation drafts via Gmail."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without creating drafts or writing DB rows.",
    )
    args = parser.parse_args()
    send_invitations(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
