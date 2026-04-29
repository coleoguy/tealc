"""
ingest_reviews.py — Phase C of the Live Reviewer Circle pipeline.

Polls Gmail for messages with label 'reviewer-circle-replies/'.
Parses review_template.md-format replies (5 dimension scores + qualitative notes + flags).
Inserts rows into reviewer_scores.
Pseudonymizes reviewer_id from email.
Computes per-dimension Spearman correlation with bootstrap CI (n=200 resamples)
between Opus critic_score and human score. Inserts into reviewer_correlations.

Usage
-----
python -m evaluations.ingest_reviews [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)

from agent.scheduler import DB_PATH  # noqa: E402

GMAIL_LABEL = "reviewer-circle-replies/"

# Score dimensions that appear in review_template.md
DIMENSIONS = ["rigor", "novelty", "grounding", "clarity", "feasibility"]

# ---------------------------------------------------------------------------
# DB schema helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reviewer_scores (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            blinded_id          TEXT NOT NULL,
            reviewer_id         TEXT NOT NULL,
            domain              TEXT NOT NULL,
            rigor               INTEGER,
            novelty             INTEGER,
            grounding           INTEGER,
            clarity             INTEGER,
            feasibility         INTEGER,
            qualitative_notes   TEXT,
            flags               TEXT,
            submitted_iso       TEXT NOT NULL,
            gmail_message_id    TEXT,
            UNIQUE(blinded_id, reviewer_id)
        );

        CREATE TABLE IF NOT EXISTS reviewer_correlations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at     TEXT NOT NULL,
            domain          TEXT NOT NULL,
            dimension       TEXT NOT NULL,
            n_pairs         INTEGER NOT NULL,
            spearman_r      REAL,
            bootstrap_ci_lo REAL,
            bootstrap_ci_hi REAL,
            n_bootstrap     INTEGER NOT NULL DEFAULT 200
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------

_SCORE_RE = re.compile(
    r"^\s*[-*]?\s*(?P<dim>rigor|novelty|grounding|clarity|feasibility)"
    r"\s*[:\-–]\s*(?P<score>[1-5])\b",
    re.IGNORECASE | re.MULTILINE,
)

_QUALITATIVE_RE = re.compile(
    r"##\s*qualitative\s+(?:notes?|comments?|review)[^\n]*\n"
    r"(?P<notes>.*?)(?=\n##|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_FLAGS_RE = re.compile(
    r"##\s*flags?[^\n]*\n"
    r"(?P<flags>.*?)(?=\n##|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_BLINDED_ID_RE = re.compile(
    r"(?:blinded[_\s]id|item[_\s]id)[:\s]+([0-9a-f-]{32,})",
    re.IGNORECASE,
)


def _pseudonymize_email(email: str) -> str:
    return "reviewer_" + hashlib.sha256(email.lower().encode()).hexdigest()[:12]


def _parse_reply(body: str) -> list[dict]:
    """
    Parse one email body that may contain one or more item reviews.
    Returns list of dicts: {blinded_id, scores, qualitative_notes, flags}.

    The template asks reviewers to copy the template block once per item, each
    block beginning with '## Item: <blinded_id>'. Falls back to treating the
    whole body as one review if no item headers are found.
    """
    # Split on '## Item:' headers
    blocks: list[tuple[Optional[str], str]] = []
    item_split = re.split(r"(?m)^##\s+Item\s*[:\-–]\s*", body)
    if len(item_split) > 1:
        # First chunk is preamble (discard), rest are item blocks
        for chunk in item_split[1:]:
            lines = chunk.strip().splitlines()
            blinded_id = lines[0].strip() if lines else None
            block_body = "\n".join(lines[1:]) if len(lines) > 1 else ""
            blocks.append((blinded_id, block_body))
    else:
        # No item headers — try to extract blinded_id from any line
        m = _BLINDED_ID_RE.search(body)
        blinded_id = m.group(1) if m else None
        blocks.append((blinded_id, body))

    results = []
    for blinded_id, block in blocks:
        scores: dict[str, Optional[int]] = {d: None for d in DIMENSIONS}
        for m in _SCORE_RE.finditer(block):
            dim = m.group("dim").lower()
            if dim in scores:
                scores[dim] = int(m.group("score"))

        notes_m = _QUALITATIVE_RE.search(block)
        qualitative_notes = notes_m.group("notes").strip() if notes_m else ""

        flags_m = _FLAGS_RE.search(block)
        raw_flags = flags_m.group("flags").strip() if flags_m else ""
        flags = [
            line.lstrip("-* ").strip()
            for line in raw_flags.splitlines()
            if line.strip().lstrip("-* ")
        ]

        results.append({
            "blinded_id": blinded_id,
            "scores": scores,
            "qualitative_notes": qualitative_notes,
            "flags": flags,
        })
    return results


# ---------------------------------------------------------------------------
# Gmail polling
# ---------------------------------------------------------------------------

def _get_gmail_service():
    try:
        import importlib
        tools_mod = importlib.import_module("agent.tools")
        svc_fn = getattr(tools_mod, "_get_google_service", None)
        if svc_fn is None:
            return None, "agent.tools._get_google_service not found"
        service, err = svc_fn("gmail", "v1")
        return service, err
    except Exception as e:
        return None, str(e)


def _get_label_id(service, label_name: str) -> Optional[str]:
    try:
        result = service.users().labels().list(userId="me").execute()
        for label in result.get("labels", []):
            if label["name"].lower() == label_name.lower():
                return label["id"]
    except Exception:
        pass
    return None


def _poll_gmail_replies() -> list[dict]:
    """
    Return list of {message_id, from_email, body} for unread messages
    in the reviewer-circle-replies/ label.
    """
    service, err = _get_gmail_service()
    if err or service is None:
        print(f"  [warn] Gmail unavailable: {err}")
        return []

    label_id = _get_label_id(service, GMAIL_LABEL)
    if not label_id:
        print(f"  [warn] Gmail label '{GMAIL_LABEL}' not found — create it first.")
        return []

    try:
        result = service.users().messages().list(
            userId="me",
            labelIds=[label_id],
            q="is:unread",
            maxResults=50,
        ).execute()
        messages = result.get("messages", [])
    except Exception as e:
        print(f"  [error] Gmail list failed: {e}")
        return []

    replies = []
    for m in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=m["id"], format="full"
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            from_header = headers.get("From", "")
            # Extract email from "Name <email>" format
            email_m = re.search(r"<([^>]+)>", from_header)
            from_email = email_m.group(1) if email_m else from_header.strip()

            # Get plain-text body
            body = _extract_body(msg)
            replies.append({
                "message_id": m["id"],
                "from_email": from_email,
                "body": body,
            })
        except Exception as e:
            print(f"  [warn] Could not fetch message {m['id']}: {e}")

    return replies


def _extract_body(msg: dict) -> str:
    """Recursively extract plain-text body from a Gmail message."""
    payload = msg.get("payload", {})
    return _extract_part(payload)


def _extract_part(part: dict) -> str:
    mime = part.get("mimeType", "")
    if mime == "text/plain":
        data = part.get("body", {}).get("data", "")
        if data:
            import base64
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if "parts" in part:
        for subpart in part["parts"]:
            text = _extract_part(subpart)
            if text:
                return text
    return ""


def _lookup_domain_for_blinded_id(conn: sqlite3.Connection, blinded_id: str) -> str:
    """Try to look up the domain from reviewer_invitations batches."""
    # Batch JSONL files contain domain info — scan batches dir
    batches_dir = os.path.join(_ROOT, "data", "reviewer_circle", "batches")
    if os.path.exists(batches_dir):
        for fname in os.listdir(batches_dir):
            if fname.endswith(".jsonl") and "_key" not in fname:
                fpath = os.path.join(batches_dir, fname)
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        for line in fh:
                            obj = json.loads(line)
                            if obj.get("blinded_id") == blinded_id:
                                return obj.get("domain", "unknown")
                except Exception:
                    pass
    return "unknown"


# ---------------------------------------------------------------------------
# Spearman + bootstrap CI
# ---------------------------------------------------------------------------

def _spearman_r(x: list[float], y: list[float]) -> Optional[float]:
    """Pure-Python Spearman rank correlation."""
    n = len(x)
    if n < 2:
        return None

    def _ranks(vals: list[float]) -> list[float]:
        sorted_vals = sorted(enumerate(vals), key=lambda t: t[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and sorted_vals[j + 1][1] == sorted_vals[j][1]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[sorted_vals[k][0]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = _ranks(x), _ranks(y)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    denom_x = sum((rx[i] - mean_rx) ** 2 for i in range(n))
    denom_y = sum((ry[i] - mean_ry) ** 2 for i in range(n))
    denom = (denom_x * denom_y) ** 0.5
    if denom == 0:
        return None
    return num / denom


def _bootstrap_ci(
    x: list[float], y: list[float], n_boot: int = 200, alpha: float = 0.05
) -> tuple[Optional[float], Optional[float]]:
    """Bootstrap confidence interval for Spearman r."""
    n = len(x)
    if n < 3:
        return None, None
    rng = random.Random(42)
    boot_stats = []
    for _ in range(n_boot):
        idx = [rng.randint(0, n - 1) for _ in range(n)]
        bx = [x[i] for i in idx]
        by = [y[i] for i in idx]
        r = _spearman_r(bx, by)
        if r is not None:
            boot_stats.append(r)
    if not boot_stats:
        return None, None
    boot_stats.sort()
    lo = boot_stats[int(alpha / 2 * len(boot_stats))]
    hi = boot_stats[int((1 - alpha / 2) * len(boot_stats))]
    return lo, hi


# ---------------------------------------------------------------------------
# Correlation computation
# ---------------------------------------------------------------------------

def _compute_and_store_correlations(conn: sqlite3.Connection, dry_run: bool) -> None:
    """
    For each (domain, dimension) pair with >= 2 rows, compute Spearman r
    between the Opus critic_score on that blinded item and the human score.
    """
    # Join reviewer_scores with output_ledger via batch key files
    # We need (human_score, opus_score) pairs per dimension per domain
    rows = conn.execute(
        "SELECT blinded_id, reviewer_id, domain, rigor, novelty, grounding, "
        "       clarity, feasibility "
        "FROM reviewer_scores"
    ).fetchall()

    if not rows:
        print("  No reviewer scores found yet.")
        return

    # Build lookup: blinded_id -> ledger_id from key files
    blinded_to_ledger: dict[str, int] = {}
    batches_dir = os.path.join(_ROOT, "data", "reviewer_circle", "batches")
    if os.path.exists(batches_dir):
        for fname in os.listdir(batches_dir):
            if fname.endswith("_key.jsonl"):
                fpath = os.path.join(batches_dir, fname)
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        for line in fh:
                            obj = json.loads(line)
                            for bid, lid in obj.get("mapping", {}).items():
                                blinded_to_ledger[bid] = int(lid)
                except Exception:
                    pass

    # Collect (domain, dim, human_score, opus_score) tuples
    from collections import defaultdict  # noqa: PLC0415
    pairs: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)

    for r in rows:
        blinded_id = r["blinded_id"]
        domain = r["domain"] or "unknown"
        ledger_id = blinded_to_ledger.get(blinded_id)
        if ledger_id is None:
            continue
        # Get opus critic_score for this ledger row
        ledger_row = conn.execute(
            "SELECT critic_score FROM output_ledger WHERE id=?", (ledger_id,)
        ).fetchone()
        if ledger_row is None or ledger_row["critic_score"] is None:
            continue
        opus_score = float(ledger_row["critic_score"])

        for dim in DIMENSIONS:
            human_score = r[dim]
            if human_score is not None:
                pairs[(domain, dim)].append((float(human_score), opus_score))

    computed_at = datetime.now(timezone.utc).isoformat()
    for (domain, dim), pair_list in pairs.items():
        if len(pair_list) < 2:
            continue
        human_scores = [p[0] for p in pair_list]
        opus_scores = [p[1] for p in pair_list]
        r = _spearman_r(human_scores, opus_scores)
        ci_lo, ci_hi = _bootstrap_ci(human_scores, opus_scores, n_boot=200)
        n = len(pair_list)
        print(
            f"  Correlation [{domain}][{dim}]: "
            f"r={r:.3f} CI=[{ci_lo:.3f}, {ci_hi:.3f}] n={n}"
            if r is not None and ci_lo is not None
            else f"  Correlation [{domain}][{dim}]: r=None n={n}"
        )
        if not dry_run:
            conn.execute(
                """INSERT INTO reviewer_correlations
                   (computed_at, domain, dimension, n_pairs,
                    spearman_r, bootstrap_ci_lo, bootstrap_ci_hi, n_bootstrap)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 200)""",
                (computed_at, domain, dim, n, r, ci_lo, ci_hi),
            )
    if not dry_run:
        conn.commit()


# ---------------------------------------------------------------------------
# Main ingest loop
# ---------------------------------------------------------------------------

def ingest_reviews(dry_run: bool = False) -> None:
    conn = _conn()
    _ensure_tables(conn)

    replies = _poll_gmail_replies()
    print(f"Found {len(replies)} unread messages in label '{GMAIL_LABEL}'.")

    inserted = 0
    skipped = 0

    for reply in replies:
        from_email = reply["from_email"]
        reviewer_id = _pseudonymize_email(from_email)
        message_id = reply["message_id"]
        submitted_iso = datetime.now(timezone.utc).isoformat()

        parsed_items = _parse_reply(reply["body"])
        print(f"  Message {message_id} from {reviewer_id}: parsed {len(parsed_items)} item(s)")

        for item in parsed_items:
            blinded_id = item.get("blinded_id")
            if not blinded_id:
                print(f"    [warn] Could not extract blinded_id — skipping item")
                skipped += 1
                continue

            scores = item["scores"]
            domain = _lookup_domain_for_blinded_id(conn, blinded_id)
            flags_json = json.dumps(item.get("flags", []))

            # Check idempotency
            existing = conn.execute(
                "SELECT id FROM reviewer_scores WHERE blinded_id=? AND reviewer_id=?",
                (blinded_id, reviewer_id),
            ).fetchone()
            if existing:
                print(f"    [skip] Already ingested blinded_id={blinded_id} for {reviewer_id}")
                skipped += 1
                continue

            if not dry_run:
                conn.execute(
                    """INSERT OR IGNORE INTO reviewer_scores
                       (blinded_id, reviewer_id, domain,
                        rigor, novelty, grounding, clarity, feasibility,
                        qualitative_notes, flags, submitted_iso, gmail_message_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        blinded_id, reviewer_id, domain,
                        scores.get("rigor"), scores.get("novelty"),
                        scores.get("grounding"), scores.get("clarity"),
                        scores.get("feasibility"),
                        item.get("qualitative_notes", ""),
                        flags_json,
                        submitted_iso, message_id,
                    ),
                )
                conn.commit()
            inserted += 1

        # Mark message as read
        if not dry_run and replies:
            try:
                import importlib
                tools_mod = importlib.import_module("agent.tools")
                svc_fn = getattr(tools_mod, "_get_google_service", None)
                if svc_fn:
                    service, _ = svc_fn("gmail", "v1")
                    if service:
                        service.users().messages().modify(
                            userId="me",
                            id=message_id,
                            body={"removeLabelIds": ["UNREAD"]},
                        ).execute()
            except Exception:
                pass

    print(f"\n  Inserted: {inserted}, Skipped: {skipped}")

    # Compute correlations
    print("\nComputing Spearman correlations...")
    _compute_and_store_correlations(conn, dry_run=dry_run)

    conn.close()
    print("\nPhase C complete.")
    if dry_run:
        print("(DRY RUN — nothing written)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest reviewer replies and compute Opus-vs-human correlations."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without writing to DB.",
    )
    args = parser.parse_args()
    ingest_reviews(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
