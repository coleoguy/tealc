"""gloss_harvester.py — mines new concept-card candidates from paper_findings.

Scheduled target: Tuesday 3:00 am Central (registered in agent/scheduler.py).

Flow per run:
  1. Load seed lexicon from data/wiki_jargon_seed.json.
  2. Load existing concept slugs + aliases from knowledge/data/concepts.json.
  3. For each seed term NOT already covered by an existing card, query
     paper_findings.quote for verbatim matches (case-insensitive).  Require
     ≥ 3 matches (tunable via jobs.gloss_harvester.min_findings_per_term).
  4. For each qualifying term, call Haiku with concept_card_writer.md and the
     3–5 best supporting finding quotes; receive strict JSON.
  5. Call Sonnet verifier with the 4-dimensional rubric adapted from
     finding_verifier.md.  Accept, reject, or revise.
  6. For accepted cards, render a markdown file matching the existing concept-
     card layout (see knowledge/concepts/achiasmy.md) and either:
       - dry_run: write to data/concept_proposals/<slug>.md
       - live:    write to knowledge/concepts/<slug>.md and invoke the
                  knowledge/data/build_concepts_json.py rebuilder.
  7. Every write is validated via wiki_tools/validate_surface.py before commit;
     failures are logged and skipped.
  8. All model calls recorded to cost_tracking + output_ledger.  Budget ceiling
     enforced between terms.  Kill switch: jobs.gloss_harvester.enabled=false.

Manual invocation:
    python -m agent.jobs.gloss_harvester [--dry-run|--live] [--term TERM] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_NAME = "gloss_harvester"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_SONNET_MODEL = "claude-sonnet-4-6"

_WIKI_ROOT = os.path.expanduser("~/Desktop/GitHub/coleoguy.github.io")
_KNOWLEDGE_ROOT = os.path.join(_WIKI_ROOT, "knowledge")
_CONCEPTS_DIR = os.path.join(_KNOWLEDGE_ROOT, "concepts")
_CONCEPTS_JSON = os.path.join(_KNOWLEDGE_ROOT, "data", "concepts.json")
_BUILD_CONCEPTS_JSON = os.path.join(_KNOWLEDGE_ROOT, "data", "build_concepts_json.py")
_VALIDATE_SURFACE = os.path.join(_WIKI_ROOT, "wiki_tools", "validate_surface.py")

_TOPICS_DIR = os.path.join(_KNOWLEDGE_ROOT, "topics")

_PROPOSALS_DIR = os.path.join(_PROJECT_ROOT, "data", "concept_proposals")
_SEED_PATH = os.path.join(_PROJECT_ROOT, "data", "wiki_jargon_seed.json")
_PROMPTS_DIR = os.path.join(_PROJECT_ROOT, "agent", "prompts")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_job_config() -> dict:
    cfg_path = os.path.join(_PROJECT_ROOT, "data", "tealc_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    return cfg.get("jobs", {}).get(_JOB_NAME, {})


def _is_enabled() -> bool:
    return bool(_load_job_config().get("enabled", True))


def _is_dry_run(override: Optional[bool] = None) -> bool:
    if override is not None:
        return override
    return bool(_load_job_config().get("dry_run", True))


def _max_concepts() -> int:
    return int(_load_job_config().get("max_concepts_per_run", 10))


def _max_cost_usd() -> float:
    return float(_load_job_config().get("max_cost_usd_per_run", 0.75))


def _min_findings() -> int:
    return int(_load_job_config().get("min_findings_per_term", 3))


def _critic_sample_ratio() -> int:
    return int(_load_job_config().get("critic_sample_ratio", 8))


# ---------------------------------------------------------------------------
# Seed + existing-concepts helpers
# ---------------------------------------------------------------------------

def _load_seed_terms() -> list[str]:
    try:
        with open(_SEED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    terms = data.get("terms", [])
    return [t.strip() for t in terms if isinstance(t, str) and t.strip()]


def _load_existing_concepts() -> tuple[set[str], set[str]]:
    """Return (slugs, lowercased aliases) already covered."""
    slugs: set[str] = set()
    aliases: set[str] = set()
    try:
        with open(_CONCEPTS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data.get("concepts", []):
            slug = (entry.get("slug") or "").strip()
            if slug:
                slugs.add(slug)
                aliases.add(slug.replace("-", " ").lower())
            for a in entry.get("aliases", []) or []:
                if isinstance(a, str):
                    aliases.add(a.strip().lower())
    except Exception:
        pass
    return slugs, aliases


def _term_to_slug(term: str) -> str:
    """Convert 'Haldane's rule' → 'haldanes-rule' for use as filename slug."""
    s = term.strip().lower()
    s = re.sub(r"[^\w\s-]+", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s


# ---------------------------------------------------------------------------
# Finding lookup
# ---------------------------------------------------------------------------

def _find_supporting_findings(term: str, limit: int = 5) -> list[dict]:
    """Return up to `limit` paper_findings rows whose quote contains the term
    (case-insensitive).  Prefer matches that appear as whole phrases."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    term_lower = term.lower()
    # Broad match first; rank client-side
    rows = conn.execute(
        """SELECT id, doi, finding_idx, finding_text, quote, page, reasoning
           FROM paper_findings
           WHERE LOWER(quote) LIKE ?
           ORDER BY id DESC
           LIMIT 50""",
        (f"%{term_lower}%",),
    ).fetchall()
    conn.close()

    # Rank: exact-word match beats substring; shorter quotes (more focused) next
    def _rank(row: sqlite3.Row) -> tuple[int, int]:
        q = (row["quote"] or "").lower()
        whole_word = 0 if re.search(rf"\b{re.escape(term_lower)}\b", q) else 1
        return (whole_word, len(q))

    ranked = sorted(rows, key=_rank)
    return [dict(r) for r in ranked[:limit]]


def _doi_to_slug(doi: str) -> str:
    """Convert '10.1093/g3journal/jkaf217' → '10_1093_g3journal_jkaf217'."""
    return re.sub(r"[./]", "_", (doi or "").strip())


# ---------------------------------------------------------------------------
# Prompt loaders
# ---------------------------------------------------------------------------

def _load_writer_prompt() -> str:
    with open(os.path.join(_PROMPTS_DIR, "concept_card_writer.md"), encoding="utf-8") as fh:
        return fh.read()


_VERIFIER_SYSTEM = """\
You are an adversarial verifier of a concept-card proposal for the Blackmon
Lab wiki.  Your job is the subjective check that cannot be automated: does
this card belong on the wiki?

Score on four dimensions (same structure as the lab's finding verifier):

1. DEFINITION-QUOTE FIT. Does the supplied primary_finding quote actually
   ground the one-sentence definition?  If the definition claims something
   the quote does not license, fail this dimension.

2. NUMERIC ACCURACY. Every numeric claim in why_matters must appear verbatim
   (up to whitespace) in one of the supplied quotes.  Rounded, re-expressed,
   or invented numbers fail.

3. VOICE / HYPE. The card must read plainly.  Flag cards that use hype
   ("revolutionary", "paradigm-shifting"), consulting-deck vocabulary
   ("robust", "leveraged", "emerging", "scaffolding", "growing toolkit",
   "opening avenues"), or hedges the source does not hedge.

4. SCOPE. why_matters must describe what the concept DOES in the literature
   (where it appears, what it predicts), not generic background. Generic
   backgrounders that could fit any concept fail.

Emit ONE of:
- {"action": "accept", "reason": "short note, usually empty string"}
- {"action": "revise", "proposed_edits": {"definition": null or str,
    "analogy": null or str, "why_matters": null or str}, "reason": "..."}
- {"action": "reject", "reason": "which dimension failed and how"}

JSON only.  No markdown fences.  No preamble.
"""

# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

def _call_writer(client: Anthropic, system_prompt: str, term: str,
                 findings: list[dict], appears_in_topics: list[str],
                 verbose: bool = False) -> tuple[Optional[dict], dict]:
    """Return (parsed_card_json_or_None, usage_dict)."""
    snippets = []
    for f in findings:
        slug = _doi_to_slug(f.get("doi", ""))
        snippets.append({
            "doi": f.get("doi"),
            "doi_slug": slug,
            "finding_idx": f.get("finding_idx"),
            "quote": f.get("quote"),
            "page": f.get("page"),
            "reasoning": f.get("reasoning"),
        })
    user = json.dumps({
        "term": term,
        "supporting_findings": snippets,
        "appears_in_topics": appears_in_topics,
    }, ensure_ascii=False, indent=2)

    msg = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=600,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    usage = {
        "input_tokens":                getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens":               getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens":     getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    raw = msg.content[0].text if msg.content else ""
    parsed = _extract_json(raw)
    if verbose:
        print(f"  [writer] in={usage['input_tokens']} out={usage['output_tokens']} "
              f"cache_read={usage['cache_read_input_tokens']}")
    return parsed, usage


def _call_verifier(client: Anthropic, card: dict, findings: list[dict],
                   verbose: bool = False) -> tuple[dict, dict]:
    payload = {
        "candidate_card": card,
        "supporting_findings": [
            {"doi": f.get("doi"), "finding_idx": f.get("finding_idx"),
             "quote": f.get("quote"), "page": f.get("page")}
            for f in findings
        ],
    }
    msg = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=400,
        system=_VERIFIER_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    usage = {
        "input_tokens":                getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens":               getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens":     getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    raw = msg.content[0].text if msg.content else ""
    parsed = _extract_json(raw) or {"action": "reject", "reason": "verifier_parse_failed"}
    if verbose:
        print(f"  [verifier] action={parsed.get('action')} in={usage['input_tokens']} "
              f"out={usage['output_tokens']}")
    return parsed, usage


def _extract_json(raw: str) -> Optional[dict]:
    """Lenient JSON extractor — strips fences and leading prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    brace = text.find("{")
    if brace < 0:
        return None
    depth = 0
    end = None
    in_str = False
    esc = False
    for i, ch in enumerate(text[brace:], start=brace):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(text[brace:end])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Numeric-parity sanity check (lightweight; verifier is the real guard)
# ---------------------------------------------------------------------------

_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")


def _numeric_parity_ok(card: dict, findings: list[dict]) -> tuple[bool, str]:
    """Every digit in why_matters must appear in at least one supplied quote."""
    card_text = f"{card.get('why_matters','')} {card.get('definition','')}"
    digits = set(_DIGIT_RE.findall(card_text.replace(",", "")))
    if not digits:
        return True, "no_numerics"
    all_quotes = " ".join(
        (f.get("quote") or "").replace(",", "") for f in findings
    )
    missing = [d for d in sorted(digits) if d not in all_quotes]
    if missing:
        return False, f"numerics not in quotes: {missing[:8]}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------

def _topics_containing_term(term: str, limit: int = 3) -> list[str]:
    """Return up to `limit` topic slugs whose body mentions `term`."""
    term_lower = term.lower()
    hits: list[str] = []
    if not os.path.isdir(_TOPICS_DIR):
        return hits
    for fname in sorted(os.listdir(_TOPICS_DIR)):
        if not fname.endswith(".md") or fname == "index.md":
            continue
        path = os.path.join(_TOPICS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        if term_lower in text.lower():
            hits.append(fname[:-3])
            if len(hits) >= limit:
                break
    return hits


def _render_card_md(slug: str, card: dict, findings: list[dict],
                    appears_in_topics: list[str]) -> str:
    title = card["title"]
    aliases = card.get("aliases", []) or []
    definition = card.get("definition", "").strip()
    analogy = card.get("analogy", "").strip()
    why_matters = card.get("why_matters", "").strip()
    primary_id = card.get("primary_finding_id", "")

    # Look up the primary finding quote
    prim_quote = ""
    prim_link = ""
    prim_source_title = ""
    if "#" in primary_id:
        pslug, anchor = primary_id.split("#", 1)
        for f in findings:
            if _doi_to_slug(f.get("doi", "")) == pslug and f.get("finding_idx") is not None:
                if anchor == f"finding-{f['finding_idx']}":
                    prim_quote = (f.get("quote") or "").strip()
                    prim_link = f"/knowledge/papers/{pslug}/#{anchor}"
                    # Render as 'Source slug, Finding N'
                    prim_source_title = f"{pslug}, Finding {f['finding_idx']}"
                    break

    # "Where you meet it in the wiki" bullets
    topic_bullets_lines = []
    for t in appears_in_topics[:4]:
        human = t.replace("_", " ").capitalize()
        topic_bullets_lines.append(f"- [{human}](/knowledge/topics/{t}/)")
    topic_bullets = "\n".join(topic_bullets_lines) if topic_bullets_lines \
        else "- (this concept will be cross-linked as topic pages reference it)"

    aliases_yaml = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    appears_yaml = "[" + ", ".join(appears_in_topics) + "]"

    primary_citation_yaml = f'"{primary_id}"' if primary_id else '""'
    today_iso = date.today().isoformat()

    # Primary citation block
    if prim_quote and prim_link:
        primary_citation_md = (
            f"**Primary citation.**\n"
            f"> \"{prim_quote}\"\n"
            f"— [{prim_source_title}]({prim_link})\n"
        )
    else:
        primary_citation_md = (
            "**Primary citation.**\n"
            "*(primary finding link pending — verifier will flag if missing)*\n"
        )

    md = f"""---
layout: default
title: "{title}"
concept_slug: {slug}
aliases: {aliases_yaml}
prerequisites: []
appears_in_topics: {appears_yaml}
related_concepts: []
primary_citation: {primary_citation_yaml}
difficulty: intermediate
last_updated: {today_iso}
permalink: /knowledge/concepts/{slug}/
---
<!-- tealc:card-start -->
# {title}

**One-sentence definition.** {definition}

**One-sentence analogy.** {analogy}

**Why it matters.** {why_matters}

**Where you meet it in the wiki.**
{topic_bullets}

{primary_citation_md}
<!-- tealc:card-end -->

<!-- user-start -->
<!-- user-end -->
"""
    return md


# ---------------------------------------------------------------------------
# Validator subprocess
# ---------------------------------------------------------------------------

def _validate_file(path: str) -> tuple[bool, str]:
    """Run wiki_tools/validate_surface.py against a single file.  Treat a
    missing validator as a pass (so gloss_harvester still runs on fresh clones
    of the wiki repo)."""
    if not os.path.isfile(_VALIDATE_SURFACE):
        return True, "validator_missing"
    try:
        r = subprocess.run(
            [sys.executable, _VALIDATE_SURFACE, "--file", path],
            cwd=_WIKI_ROOT, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return False, f"validator_exec_error: {exc}"
    if r.returncode == 0:
        return True, "ok"
    return False, f"validator_rc={r.returncode}: {(r.stderr or r.stdout)[:300]}"


def _rebuild_concepts_json() -> tuple[bool, str]:
    if not os.path.isfile(_BUILD_CONCEPTS_JSON):
        return False, "build_concepts_json.py not found"
    try:
        r = subprocess.run(
            [sys.executable, _BUILD_CONCEPTS_JSON],
            cwd=_WIKI_ROOT, capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        return False, f"rebuild_exec_error: {exc}"
    if r.returncode == 0:
        return True, (r.stdout or "").strip()
    return False, f"rebuild_rc={r.returncode}: {(r.stderr or r.stdout)[:300]}"


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _log(kind: str, slug: str, content: str, usage: dict, extra: dict) -> None:
    try:
        record_output(
            kind=kind, job_name=_JOB_NAME, model=_HAIKU_MODEL,
            project_id=None, content_md=f"[{slug}] {content}",
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            provenance={"slug": slug, **extra},
        )
    except Exception as exc:
        print(f"[{_JOB_NAME}] ledger write failed for {slug}: {exc}")


# ---------------------------------------------------------------------------
# Per-term processor
# ---------------------------------------------------------------------------

def _process_term(term: str, client: Anthropic, writer_prompt: str,
                  dry_run: bool, verbose: bool = False) -> tuple[bool, float, str]:
    """Process one candidate term.  Returns (wrote_card, cost_usd, reason)."""
    slug = _term_to_slug(term)
    if verbose:
        print(f"[{_JOB_NAME}] term={term!r} slug={slug}")

    findings = _find_supporting_findings(term, limit=5)
    if len(findings) < _min_findings():
        return False, 0.0, f"insufficient_findings ({len(findings)})"

    appears_in = _topics_containing_term(term, limit=3)

    try:
        card, w_usage = _call_writer(client, writer_prompt, term, findings,
                                     appears_in, verbose)
    except Exception as exc:
        _log("concept_card_warning", slug, f"writer_api_error: {exc}", {}, {})
        return False, 0.0, f"writer_api_error: {exc}"

    w_cost = _compute_cost(_HAIKU_MODEL, w_usage)
    try:
        record_call(job_name=_JOB_NAME, model=_HAIKU_MODEL, usage=w_usage)
    except Exception:
        pass

    if not card:
        return False, w_cost, "writer_parse_failed"
    if card.get("insufficient_evidence"):
        return False, w_cost, "writer_declined_insufficient_evidence"

    # Required-field sanity check
    required = ("title", "aliases", "definition", "analogy", "why_matters",
                "primary_finding_id")
    missing = [k for k in required if not card.get(k)]
    if missing:
        _log("concept_card_warning", slug, f"missing_fields: {missing}",
             w_usage, {"card": card})
        return False, w_cost, f"missing_fields: {missing}"

    # Lightweight deterministic pre-filter
    parity_ok, parity_reason = _numeric_parity_ok(card, findings)
    if not parity_ok:
        _log("concept_card_warning", slug, parity_reason, w_usage, {"card": card})
        return False, w_cost, f"numeric_parity_fail: {parity_reason}"

    # Verifier
    try:
        verdict, v_usage = _call_verifier(client, card, findings, verbose)
    except Exception as exc:
        _log("concept_card_warning", slug, f"verifier_api_error: {exc}",
             w_usage, {"card": card})
        return False, w_cost, f"verifier_api_error: {exc}"
    v_cost = _compute_cost(_SONNET_MODEL, v_usage)
    try:
        record_call(job_name=_JOB_NAME, model=_SONNET_MODEL, usage=v_usage)
    except Exception:
        pass

    total_cost = w_cost + v_cost
    action = (verdict or {}).get("action")
    if action == "reject":
        _log("concept_card_rejected", slug, str(verdict.get("reason", ""))[:200],
             v_usage, {"card": card, "verdict": verdict})
        return False, total_cost, f"verifier_rejected: {verdict.get('reason','?')}"
    if action == "revise":
        edits = (verdict.get("proposed_edits") or {})
        for field in ("definition", "analogy", "why_matters"):
            new_val = edits.get(field)
            if new_val:
                card[field] = new_val
        # Re-check numeric parity after revision
        parity_ok, parity_reason = _numeric_parity_ok(card, findings)
        if not parity_ok:
            _log("concept_card_warning", slug, f"revision_fails_numeric: {parity_reason}",
                 v_usage, {"card": card})
            return False, total_cost, "revision_fails_numeric_parity"

    # Render card markdown
    try:
        card_md = _render_card_md(slug, card, findings, appears_in)
    except Exception as exc:
        return False, total_cost, f"render_error: {exc}"

    # Write to dry-run proposal or live concepts dir
    if dry_run:
        os.makedirs(_PROPOSALS_DIR, exist_ok=True)
        target = os.path.join(_PROPOSALS_DIR, f"{slug}.md")
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(card_md)
        except Exception as exc:
            return False, total_cost, f"proposal_write_error: {exc}"
    else:
        os.makedirs(_CONCEPTS_DIR, exist_ok=True)
        target = os.path.join(_CONCEPTS_DIR, f"{slug}.md")
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(card_md)
        except Exception as exc:
            return False, total_cost, f"concept_write_error: {exc}"

    # Validator (live-only — dry-run output lives outside the wiki tree)
    if not dry_run:
        ok, reason = _validate_file(target)
        if not ok:
            _log("concept_card_warning", slug, f"validator_fail: {reason}",
                 v_usage, {"card": card, "target": target})
            # Leave the file for inspection — don't delete; just flag
            return False, total_cost, f"validator_fail: {reason[:200]}"

    _log("concept_card_created", slug,
         f"accepted ({action}) cost=${total_cost:.4f}",
         w_usage,
         {"card": card, "verdict": verdict, "dry_run": dry_run,
          "target": target, "cost_usd": total_cost})
    return True, total_cost, "ok"


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job(
    dry_run_override: Optional[bool] = None,
    force_term: Optional[str] = None,
    verbose: bool = False,
) -> str:
    if not _is_enabled():
        return "disabled"

    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle(_JOB_NAME):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # Working-hours guard — bypass with FORCE_RUN=1
    if not os.environ.get("FORCE_RUN"):
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            hour = datetime.now(ZoneInfo("America/Chicago")).hour
        except Exception:
            import time as _time
            hour = _time.localtime().tm_hour
        if 8 <= hour < 22:
            return f"skipped: working-hours guard (hour={hour})"

    dry_run = _is_dry_run(dry_run_override)
    cap_n = _max_concepts()
    cap_cost = _max_cost_usd()

    try:
        writer_prompt = _load_writer_prompt()
    except Exception as exc:
        return f"prompt_load_failed: {exc}"

    seed = [force_term] if force_term else _load_seed_terms()
    if not seed:
        return "empty_seed_lexicon"

    existing_slugs, existing_aliases = _load_existing_concepts()

    # Candidate filter: slug not seen AND term not already aliased
    candidates: list[str] = []
    for term in seed:
        slug = _term_to_slug(term)
        if slug in existing_slugs:
            continue
        if term.strip().lower() in existing_aliases:
            continue
        candidates.append(term)

    if not candidates:
        return "no_new_candidates"

    client = Anthropic()

    processed = 0
    wrote = 0
    total_cost = 0.0
    results: list[str] = []

    for term in candidates[: max(cap_n * 3, cap_n)]:
        if wrote >= cap_n:
            break
        if total_cost >= cap_cost:
            results.append(f"{term}:budget_breach_abort")
            break

        ok, cost, reason = _process_term(term, client, writer_prompt, dry_run, verbose)
        processed += 1
        total_cost += cost
        if ok:
            wrote += 1
        results.append(f"{term}:{'ok' if ok else 'skip:'+reason}")
        if verbose:
            print(f"  [{term}] {reason} cost=${cost:.4f}")

    # Rebuild the site-level concepts.json when live
    rebuild_msg = ""
    if wrote and not dry_run:
        ok, msg = _rebuild_concepts_json()
        rebuild_msg = f" rebuild={'ok' if ok else 'fail'}:{msg[:120]}"

    summary = (
        f"gloss_harvester: processed={processed} wrote={wrote} "
        f"cost=${total_cost:.4f} dry_run={dry_run}{rebuild_msg} "
        f"terms=[{' '.join(results[:20])}]"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="gloss_harvester CLI")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=None)
    g.add_argument("--live",    action="store_true", default=False)
    p.add_argument("--term",    metavar="TERM",
                   help="Process only this term; bypasses seed filter.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    override: Optional[bool]
    if args.live:
        override = False
    elif args.dry_run:
        override = True
    else:
        override = None
    os.environ.setdefault("FORCE_RUN", "1")
    print(job(dry_run_override=override, force_term=args.term, verbose=args.verbose))
    sys.exit(0)
