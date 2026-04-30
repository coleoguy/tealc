"""extract_heath_claims.py — Extract claim triples from Heath's papers via Sonnet.

Reads Discussion + Conclusion sentences from heath_sentences, calls Sonnet once
per paper, inserts results into heath_claims + heath_claims_fts.

Cost cap: bails if cumulative spend exceeds $5.00.

Manual run:
    python -m agent.jobs.extract_heath_claims
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

import anthropic

from agent.scheduler import DB_PATH
from agent.cost_tracking import record_call, summarize_costs
from agent.jobs import tracked

_MODEL = "claude-sonnet-4-6"
_COST_CAP_USD = 5.00
_JOB_NAME = "extract_heath_claims"

_SYSTEM_PROMPT = """\
You are a scientific claim extractor. Given text from the Discussion or Conclusion section of a biology paper, extract the key scientific claims as triples.

Output ONLY a JSON array (no markdown, no extra text) of objects with these keys:
  subject     - the biological entity or concept being described
  predicate   - a verb phrase ("increases", "is associated with", "causes", "predicts", "reduces", "evolves faster in", etc.)
  object      - what is being said about the subject
  evidence_quote - a short verbatim quote (≤120 chars) from the text supporting the claim
  sentence_index - integer index of the sentence in the provided list (0-based)
  confidence  - float in [0, 1] reflecting certainty expressed in the text

Focus on empirical claims. Skip methodological statements and generic background claims. Return [] if there are no suitable claims."""


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _ensure_tables() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS heath_claims (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id       TEXT NOT NULL,
            year           INTEGER,
            subject        TEXT NOT NULL,
            predicate      TEXT NOT NULL,
            object         TEXT NOT NULL,
            evidence_quote TEXT,
            sentence_id    TEXT,
            confidence     REAL,
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hc_paper_id ON heath_claims(paper_id);
        CREATE INDEX IF NOT EXISTS idx_hc_subject  ON heath_claims(subject);

        CREATE VIRTUAL TABLE IF NOT EXISTS heath_claims_fts USING fts5(
            paper_id UNINDEXED,
            subject,
            predicate,
            object,
            evidence_quote,
            content='heath_claims',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS heath_claims_ai AFTER INSERT ON heath_claims
        BEGIN
            INSERT INTO heath_claims_fts(rowid, paper_id, subject, predicate, object, evidence_quote)
            VALUES (new.id, new.paper_id, new.subject, new.predicate, new.object, new.evidence_quote);
        END;
    """)
    conn.commit()
    conn.close()


def _papers_already_extracted() -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute("SELECT DISTINCT paper_id FROM heath_claims").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _get_discussion_sentences(paper_id: str) -> list[dict]:
    """Return discussion/conclusion sentences for a paper from heath_sentences."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT sentence_id, sentence, section, year
           FROM heath_sentences
           WHERE paper_id = ?
             AND section IN ('discussion', 'body')
             AND sentence != '__no_text__'
           ORDER BY id""",
        (paper_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_all_paper_ids_with_sentences() -> list[tuple[str, int]]:
    """Return (paper_id, year) pairs that have sentences but no sentinel."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute(
        """SELECT DISTINCT paper_id, year
           FROM heath_sentences
           WHERE sentence != '__no_text__'
           ORDER BY paper_id""",
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def _insert_claims(claims: list[dict], paper_id: str, year: int,
                   sentences: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    for c in claims:
        # Map sentence_index to sentence_id
        sidx = c.get("sentence_index")
        sid = sentences[sidx]["sentence_id"] if (
            isinstance(sidx, int) and 0 <= sidx < len(sentences)
        ) else None
        conn.execute(
            """INSERT INTO heath_claims
               (paper_id, year, subject, predicate, object,
                evidence_quote, sentence_id, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper_id,
                year,
                str(c.get("subject", ""))[:200],
                str(c.get("predicate", ""))[:200],
                str(c.get("object", ""))[:400],
                str(c.get("evidence_quote", ""))[:300],
                sid,
                float(c.get("confidence", 0.5)),
                now,
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Sonnet extraction
# ---------------------------------------------------------------------------

def _call_sonnet(sentences: list[dict]) -> tuple[list[dict], dict]:
    """Call Sonnet to extract claims from the given sentences.

    Returns (claims_list, usage_dict).
    """
    client = anthropic.Anthropic()
    numbered = "\n".join(
        f"[{i}] {s['sentence']}" for i, s in enumerate(sentences)
    )
    user_msg = f"Extract claims from these sentences:\n\n{numbered}"

    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        temperature=0,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip() if resp.content else "[]"
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }

    # Parse JSON — strip any accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        claims = json.loads(raw)
        if not isinstance(claims, list):
            claims = []
    except Exception:
        claims = []

    return claims, usage


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("extract_heath_claims")
def job(limit: int | None = None) -> str:
    _ensure_tables()

    already_done = _papers_already_extracted()
    all_papers = _get_all_paper_ids_with_sentences()

    papers_processed = 0
    total_claims = 0
    cumulative_cost = 0.0

    for paper_id, year in all_papers:
        if limit is not None and papers_processed >= limit:
            break

        if paper_id in already_done:
            continue

        # Cost cap check
        if cumulative_cost >= _COST_CAP_USD:
            break

        sentences = _get_discussion_sentences(paper_id)
        if not sentences:
            # Insert a sentinel claim so we don't retry
            _insert_claims(
                [{"subject": "__skip__", "predicate": "no_discussion",
                  "object": "__skip__", "evidence_quote": "",
                  "sentence_index": 0, "confidence": 0.0}],
                paper_id, year, [{"sentence_id": None}]
            )
            already_done.add(paper_id)
            continue

        # Cap sentences sent to Sonnet to avoid huge prompts
        sentences = sentences[:80]

        try:
            claims, usage = _call_sonnet(sentences)
        except Exception as exc:
            print(f"  Sonnet error for {paper_id}: {exc}")
            time.sleep(2)
            continue

        record_call(_JOB_NAME, _MODEL, usage)

        # Estimate cost for this call to track running total
        from agent.cost_tracking import _compute_cost
        call_cost = _compute_cost(_MODEL, usage)
        cumulative_cost += call_cost

        if claims:
            _insert_claims(claims, paper_id, year, sentences)
            total_claims += len(claims)

        already_done.add(paper_id)
        papers_processed += 1

        # Polite pause between API calls
        time.sleep(0.5)

    return (
        f"processed {papers_processed} papers, "
        f"{total_claims} claims extracted, "
        f"cumulative cost ${cumulative_cost:.4f}"
    )


if __name__ == "__main__":
    print(job())
