"""Prereg-to-Replication Loop — Bet 2 / Tier 1 #1.

Two entry points:
  run_monday_prereg(force=False)  — runs Monday 04:00 CT via APScheduler
  run_daily_t7_sweep()            — runs daily 03:30 CT via APScheduler

Monday job:
  - Picks the highest-critic_score hypothesis_proposal from the past 7 days
    (not adopted, human_review IS NULL or 'auto').
  - Opus 4.7 generates a structured prereg block with falsifiable prediction,
    named DB, named statistical test, p-threshold, and citation-backed rationale.
  - Persists prereg fields into hypothesis_proposals (new columns).
  - Publishes a privacy-safe markdown block to the aquarium.

T+7 sweep:
  - Finds all prereg_published_at rows >= 7 days old with adjudicated_at IS NULL.
  - Sonnet 4.6 writes R code keyed to the registered test + DB from prereg_test_json.
  - Runs R via run_r_script (same path as weekly_comparative_analysis).
  - DETERMINISTIC adjudication: parses p-value + direction, no LLM judge for verdict.
  - Opus 4.7 writes a prose rationale AROUND the deterministic verdict.
  - Pipes through critic_pass("analysis") + pre_submission_review("journal_generic").
  - Persists + publishes verdict.

Run manually:
    python -m agent.jobs.prereg_replication_loop monday
    python -m agent.jobs.prereg_replication_loop sweep
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output, update_critic  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.critic import critic_pass  # noqa: E402
from agent.submission_review import pre_submission_review  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPUS = "claude-opus-4-7"
_SONNET = "claude-sonnet-4-6"
_JOB_NAME = "prereg_replication_loop"
_KNOWN_SHEETS_PATH = os.path.join(_PROJECT_ROOT, "data", "known_sheets.json")

# Privacy-safe deny patterns (mirrors agent/privacy.py DENY_PATTERNS subset).
# These are stripped from public prereg blocks at the paragraph level.
_PRIV_DENY = re.compile(
    r"(/Users/\S+|/Volumes/\S+|researcher@example|@tamu\.edu|@gmail\.com)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Prereg schema — matches prereg_test_json column
# ---------------------------------------------------------------------------

_PREREG_SCHEMA = """\
{
  "prediction": "<one falsifiable sentence>",
  "db_name": "<key from known_sheets.json>",
  "test_name": "BiSSE | BAMM | Mantel | PGLS | Fisher | Chi-squared | other",
  "test_params": {},
  "p_threshold": 0.05,
  "expected_direction": "positive | negative | non-zero | other",
  "expected_magnitude_min": 0.0,
  "rationale_md": "<2-4 paragraphs with cited DOIs>",
  "citations": ["doi:...", "doi:..."]
}"""

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PREREG_SYSTEM = f"""\
You are the researcher's AI postdoc. Your job is to convert a hypothesis proposal \
into a rigorous, public preregistration block.

The block must be:
- Falsifiable: state exactly what result would SUPPORT and what would REFUTE the prediction.
- Concrete: name one registered karyotype database (from the provided list), \
one statistical test, a p-value threshold, the expected effect direction, \
and a minimum magnitude if applicable.
- Cite-backed: every factual premise in rationale_md must cite a DOI.
- Honest: if no registered DB covers the hypothesis, say so in the prediction and \
set db_name to null.

AVAILABLE REGISTERED DATABASES are listed in the user message under "REGISTERED DBS".
Use ONLY names from that list for db_name. Do NOT invent file paths or DB names.

Output ONLY valid JSON matching this schema (no markdown fences):
{_PREREG_SCHEMA}"""

_R_CODE_SYSTEM = """\
You write R code to execute one preregistered statistical test.
Heath's lab uses: ape, phytools, geiger, diversitree, tidyverse.

The user message provides:
- prereg_test_json: the full preregistration JSON with test_name, db_name, test_params, \
  expected_direction.
- db_path: absolute path to the CSV.
- db_notes: column names and schema of the CSV.

RULES:
1. Read the CSV from db_path. Do NOT invent file paths.
2. Run the named test. Fit the simplest valid version of it.
3. At the END of stdout, print a single line in this exact format:
   TEALC_RESULT: p=<float> direction=<positive|negative|non-zero|zero|error> \
magnitude=<float|NA>
   (e.g.: TEALC_RESULT: p=0.023 direction=positive magnitude=0.41)
4. If the test errors, print: TEALC_RESULT: p=NA direction=error magnitude=NA
5. Do NOT call system(), shell(), or write outside the working directory.
6. Output ONLY valid JSON: {"libraries": "pkg1,pkg2", "code": "<r code>"}"""

_RATIONALE_SYSTEM = """\
You are the researcher's AI postdoc writing a 3-4 paragraph adjudication rationale \
for a public preregistration record. The verdict is already determined (you did NOT \
decide it — it was computed deterministically from p-value and direction). Your job is \
to put the statistical result in scientific context:
- What does this result imply for the hypothesis?
- What are the main caveats (sample size, phylogenetic non-independence, etc.)?
- What are the next logical tests?

Be honest about null results. Do not overstate significance. Use plain markdown. \
200-350 words."""

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _load_known_sheets() -> dict:
    try:
        with open(_KNOWN_SHEETS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _known_sheets_summary(sheets: dict) -> str:
    """Build a compact list of registered DBs for Opus prompt."""
    lines = []
    for key, info in sheets.items():
        if info.get("kind") in ("local_csv", "local_json"):
            path = info.get("path", "")
            notes = info.get("notes", "")[:120]
            lines.append(f"  {key}: {path}  [{notes}]")
    return "\n".join(lines) if lines else "  (none)"


def _create_briefing(kind: str, urgency: str, title: str, content_md: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, urgency, title, content_md, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _strip_private_paths(text: str) -> str:
    """Remove filesystem paths and personal identifiers from public markdown."""
    text = _PRIV_DENY.sub("[redacted]", text)
    # Remove remaining bare /Users/... and /Volumes/... paths
    text = re.sub(r"/(?:Users|Volumes)/[^\s,;)'\"]+", "[path-redacted]", text)
    return text


def _usage_dict(msg) -> dict:
    u = msg.usage
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
    }


# ---------------------------------------------------------------------------
# Monday: Prereg generation
# ---------------------------------------------------------------------------


def _pick_candidate() -> dict | None:
    """Return the highest-critic_score hypothesis_proposal from the past 7 days.

    Filters: created_at >= now-7d, (human_review IS NULL OR 'auto'), adopted_at IS NULL.
    Returns None if no qualifying row.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT hp.id, hp.project_id, hp.hypothesis_md, hp.rationale_md,
                      hp.proposed_test_md, hp.cited_paper_dois,
                      ol.critic_score,
                      COALESCE(p.name, hp.project_id) AS project_name
               FROM hypothesis_proposals hp
               LEFT JOIN output_ledger ol
                  ON ol.provenance_json LIKE '%"hypothesis_id": ' || hp.id || '%'
               LEFT JOIN research_projects p ON p.id = hp.project_id
               WHERE (hp.proposed_iso >= ? OR hp.created_at >= ?)
                 AND (hp.human_review IS NULL OR hp.human_review = 'auto')
                 AND hp.adopted_at IS NULL
                 AND hp.prereg_published_at IS NULL
               ORDER BY COALESCE(ol.critic_score, 0) DESC
               LIMIT 1""",
            (since, since),
        ).fetchone()
    except Exception:
        # Fallback: hypothesis_proposals may not have proposed_iso vs created_at;
        # try without date filter on older schema
        try:
            row = conn.execute(
                """SELECT hp.id, hp.project_id, hp.hypothesis_md, hp.rationale_md,
                          hp.proposed_test_md, hp.cited_paper_dois,
                          ol.critic_score,
                          COALESCE(p.name, hp.project_id) AS project_name
                   FROM hypothesis_proposals hp
                   LEFT JOIN output_ledger ol
                      ON ol.provenance_json LIKE '%"hypothesis_id": ' || hp.id || '%'
                   LEFT JOIN research_projects p ON p.id = hp.project_id
                   WHERE (hp.human_review IS NULL OR hp.human_review = 'auto')
                     AND hp.adopted_at IS NULL
                     AND hp.prereg_published_at IS NULL
                   ORDER BY COALESCE(ol.critic_score, 0) DESC
                   LIMIT 1""",
            ).fetchone()
        except Exception:
            row = None
    conn.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "project_id": row[1],
        "hypothesis_md": row[2] or "",
        "rationale_md": row[3] or "",
        "proposed_test_md": row[4] or "",
        "cited_paper_dois": row[5] or "",
        "critic_score": row[6],
        "project_name": row[7] or "",
    }


def _call_opus_prereg(client: Anthropic, proposal: dict, sheets_summary: str) -> tuple[dict | None, Any]:
    """Call Opus 4.7 to generate a structured prereg block.

    Returns (parsed_json | None, raw_message).
    """
    user_msg = (
        f"Hypothesis proposal (id={proposal['id']}):\n"
        f"{proposal['hypothesis_md']}\n\n"
        f"Rationale:\n{proposal['rationale_md']}\n\n"
        f"Proposed test (from author):\n{proposal['proposed_test_md']}\n\n"
        f"Cited DOIs:\n{proposal['cited_paper_dois']}\n\n"
        f"REGISTERED DBS (use ONLY these names for db_name):\n{sheets_summary}\n"
    )
    try:
        msg = client.messages.create(
            model=_OPUS,
            max_tokens=2000,
            system=_PREREG_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        print(f"[{_JOB_NAME}] Opus prereg call failed: {e}")
        return None, None

    raw = msg.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    # Find first { ... }
    brace = raw.find("{")
    if brace == -1:
        return None, msg
    try:
        parsed = json.loads(raw[brace:])
        return parsed, msg
    except Exception:
        # Try to extract up to matching brace
        snippet = raw[brace:]
        depth = 0
        end = None
        in_s = escape = False
        for i, ch in enumerate(snippet):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_s = not in_s
                continue
            if in_s:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                return json.loads(snippet[:end]), msg
            except Exception:
                pass
        return None, msg


def _publish_prereg_to_aquarium(proposal: dict, prereg: dict) -> None:
    """Push a privacy-safe prereg event to the aquarium JSON."""
    try:
        from agent.jobs.publish_aquarium import AQUARIUM_LOG, AQUARIUM_MAX_EVENTS, _load_aquarium, _push_to_worker  # noqa: PLC0415
        log = _load_aquarium()
        events = log.get("recent_activity", [])
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events.insert(0, {
            "time": now_iso,
            "type": "tool",
            "description": "Published a public preregistration",
        })
        log["recent_activity"] = events[:AQUARIUM_MAX_EVENTS]
        log["last_updated"] = now_iso
        with open(AQUARIUM_LOG, "w") as f:
            json.dump(log, f, indent=2)
        _push_to_worker(json.dumps(log, indent=2).encode("utf-8"))
    except Exception as e:
        print(f"[{_JOB_NAME}] aquarium publish error: {e}")


@tracked("prereg_replication_loop")
def run_monday_prereg(force: bool = False) -> dict:
    """Monday 04:00 CT: pick best proposal, generate prereg, publish.

    Returns a dict with keys: status, hypothesis_id, prereg_test_json.
    """
    # Working-hours guard — bypass with force=True or FORCE_RUN env
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and not force and not os.environ.get("FORCE_RUN"):
        return {"status": f"skipped: working-hours guard (hour={hour})"}

    # 1. Pick best candidate
    proposal = _pick_candidate()
    if proposal is None:
        _create_briefing(
            kind="prereg",
            urgency="info",
            title="No prereg candidates this week",
            content_md=(
                "The Monday prereg sweep found no qualifying hypothesis proposals "
                "(past 7 days, human_review IS NULL or auto, not yet adopted, "
                "no existing prereg). Nothing published."
            ),
        )
        return {"status": "no_candidates"}

    # 2. Load registered DBs
    sheets = _load_known_sheets()
    sheets_summary = _known_sheets_summary(sheets)

    # 3. Opus 4.7 generates prereg block
    client = Anthropic()
    prereg, opus_msg = _call_opus_prereg(client, proposal, sheets_summary)

    if opus_msg is not None:
        try:
            record_call(
                job_name=_JOB_NAME,
                model=_OPUS,
                usage=_usage_dict(opus_msg),
            )
        except Exception as e:
            print(f"[{_JOB_NAME}] cost tracking error: {e}")

    if prereg is None:
        return {"status": "opus_parse_failure", "hypothesis_id": proposal["id"]}

    # 4. Validate DB name is registered
    db_name = prereg.get("db_name") or ""
    if db_name and db_name not in sheets:
        # Opus hallucinated a DB name — clear it
        prereg["db_name"] = None
        db_name = ""

    prereg_test_json = json.dumps(prereg)

    # 5. Build public-safe markdown block (strip paths/names per paragraph)
    now_iso = datetime.now(timezone.utc).isoformat()
    db_path = sheets.get(db_name, {}).get("path", "registered DB") if db_name else "no registered DB"
    public_md_raw = (
        f"## Preregistration: {proposal['project_name']}\n\n"
        f"**Prediction:** {prereg.get('prediction', '')}\n\n"
        f"**Database:** {db_name or '(none)'}\n"
        f"**Test:** {prereg.get('test_name', '')}\n"
        f"**p-threshold:** {prereg.get('p_threshold', 0.05)}\n"
        f"**Expected direction:** {prereg.get('expected_direction', '')}\n"
        f"**Expected magnitude min:** {prereg.get('expected_magnitude_min', 0.0)}\n\n"
        f"**Rationale:**\n\n{prereg.get('rationale_md', '')}\n\n"
        f"**Citations:** {'; '.join(prereg.get('citations', []))}\n\n"
        f"*Preregistered: {now_iso[:10]}*\n"
    )
    public_md = _strip_private_paths(public_md_raw)
    prereg_aquarium_url = ""  # populated if aquarium worker returns a URL

    # 6. Persist to hypothesis_proposals
    conn = _conn()
    try:
        conn.execute(
            """UPDATE hypothesis_proposals
               SET prereg_published_at=?,
                   prereg_md=?,
                   prereg_test_json=?,
                   prereg_aquarium_url=?
               WHERE id=?""",
            (now_iso, public_md, prereg_test_json, prereg_aquarium_url, proposal["id"]),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"status": f"db_persist_error: {e}", "hypothesis_id": proposal["id"]}
    conn.close()

    # 7. Record in output_ledger
    try:
        tok_in = _usage_dict(opus_msg)["input_tokens"] if opus_msg else 0
        tok_out = _usage_dict(opus_msg)["output_tokens"] if opus_msg else 0
        ledger_id = record_output(
            kind="prereg",
            job_name=_JOB_NAME,
            model=_OPUS,
            project_id=str(proposal["project_id"]) if proposal.get("project_id") else None,
            content_md=public_md,
            tokens_in=tok_in,
            tokens_out=tok_out,
            provenance={
                "hypothesis_id": proposal["id"],
                "db_name": db_name,
                "test_name": prereg.get("test_name"),
                "p_threshold": prereg.get("p_threshold", 0.05),
                "expected_direction": prereg.get("expected_direction"),
                "step": "monday_prereg",
            },
        )
    except Exception as e:
        print(f"[{_JOB_NAME}] ledger error: {e}")
        ledger_id = None

    # 8. Publish to aquarium
    _publish_prereg_to_aquarium(proposal, prereg)

    # 9. Briefing for Heath
    _create_briefing(
        kind="prereg",
        urgency="info",
        title=f"Preregistration published — {proposal['project_name']}",
        content_md=(
            f"A preregistration has been published for proposal #{proposal['id']}.\n\n"
            f"**Test:** {prereg.get('test_name')} on `{db_name}`  \n"
            f"**p-threshold:** {prereg.get('p_threshold', 0.05)}  \n"
            f"**Direction:** {prereg.get('expected_direction')}\n\n"
            f"The T+7 sweep will adjudicate automatically in ≥7 days.\n\n"
            f"---\n\n{public_md}"
        ),
    )

    return {
        "status": "published",
        "hypothesis_id": proposal["id"],
        "db_name": db_name,
        "test_name": prereg.get("test_name"),
        "prereg_test_json": prereg_test_json,
        "ledger_id": ledger_id,
    }


# ---------------------------------------------------------------------------
# T+7 sweep: Adjudication
# ---------------------------------------------------------------------------


def _load_ripe_preregs() -> list[dict]:
    """Return hypothesis_proposals rows with prereg >= 7 days old and not adjudicated."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, project_id, hypothesis_md, prereg_test_json,
                      prereg_md, prereg_published_at
               FROM hypothesis_proposals
               WHERE prereg_published_at IS NOT NULL
                 AND prereg_published_at <= ?
                 AND adjudicated_at IS NULL
               ORDER BY prereg_published_at ASC""",
            (cutoff,),
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [
        {
            "id": r[0],
            "project_id": r[1],
            "hypothesis_md": r[2] or "",
            "prereg_test_json": r[3] or "{}",
            "prereg_md": r[4] or "",
            "prereg_published_at": r[5] or "",
        }
        for r in rows
    ]


def _generate_r_code(client: Anthropic, prereg_json: dict, db_info: dict) -> tuple[str, Any]:
    """Sonnet 4.6 writes R code for the named test against the named DB.

    Returns (r_code, message).
    """
    db_path = db_info.get("path", "")
    db_notes = db_info.get("notes", "")
    user_msg = (
        f"prereg_test_json:\n{json.dumps(prereg_json, indent=2)}\n\n"
        f"db_path: {db_path}\n"
        f"db_notes: {db_notes}\n"
    )
    try:
        msg = client.messages.create(
            model=_SONNET,
            max_tokens=3000,
            system=_R_CODE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return "", None  # type: ignore[return-value]

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    brace = raw.find("{")
    if brace == -1:
        return "", msg
    try:
        data = json.loads(raw[brace:])
        return data.get("code", ""), msg
    except Exception:
        return "", msg


def _parse_tealc_result(stdout: str) -> tuple[float | None, str, float | None]:
    """Parse the TEALC_RESULT line from R stdout.

    Returns (p_value, direction, magnitude).
    direction is one of: positive, negative, non-zero, zero, error, unknown.
    """
    for line in stdout.splitlines():
        if line.strip().startswith("TEALC_RESULT:"):
            # e.g. TEALC_RESULT: p=0.023 direction=positive magnitude=0.41
            p_val = None
            direction = "unknown"
            magnitude = None
            p_match = re.search(r"p=([0-9.eE+\-]+|NA)", line)
            d_match = re.search(r"direction=(\S+)", line)
            m_match = re.search(r"magnitude=([0-9.eE+\-]+|NA)", line)
            if p_match and p_match.group(1) != "NA":
                try:
                    p_val = float(p_match.group(1))
                except ValueError:
                    p_val = None
            if d_match:
                direction = d_match.group(1).lower()
            if m_match and m_match.group(1) != "NA":
                try:
                    magnitude = float(m_match.group(1))
                except ValueError:
                    magnitude = None
            return p_val, direction, magnitude
    return None, "unknown", None


def _adjudicate(
    prereg_json: dict,
    p_val: float | None,
    direction: str,
    magnitude: float | None,
    exit_code: int,
    stdout: str,
) -> str:
    """Deterministic verdict — NO LLM involved.

    Returns one of: supported | refuted | null | aborted
    """
    if exit_code != 0 or direction == "error" or p_val is None:
        return "aborted"

    threshold = float(prereg_json.get("p_threshold") or 0.05)
    expected_dir = (prereg_json.get("expected_direction") or "").lower()
    mag_min = float(prereg_json.get("expected_magnitude_min") or 0.0)

    if p_val >= threshold:
        return "null"

    # p < threshold — check direction
    dir_ok = False
    if expected_dir in ("positive", "negative"):
        dir_ok = direction == expected_dir
    elif expected_dir == "non-zero":
        dir_ok = direction not in ("zero", "error", "unknown")
    else:
        # "other" or unspecified — direction match is not checked
        dir_ok = True

    if not dir_ok:
        return "refuted"

    # Direction matches — check magnitude
    if magnitude is not None and magnitude < mag_min:
        return "refuted"

    return "supported"


def _call_opus_rationale(
    client: Anthropic,
    hypothesis_md: str,
    prereg_json: dict,
    verdict: str,
    p_val: float | None,
    direction: str,
    magnitude: float | None,
    r_stdout: str,
) -> tuple[str, Any]:
    """Opus 4.7 writes a prose rationale AROUND the deterministic verdict."""
    user_msg = (
        f"Hypothesis:\n{hypothesis_md}\n\n"
        f"Preregistered test: {prereg_json.get('test_name')} on {prereg_json.get('db_name')}\n"
        f"Preregistered prediction: {prereg_json.get('prediction')}\n"
        f"p-threshold: {prereg_json.get('p_threshold', 0.05)}\n"
        f"Expected direction: {prereg_json.get('expected_direction')}\n\n"
        f"Deterministic verdict: **{verdict.upper()}**\n"
        f"Actual result: p={p_val}, direction={direction}, magnitude={magnitude}\n\n"
        f"R stdout (first 2000 chars):\n{r_stdout[:2000]}\n"
    )
    try:
        msg = client.messages.create(
            model=_OPUS,
            max_tokens=1500,
            system=_RATIONALE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = msg.content[0].text.strip()
        return text, msg
    except Exception as e:
        return f"Rationale generation failed: {e}", None


def _persist_adjudication(
    hypothesis_id: int,
    verdict: str,
    rationale_md: str,
    run_id: int | None,
) -> None:
    """Write adjudication fields back to hypothesis_proposals."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        conn.execute(
            """UPDATE hypothesis_proposals
               SET adjudicated_at=?,
                   adjudication=?,
                   adjudication_rationale_md=?,
                   replication_run_id=?
               WHERE id=?""",
            (now_iso, verdict, rationale_md, run_id, hypothesis_id),
        )
        conn.commit()
    except Exception as e:
        print(f"[{_JOB_NAME}] adjudication persist error: {e}")
    finally:
        conn.close()


def _publish_verdict_to_aquarium(verdict: str) -> None:
    """Push a privacy-safe verdict event to the aquarium JSON."""
    try:
        from agent.jobs.publish_aquarium import AQUARIUM_LOG, AQUARIUM_MAX_EVENTS, _load_aquarium, _push_to_worker  # noqa: PLC0415
        log = _load_aquarium()
        events = log.get("recent_activity", [])
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events.insert(0, {
            "time": now_iso,
            "type": "tool",
            "description": f"Published a replication verdict ({verdict})",
        })
        log["recent_activity"] = events[:AQUARIUM_MAX_EVENTS]
        log["last_updated"] = now_iso
        with open(AQUARIUM_LOG, "w") as f:
            json.dump(log, f, indent=2)
        _push_to_worker(json.dumps(log, indent=2).encode("utf-8"))
    except Exception as e:
        print(f"[{_JOB_NAME}] aquarium verdict publish error: {e}")


def _adjudicate_one(client: Anthropic, row: dict) -> dict:
    """Run the full adjudication pipeline for one ripe prereg row.

    Returns a dict with keys: hypothesis_id, verdict, status.
    """
    hypothesis_id = row["id"]
    project_id = row.get("project_id")

    # 1. Parse prereg_test_json
    try:
        prereg_json = json.loads(row["prereg_test_json"])
    except Exception:
        _persist_adjudication(hypothesis_id, "aborted", "Could not parse prereg_test_json.", None)
        return {"hypothesis_id": hypothesis_id, "verdict": "aborted", "status": "parse_error"}

    db_name = prereg_json.get("db_name") or ""
    sheets = _load_known_sheets()
    db_info = sheets.get(db_name, {})

    if not db_name or not db_info or db_info.get("kind") not in ("local_csv", "local_json"):
        _persist_adjudication(
            hypothesis_id, "aborted",
            f"DB '{db_name}' not found in known_sheets.json or not a local CSV/JSON.", None,
        )
        return {"hypothesis_id": hypothesis_id, "verdict": "aborted", "status": "db_missing"}

    db_path = db_info.get("path", "")
    if not os.path.exists(db_path):
        _persist_adjudication(
            hypothesis_id, "aborted",
            f"DB file not found on disk: {db_path}", None,
        )
        return {"hypothesis_id": hypothesis_id, "verdict": "aborted", "status": "db_file_missing"}

    # 2. Sonnet generates R code
    r_code, sonnet_msg = _generate_r_code(client, prereg_json, db_info)
    if sonnet_msg is not None:
        try:
            record_call(job_name=_JOB_NAME, model=_SONNET, usage=_usage_dict(sonnet_msg))
        except Exception:
            pass

    if not r_code:
        _persist_adjudication(hypothesis_id, "aborted", "Sonnet failed to generate R code.", None)
        return {"hypothesis_id": hypothesis_id, "verdict": "aborted", "status": "r_gen_error"}

    # 3. Run R via run_r_script (same path as weekly_comparative_analysis)
    try:
        from agent.tools import run_r_script  # noqa: PLC0415
        result_str = run_r_script.invoke({
            "code": r_code,
            "libraries": "",
            "timeout_seconds": 600,
        })
        result = json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        _persist_adjudication(hypothesis_id, "aborted", f"run_r_script failed: {e}", None)
        return {"hypothesis_id": hypothesis_id, "verdict": "aborted", "status": f"r_run_error: {e}"}

    exit_code = result.get("exit_code", -1)
    stdout = (result.get("stdout") or "")[:6000]
    stderr = (result.get("stderr") or "")[:2000]
    working_dir = result.get("working_dir", "")

    # 4. Store in analysis_runs for traceability
    run_id: int | None = None
    try:
        conn = _conn()
        cur = conn.execute(
            """INSERT INTO analysis_runs (
                project_id, run_iso, next_action_text, r_code, working_dir,
                exit_code, stdout_truncated, stderr_truncated,
                plot_paths, created_files, interpretation_md, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                datetime.now(timezone.utc).isoformat(),
                f"prereg_t7: hypothesis_id={hypothesis_id}",
                r_code,
                working_dir,
                exit_code,
                stdout,
                stderr,
                json.dumps(result.get("plot_paths") or []),
                json.dumps(result.get("created_files") or []),
                "",  # filled in after rationale
                "success" if exit_code == 0 else "r_error",
            ),
        )
        run_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[{_JOB_NAME}] analysis_runs insert error: {e}")

    # 5. Deterministic adjudication (NO LLM)
    p_val, direction, magnitude = _parse_tealc_result(stdout)
    verdict = _adjudicate(prereg_json, p_val, direction, magnitude, exit_code, stdout)

    # 6. Opus writes prose rationale AROUND the deterministic verdict
    rationale_md, opus_msg = _call_opus_rationale(
        client, row["hypothesis_md"], prereg_json,
        verdict, p_val, direction, magnitude, stdout,
    )
    if opus_msg is not None:
        try:
            record_call(job_name=_JOB_NAME, model=_OPUS, usage=_usage_dict(opus_msg))
        except Exception:
            pass

    # 7. Critic pass on rationale
    critic_result: dict = {}
    try:
        critic_result = critic_pass(rationale_md, rubric_name="analysis")
    except Exception as e:
        print(f"[{_JOB_NAME}] critic_pass error: {e}")

    # 8. Pre-submission review on the full verdict document
    full_verdict_doc = (
        f"## Replication Verdict: {verdict.upper()}\n\n"
        f"**Hypothesis:** {row['hypothesis_md']}\n\n"
        f"**Preregistered prediction:** {prereg_json.get('prediction', '')}\n\n"
        f"**Statistical result:** p={p_val}, direction={direction}, "
        f"magnitude={magnitude}\n\n"
        f"**Adjudication rationale:**\n\n{rationale_md}"
    )
    review_result: dict = {}
    try:
        review_result = pre_submission_review(full_verdict_doc, venue="journal_generic")
    except Exception as e:
        print(f"[{_JOB_NAME}] pre_submission_review error: {e}")

    # 9. Persist adjudication
    _persist_adjudication(hypothesis_id, verdict, rationale_md, run_id)

    # 10. Update analysis_runs with interpretation
    if run_id is not None:
        try:
            conn = _conn()
            conn.execute(
                "UPDATE analysis_runs SET interpretation_md=? WHERE id=?",
                (rationale_md, run_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # 11. Ledger record
    ledger_id: int | None = None
    try:
        tok_in = (_usage_dict(opus_msg)["input_tokens"] if opus_msg else 0)
        tok_out = (_usage_dict(opus_msg)["output_tokens"] if opus_msg else 0)
        ledger_id = record_output(
            kind="replication_verdict",
            job_name=_JOB_NAME,
            model=_OPUS,
            project_id=str(project_id) if project_id else None,
            content_md=rationale_md,
            tokens_in=tok_in,
            tokens_out=tok_out,
            provenance={
                "hypothesis_id": hypothesis_id,
                "verdict": verdict,
                "p_val": p_val,
                "direction": direction,
                "magnitude": magnitude,
                "analysis_run_id": run_id,
                "critic_score": critic_result.get("score"),
                "review_consensus": review_result.get("consensus", ""),
                "step": "t7_adjudication",
            },
        )
    except Exception as e:
        print(f"[{_JOB_NAME}] ledger record error: {e}")

    if ledger_id is not None and critic_result:
        try:
            update_critic(
                ledger_id,
                int(critic_result.get("score") or 0),
                critic_result.get("overall_notes", ""),
                critic_result.get("model", _OPUS),
            )
        except Exception as e:
            print(f"[{_JOB_NAME}] update_critic error: {e}")

    # 12. Publish to aquarium
    _publish_verdict_to_aquarium(verdict)

    # 13. Briefing for Heath
    _create_briefing(
        kind="replication_verdict",
        urgency="warn" if verdict == "supported" else "info",
        title=f"Replication verdict: {verdict.upper()} — hypothesis #{hypothesis_id}",
        content_md=(
            f"**Verdict:** {verdict.upper()}  \n"
            f"**Test:** {prereg_json.get('test_name')} on `{db_name}`  \n"
            f"**p-value:** {p_val}  (threshold: {prereg_json.get('p_threshold', 0.05)})  \n"
            f"**Direction:** {direction} (expected: {prereg_json.get('expected_direction')})  \n"
            f"**Magnitude:** {magnitude}  \n\n"
            f"---\n\n{rationale_md}\n\n"
            f"---\n\n*Critic score: {critic_result.get('score', '?')}/5*  \n"
            f"*Reviewer consensus: {review_result.get('consensus', '(not available)')}*"
        ),
    )

    return {
        "hypothesis_id": hypothesis_id,
        "verdict": verdict,
        "p_val": p_val,
        "direction": direction,
        "status": "adjudicated",
    }


@tracked("prereg_replication_loop")
def run_daily_t7_sweep() -> dict:
    """Daily 03:30 CT: adjudicate all ripe preregs (published >= 7 days, not yet adjudicated).

    Returns dict with keys: status, adjudicated (list of results).
    """
    ripe = _load_ripe_preregs()
    if not ripe:
        return {"status": "no_ripe_preregs", "adjudicated": []}

    client = Anthropic()
    results = []
    for row in ripe:
        try:
            r = _adjudicate_one(client, row)
        except Exception as e:
            r = {"hypothesis_id": row["id"], "verdict": "aborted", "status": f"exception: {e}"}
        results.append(r)

    verdicts = [r["verdict"] for r in results]
    return {
        "status": f"adjudicated {len(results)} preregs",
        "adjudicated": results,
        "verdicts": verdicts,
    }


# ---------------------------------------------------------------------------
# Manual entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "monday"
    if cmd == "monday":
        print(json.dumps(run_monday_prereg(force=True), indent=2))
    elif cmd == "sweep":
        print(json.dumps(run_daily_t7_sweep(), indent=2))
    else:
        print(f"Usage: python -m agent.jobs.prereg_replication_loop [monday|sweep]")
