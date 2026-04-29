"""Provenance and output tracking — every research artifact gets a ledger row."""
import json
import sqlite3
from datetime import datetime, timezone

from agent.scheduler import DB_PATH


def record_output(
    kind: str,
    job_name: str,
    model: str,
    project_id: str | None,
    content_md: str,
    tokens_in: int,
    tokens_out: int,
    provenance: dict,
) -> int:
    """Insert a row into output_ledger and return its row id."""
    created_at = datetime.now(timezone.utc).isoformat()
    provenance_json = json.dumps(provenance)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.execute(
        """INSERT INTO output_ledger
           (created_at, kind, job_name, model, project_id,
            content_md, tokens_in, tokens_out, provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            created_at,
            kind,
            job_name,
            model,
            project_id,
            content_md,
            tokens_in,
            tokens_out,
            provenance_json,
        ),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_critic(row_id: int, score: int, notes: str, model: str) -> None:
    """Record critic-pass results on an existing ledger row."""
    critic_ran_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """UPDATE output_ledger
           SET critic_score=?, critic_notes=?, critic_model=?, critic_ran_at=?
           WHERE id=?""",
        (score, notes, model, critic_ran_at, row_id),
    )
    conn.commit()
    conn.close()


def update_user_action(row_id: int, action: str, reason: str | None = None) -> None:
    """Record Heath's adopt/reject/ignore decision on a ledger row."""
    if action not in {"adopted", "rejected", "ignored"}:
        raise ValueError(f"action must be 'adopted', 'rejected', or 'ignored'; got {action!r}")
    user_action_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """UPDATE output_ledger
           SET user_action=?, user_reason=?, user_action_at=?
           WHERE id=?""",
        (action, reason, user_action_at, row_id),
    )
    conn.commit()
    conn.close()


def query_outputs(
    kind: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
    min_score: int | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return ledger rows matching filters, newest first, with parsed provenance."""
    conditions = []
    params: list = []
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if since_iso:
        conditions.append("created_at >= ?")
        params.append(since_iso)
    if until_iso:
        conditions.append("created_at <= ?")
        params.append(until_iso)
    if min_score is not None:
        conditions.append("critic_score >= ?")
        params.append(min_score)
    if project_id:
        conditions.append("project_id = ?")
        params.append(project_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM output_ledger {where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        try:
            d["provenance"] = json.loads(d.get("provenance_json") or "{}")
        except Exception:
            d["provenance"] = {}
        results.append(d)
    return results


def get_entry(row_id: int) -> dict | None:
    """Return a single ledger row by id, or None if not found."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM output_ledger WHERE id = ?", (row_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["provenance"] = json.loads(d.get("provenance_json") or "{}")
    except Exception:
        d["provenance"] = {}
    return d
