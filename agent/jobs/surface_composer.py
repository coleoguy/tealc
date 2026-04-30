"""surface_composer.py — dual-register lead composer for wiki topic pages.

Scheduled target: daily 3:00 am Central (PI wires cron separately).

For each topic page it:
  1. Extracts the researcher lead from the existing tealc:auto-start region
     (first paragraph(s) of ## Current understanding).
  2. Calls Haiku 4.5 to rewrite that lead for an undergraduate reader.
  3. Validates digit-substring consistency across both registers.
  4. Optionally runs critic_pass (1-in-8 deterministic sampling).
  5. Wraps both versions in the <!-- tealc:lead-start/end --> block the
     wikiRegisterToggle JS expects, writes in-place (live) or to a
     .proposed file (dry-run).

All model calls are logged to cost_tracking + output_ledger. Budget ceiling
is enforced between topics.

Manual invocation:
    python -m agent.jobs.surface_composer [--dry-run|--live] [--topic <slug>] [--verbose]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from typing import Optional

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call, _compute_cost  # noqa: E402
from agent.critic import critic_pass  # noqa: E402
from agent.config import should_run_this_cycle  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_NAME = "surface_composer"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_WIKI_ROOT = os.environ.get(
    "WIKI_TOPICS_DIR",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/knowledge"),
)
_TOPICS_DIR = os.path.join(_WIKI_ROOT, "topics")
_PROPOSALS_DIR = os.path.join(_PROJECT_ROOT, "data", "surface_composer_proposals")

_PROMPTS_DIR = os.path.join(_PROJECT_ROOT, "agent", "prompts")

# Marker constants
_LEAD_START = "<!-- tealc:lead-start -->"
_LEAD_END = "<!-- tealc:lead-end -->"
_AUTO_START = "<!-- tealc:auto-start -->"
_AUTO_END = "<!-- tealc:auto-end -->"

# DDL for the topic_lead_cache table — created on first use
_TOPIC_LEAD_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS topic_lead_cache (
  topic_slug TEXT PRIMARY KEY,
  student_lead_hash TEXT,
  researcher_lead_hash TEXT,
  last_rendered TEXT,
  last_critic_score REAL,
  last_cost_usd REAL
)
"""

# Digit check — re-use wiki_pipeline module-level helpers
_DIGIT_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_job_config() -> dict:
    """Load the surface_composer block from tealc_config.json."""
    cfg_path = os.path.join(_PROJECT_ROOT, "data", "tealc_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    return cfg.get("jobs", {}).get("surface_composer", {})


def _is_dry_run(override: Optional[bool] = None) -> bool:
    if override is not None:
        return override
    return bool(_load_job_config().get("dry_run", True))


def _budget_ceiling() -> float:
    return float(_load_job_config().get("max_cost_usd_per_run", 1.50))


def _max_topics() -> int:
    return int(_load_job_config().get("max_topics_per_run", 5))


def _critic_sample_ratio() -> int:
    return int(_load_job_config().get("critic_sample_ratio", 8))


# ---------------------------------------------------------------------------
# Digit-substring helpers  (mirrors wiki_pipeline._digit_substring_check)
# ---------------------------------------------------------------------------

def _normalize_for_compare(s: str) -> str:
    """Lowercase + collapse whitespace. Mirrors wiki_pipeline helper."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()


def _extract_digits(text: str) -> set[str]:
    """Return the set of numeric strings found in text."""
    norm = _normalize_for_compare(text).replace(",", "")
    return set(_DIGIT_NUM_RE.findall(norm))


def _digit_substring_ok(researcher_lead: str, student_lead: str) -> tuple[bool, str]:
    """Check that every digit in the researcher lead appears in the student lead.

    Returns (ok, reason). Fails fast on the first missing number.
    """
    r_digits = _extract_digits(researcher_lead)
    s_norm = _normalize_for_compare(student_lead).replace(",", "")
    missing = [d for d in sorted(r_digits) if d not in s_norm]
    if missing:
        return False, f"student lead missing numerics: {missing[:8]}"
    # Also verify digit sets are consistent (no new numbers invented)
    s_digits = _extract_digits(student_lead)
    invented = s_digits - r_digits
    if invented:
        return False, f"student lead invented new numerics: {list(invented)[:8]}"
    return True, "ok"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _ensure_table() -> None:
    """Create topic_lead_cache table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_TOPIC_LEAD_CACHE_DDL)
    conn.commit()
    conn.close()


def _get_cache_row(slug: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM topic_lead_cache WHERE topic_slug = ?", (slug,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _upsert_cache(slug: str, student_hash: str, researcher_hash: str,
                  critic_score: Optional[float], cost_usd: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO topic_lead_cache
               (topic_slug, student_lead_hash, researcher_lead_hash,
                last_rendered, last_critic_score, last_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(topic_slug) DO UPDATE SET
               student_lead_hash=excluded.student_lead_hash,
               researcher_lead_hash=excluded.researcher_lead_hash,
               last_rendered=excluded.last_rendered,
               last_critic_score=excluded.last_critic_score,
               last_cost_usd=excluded.last_cost_usd""",
        (slug, student_hash, researcher_hash, now, critic_score, cost_usd),
    )
    conn.commit()
    conn.close()


def _topic_last_updated_from_db(slug: str) -> Optional[str]:
    """Return topics.last_updated from agent.db for the slug, or None."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT last_updated FROM topics WHERE slug = ?", (slug,)
        ).fetchone()
        conn.close()
        return row["last_updated"] if row else None
    except Exception:
        return None


def _needs_render(slug: str, topic_path: str) -> bool:
    """True if the topic has been updated since the lead was last composed."""
    cache = _get_cache_row(slug)
    if not cache:
        return True
    last_rendered_str = cache.get("last_rendered") or ""
    if not last_rendered_str:
        return True

    # Get topic last_updated: prefer DB, fall back to mtime
    topic_updated_str = _topic_last_updated_from_db(slug)
    if topic_updated_str:
        try:
            topic_dt = datetime.fromisoformat(topic_updated_str.replace("Z", "+00:00"))
        except Exception:
            topic_dt = None
    else:
        topic_dt = None

    if topic_dt is None:
        # Fallback: file mtime
        try:
            mtime = os.path.getmtime(topic_path)
            topic_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        except Exception:
            return True

    try:
        rendered_dt = datetime.fromisoformat(last_rendered_str.replace("Z", "+00:00"))
    except Exception:
        return True

    return topic_dt > rendered_dt


# ---------------------------------------------------------------------------
# Frontmatter parser (lightweight line-scanner)
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> tuple[dict, int]:
    """Return (frontmatter_dict, body_start_index). Returns ({}, 0) if absent."""
    if not content.startswith("---"):
        return {}, 0
    end = content.find("\n---\n", 3)
    if end == -1:
        return {}, 0
    fm_text = content[3:end]
    fm: dict = {}
    for line in fm_text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, end + 5


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Load topic_page_writer.md from agent/prompts/."""
    path = os.path.join(_PROMPTS_DIR, "topic_page_writer.md")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Lead extraction helpers
# ---------------------------------------------------------------------------

def _extract_auto_region(content: str) -> str:
    """Return the body inside tealc:auto-start/end markers, or empty string."""
    start_idx = content.find(_AUTO_START)
    end_idx = content.find(_AUTO_END)
    if start_idx == -1 or end_idx == -1:
        return ""
    inner = content[start_idx + len(_AUTO_START):end_idx]
    return inner.strip()


def _extract_researcher_lead(auto_body: str) -> str:
    """Extract the first paragraph(s) of ## Current understanding from auto body.

    Returns the prose text (with markdown links intact) up to the next H2.
    """
    # Find '## Current understanding'
    cu_match = re.search(r"##\s+Current understanding\s*\n", auto_body, re.IGNORECASE)
    if not cu_match:
        return ""
    body_after = auto_body[cu_match.end():]
    # Stop at the next H2
    next_h2 = re.search(r"\n##\s+", body_after)
    section = body_after[:next_h2.start()] if next_h2 else body_after
    return section.strip()


def _strip_leading_h1(text: str) -> str:
    """Remove any leading H1 line the model may have added."""
    return re.sub(r"^#[^#][^\n]*\n+", "", text.lstrip("\n"), count=1).strip()


# ---------------------------------------------------------------------------
# Haiku call
# ---------------------------------------------------------------------------

def _call_haiku(client: Anthropic, system_prompt_text: str,
                researcher_lead: str, verbose: bool = False) -> tuple[str, dict]:
    """Call Haiku with the topic_page_writer system prompt + student-register suffix.

    Returns (student_lead_text, usage_dict).
    """
    suffix = (
        "You are rewriting the LEAD of this topic page for an undergraduate reader. "
        "Output ONLY the lead prose — no H1, no section headings. "
        "Target 180–280 words. Flesch-Kincaid grade level ≤ 11. "
        "Preserve EVERY numeric claim, percentage, and cardinal number from the "
        "researcher lead below (they are required for validator pass). "
        "Keep the same markdown links. Do not add new claims. "
        "Do not introduce jargon without linking to `/knowledge/concepts/<slug>/` "
        "(even if that page does not exist yet — the link is the placeholder).\n\n"
        "Researcher lead (verbatim):\n"
        + researcher_lead
        + "\n\nExisting glossary terms to link on first use: [leave blank — V1 does not harvest concepts yet]"
    )

    msg = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=800,
        system=[
            {
                "type": "text",
                "text": system_prompt_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": suffix}],
    )
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    raw = msg.content[0].text if msg.content else ""
    student_lead = _strip_leading_h1(raw)
    if verbose:
        print(f"  [haiku] in={usage['input_tokens']} out={usage['output_tokens']} "
              f"cache_read={usage['cache_read_input_tokens']}")
    return student_lead, usage


# ---------------------------------------------------------------------------
# Lead region composition
# ---------------------------------------------------------------------------

def _compose_lead_block(researcher_lead: str, student_lead: str) -> str:
    """Return the full <!-- tealc:lead-start/end --> block."""
    return (
        "<!-- tealc:lead-start -->\n"
        "<div class=\"wiki-lead\" data-active=\"researcher\">\n"
        "<div data-register=\"researcher\" markdown=\"1\">\n"
        "\n"
        + researcher_lead.strip()
        + "\n"
        "\n"
        "</div>\n"
        "<div data-register=\"student\" markdown=\"1\">\n"
        "\n"
        + student_lead.strip()
        + "\n"
        "\n"
        "</div>\n"
        "</div>\n"
        "<!-- tealc:lead-end -->"
    )


def _splice_lead_into_page(original: str, lead_block: str) -> str:
    """Insert or replace the lead block in the topic page content.

    Strategy:
      - If tealc:lead-start/end markers already exist, replace the region.
      - Otherwise insert the block between the frontmatter and tealc:auto-start.
    """
    start_idx = original.find(_LEAD_START)
    end_idx = original.find(_LEAD_END)

    if start_idx != -1 and end_idx != -1:
        # Replace existing region
        before = original[:start_idx]
        after = original[end_idx + len(_LEAD_END):]
        return before + lead_block + after

    # Insert between frontmatter and tealc:auto-start
    auto_idx = original.find(_AUTO_START)
    if auto_idx == -1:
        # Fallback: prepend after frontmatter
        _, body_start = _parse_frontmatter(original)
        before = original[:body_start]
        after = original[body_start:]
        return before + lead_block + "\n" + after

    before = original[:auto_idx]
    after = original[auto_idx:]
    # Ensure exactly one blank line between lead block and auto-start
    return before.rstrip("\n") + "\n" + lead_block + "\n\n" + after


# ---------------------------------------------------------------------------
# Critic sampling
# ---------------------------------------------------------------------------

def _should_run_critic(slug: str) -> bool:
    """Deterministic 1-in-N sampling: hash(slug) % N == date.today().toordinal() % N."""
    ratio = _critic_sample_ratio()
    if ratio <= 0:
        return False
    return (hash(slug) % ratio) == (date.today().toordinal() % ratio)


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _log_warning(slug: str, reason: str, dry_run: bool) -> None:
    try:
        record_output(
            kind="wiki_warning",
            job_name=_JOB_NAME,
            model=_HAIKU_MODEL,
            project_id=None,
            content_md=f"[{slug}] skipped: {reason}",
            tokens_in=0,
            tokens_out=0,
            provenance={"slug": slug, "reason": reason, "dry_run": dry_run},
        )
    except Exception as exc:
        print(f"[{_JOB_NAME}] ledger warning write failed for {slug}: {exc}")


def _log_edit(slug: str, digit_ok: bool, critic_score: Optional[float],
              cost_usd: float, dry_run: bool, render_hash: str,
              usage: dict, topic_path: str) -> None:
    try:
        record_output(
            kind="wiki_edit",
            job_name=_JOB_NAME,
            model=_HAIKU_MODEL,
            project_id=None,
            content_md=(
                f"[{slug}] lead composed | digit_ok={digit_ok} "
                f"critic_score={critic_score} cost_usd={cost_usd:.6f} "
                f"dry_run={dry_run}"
            ),
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            provenance={
                "slug": slug,
                "digit_substring_pass": digit_ok,
                "critic_score": critic_score,
                "cost_usd": cost_usd,
                "dry_run": dry_run,
                "render_hash": render_hash,
                "topic_path": topic_path,
            },
        )
    except Exception as exc:
        print(f"[{_JOB_NAME}] ledger edit write failed for {slug}: {exc}")


def _log_budget_breach(spent: float, ceiling: float) -> None:
    try:
        record_output(
            kind="wiki_budget_breach",
            job_name=_JOB_NAME,
            model="none",
            project_id=None,
            content_md=f"Budget ceiling hit: spent ${spent:.4f} >= ceiling ${ceiling:.4f}",
            tokens_in=0,
            tokens_out=0,
            provenance={"spent_usd": spent, "ceiling_usd": ceiling},
        )
    except Exception as exc:
        print(f"[{_JOB_NAME}] ledger budget-breach write failed: {exc}")


# ---------------------------------------------------------------------------
# Per-topic processor
# ---------------------------------------------------------------------------

def _process_topic(
    slug: str,
    topic_path: str,
    client: Anthropic,
    system_prompt: str,
    dry_run: bool,
    verbose: bool = False,
) -> tuple[bool, float, str]:
    """Process one topic. Returns (success, cost_usd, reason).

    Writes to .proposed (dry-run) or in-place (live).
    """
    if verbose:
        print(f"[{_JOB_NAME}] processing {slug}")

    # Read the file
    try:
        with open(topic_path, encoding="utf-8") as fh:
            original = fh.read()
    except Exception as exc:
        _log_warning(slug, f"read_error: {exc}", dry_run)
        return False, 0.0, f"read_error: {exc}"

    # Check editor_frozen in frontmatter
    fm, _ = _parse_frontmatter(original)
    if fm.get("editor_frozen", "").lower() == "true":
        return False, 0.0, "editor_frozen"

    # Extract auto region
    auto_body = _extract_auto_region(original)
    if not auto_body:
        _log_warning(slug, "no_auto_region", dry_run)
        return False, 0.0, "no_auto_region"

    # Extract researcher lead
    researcher_lead = _extract_researcher_lead(auto_body)
    if not researcher_lead:
        _log_warning(slug, "no_current_understanding_section", dry_run)
        return False, 0.0, "no_current_understanding_section"

    # Call Haiku for student lead
    try:
        student_lead, usage = _call_haiku(client, system_prompt, researcher_lead, verbose)
    except Exception as exc:
        _log_warning(slug, f"haiku_api_error: {exc}", dry_run)
        return False, 0.0, f"haiku_api_error: {exc}"

    # Record Haiku call cost
    call_cost = _compute_cost(_HAIKU_MODEL, usage)
    try:
        record_call(job_name=_JOB_NAME, model=_HAIKU_MODEL, usage=usage)
    except Exception as exc:
        print(f"[{_JOB_NAME}] cost_tracking write failed: {exc}")

    if not student_lead.strip():
        _log_warning(slug, "empty_student_lead", dry_run)
        return False, call_cost, "empty_student_lead"

    # Digit-substring validation
    digit_ok, digit_reason = _digit_substring_ok(researcher_lead, student_lead)
    if not digit_ok:
        _log_warning(slug, f"digit_substring_fail: {digit_reason}", dry_run)
        return False, call_cost, f"digit_fail: {digit_reason}"

    # Critic pass (sampled deterministically)
    critic_score: Optional[float] = None
    if _should_run_critic(slug):
        combined = f"RESEARCHER LEAD:\n{researcher_lead}\n\nSTUDENT LEAD:\n{student_lead}"
        try:
            critic_result = critic_pass(combined, rubric_name="wiki_edit")
            critic_score = float(critic_result.get("score", 0))
            if verbose:
                print(f"  [critic] score={critic_score}")
            if critic_score < 4:
                reason = f"critic_score_too_low ({critic_score})"
                _log_warning(slug, reason, dry_run)
                return False, call_cost, reason
        except Exception as exc:
            print(f"[{_JOB_NAME}] critic_pass failed for {slug}: {exc}")

    # Compose lead block
    lead_block = _compose_lead_block(researcher_lead, student_lead)
    new_content = _splice_lead_into_page(original, lead_block)

    # Render hash for provenance
    render_hash = hashlib.sha256(lead_block.encode()).hexdigest()[:16]

    # Write output
    if dry_run:
        os.makedirs(_PROPOSALS_DIR, exist_ok=True)
        proposal_path = os.path.join(_PROPOSALS_DIR, f"{slug}.md.proposed")
        try:
            with open(proposal_path, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            if verbose:
                print(f"  [dry-run] wrote proposal to {proposal_path}")
        except Exception as exc:
            _log_warning(slug, f"proposal_write_error: {exc}", dry_run)
            return False, call_cost, f"proposal_write_error: {exc}"
    else:
        try:
            tmp = topic_path + ".sc_tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            os.replace(tmp, topic_path)
            if verbose:
                print(f"  [live] wrote {topic_path}")
        except Exception as exc:
            _log_warning(slug, f"write_error: {exc}", dry_run)
            return False, call_cost, f"write_error: {exc}"

    # Update cache
    student_hash = hashlib.sha256(student_lead.encode()).hexdigest()[:16]
    researcher_hash = hashlib.sha256(researcher_lead.encode()).hexdigest()[:16]
    try:
        _upsert_cache(slug, student_hash, researcher_hash, critic_score, call_cost)
    except Exception as exc:
        print(f"[{_JOB_NAME}] cache upsert failed for {slug}: {exc}")

    # Log successful edit
    _log_edit(slug, digit_ok, critic_score, call_cost, dry_run, render_hash, usage, topic_path)

    return True, call_cost, "ok"


# ---------------------------------------------------------------------------
# Topic enumeration and change-detection
# ---------------------------------------------------------------------------

def _candidate_topics(force_slug: Optional[str] = None) -> list[tuple[str, str]]:
    """Return [(slug, path), ...] sorted by staleness (oldest cache first).

    If force_slug is given, return only that slug (bypasses change-detection).
    """
    if not os.path.isdir(_TOPICS_DIR):
        return []

    slugs_paths = [
        (fname[:-3], os.path.join(_TOPICS_DIR, fname))
        for fname in sorted(os.listdir(_TOPICS_DIR))
        if fname.endswith(".md") and fname != "index.md"
    ]

    if force_slug:
        filtered = [(s, p) for s, p in slugs_paths if s == force_slug]
        return filtered

    # Filter to those needing a render, sort by oldest cache row
    def _staleness_key(sp: tuple[str, str]) -> float:
        slug, path = sp
        if not _needs_render(slug, path):
            return -1.0  # will be excluded
        row = _get_cache_row(slug)
        if not row or not row.get("last_rendered"):
            return float("inf")
        try:
            dt = datetime.fromisoformat(
                row["last_rendered"].replace("Z", "+00:00")
            )
            return -dt.timestamp()  # more negative = more recent (we want oldest first)
        except Exception:
            return float("inf")

    pending = [(s, p) for s, p in slugs_paths if _needs_render(s, p)]
    pending.sort(key=lambda sp: _staleness_key(sp), reverse=True)
    return pending


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job(
    dry_run_override: Optional[bool] = None,
    force_slug: Optional[str] = None,
    verbose: bool = False,
) -> str:
    """Nightly lead-composition pass. Cron target: 3:00 am Central.

    Args:
        dry_run_override: if not None, overrides tealc_config.json dry_run.
        force_slug: if set, process only this topic slug (bypasses change-detection).
        verbose: print progress to stdout.
    """
    # Config guard
    try:
        if not should_run_this_cycle(_JOB_NAME):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # Working-hours guard — skip 8am–10pm Central; bypass with FORCE_RUN=1
    if not os.environ.get("FORCE_RUN"):
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            hour = datetime.now(ZoneInfo("America/Chicago")).hour
        except Exception:
            import time as _time
            hour = _time.localtime().tm_hour
        if 8 <= hour < 22:
            return f"skipped: working-hours guard (hour={hour}; set FORCE_RUN=1 to bypass)"

    dry_run = _is_dry_run(dry_run_override)
    ceiling = _budget_ceiling()
    max_topics = _max_topics() if not force_slug else 1

    if verbose:
        print(f"[{_JOB_NAME}] dry_run={dry_run} ceiling=${ceiling:.2f} max={max_topics}")

    # Ensure the topic_lead_cache table exists
    try:
        _ensure_table()
    except Exception as exc:
        return f"db_setup_failed: {exc}"

    # Load system prompt once; Haiku caches it via cache_control across calls
    try:
        system_prompt = _load_system_prompt()
    except Exception as exc:
        return f"prompt_load_failed: {exc}"

    client = Anthropic()

    candidates = _candidate_topics(force_slug)
    if not candidates:
        return "no_topics_need_render"

    processed = 0
    succeeded = 0
    total_cost = 0.0
    results: list[str] = []

    for slug, path in candidates[:max_topics]:
        # Budget check before each topic
        if total_cost >= ceiling:
            _log_budget_breach(total_cost, ceiling)
            results.append(f"{slug}: budget_breach_abort")
            break

        ok, cost, reason = _process_topic(
            slug, path, client, system_prompt, dry_run, verbose
        )
        total_cost += cost
        processed += 1
        if ok:
            succeeded += 1
        tag = "ok" if ok else f"skip:{reason}"
        results.append(f"{slug}:{tag}")

        if verbose:
            print(f"  [{slug}] {tag} cost=${cost:.6f}")

    summary = (
        f"surface_composer: processed={processed} succeeded={succeeded} "
        f"cost=${total_cost:.4f} dry_run={dry_run} "
        f"topics=[{' '.join(results)}]"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="surface_composer — dual-register lead composer for wiki topic pages"
    )
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=None,
        help="Force dry-run mode (writes .proposed files, does not modify wiki).",
    )
    mode_group.add_argument(
        "--live", dest="live", action="store_true", default=False,
        help="Force live mode (writes wiki files in place). Overrides config.",
    )
    p.add_argument(
        "--topic", metavar="SLUG",
        help="Process only this topic slug; bypasses change-detection.",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Print per-step progress to stdout.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.live:
        dry_run_override: Optional[bool] = False
    elif args.dry_run:
        dry_run_override = True
    else:
        dry_run_override = None  # respect config

    # Set FORCE_RUN so the working-hours guard doesn't block CLI runs
    os.environ.setdefault("FORCE_RUN", "1")

    result = job(
        dry_run_override=dry_run_override,
        force_slug=args.topic or None,
        verbose=args.verbose,
    )
    print(result)
    sys.exit(0)
