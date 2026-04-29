"""Contradictions dashboard — aggregates Contradictions sections from all topic pages.

Cron: daily 5am Central (APScheduler wiring is the PI's job per WIKI_V1_PLAN.md).

Idle gate: working-hours guard identical to improve_wiki — skips if Central hour
is in [8, 22) unless FORCE_RUN=1.  No-op on weekends (skip guard is by hour, not
day, so it naturally runs only in the nightly window).

Model: NONE — pure filesystem grep + template render.  Zero LLM calls.

Algorithm:
  1. Walk knowledge/topics/*.md in the website repo.
  2. Parse YAML frontmatter (minimal, no PyYAML dependency) to get topic_slug,
     title, category, last_updated.
  3. Extract the body block between '## Contradictions / open disagreements' and
     the next top-level '##' (or '<!-- tealc:' marker, or end-of-file).
  4. Skip topics with no contradictions section or an effectively empty one.
  5. Group by category field, then render a single aggregated markdown page to
     knowledge/contradictions/index.md.
  6. Write via website_git.stage_files() if available; otherwise write directly
     and let the existing git loop pick it up.
  7. Log one row to output_ledger (job_name='contradictions_index').
  8. Log one row to cost_tracking (model='none', cost=0, tokens=0).

Failure policy: if a topic file's frontmatter can't be parsed, log a warning to
output_ledger and skip that file.  Never write a partial or corrupted output file.
The output file is only written once the full render is ready in memory.

Manual run (bypass working-hours guard):
    FORCE_RUN=1 PYTHONPATH=/path/to/00-Lab-Agent \
      python -m agent.jobs.contradictions_index
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_WEBSITE_REPO = os.environ.get(
    "TEALC_WEBSITE_REPO",
    "/Users/blackmon/Desktop/GitHub/coleoguy.github.io",
)
_TOPICS_DIR = Path(_WEBSITE_REPO) / "knowledge" / "topics"
_OUTPUT_DIR = Path(_WEBSITE_REPO) / "knowledge" / "contradictions"
_OUTPUT_FILE = _OUTPUT_DIR / "index.md"

_JOB_NAME = "contradictions_index"

# Regex that locates the start of the contradictions section (case-insensitive
# to tolerate minor heading-case drift).
_CONTRAD_HEADING_RE = re.compile(
    r"^##\s+contradictions\s*/\s*open\s+disagreements",
    re.IGNORECASE | re.MULTILINE,
)

# Matches the next top-level ## heading or a tealc marker, whichever comes first.
_NEXT_SECTION_RE = re.compile(
    r"^##\s|<!-- tealc:",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Frontmatter parser (minimal — same approach as wiki_janitor.py)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text).

    Parses the YAML block between the first pair of '---' delimiters.
    Values are returned as raw stripped strings. Returns ({}, text) if no
    frontmatter is found.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm: dict = {}
    for line in fm_block.splitlines():
        if not line or line.lstrip().startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip().strip('"').strip("'")
        if key and key not in fm:  # first occurrence wins
            fm[key] = val
    return fm, body


# ---------------------------------------------------------------------------
# Contradiction-section extractor
# ---------------------------------------------------------------------------

def _extract_contradictions(body: str) -> str | None:
    """Return the text block between the contradictions heading and the next
    top-level heading (or tealc marker / EOF).  Returns None if no such
    heading exists or the block is effectively empty (only whitespace /
    placeholder text)."""
    m = _CONTRAD_HEADING_RE.search(body)
    if m is None:
        return None
    # Text starts immediately after the heading line
    start = m.end()
    # Find where the next top-level section begins (after the heading we just found)
    remainder = body[start:]
    next_m = _NEXT_SECTION_RE.search(remainder)
    if next_m:
        block = remainder[: next_m.start()]
    else:
        block = remainder

    stripped = block.strip()
    if not stripped:
        return None
    # Skip purely placeholder blocks (no real content beyond a single short line)
    # We consider < 30 chars of actual text as "empty".
    if len(stripped) < 30:
        return None
    return stripped


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _render_index(
    groups: dict[str, list[dict]],
    now_iso: str,
) -> str:
    """Render the full aggregated markdown file content."""
    lines: list[str] = [
        "---",
        "layout: default",
        'title: "Contradictions and open disagreements"',
        "permalink: /knowledge/contradictions/",
        f"last_updated: {now_iso}",
        "---",
        "",
        "# Contradictions and open disagreements across lab topics",
        "",
        "Every lab topic page has a `## Contradictions / open disagreements` section. "
        "This page aggregates them across the wiki — a single place to see where the "
        "literature actively disagrees with itself.",
        "",
    ]

    sorted_categories = sorted(groups.keys())
    for category in sorted_categories:
        lines.append(f"## {category or 'Uncategorized'}")
        lines.append("")
        for entry in sorted(groups[category], key=lambda e: e["title"]):
            slug = entry["slug"]
            title = entry["title"]
            contradiction_text = entry["text"]
            lines.append(f"### [{title}](/knowledge/topics/{slug}/)")
            lines.append("")
            lines.append(contradiction_text)
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _collect_contradictions() -> tuple[dict[str, list[dict]], list[str]]:
    """Walk topics dir and collect contradiction blocks.

    Returns:
        groups: dict mapping category → list of dicts with keys
                slug, title, category, last_updated, text
        warnings: list of human-readable warning strings for skipped files
    """
    groups: dict[str, list[dict]] = {}
    warnings: list[str] = []

    if not _TOPICS_DIR.is_dir():
        warnings.append(f"topics dir not found: {_TOPICS_DIR}")
        return groups, warnings

    for fpath in sorted(_TOPICS_DIR.glob("*.md")):
        if fpath.name == "index.md":
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception as exc:
            warnings.append(f"read error {fpath.name}: {exc}")
            continue

        try:
            fm, body = _parse_frontmatter(text)
        except Exception as exc:
            warnings.append(f"frontmatter parse error {fpath.name}: {exc}")
            continue

        slug = fm.get("topic_slug") or fpath.stem
        title = fm.get("title") or slug.replace("_", " ").title()
        category = fm.get("category") or "Uncategorized"
        last_updated = fm.get("last_updated") or ""

        contradiction_text = _extract_contradictions(body)
        if contradiction_text is None:
            continue

        entry = {
            "slug": slug,
            "title": title,
            "category": category,
            "last_updated": last_updated,
            "text": contradiction_text,
        }
        groups.setdefault(category, []).append(entry)

    return groups, warnings


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------

@tracked(_JOB_NAME)
def job() -> str:
    """Daily 5am Central: aggregate contradictions from all topic pages."""
    # Control-tab gate
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle(_JOB_NAME):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # Working-hours guard (same pattern as improve_wiki)
    hour = datetime.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and not os.environ.get("FORCE_RUN"):
        return f"skipped: working-hours guard (hour={hour} CT; set FORCE_RUN=1 to bypass)"

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # --- Collect ---
    groups, warnings = _collect_contradictions()

    # Log any parse warnings
    for w in warnings:
        record_output(
            kind="wiki_warning",
            job_name=_JOB_NAME,
            model="none",
            project_id=None,
            content_md=w,
            tokens_in=0,
            tokens_out=0,
            provenance={"warning": w},
        )

    if not groups:
        status = "noop"
        summary = "contradictions_index: no contradictions sections found in any topic page"
    else:
        # --- Render ---
        rendered = _render_index(groups, now_iso)

        # --- Write ---
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Prefer website_git.stage_files() — falls back to direct write
        write_via_git = False
        try:
            from agent.jobs.website_git import stage_files  # noqa: PLC0415
            rel_path = str(
                _OUTPUT_FILE.relative_to(Path(_WEBSITE_REPO))
            )
            stage_files({rel_path: rendered})
            write_via_git = True
        except Exception:
            pass  # Fall through to direct write

        if not write_via_git:
            _OUTPUT_FILE.write_text(rendered, encoding="utf-8")

        total_topics = sum(len(v) for v in groups.values())
        status = "ok"
        summary = (
            f"contradictions_index: wrote {total_topics} topics across "
            f"{len(groups)} categories; "
            f"{'staged via website_git' if write_via_git else 'wrote directly'}; "
            f"{len(warnings)} parse warnings"
        )

    # --- Audit log ---
    record_output(
        kind="wiki_surface",
        job_name=_JOB_NAME,
        model="none",
        project_id=None,
        content_md=summary,
        tokens_in=0,
        tokens_out=0,
        provenance={"status": status, "warnings_count": len(warnings)},
    )

    # Zero-cost row for audit completeness (no-LLM jobs still log to cost_tracking)
    record_call(
        job_name=_JOB_NAME,
        model="none",
        usage={"input_tokens": 0, "output_tokens": 0},
    )

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    result = job()
    print(result)
    sys.exit(0 if "error" not in str(result).lower() else 1)
