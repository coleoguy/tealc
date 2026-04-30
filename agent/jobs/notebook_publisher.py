"""Drain the publish queue and write/overwrite static pages under public/notebook/.

State transitions handled:
  embargo → published   (when embargo_until <= now)
  redacted              (overwrite page with placeholder if public_url is set)

Schedule: IntervalTrigger(minutes=30)

Run manually:
    python -m agent.jobs.notebook_publisher
"""
import html
import json
import os
import sqlite3
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_NOTEBOOK_DIR = os.path.join(_PROJECT_ROOT, "public", "notebook")
_MANIFEST_PATH = os.path.join(_NOTEBOOK_DIR, "manifest.json")


# ---------------------------------------------------------------------------
# HTML generation helpers
# ---------------------------------------------------------------------------

def _read_template() -> str:
    tpl_path = os.path.join(_NOTEBOOK_DIR, "_template.html")
    with open(tpl_path) as f:
        return f.read()


def _estimate_cost(row: dict) -> str:
    """Very rough cost estimate from token counts (Sonnet 4 pricing)."""
    tin  = row.get("tokens_in") or 0
    tout = row.get("tokens_out") or 0
    cache_read  = row.get("cache_read_tokens") or 0
    cache_write = row.get("cache_write_tokens") or 0
    # Sonnet 4 rates: $3/$15 per 1M input/output, $0.30/$3.75 cache read/write
    cost = (
        tin  * 3.0   / 1_000_000
      + tout * 15.0  / 1_000_000
      + cache_read   * 0.30 / 1_000_000
      + cache_write  * 3.75 / 1_000_000
    )
    return f"${cost:.4f}" if cost > 0 else "n/a"


def _render_page(row: dict, template: str) -> str:
    """Render a single artifact page from a ledger row dict."""
    return template.format(
        id             = row.get("id", ""),
        kind           = html.escape(row.get("kind") or ""),
        model          = html.escape(row.get("model") or ""),
        job_name       = html.escape(row.get("job_name") or ""),
        project_id     = html.escape(row.get("project_id") or "—"),
        created_at     = html.escape(row.get("created_at") or ""),
        published_at   = html.escape(row.get("published_at") or ""),
        critic_score   = row.get("critic_score") if row.get("critic_score") is not None else "—",
        tokens_in      = row.get("tokens_in") or 0,
        tokens_out     = row.get("tokens_out") or 0,
        cost_usd       = _estimate_cost(row),
        prompt_sha     = html.escape(row.get("prompt_sha") or "—"),
        code_sha       = html.escape(row.get("code_sha") or "—"),
        data_sha       = html.escape(row.get("data_sha") or "—"),
        content_md_escaped = html.escape(row.get("content_md") or ""),
    )


_REDACTED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Redacted — Tealc Open Lab Notebook</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 600px; margin: 4rem auto;
            text-align: center; color: #64748b; }}
    h1 {{ color: #dc2626; }}
  </style>
</head>
<body>
  <h1>Redacted by author</h1>
  <p>Entry #{id} was removed from the public notebook by the author.</p>
  <p><a href="/notebook/">Return to index</a></p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _load_manifest() -> list[dict]:
    if not os.path.isfile(_MANIFEST_PATH):
        return []
    try:
        with open(_MANIFEST_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_manifest(entries: list[dict]) -> None:
    os.makedirs(_NOTEBOOK_DIR, exist_ok=True)
    with open(_MANIFEST_PATH, "w") as f:
        json.dump(entries, f, indent=2)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("notebook_publisher")
def job() -> str:
    from agent.privacy import classify_artifact  # noqa: PLC0415
    from agent.ledger import compute_provenance_hashes  # noqa: PLC0415

    os.makedirs(_NOTEBOOK_DIR, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    published_count = 0
    redacted_count = 0
    skipped_count = 0

    # ── 1. Promote embargo → published ──────────────────────────────────────
    embargo_rows = conn.execute(
        "SELECT * FROM output_ledger "
        "WHERE publish_state='embargo' AND embargo_until <= ?",
        (now_iso,),
    ).fetchall()

    template = _read_template()
    manifest = _load_manifest()
    manifest_ids = {e["id"] for e in manifest}

    for r in embargo_rows:
        row = dict(r)
        lid = row["id"]

        # Defense-in-depth: re-run classifier
        clf = classify_artifact(
            kind=row.get("kind", "unknown"),
            content_md=row.get("content_md", ""),
            project_id=row.get("project_id"),
            decided_by="heath",          # original request was from heath
        )
        if not clf["ok"]:
            # Bump back to private, record why
            conn.execute(
                "UPDATE output_ledger SET publish_state='private' WHERE id=?", (lid,)
            )
            conn.execute(
                "INSERT INTO publish_decisions (ledger_id, decision, reason, decided_by, decided_at) "
                "VALUES (?, 'redact', ?, 'classifier', ?)",
                (lid, "; ".join(clf["blockers"]), now_iso),
            )
            conn.commit()
            skipped_count += 1
            continue

        # Ensure hashes are computed
        try:
            compute_provenance_hashes(lid)
            # Re-fetch to get hashes
            row = dict(conn.execute(
                "SELECT * FROM output_ledger WHERE id=?", (lid,)
            ).fetchone())
        except Exception:
            pass

        # Write static page
        page_html = _render_page(row, template)
        page_path = os.path.join(_NOTEBOOK_DIR, f"{lid}.html")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(page_html)

        public_url = f"/notebook/{lid}.html"
        conn.execute(
            "UPDATE output_ledger SET publish_state='published', published_at=?, public_url=? WHERE id=?",
            (now_iso, public_url, lid),
        )
        conn.commit()

        if lid not in manifest_ids:
            manifest.append({
                "id": lid,
                "kind": row.get("kind"),
                "created_at": row.get("created_at"),
                "published_at": now_iso,
                "critic_score": row.get("critic_score"),
                "public_url": public_url,
            })
            manifest_ids.add(lid)

        published_count += 1

    # ── 2. Overwrite redacted pages ──────────────────────────────────────────
    redacted_rows = conn.execute(
        "SELECT id FROM output_ledger "
        "WHERE publish_state='redacted' AND public_url IS NOT NULL"
    ).fetchall()

    for r in redacted_rows:
        lid = r["id"]
        page_path = os.path.join(_NOTEBOOK_DIR, f"{lid}.html")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(_REDACTED_HTML.format(id=lid))
        # Remove from manifest
        manifest = [e for e in manifest if e["id"] != lid]
        redacted_count += 1

    conn.close()
    _save_manifest(manifest)

    summary = (
        f"notebook_publisher: +{published_count} published, "
        f"{redacted_count} redacted, {skipped_count} classifier-blocked"
    )
    return summary


if __name__ == "__main__":
    print(job())
