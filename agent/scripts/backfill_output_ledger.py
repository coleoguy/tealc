"""
backfill_output_ledger.py — Phase A of the Live Reviewer Circle pipeline.

Walk analysis_runs, hypothesis_proposals, overnight_drafts, paper_findings,
and the NAS case packet rows; for each one that lacks a matching output_ledger
entry, insert one via record_output. Then pick the top-scoring rows (cap 3 per
domain) and write data/reviewer_circle/manifest.json.

Usage
-----
python -m agent.scripts.backfill_output_ledger [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _ROOT)

from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output, query_outputs, update_critic  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANIFEST_PATH = os.path.join(_ROOT, "data", "reviewer_circle", "manifest.json")

# Map DB table / kind strings to output_ledger kind + domain.
# domain must match one of the 4 rubric file stems.
_KIND_TO_DOMAIN = {
    "hypothesis":             "chromosomal_evolution",   # default; overridden by keywords
    "analysis":               "macroevolution",
    "literature_synthesis":   "comparative_genomics",
    "grant_draft":            "sex_chromosome_evolution",
}

# Keywords for domain inference (checked against project_id + content, case-insensitive)
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "chromosomal_evolution": [
        "chromosome", "karyotype", "fission", "fusion", "holocentric",
        "polyploid", "b chromosome", "chromosome number",
    ],
    "sex_chromosome_evolution": [
        "sex chromosome", "xy", "zw", "dosage compensation", "y degeneration",
        "sex determination", "heterogamety", "pseudoautosomal",
    ],
    "comparative_genomics": [
        "genome", "synteny", "dnds", "dn/ds", "gene family", "repeat",
        "genome size", "ortholog", "busco", "assembly",
    ],
    "macroevolution": [
        "diversification", "speciation", "extinction", "bamm", "bisse",
        "biogeography", "macroevolution", "adaptive radiation", "pgls",
    ],
}

DOMAIN_CAP = 3  # max items per domain sent to reviewers
CRITIC_CALL_CAP = 12  # max Anthropic calls to fill missing scores
MIN_CRITIC_SCORE = 3


# ---------------------------------------------------------------------------
# Domain inference
# ---------------------------------------------------------------------------

def infer_domain(project_id: Optional[str], content: str) -> str:
    """Return the best-matching domain for a text blob, or 'macroevolution' as fallback."""
    haystack = ((project_id or "") + " " + (content or "")).lower()
    # Score each domain by keyword hits
    scores: dict[str, int] = {d: 0 for d in _DOMAIN_KEYWORDS}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw in haystack:
                scores[domain] += 1
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else "macroevolution"


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def _existing_ledger_provenance_ids(conn: sqlite3.Connection) -> set[str]:
    """Return set of 'source_id' values already recorded in provenance_json."""
    rows = conn.execute(
        "SELECT provenance_json FROM output_ledger WHERE provenance_json IS NOT NULL"
    ).fetchall()
    ids: set[str] = set()
    for r in rows:
        try:
            prov = json.loads(r["provenance_json"] or "{}")
            sid = prov.get("source_id")
            if sid:
                ids.add(str(sid))
        except Exception:
            pass
    return ids


# ---------------------------------------------------------------------------
# Source walkers
# ---------------------------------------------------------------------------

def _walk_analysis_runs(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, project_id, run_iso, interpretation_md, outcome "
            "FROM analysis_runs "
            "WHERE interpretation_md IS NOT NULL AND trim(interpretation_md) != '' "
            "ORDER BY run_iso DESC LIMIT 50"
        ).fetchall()
    except Exception:
        return []
    results = []
    for r in rows:
        results.append({
            "source_table": "analysis_runs",
            "source_id": str(r["id"]),
            "kind": "analysis",
            "project_id": r["project_id"] or "",
            "content_md": r["interpretation_md"] or "",
            "created_at": r["run_iso"] or "",
        })
    return results


def _walk_hypothesis_proposals(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, project_id, proposed_iso, hypothesis_md, rationale_md, "
            "       novelty_score, feasibility_score "
            "FROM hypothesis_proposals "
            "WHERE hypothesis_md IS NOT NULL AND trim(hypothesis_md) != '' "
            "ORDER BY proposed_iso DESC LIMIT 50"
        ).fetchall()
    except Exception:
        return []
    results = []
    for r in rows:
        content = (r["hypothesis_md"] or "")
        if r["rationale_md"]:
            content += "\n\n## Rationale\n" + r["rationale_md"]
        results.append({
            "source_table": "hypothesis_proposals",
            "source_id": str(r["id"]),
            "kind": "hypothesis",
            "project_id": r["project_id"] or "",
            "content_md": content,
            "created_at": r["proposed_iso"] or "",
        })
    return results


def _walk_overnight_drafts(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, project_id, created_at, source_artifact_title, "
            "       drafted_section, reasoning "
            "FROM overnight_drafts "
            "WHERE drafted_section IS NOT NULL AND trim(drafted_section) != '' "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    except Exception:
        return []
    results = []
    for r in rows:
        content = f"# Draft: {r['source_artifact_title'] or 'Untitled'}\n\n"
        content += r["drafted_section"] or ""
        if r["reasoning"]:
            content += "\n\n## Reasoning\n" + r["reasoning"]
        results.append({
            "source_table": "overnight_drafts",
            "source_id": str(r["id"]),
            "kind": "grant_draft",
            "project_id": r["project_id"] or "",
            "content_md": content,
            "created_at": r["created_at"] or "",
        })
    return results


def _walk_paper_findings(conn: sqlite3.Connection) -> list[dict]:
    """Aggregate findings per DOI into a literature_synthesis entry."""
    try:
        rows = conn.execute(
            "SELECT doi, GROUP_CONCAT(finding_text, '\n\n') AS findings, "
            "       MIN(created_at) AS created_at "
            "FROM paper_findings "
            "WHERE finding_text IS NOT NULL "
            "GROUP BY doi "
            "ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    except Exception:
        return []
    results = []
    for r in rows:
        content = f"# Literature synthesis: {r['doi']}\n\n{r['findings'] or ''}"
        results.append({
            "source_table": "paper_findings",
            "source_id": f"doi:{r['doi']}",
            "kind": "literature_synthesis",
            "project_id": "",
            "content_md": content,
            "created_at": r["created_at"] or "",
        })
    return results


def _walk_literature_notes(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, project_id, doi, title, extracted_findings_md, created_at "
            "FROM literature_notes "
            "WHERE extracted_findings_md IS NOT NULL "
            "  AND trim(extracted_findings_md) != '' "
            "ORDER BY created_at DESC LIMIT 40"
        ).fetchall()
    except Exception:
        return []
    results = []
    for r in rows:
        content = f"# Literature note: {r['title'] or r['doi'] or 'unknown'}\n\n"
        content += r["extracted_findings_md"] or ""
        results.append({
            "source_table": "literature_notes",
            "source_id": str(r["id"]),
            "kind": "literature_synthesis",
            "project_id": r["project_id"] or "",
            "content_md": content,
            "created_at": r["created_at"] or "",
        })
    return results


# ---------------------------------------------------------------------------
# Critic pass (capped)
# ---------------------------------------------------------------------------

def _run_critic_pass(ledger_id: int, content_md: str, domain: str,
                     critic_calls_used: int) -> Optional[int]:
    """
    Call Opus to score content. Returns critic_score (1-5) or None on failure.
    Side-effect: writes to output_ledger via update_critic.
    Caller must enforce CRITIC_CALL_CAP.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()
        prompt = (
            f"You are a rigorous scientific reviewer. "
            f"Score the following research output (domain: {domain.replace('_',' ')}) "
            f"on a 1-5 scale where 1=poor and 5=excellent, considering rigor, novelty, "
            f"and grounding in literature. Reply ONLY with a JSON object: "
            f'{{\"score\": <int 1-5>, \"notes\": \"<one sentence>\"}}.\n\n'
            f"OUTPUT:\n{content_md[:3000]}"
        )
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        data = json.loads(raw)
        score = int(data["score"])
        notes = str(data.get("notes", ""))
        update_critic(ledger_id, score, notes, "claude-opus-4-5")
        return score
    except Exception as e:
        print(f"  [critic] ledger_id={ledger_id} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------

def backfill(dry_run: bool = False) -> dict:
    conn = _conn()
    existing_source_ids = _existing_ledger_provenance_ids(conn)

    # Gather candidates from all source tables
    candidates: list[dict] = []
    candidates.extend(_walk_analysis_runs(conn))
    candidates.extend(_walk_hypothesis_proposals(conn))
    candidates.extend(_walk_overnight_drafts(conn))
    candidates.extend(_walk_paper_findings(conn))
    candidates.extend(_walk_literature_notes(conn))
    conn.close()

    print(f"\nFound {len(candidates)} candidate artifacts across all source tables.")

    kind_counts: dict[str, int] = {}
    inserted = 0
    skipped_existing = 0

    for c in candidates:
        kind_counts[c["kind"]] = kind_counts.get(c["kind"], 0) + 1
        if c["source_id"] in existing_source_ids:
            skipped_existing += 1
            continue
        domain = infer_domain(c["project_id"], c["content_md"])
        provenance = {
            "source_table": c["source_table"],
            "source_id": c["source_id"],
        }
        if not dry_run:
            record_output(
                kind=c["kind"],
                job_name="backfill_output_ledger",
                model="(backfilled)",
                project_id=c["project_id"] or None,
                content_md=c["content_md"],
                tokens_in=0,
                tokens_out=0,
                provenance=provenance,
            )
            # Also add domain — requires a direct SQL update since record_output
            # doesn't expose domain. Use a separate UPDATE.
            _conn2 = _conn()
            _conn2.execute(
                "UPDATE output_ledger SET domain=? "
                "WHERE provenance_json LIKE ? ORDER BY id DESC LIMIT 1",
                (domain, f'%{c["source_id"]}%'),
            )
            _conn2.commit()
            _conn2.close()
        inserted += 1

    print(f"  Artifacts by kind: {kind_counts}")
    print(f"  Already in ledger (skipped): {skipped_existing}")
    print(f"  Inserted: {inserted} {'(DRY RUN — nothing written)' if dry_run else ''}")

    # ------------------------------------------------------------------
    # Now query ledger for rows with domain set and critic_score >= MIN
    # Fill missing critic scores first (capped)
    # ------------------------------------------------------------------
    conn = _conn()
    # Ensure domain column exists (added by this script's companion migration)
    try:
        conn.execute("ALTER TABLE output_ledger ADD COLUMN domain TEXT")
        conn.commit()
    except Exception:
        pass  # already exists

    # Rows missing domain: infer from content + project_id
    undomain_rows = conn.execute(
        "SELECT id, kind, project_id, content_md FROM output_ledger "
        "WHERE domain IS NULL OR domain = ''"
    ).fetchall()
    for r in undomain_rows:
        d = infer_domain(r["project_id"] or "", r["content_md"] or "")
        conn.execute("UPDATE output_ledger SET domain=? WHERE id=?", (d, r["id"]))
    conn.commit()

    # Rows missing critic_score — fill up to cap
    unscored = conn.execute(
        "SELECT id, content_md, domain FROM output_ledger "
        "WHERE critic_score IS NULL ORDER BY id DESC"
    ).fetchall()
    critic_calls_used = 0
    for r in unscored:
        if critic_calls_used >= CRITIC_CALL_CAP:
            print(f"  Critic cap ({CRITIC_CALL_CAP}) reached; skipping remaining unscored rows.")
            break
        if not dry_run:
            score = _run_critic_pass(r["id"], r["content_md"] or "", r["domain"] or "", critic_calls_used)
            if score is not None:
                critic_calls_used += 1
                print(f"  Scored ledger_id={r['id']}: {score}/5")
        else:
            critic_calls_used += 1

    conn.close()

    # ------------------------------------------------------------------
    # Select manifest: top critic_score rows, cap 3 per domain
    # ------------------------------------------------------------------
    all_rows = query_outputs(min_score=MIN_CRITIC_SCORE, limit=200)

    manifest_by_domain: dict[str, list[int]] = {
        "chromosomal_evolution": [],
        "sex_chromosome_evolution": [],
        "comparative_genomics": [],
        "macroevolution": [],
    }

    for row in sorted(all_rows, key=lambda r: (r.get("critic_score") or 0), reverse=True):
        domain = row.get("domain") or infer_domain(row.get("project_id"), row.get("content_md", ""))
        if domain not in manifest_by_domain:
            domain = "macroevolution"
        if len(manifest_by_domain[domain]) < DOMAIN_CAP:
            manifest_by_domain[domain].append(row["id"])

    total_selected = sum(len(v) for v in manifest_by_domain.values())
    print(f"\nManifest composition (critic_score >= {MIN_CRITIC_SCORE}, cap {DOMAIN_CAP}/domain):")
    for d, ids in manifest_by_domain.items():
        print(f"  {d}: {ids}")
    print(f"  Total selected: {total_selected}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_critic_score": MIN_CRITIC_SCORE,
        "domain_cap": DOMAIN_CAP,
        "domains": manifest_by_domain,
        "total_items": total_selected,
    }

    if not dry_run:
        os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
        with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        print(f"\nManifest written to {MANIFEST_PATH}")

    return {
        "candidates_found": len(candidates),
        "kind_counts": kind_counts,
        "inserted": inserted,
        "critic_calls": critic_calls_used,
        "manifest": manifest_by_domain,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill output_ledger from analysis_runs, hypothesis_proposals, etc."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing to DB or manifest.",
    )
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
