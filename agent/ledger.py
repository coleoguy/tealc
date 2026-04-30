"""Provenance and output tracking — every research artifact gets a ledger row."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

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


# ---------------------------------------------------------------------------
# Open Lab Notebook — publish state machine
# ---------------------------------------------------------------------------

_EMBARGO_HOURS = 24


def _sha256(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_provenance_hashes(ledger_id: int) -> dict:
    """Compute and persist code_sha, data_sha, prompt_sha from provenance_json.

    Reads the provenance_json blob and derives deterministic SHA-256 hashes
    for any keys named 'code', 'data_path'/'data', and 'prompt'/'system_prompt'.
    Writes the computed columns back to the row and returns the hash dict.
    """
    entry = get_entry(ledger_id)
    if entry is None:
        raise ValueError(f"ledger row {ledger_id} not found")

    prov = entry.get("provenance") or {}
    code_text  = prov.get("code") or prov.get("r_code") or prov.get("python_code")
    data_text  = prov.get("data_path") or prov.get("data") or prov.get("db_name")
    prompt_text = prov.get("prompt") or prov.get("system_prompt") or entry.get("content_md")

    hashes = {
        "code_sha":   _sha256(code_text),
        "data_sha":   _sha256(str(data_text) if data_text else None),
        "prompt_sha": _sha256(prompt_text),
    }

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "UPDATE output_ledger SET code_sha=?, data_sha=?, prompt_sha=? WHERE id=?",
        (hashes["code_sha"], hashes["data_sha"], hashes["prompt_sha"], ledger_id),
    )
    conn.commit()
    conn.close()
    return hashes


def auto_classify_for_publish(ledger_id: int) -> dict:
    """Run the privacy classifier against a ledger row without changing state.

    Returns::
        {"ok": bool, "blockers": list[str], "kind": str}
    """
    from agent.privacy import classify_artifact  # noqa: PLC0415 (avoid circular import at module level)

    entry = get_entry(ledger_id)
    if entry is None:
        return {"ok": False, "kind": "unknown", "blockers": [f"ledger row {ledger_id} not found"]}

    return classify_artifact(
        kind=entry.get("kind", "unknown"),
        content_md=entry.get("content_md", ""),
        project_id=entry.get("project_id"),
        decided_by="auto",
    )


def _record_decision(
    conn: sqlite3.Connection,
    ledger_id: int,
    decision: str,
    reason: str,
    decided_by: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO publish_decisions (ledger_id, decision, reason, decided_by, decided_at)
           VALUES (?, ?, ?, ?, ?)""",
        (ledger_id, decision, reason, decided_by, now),
    )


def request_publish(ledger_id: int, reason: str = "", decided_by: str = "heath") -> dict:
    """Explicit Heath approval: moves a private artifact to 'queued' → 'embargo'.

    Returns a dict with 'ok', 'state', and 'embargo_until' (or 'blockers').
    Runs the classifier first; if unsafe, returns ok=False without state change.
    """
    from agent.privacy import classify_artifact  # noqa: PLC0415

    entry = get_entry(ledger_id)
    if entry is None:
        return {"ok": False, "blockers": [f"ledger row {ledger_id} not found"]}

    clf = classify_artifact(
        kind=entry.get("kind", "unknown"),
        content_md=entry.get("content_md", ""),
        project_id=entry.get("project_id"),
        decided_by=decided_by,
    )
    if not clf["ok"]:
        return {"ok": False, "blockers": clf["blockers"]}

    now = datetime.now(timezone.utc)
    embargo_until = (now + timedelta(hours=_EMBARGO_HOURS)).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "UPDATE output_ledger SET publish_state='embargo', embargo_until=? WHERE id=?",
        (embargo_until, ledger_id),
    )
    _record_decision(conn, ledger_id, "publish", reason, decided_by)
    conn.commit()
    conn.close()

    # Compute hashes now so they're ready when the publisher runs.
    try:
        compute_provenance_hashes(ledger_id)
    except Exception:
        pass

    return {"ok": True, "state": "embargo", "embargo_until": embargo_until}


def publish_artifact(ledger_id: int, reason: str = "", decided_by: str = "heath") -> dict:
    """Idempotent: classify, embargo, then immediately mark as published.

    Used by notebook_publisher after embargo passes, or by Heath to force-publish.
    Returns {"ok": bool, "state": str, "public_url": str | None}.
    """
    from agent.privacy import classify_artifact  # noqa: PLC0415

    entry = get_entry(ledger_id)
    if entry is None:
        return {"ok": False, "blockers": [f"ledger row {ledger_id} not found"]}

    # Idempotent: already published is a no-op
    if entry.get("publish_state") == "published":
        return {"ok": True, "state": "published", "public_url": entry.get("public_url")}

    clf = classify_artifact(
        kind=entry.get("kind", "unknown"),
        content_md=entry.get("content_md", ""),
        project_id=entry.get("project_id"),
        decided_by=decided_by,
    )
    if not clf["ok"]:
        return {"ok": False, "blockers": clf["blockers"]}

    now = datetime.now(timezone.utc).isoformat()
    public_url = f"/notebook/{ledger_id}.html"

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """UPDATE output_ledger
           SET publish_state='published', published_at=?, public_url=?
           WHERE id=?""",
        (now, public_url, ledger_id),
    )
    _record_decision(conn, ledger_id, "publish", reason, decided_by)
    conn.commit()
    conn.close()
    return {"ok": True, "state": "published", "public_url": public_url}


def unpublish_artifact(ledger_id: int, reason: str = "") -> dict:
    """Move an artifact to 'redacted'; the static page is overwritten with a placeholder.

    Returns {"ok": bool, "state": str}.
    """
    entry = get_entry(ledger_id)
    if entry is None:
        return {"ok": False, "error": f"ledger row {ledger_id} not found"}

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "UPDATE output_ledger SET publish_state='redacted' WHERE id=?",
        (ledger_id,),
    )
    _record_decision(conn, ledger_id, "redact", reason, "heath")
    conn.commit()
    conn.close()
    return {"ok": True, "state": "redacted"}
