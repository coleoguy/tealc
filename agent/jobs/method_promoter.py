"""method_promoter.py — expands /knowledge/methods/ from a curated seed list.

Scheduled target: Wednesday 3:00 am Central (registered in agent/scheduler.py).

Flow per run:
  1. Load data/known_methods.json.  Each entry: {slug, title, language,
     package, primary_doi, difficulty}.
  2. Scan knowledge/methods/ for slugs already present; candidates = delta.
  3. For each candidate (up to max_methods_per_run):
       a. Call Sonnet 4.6 with topic_page_writer.md as system + a method-page
          instruction suffix in the user message.  Receive markdown body.
       b. If the validator fails on the resulting page, retry once with Opus.
       c. Render the full page (frontmatter + body) and either:
            - dry_run: write to data/method_proposals/<slug>.md
            - live:    write to knowledge/methods/<slug>.md and rewrite
                       knowledge/methods/index.md to add the row.
  4. All model calls logged to cost_tracking + output_ledger.  Budget ceiling
     enforced between methods.  Kill switch: jobs.method_promoter.enabled=false.

Manual invocation:
    python -m agent.jobs.method_promoter [--dry-run|--live] [--slug SLUG] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call, _compute_cost  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_NAME = "method_promoter"
_SONNET_MODEL = "claude-sonnet-4-6"
_OPUS_MODEL = "claude-opus-4-7"

_WIKI_ROOT = os.environ.get(
    "TEALC_WEBSITE_REPO",
    os.path.expanduser("~/Desktop/GitHub/lab-pages"),
)
_KNOWLEDGE_ROOT = os.path.join(_WIKI_ROOT, "knowledge")
_METHODS_DIR = os.path.join(_KNOWLEDGE_ROOT, "methods")
_METHODS_INDEX = os.path.join(_METHODS_DIR, "index.md")
_VALIDATE_SURFACE = os.path.join(_WIKI_ROOT, "wiki_tools", "validate_surface.py")

_PROPOSALS_DIR = os.path.join(_PROJECT_ROOT, "data", "method_proposals")
_SEED_PATH = os.path.join(_PROJECT_ROOT, "data", "known_methods.json")
_PROMPTS_DIR = os.path.join(_PROJECT_ROOT, "agent", "prompts")

# Method-page content region markers matched by wiki_tools/validate_surface.py
_METHOD_START = "<!-- tealc:method-start -->"
_METHOD_END = "<!-- tealc:method-end -->"
_EXAMPLE_START = "<!-- tealc:example-start -->"
_EXAMPLE_END = "<!-- tealc:example-end -->"
_GOTCHAS_START = "<!-- tealc:gotchas-start -->"
_GOTCHAS_END = "<!-- tealc:gotchas-end -->"
_PAPERS_START = "<!-- tealc:papers-start -->"
_PAPERS_END = "<!-- tealc:papers-end -->"

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


def _max_methods() -> int:
    return int(_load_job_config().get("max_methods_per_run", 3))


def _max_cost_usd() -> float:
    return float(_load_job_config().get("max_cost_usd_per_run", 0.50))


# ---------------------------------------------------------------------------
# Seed + existing-method helpers
# ---------------------------------------------------------------------------

def _load_seed_methods() -> list[dict]:
    try:
        with open(_SEED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    return [m for m in data.get("methods", []) if isinstance(m, dict) and m.get("slug")]


def _existing_method_slugs() -> set[str]:
    if not os.path.isdir(_METHODS_DIR):
        return set()
    return {f[:-3] for f in os.listdir(_METHODS_DIR)
            if f.endswith(".md") and f != "index.md"}


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _load_topic_writer_prompt() -> str:
    with open(os.path.join(_PROMPTS_DIR, "topic_page_writer.md"), encoding="utf-8") as fh:
        return fh.read()


_METHOD_INSTRUCTION_SUFFIX = """\
OVERRIDE: you are writing a METHOD REFERENCE PAGE, not a topic page.  The
topic-page system prompt describes the default mode; for this call, produce a
method page body following the structure below.  The output wrapper format
(<<<BODY_MD_BEGIN>>> / <<<BODY_MD_END>>>) still applies; the EDIT_NOTE_JSON
header also still applies.

METHOD PAGE STRUCTURE

# {title}

<!-- tealc:method-start -->
**What it does.** Two to four sentences: what the method computes, what
dataset shape it expects (inputs), and what the parameters are. Name the
formal model briefly (e.g. "likelihood-based", "Bayesian with birth-death
prior", "state-space EM"). Mention the canonical implementation.

**When to use it.** 3–5 bullets: concrete data situations where this method
is the right choice. Include a quantitative guideline if the literature has
one (e.g. "≥300 tips to keep Type I error manageable").

**When NOT to use it.** 3–5 bullets: common failure modes or incompatible
data situations.  Be concrete — name the alternatives a reader should pick
up instead (e.g. "use HiSSE if your tree has unrelated background rate
heterogeneity").
<!-- tealc:method-end -->

<!-- tealc:example-start -->
## Worked example

```<language>
# 10–30 line minimal reproducible snippet.  Comments explain each step.
# Inputs named consistently.  Output interpretation shown at the end.
# Use placeholders for file paths; never reference private lab paths.
```
<!-- tealc:example-end -->

<!-- tealc:gotchas-start -->
## Gotchas we've hit

3–5 bullets naming specific failure modes the lab (or its cited literature)
has actually hit.  Each gotcha must be actionable — say what breaks and what
to do instead.  If you don't have a specific gotcha, omit the bullet; do not
pad with generic warnings.
<!-- tealc:gotchas-end -->

<!-- tealc:papers-start -->
## Key papers that use this method in the lab

Bullet list of 2–5 lab papers or canonical methodology papers that a reader
should know before running this method.  Each bullet links to the paper page
at /knowledge/papers/<doi_slug>/ when available, followed by a one-sentence
note on what that paper contributes.  If no lab papers have been ingested for
this method yet, write "*No lab papers ingested yet — this page will update
as papers are added to /knowledge/papers/.*" and nothing else.
<!-- tealc:papers-end -->

RULES
- Do NOT fabricate DOIs, paper titles, or findings.  If a paper is not
  already ingested into /knowledge/papers/, do not invent a link.
- Do NOT include numeric claims that a reader cannot verify against a named
  source.  If a Type I error rate is quoted, cite the paper.
- Keep the whole page under ~450 words of prose (the code block does not
  count toward that limit).
- All FOUR region-marker pairs above must be present exactly once each.
- The `edit_note_json` first-line wrapper still applies.  Fill it with
  what_changed="Created method reference page for {title}.",
  why_changed="New entry from known_methods.json seed.",
  evidence_quote="", counter_argument="".
"""


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------

def _extract_body(raw: str) -> Optional[str]:
    """Pull the block between <<<BODY_MD_BEGIN>>> and <<<BODY_MD_END>>>."""
    if not raw:
        return None
    start_m = raw.find("<<<BODY_MD_BEGIN>>>")
    end_m = raw.find("<<<BODY_MD_END>>>")
    if start_m < 0 or end_m < 0:
        return None
    return raw[start_m + len("<<<BODY_MD_BEGIN>>>"):end_m].strip("\n")


def _call_writer(client: Anthropic, system_prompt: str, method: dict,
                 model: str, verbose: bool = False) -> tuple[Optional[str], dict]:
    suffix = _METHOD_INSTRUCTION_SUFFIX.format(title=method["title"])
    user = (
        f"Method to write: {method['title']}\n"
        f"Slug: {method['slug']}\n"
        f"Language: {method.get('language','')}\n"
        f"Package: {method.get('package','')}\n"
        f"Primary DOI (for reader reference, not mandatory to cite inline): "
        f"{method.get('primary_doi','')}\n"
        f"Difficulty: {method.get('difficulty','intermediate')}\n\n"
        f"{suffix}"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=2500,
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
    body = _extract_body(raw)
    if verbose:
        print(f"  [{model}] in={usage['input_tokens']} out={usage['output_tokens']} "
              f"body={'yes' if body else 'no'}")
    return body, usage


# ---------------------------------------------------------------------------
# Page rendering + region validation
# ---------------------------------------------------------------------------

def _has_all_regions(body: str) -> tuple[bool, str]:
    missing = []
    for marker in (_METHOD_START, _METHOD_END, _EXAMPLE_START, _EXAMPLE_END,
                   _GOTCHAS_START, _GOTCHAS_END, _PAPERS_START, _PAPERS_END):
        if marker not in body:
            missing.append(marker)
    if missing:
        return False, f"missing_regions: {missing}"
    return True, "ok"


def _extract_depends_on_concepts(body: str) -> list[str]:
    """Scan the generated body for /knowledge/concepts/<slug>/ references."""
    slugs = re.findall(r"/knowledge/concepts/([a-z0-9-]+)/", body)
    # Dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _render_page_md(method: dict, body: str) -> str:
    slug = method["slug"]
    title = method["title"]
    language = method.get("language", "")
    package = method.get("package", "")
    difficulty = method.get("difficulty", "intermediate")
    today_iso = date.today().isoformat()

    depends = _extract_depends_on_concepts(body)
    depends_yaml = "[" + ", ".join(depends) + "]"

    frontmatter = f"""---
layout: default
title: "{title}"
method_slug: {slug}
language: {language}
package: {package}
depends_on_concepts: {depends_yaml}
appears_in_topics: []
appears_in_papers: []
difficulty: {difficulty}
last_updated: {today_iso}
permalink: /knowledge/methods/{slug}/
---
"""
    tail = "\n\n<!-- user-start -->\n<!-- user-end -->\n"
    return frontmatter + body.strip() + tail


# ---------------------------------------------------------------------------
# Validator + index update
# ---------------------------------------------------------------------------

def _validate_file(path: str) -> tuple[bool, str]:
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


def _append_to_methods_index(method: dict) -> None:
    """Add a row to the methods index table.  Idempotent — no-op if slug already
    present in the table."""
    if not os.path.isfile(_METHODS_INDEX):
        return
    try:
        with open(_METHODS_INDEX, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return
    link = f"/knowledge/methods/{method['slug']}/"
    if link in text:
        return  # already indexed
    new_row = (
        f"| [{method['title'].split('—')[0].strip()}]({link}) | "
        f"{method.get('language','')} | {method.get('package','')} | "
        f"{method.get('difficulty','')} |"
    )
    # Insert the row at the end of the main table (before the "## Filter by
    # difficulty" heading, or before the first blank line after the table).
    filter_idx = text.find("\n## Filter by difficulty")
    if filter_idx < 0:
        # Append to file tail
        new_text = text.rstrip() + "\n" + new_row + "\n"
    else:
        # Walk back from filter_idx to find the last table line
        head = text[:filter_idx].rstrip()
        tail = text[filter_idx:]
        new_text = head + "\n" + new_row + "\n" + tail
    try:
        with open(_METHODS_INDEX, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Ledger helper
# ---------------------------------------------------------------------------

def _log(kind: str, slug: str, content: str, usage: dict, extra: dict,
         model: str = _SONNET_MODEL) -> None:
    try:
        record_output(
            kind=kind, job_name=_JOB_NAME, model=model,
            project_id=None, content_md=f"[{slug}] {content}",
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            provenance={"slug": slug, **extra},
        )
    except Exception as exc:
        print(f"[{_JOB_NAME}] ledger write failed for {slug}: {exc}")


# ---------------------------------------------------------------------------
# Per-method processor
# ---------------------------------------------------------------------------

def _process_method(method: dict, client: Anthropic, system_prompt: str,
                    dry_run: bool, verbose: bool = False) -> tuple[bool, float, str]:
    slug = method["slug"]
    if verbose:
        print(f"[{_JOB_NAME}] method={slug}")

    total_cost = 0.0

    # First try Sonnet
    try:
        body, s_usage = _call_writer(client, system_prompt, method, _SONNET_MODEL, verbose)
    except Exception as exc:
        _log("method_page_warning", slug, f"sonnet_api_error: {exc}", {}, {})
        return False, 0.0, f"sonnet_api_error: {exc}"
    s_cost = _compute_cost(_SONNET_MODEL, s_usage)
    total_cost += s_cost
    try:
        record_call(job_name=_JOB_NAME, model=_SONNET_MODEL, usage=s_usage)
    except Exception:
        pass

    chosen_body: Optional[str] = None
    chosen_usage = s_usage
    chosen_model = _SONNET_MODEL

    if body:
        ok, reason = _has_all_regions(body)
        if ok:
            chosen_body = body
        elif verbose:
            print(f"  [sonnet] region-check failed: {reason}; retrying with Opus")

    # Opus retry if Sonnet produced unusable output
    if chosen_body is None:
        try:
            body_o, o_usage = _call_writer(client, system_prompt, method, _OPUS_MODEL, verbose)
        except Exception as exc:
            _log("method_page_warning", slug, f"opus_api_error: {exc}",
                 s_usage, {"sonnet_body_sample": (body or "")[:300]})
            return False, total_cost, f"opus_api_error: {exc}"
        o_cost = _compute_cost(_OPUS_MODEL, o_usage)
        total_cost += o_cost
        try:
            record_call(job_name=_JOB_NAME, model=_OPUS_MODEL, usage=o_usage)
        except Exception:
            pass
        if not body_o:
            return False, total_cost, "both_models_empty"
        ok2, reason2 = _has_all_regions(body_o)
        if not ok2:
            _log("method_page_warning", slug, f"opus_region_fail: {reason2}",
                 o_usage, {})
            return False, total_cost, f"opus_region_fail: {reason2}"
        chosen_body = body_o
        chosen_usage = o_usage
        chosen_model = _OPUS_MODEL

    page_md = _render_page_md(method, chosen_body)

    if dry_run:
        os.makedirs(_PROPOSALS_DIR, exist_ok=True)
        target = os.path.join(_PROPOSALS_DIR, f"{slug}.md")
    else:
        os.makedirs(_METHODS_DIR, exist_ok=True)
        target = os.path.join(_METHODS_DIR, f"{slug}.md")

    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(page_md)
    except Exception as exc:
        return False, total_cost, f"write_error: {exc}"

    if not dry_run:
        ok, reason = _validate_file(target)
        if not ok:
            _log("method_page_warning", slug, f"validator_fail: {reason}",
                 chosen_usage, {"target": target, "model": chosen_model})
            return False, total_cost, f"validator_fail: {reason[:200]}"
        _append_to_methods_index(method)

    _log("method_page_created", slug,
         f"created via {chosen_model} cost=${total_cost:.4f}",
         chosen_usage,
         {"target": target, "dry_run": dry_run, "model": chosen_model,
          "cost_usd": total_cost},
         model=chosen_model)
    return True, total_cost, "ok"


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job(
    dry_run_override: Optional[bool] = None,
    force_slug: Optional[str] = None,
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
    cap_n = _max_methods()
    cap_cost = _max_cost_usd()

    try:
        system_prompt = _load_topic_writer_prompt()
    except Exception as exc:
        return f"prompt_load_failed: {exc}"

    seed = _load_seed_methods()
    if not seed:
        return "empty_seed_list"

    existing = _existing_method_slugs()
    candidates = [m for m in seed if m["slug"] not in existing]
    if force_slug:
        candidates = [m for m in seed if m["slug"] == force_slug]

    if not candidates:
        return "no_new_methods"

    client = Anthropic()

    processed = 0
    wrote = 0
    total_cost = 0.0
    results: list[str] = []

    for method in candidates:
        if wrote >= cap_n:
            break
        if total_cost >= cap_cost:
            results.append(f"{method['slug']}:budget_breach_abort")
            break

        ok, cost, reason = _process_method(method, client, system_prompt, dry_run, verbose)
        processed += 1
        total_cost += cost
        if ok:
            wrote += 1
        results.append(f"{method['slug']}:{'ok' if ok else 'skip:'+reason}")
        if verbose:
            print(f"  [{method['slug']}] {reason} cost=${cost:.4f}")

    summary = (
        f"method_promoter: processed={processed} wrote={wrote} "
        f"cost=${total_cost:.4f} dry_run={dry_run} "
        f"methods=[{' '.join(results)}]"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="method_promoter CLI")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=None)
    g.add_argument("--live",    action="store_true", default=False)
    p.add_argument("--slug",    metavar="SLUG",
                   help="Process only this method slug.")
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
    print(job(dry_run_override=override, force_slug=args.slug, verbose=args.verbose))
    sys.exit(0)
