"""Opus wiki-improvement job — runs Sunday 10am Central.

Picks 2 topic pages + 1 paper page (oldest-edited first, skipping pages edited
in the past 14 days or marked `editor_frozen: true`), runs Opus with Heath's
voice exemplars as context, and applies targeted prose improvements IN PLACE if
the change size is within the 40% cap. If the cap is hit, skips the edit and
files a briefing suggesting manual review.

First 2 weeks: dry-run mode default ON. Writes proposed diffs to briefings,
does NOT touch files. Flip `improve_wiki.dry_run` to `false` in
`data/tealc_config.json` to go live.

Run manually (advisor/dry-run respected):
    python -m agent.jobs.improve_wiki

Run manually, force live writes even in dry-run config:
    FORCE_RUN=1 IMPROVE_WIKI_LIVE=1 python -m agent.jobs.improve_wiki

Cost: ~$0.80/page × 3 pages/run × weekly = ~$10/month at full Opus pricing.
"""
import json
import os
import re
import sqlite3
import difflib
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.tools import retrieve_voice_exemplars  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WIKI_ROOT = os.environ.get(
    "WIKI_TOPICS_DIR",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/knowledge"),
)
_TOPICS_DIR = os.path.join(_WIKI_ROOT, "topics")
_PAPERS_DIR = os.path.join(_WIKI_ROOT, "papers")

_SKIP_IF_RECENT_DAYS = 14
_CHANGE_CAP = 0.40          # Hard safety lever — any edit above this is skipped
_TOPICS_PER_RUN = 2
_PAPERS_PER_RUN = 1
_MAX_VOICE_EXEMPLARS = 5
_OPUS_MODEL = "claude-opus-4-7"
_MAX_TOKENS_OUT = 16000     # Big enough for a full-page rewrite
_BUDGET_USD_CEILING = 2.5   # Abort new calls if spend in this run approaches this

# ---------------------------------------------------------------------------
# Editor system prompt
# ---------------------------------------------------------------------------

_EDITOR_SYSTEM = """\
You are a scientific wiki editor for the lab's public wiki at
the lab's GitHub Pages /knowledge/. You improve existing pages (topic syntheses
and paper pages) to read more like the researcher's own writing, stay internally
consistent, and cross-link correctly.

SCOPE OF ALLOWED EDITS:

1. Prose tightening — smooth awkward transitions between paragraphs,
   break overly packed sentences, remove redundancy where the same claim
   appears in two places.

2. Voice matching — revise paragraphs that drift toward generic AI-assistant
   register (phrases like "queryable", "specimen", "scaffolding", "robust",
   "leveraged", excessive hedging). Heath's voice is direct, artifact-pointing,
   quantitative, and avoids consulting-deck vocabulary. Voice exemplars from
   Heath's actual prose are included in the user message — match their
   density, hedging level, and register.

3. Cross-link hygiene — where a paper is mentioned by author-year or DOI but
   not linked to `/knowledge/papers/<DOI_SLUG>/`, add the link. Where a topic
   is mentioned and a matching topic page exists (slug in the user message),
   link to `/knowledge/topics/<slug>/`. Flag broken finding anchors (#finding-N
   pointing at a non-existent anchor) by removing them rather than leaving
   broken references.

4. Contradiction-section enrichment — if supporting-evidence paragraphs
   contain hedging, methodological caveats, sample-size warnings, or
   acknowledged limits, surface them explicitly into the "Contradictions /
   open disagreements" section. NEVER invent contradictions that aren't
   already in the prose.

5. Structural normalization — ensure the standard sections exist
   (Current understanding / Supporting evidence / Contradictions or open
   disagreements / Tealc's citation-neighborhood suggestions). If a section
   is missing, add the header but leave the body empty — DO NOT fabricate
   content.

RULES YOU MUST NOT BREAK:

- Do NOT add new scientific claims, findings, numbers, or citations not
  already present in the page.
- Do NOT change paper titles, DOIs, slugs, or identity-bearing metadata
  (`doi`, `topic_slug`, `permalink`, `tier`, `ingested_at`, `fingerprint_sha256`,
  `papers_supporting`, `category`).
- Do NOT change the meaning of any claim — only its prose.
- Do NOT reorder findings' anchor IDs (`#finding-1`, `#finding-2`, etc.) —
  external pages cite these anchors.
- Do NOT add figures, images, tables, or embedded media. Out of scope.
- Preserve all YAML frontmatter fields not explicitly named for edits.
  The only frontmatter field you may modify is `last_updated` — set it to
  the current ISO timestamp if you applied any edits.

OUTPUT FORMAT:

Return JSON only, no prose preamble, no markdown code fences:

{
  "changes": [
    {"kind": "<short label>", "section": "<section name>", "note": "<one-line description>"},
    ...
  ],
  "updated_page": "<the FULL updated page body, including unchanged frontmatter and unchanged sections>"
}

Valid `kind` values: "tightened_paragraph", "voice_revision", "added_crosslink",
"removed_broken_anchor", "moved_to_contradictions", "structural_section_added",
"redundancy_removed", "unrelated_fix".

If the page is already in good shape and needs no edits, return
{"changes": [], "updated_page": "<page unchanged>"}.
"""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> tuple[dict, int]:
    """Return (frontmatter_dict, fm_end_index). fm_end_index points to just
    AFTER the closing '\\n---\\n'. Returns ({}, 0) if no frontmatter."""
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


def _load_page(path: str) -> dict:
    """Read a wiki page into a dict: {path, content, frontmatter, frozen}."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    fm, _ = _parse_frontmatter(content)
    return {
        "path": path,
        "rel_path": os.path.relpath(path, _WIKI_ROOT),
        "content": content,
        "frontmatter": fm,
        "last_updated": fm.get("last_updated", ""),
        "editor_frozen": fm.get("editor_frozen", "").lower() == "true",
    }


def _days_since(iso: str, now: datetime) -> float:
    """Return days between now and ISO timestamp. Large number if unparseable."""
    if not iso:
        return 10_000.0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 86400.0
    except Exception:
        return 10_000.0


def _pick_pages_oldest_first(dir_path: str, n: int, now: datetime) -> list[dict]:
    """List candidate pages, sort oldest-edited first, apply filters, return top n."""
    if n <= 0 or not os.path.isdir(dir_path):
        return []
    candidates: list[dict] = []
    for fname in os.listdir(dir_path):
        if not fname.endswith(".md") or fname == "index.md":
            continue
        path = os.path.join(dir_path, fname)
        try:
            p = _load_page(path)
        except Exception:
            continue
        if p["editor_frozen"]:
            continue
        days = _days_since(p["last_updated"], now)
        if days < _SKIP_IF_RECENT_DAYS:
            continue
        p["_days_since"] = days
        candidates.append(p)
    # Oldest first — biggest days_since first (note: _days_since larger = older)
    candidates.sort(key=lambda p: -p["_days_since"])
    return candidates[:n]


def _change_ratio(before: str, after: str) -> float:
    """Fraction of content changed, via difflib SequenceMatcher. Range [0,1]."""
    if before == after:
        return 0.0
    sm = difflib.SequenceMatcher(None, before, after)
    return 1.0 - sm.ratio()


def _extract_json(raw: str) -> dict | None:
    """Pull the first JSON object out of a response. Handles code fences and
    leading prose preamble."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    brace = text.find("{")
    if brace == -1:
        return None
    text = text[brace:]
    depth = 0
    end = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
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
        return json.loads(text[:end])
    except Exception:
        return None


def _load_context_pages_for_topic(topic_page: dict, n: int = 3) -> str:
    """Return a concatenated string of up to `n` related topic pages' content,
    for use as editor context. Related topics come from the page's
    frontmatter `topics:` list if present, otherwise empty."""
    # Find related topic slugs — look in frontmatter or body for `[...](/knowledge/topics/<slug>/)`
    content = topic_page["content"]
    slugs = re.findall(r"/knowledge/topics/([A-Za-z0-9_]+)/", content)
    my_slug = os.path.basename(topic_page["path"])[:-3]
    # Dedupe, preserve order, drop self
    seen: set[str] = {my_slug}
    related: list[str] = []
    for s in slugs:
        if s in seen:
            continue
        seen.add(s)
        related.append(s)
        if len(related) >= n:
            break
    blocks = []
    for s in related:
        path = os.path.join(_TOPICS_DIR, f"{s}.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read()
            # Cap context size per related page
            if len(body) > 4000:
                body = body[:4000] + "\n\n[... truncated ...]"
            blocks.append(f"=== RELATED TOPIC: {s} ===\n{body}")
        except Exception:
            continue
    return "\n\n".join(blocks)


def _list_known_topic_slugs() -> list[str]:
    """Enumerate topic slugs so the editor can verify cross-links resolve."""
    if not os.path.isdir(_TOPICS_DIR):
        return []
    return sorted(
        f[:-3] for f in os.listdir(_TOPICS_DIR)
        if f.endswith(".md") and f != "index.md"
    )


def _is_dry_run() -> bool:
    """Dry-run default is ON. Flip `improve_wiki.dry_run` in tealc_config.json to disable."""
    # FORCE override for manual test runs
    if os.environ.get("IMPROVE_WIKI_LIVE") == "1":
        return False
    try:
        with open(os.path.join(_PROJECT_ROOT, "data", "tealc_config.json")) as f:
            cfg = json.load(f)
        cfg_val = cfg.get("jobs", {}).get("improve_wiki", {}).get("dry_run")
        if cfg_val is None:
            return True  # default ON
        return bool(cfg_val)
    except Exception:
        return True  # default ON on any error


def _create_briefing(title: str, content_md: str, urgency: str = "info") -> None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('wiki_improvement', ?, ?, ?, ?)",
            (urgency, title, content_md, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[improve_wiki] briefing write failed: {e}")


# ---------------------------------------------------------------------------
# Core editor call (shared by improve_wiki and wiki_pipeline enhancement)
# ---------------------------------------------------------------------------

def edit_wiki_page(
    client: Anthropic,
    page: dict,
    *,
    voice_query: str,
    context_block: str = "",
    known_slugs: list[str] | None = None,
    integration_hint: str = "",
    dry_run: bool = True,
) -> dict:
    """Run Opus on a single wiki page, apply edit if within the 40% cap.

    Returns a dict:
      {applied, changes, ratio, reason, tokens_in, tokens_out}

    Always records the outcome to output_ledger. Writes the file only if
    applied=True (i.e., ratio <= _CHANGE_CAP and dry_run=False).
    """
    voice_block = retrieve_voice_exemplars.invoke({
        "query": voice_query, "k": _MAX_VOICE_EXEMPLARS,
    })
    known_slug_hint = ""
    if known_slugs:
        known_slug_hint = (
            "KNOWN TOPIC SLUGS (valid `/knowledge/topics/<slug>/` targets):\n"
            + ", ".join(known_slugs) + "\n\n"
        )
    integration_hint_block = ""
    if integration_hint:
        integration_hint_block = (
            f"INTEGRATION HINT (context for this edit pass):\n{integration_hint}\n\n"
        )

    user_msg = (
        f"{voice_block}\n\n"
        f"{known_slug_hint}"
        f"{integration_hint_block}"
        f"{context_block}\n\n"
        f"=== PAGE TO IMPROVE ({page['rel_path']}) ===\n"
        f"{page['content']}"
    )

    try:
        msg = client.messages.create(
            model=_OPUS_MODEL,
            max_tokens=_MAX_TOKENS_OUT,
            system=_EDITOR_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return {
            "applied": False, "changes": [], "ratio": 0.0,
            "reason": f"api_error: {type(e).__name__}: {e}",
            "tokens_in": 0, "tokens_out": 0,
        }

    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    try:
        record_call(job_name="opus_wiki_editor", model=_OPUS_MODEL, usage=usage)
    except Exception:
        pass

    raw = msg.content[0].text
    parsed = _extract_json(raw)
    if not parsed:
        return {
            "applied": False, "changes": [], "ratio": 0.0,
            "reason": "json_parse_failed",
            "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
        }

    changes = parsed.get("changes", []) or []
    updated = parsed.get("updated_page", "") or ""

    if not changes or not updated:
        # Record the no-op to the ledger too so we can see what was inspected
        _record_ledger(page, [], 0.0, dry_run, usage, "no_changes_needed")
        return {
            "applied": False, "changes": [], "ratio": 0.0,
            "reason": "no_changes_needed",
            "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
        }

    ratio = _change_ratio(page["content"], updated)
    if ratio > _CHANGE_CAP:
        _record_ledger(page, changes, ratio, dry_run, usage,
                       f"change_cap_hit ({ratio:.1%} > {_CHANGE_CAP:.0%}) — skipped")
        return {
            "applied": False, "changes": changes, "ratio": ratio,
            "reason": f"change_cap_hit ({ratio:.1%})",
            "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
        }

    if not dry_run:
        try:
            with open(page["path"], "w", encoding="utf-8") as f:
                f.write(updated)
        except Exception as e:
            _record_ledger(page, changes, ratio, dry_run, usage, f"write_failed: {e}")
            return {
                "applied": False, "changes": changes, "ratio": ratio,
                "reason": f"write_failed: {e}",
                "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
            }

    _record_ledger(page, changes, ratio, dry_run, usage,
                   "applied" if not dry_run else "dry_run_ok")

    return {
        "applied": not dry_run, "changes": changes, "ratio": ratio,
        "reason": ("applied" if not dry_run else "dry_run_ok"),
        "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
        "updated_page": updated,
    }


def _record_ledger(page: dict, changes: list, ratio: float, dry_run: bool,
                   usage: dict, outcome: str) -> None:
    """Write one output_ledger row per edit attempt."""
    try:
        content_summary = "[DRY RUN] " if dry_run else ""
        content_summary += (
            f"{outcome} | ratio={ratio:.1%} | "
            + "; ".join(
                f"{c.get('kind','?')}: {c.get('note','')[:80]}"
                for c in changes[:5]
            )
        ) or f"{outcome} (no changes proposed)"
        record_output(
            kind="wiki_edit",
            job_name="opus_wiki_editor",
            model=_OPUS_MODEL,
            project_id=None,
            content_md=content_summary,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            provenance={
                "page_path": page["rel_path"],
                "outcome": outcome,
                "change_ratio": ratio,
                "dry_run": dry_run,
                "changes": changes,
                "days_since_last_update": page.get("_days_since"),
            },
        )
    except Exception as e:
        print(f"[improve_wiki] ledger write failed for {page.get('rel_path')}: {e}")


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("improve_wiki")
def job() -> str:
    """Weekly wiki-improvement pass. Picks 2 topics + 1 paper, oldest-edited first,
    runs Opus editor, applies or dry-runs per config, writes summary briefing."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("improve_wiki"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # Working-hours guard — same pattern as other expensive overnight-ish jobs.
    # Bypass with FORCE_RUN=1 for manual tests.
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and not os.environ.get("FORCE_RUN"):
        return f"skipped: working-hours guard (hour={hour}; set FORCE_RUN=1 to bypass)"

    if not os.path.isdir(_WIKI_ROOT):
        return f"wiki root not found: {_WIKI_ROOT}"

    now = datetime.now(timezone.utc)
    dry_run = _is_dry_run()
    client = Anthropic()

    known_slugs = _list_known_topic_slugs()
    topic_pages = _pick_pages_oldest_first(_TOPICS_DIR, _TOPICS_PER_RUN, now)
    paper_pages = _pick_pages_oldest_first(_PAPERS_DIR, _PAPERS_PER_RUN, now)

    results = []

    # Process topic pages first — they're higher-leverage
    for page in topic_pages:
        slug = os.path.basename(page["path"])[:-3]
        voice_query = (
            f"scientific topic page {slug.replace('_', ' ')} — "
            f"synthesis with findings and open disagreements"
        )
        context_block = _load_context_pages_for_topic(page, n=3)
        outcome = edit_wiki_page(
            client, page,
            voice_query=voice_query,
            context_block=context_block,
            known_slugs=known_slugs,
            integration_hint="Routine improvement pass — page was picked because it's one of the oldest-edited.",
            dry_run=dry_run,
        )
        results.append({"page": page["rel_path"], **outcome})

    # Process paper pages
    for page in paper_pages:
        slug = os.path.basename(page["path"])[:-3]
        voice_query = (
            f"paper summary page — findings of scientific paper, "
            f"methods prose, concise author-voice annotation"
        )
        context_block = ""  # paper pages don't need topic context for prose edits
        outcome = edit_wiki_page(
            client, page,
            voice_query=voice_query,
            context_block=context_block,
            known_slugs=known_slugs,
            integration_hint="Routine improvement pass — paper page review.",
            dry_run=dry_run,
        )
        results.append({"page": page["rel_path"], **outcome})

    # Summary briefing
    applied = sum(1 for r in results if r["applied"])
    skipped = sum(1 for r in results if r["reason"].startswith("change_cap_hit"))
    no_op = sum(1 for r in results if r["reason"] == "no_changes_needed")
    errs = sum(1 for r in results if "error" in r["reason"] or "failed" in r["reason"])

    lines = [
        f"### improve_wiki — weekly pass ({now.strftime('%Y-%m-%d %H:%M UTC')})",
        f"mode: {'DRY-RUN' if dry_run else 'LIVE'}  |  "
        f"applied: {applied}  |  cap-hit: {skipped}  |  no-op: {no_op}  |  errors: {errs}",
        "",
    ]
    for r in results:
        hdr = f"**{r['page']}**"
        tail = f"ratio={r['ratio']:.1%}  reason={r['reason']}"
        lines.append(f"- {hdr}  ({tail})")
        for c in r.get("changes", [])[:6]:
            lines.append(f"    - {c.get('kind','?')}: {c.get('note','')[:120]}")
    content_md = "\n".join(lines)
    _create_briefing(
        title=f"Wiki improvement pass ({applied} applied, {skipped} cap-hit)",
        content_md=content_md,
        urgency="info",
    )

    return f"improve_wiki: applied={applied} cap_hit={skipped} no_op={no_op} errors={errs} dry_run={dry_run}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(job())
