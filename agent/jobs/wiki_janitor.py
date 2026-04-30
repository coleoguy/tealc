"""Wiki janitor job — runs every Monday at 8am Central via APScheduler.

Audits the lab wiki at the lab's GitHub Pages /knowledge/ for
integrity issues and surfaces them as a briefing row in data/agent.db.

Nine checks (all read-only, no API calls):
  1. Missing category: on topic pages
  2. Stub titles on paper pages
  3. Title/h1 mismatch
  4. Sub-index files present (ERROR-level)
  5. Broken finding-anchor cross-links in topic pages
  6. Topic referenced in paper but no topic page exists
  7. Topic page references papers (DOI-style) not in papers_supporting:
  8. Broken-slug paper links — body markdown links of the form
       [...]( /knowledge/papers/<slug>/) or [...](/knowledge/papers/<slug>/#...)
     that point to a non-existent file (ERROR), have a slug casing mismatch
     vs. the filename (ERROR), or end with .md (WARNING).  Checked in both
     topic pages and paper pages.
  9. Cross-link opportunities suggester (NOT auto-fix) — finds topic↔topic
     and paper↔paper pairs that share ≥ 2 topics: tags but are not yet
     cross-linked.  Results are summarised in content_md and written in full
     as metadata_json["cross_link_candidates"].  Candidates are intended for
     a "## Related on this site" section demarcated by the Tealc auto-edit
     markers:
       <!-- tealc:auto-start -->
       ...
       <!-- tealc:auto-end -->
     Content between those markers is fair game for automation; anything
     outside is human-preserved.  This contract lets check 9 be the proposal
     stage and a future Tealc topic-writer pass be the execution stage.

Run manually:
    cd /path/to/00-Lab-Agent && \\
      PYTHONPATH="$PWD" ~/.lab-agent-venv/bin/python -m agent.jobs.wiki_janitor
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
_WIKI_ROOT = Path(os.environ.get(
    "WIKI_TOPICS_DIR",
    os.path.expanduser("~/Desktop/GitHub/lab-pages/knowledge"),
))
_PAPERS_DIR = _WIKI_ROOT / "papers"
_TOPICS_DIR = _WIKI_ROOT / "topics"
_REPOS_DIR = _WIKI_ROOT / "repos"
_CONCEPTS_DIR = _WIKI_ROOT / "concepts"
_METHODS_DIR = _WIKI_ROOT / "methods"

_FRESHNESS_START = "<!-- tealc:freshness-start -->"
_FRESHNESS_END = "<!-- tealc:freshness-end -->"

# Tealc config — gate the check-12 auto-add behavior
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "data" / "tealc_config.json"


def _janitor_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return {}
    return (cfg.get("jobs", {}) or {}).get("wiki_janitor", {}) or {}


def _auto_add_freshness_enabled() -> bool:
    """Default True per V5 handoff; can be disabled via jobs.wiki_janitor.auto_add_freshness."""
    val = _janitor_config().get("auto_add_freshness")
    return True if val is None else bool(val)

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Stub title pattern — matches values like "2014b", "2016 saga", "2020 microsats"
# Applied to the raw YAML value (including surrounding quotes if present).
_STUB_RE = re.compile(r'^"?\d{4}[a-z_ -]{0,25}"?$', re.IGNORECASE)

# Finding-anchor cross-link in topic pages: /knowledge/papers/<slug>/#finding-<N>
_CROSSLINK_RE = re.compile(r'/knowledge/papers/([^/#]+)/#(finding-\d+)')

# DOI-style references in topic body (e.g. 10.1234/something or 10_1234_something)
# We look for bare DOI strings starting with "10." as they appear in inline citations
# like [SLUG, Finding N](/knowledge/papers/10_1534_genetics_117_300382/#finding-1)
_PAPER_SLUG_RE = re.compile(r'/knowledge/papers/([^/#"\')\s]+)/')

# ---------------------------------------------------------------------------
# Frontmatter parser (minimal — no PyYAML dependency)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text).

    Parses the YAML block between the first pair of '---' delimiters.
    Values are returned as raw strings (not typed). Returns ({}, text) if
    no frontmatter found.
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
        # Skip list items and blank lines
        if not line or line.lstrip().startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip()
        if key and key not in fm:  # first occurrence wins
            fm[key] = val
    return fm, body


def _extract_list_field(text: str, field: str) -> list[str]:
    """Extract a YAML list field like 'topics: [a, b, c]' or multi-line block."""
    # Inline list: field: [a, b, c]
    inline = re.search(
        r'^' + re.escape(field) + r':\s*\[([^\]]*)\]',
        text, re.MULTILINE,
    )
    if inline:
        raw = inline.group(1)
        items = [s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()]
        return items
    # Block list: field:\n  - a\n  - b
    block = re.search(
        r'^' + re.escape(field) + r':\s*\n((?:\s+-[^\n]*\n?)+)',
        text, re.MULTILINE,
    )
    if block:
        items = []
        for m in re.finditer(r'^\s+-\s+(.+)$', block.group(1), re.MULTILINE):
            items.append(m.group(1).strip().strip('"').strip("'"))
        return items
    return []


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_missing_category(topics_dir: Path) -> list[str]:
    """Check 1: topic pages missing category: field."""
    issues = []
    for md in sorted(topics_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm, _ = _parse_frontmatter(text)
        cat = fm.get("category", "").strip().strip('"').strip("'")
        if not cat:
            issues.append(md.name)
    return issues


def check_stub_titles(papers_dir: Path) -> list[str]:
    """Check 2: paper pages with stub titles."""
    issues = []
    for md in sorted(papers_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm, _ = _parse_frontmatter(text)
        raw_title = fm.get("title", "")
        # Strip surrounding quotes for matching
        bare = raw_title.strip()
        if _STUB_RE.match(bare):
            issues.append(f"{md.name}: title={raw_title!r}")
    return issues


def check_title_h1_mismatch(papers_dir: Path) -> list[str]:
    """Check 3: title: frontmatter field doesn't match first # h1 in body."""
    issues = []
    for md in sorted(papers_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_frontmatter(text)
        raw_title = fm.get("title", "")
        fm_title = raw_title.strip().strip('"').strip("'")
        h1_match = re.search(r'^#\s+(.+)$', body, re.MULTILINE)
        if not h1_match:
            if fm_title:
                issues.append(f"{md.name}: no h1 found (title={fm_title!r})")
            continue
        h1 = h1_match.group(1).strip()
        if fm_title != h1:
            issues.append(f"{md.name}: title={fm_title!r} != h1={h1!r}")
    return issues


def check_subindex_files(wiki_root: Path) -> list[str]:
    """Check 4: forbidden sub-index files."""
    issues = []
    for subdir in ("papers", "topics", "repos"):
        candidate = wiki_root / subdir / "index.md"
        if candidate.exists():
            issues.append(str(candidate.relative_to(wiki_root)))
    return issues


def check_broken_finding_anchors(
    topics_dir: Path, papers_dir: Path
) -> list[str]:
    """Check 5: /knowledge/papers/<slug>/#finding-<N> links where the anchor
    doesn't exist in the target paper page."""
    issues = []
    for md in sorted(topics_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in _CROSSLINK_RE.finditer(text):
            slug = m.group(1)
            anchor = m.group(2)   # e.g. "finding-1"
            paper_path = papers_dir / f"{slug}.md"
            if not paper_path.exists():
                issues.append(
                    f"{md.name} → /knowledge/papers/{slug}/#{anchor}"
                    f" (paper page missing)"
                )
                continue
            paper_text = paper_path.read_text(encoding="utf-8", errors="replace")
            # We write anchors as: <a id="finding-N"></a>
            if f'<a id="{anchor}"></a>' not in paper_text:
                issues.append(
                    f"{md.name} → /knowledge/papers/{slug}/#{anchor}"
                    f" (anchor missing in paper)"
                )
    return issues


def check_orphan_topic_refs(
    papers_dir: Path, topics_dir: Path
) -> list[str]:
    """Check 6: paper topics: [...] list contains slugs with no topic page."""
    issues = []
    existing_topics = {md.stem for md in topics_dir.glob("*.md") if md.name != "index.md"}
    for md in sorted(papers_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        slugs = _extract_list_field(text, "topics")
        for slug in slugs:
            if slug and slug not in existing_topics:
                issues.append(f"{md.name}: topics: contains {slug!r} (no topic page)")
    return issues


def check_silent_paper_refs(
    topics_dir: Path, papers_dir: Path
) -> list[str]:
    """Check 7: topic body links to a paper slug but that slug is not in
    papers_supporting: frontmatter.

    We detect paper slugs from /knowledge/papers/<slug>/ links in the body,
    then cross-check against the papers_supporting list.
    DOI slugs (contain underscores and start with 10_) are normalised back
    to DOI form (10.XXXX/...) for comparison, since papers_supporting stores
    raw DOIs not file slugs.
    """
    issues = []
    for md in sorted(topics_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_frontmatter(text)

        # papers_supporting may contain DOIs or SHA256 hashes
        supporting_raw = _extract_list_field(text, "papers_supporting")
        supporting = set(s.strip() for s in supporting_raw if s.strip())

        # Build normalised set: for DOI slugs, convert 10_xxx_yyy → 10.xxx/yyy
        # Legacy slugs (2012a, 2020_microsats) are kept as-is (no DOI match possible)
        def slug_to_doi(slug: str) -> str:
            """Best-effort: convert file slug to DOI for lookup."""
            if slug.startswith("10_"):
                # Replace first underscore after "10" with ".", rest with "/"
                # e.g. 10_1534_genetics_117_300382 → 10.1534/genetics.117.300382
                # Strategy: split on first "_", then replace remaining "_" with "."
                # But DOIs use "/" after the registrant prefix.
                # The slug encoding: DOI "10.REG/SUFFIX" → "10_REG_SUFFIX"
                # where "/" → "_" and "." → "_".
                # We can't perfectly invert because both "/" and "." both become "_".
                # Return the slug itself for set lookup (supporting may have slug form).
                return slug  # leave as slug — supporting sometimes stores DOI forms
            return slug

        slugs_in_body = set(_PAPER_SLUG_RE.findall(body))
        # Remove self-references (permalinks often appear in related-papers section)
        own_slug = md.stem

        for slug in sorted(slugs_in_body):
            if slug == own_slug:
                continue
            # Check if this slug appears in supporting (as slug or as DOI)
            if slug in supporting:
                continue
            # Try DOI form: for DOI-slug files the DOI is in the supporting list
            # e.g. slug=10_1534_genetics_117_300382, DOI=10.1534/genetics.117.300382
            # The supporting list stores raw DOIs — reconstruct the DOI from slug
            doi_candidate = _slug_to_doi_heuristic(slug)
            if doi_candidate and doi_candidate in supporting:
                continue
            # Also check if the paper slug is referenced as a full path in supporting
            # (some supporting entries might be SHA256 hashes — nothing we can match)
            # Flag it
            paper_exists = (papers_dir / f"{slug}.md").exists()
            if paper_exists:
                issues.append(
                    f"{md.name}: body links /knowledge/papers/{slug}/ "
                    f"but slug not in papers_supporting:"
                )
    return issues


def check_broken_slug_links(
    topics_dir: Path, papers_dir: Path
) -> list[str]:
    """Check 8: body markdown links to /knowledge/papers/<slug>/ where:
    - The slug has no matching file (ERROR)
    - The slug has a casing mismatch with the actual filename (ERROR)
    - The link includes a trailing .md (WARNING)

    Scans both topic pages and paper pages.
    """
    # Build lookup structures: lower-cased slug → actual slug on disk
    existing_slugs: dict[str, str] = {}  # lower → actual
    for md in papers_dir.glob("*.md"):
        if md.name == "index.md":
            continue
        existing_slugs[md.stem.lower()] = md.stem

    # Regex: /knowledge/papers/<slug>/ optionally followed by #anchor
    # Also catches .md variant: /knowledge/papers/<slug>.md
    _SLUG_LINK_RE = re.compile(
        r'/knowledge/papers/([^/#"\')\s]+?)(?:\.md)?(?:/(?:#[^\s"\')\]]*)?)?(?=["\'\s)\]]|$)'
    )

    issues: list[str] = []

    def _scan_file(source_file: Path) -> None:
        text = source_file.read_text(encoding="utf-8", errors="replace")
        _, body = _parse_frontmatter(text)
        source_name = source_file.name

        # Detect .md trailing links separately
        md_link_re = re.compile(r'/knowledge/papers/([^/#"\')\s]+?)\.md')
        for m in md_link_re.finditer(body):
            slug = m.group(1)
            issues.append(
                f"[WARNING] {source_name} → links to /knowledge/papers/{slug}.md"
                f" (should use permalink form without .md)"
            )

        # Detect slug links (with or without anchor, without .md)
        clean_link_re = re.compile(
            r'/knowledge/papers/([^/#"\')\s.][^/#"\')\s]*)/?(?:#[^\s"\')\]]*)?'
        )
        seen_in_file: set[str] = set()
        for m in clean_link_re.finditer(body):
            slug = m.group(1)
            # Skip if this was caught as a .md link already
            if slug.endswith(".md"):
                continue
            if slug in seen_in_file:
                continue
            seen_in_file.add(slug)

            slug_lower = slug.lower()
            if slug_lower not in existing_slugs:
                issues.append(
                    f"[ERROR] {source_name} → links to /knowledge/papers/{slug}/"
                    f" (no such paper file)"
                )
            elif existing_slugs[slug_lower] != slug:
                actual = existing_slugs[slug_lower]
                issues.append(
                    f"[ERROR] {source_name} → links to /knowledge/papers/{slug}/"
                    f" (casing mismatch: file is {actual}.md)"
                )

    for md in sorted(topics_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        _scan_file(md)

    for md in sorted(papers_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        _scan_file(md)

    return issues


def check_cross_link_opportunities(
    topics_dir: Path, papers_dir: Path
) -> tuple[list[str], dict]:
    """Check 9: cross-link opportunity suggester (read-only, no auto-fix).

    Builds paper_slug → set(topic_slugs) from paper frontmatter, inverts to
    topic_slug → set(paper_slugs), then finds pairs sharing ≥ 2 papers/topics.
    Returns (summary_lines, candidates_dict) where candidates_dict is suitable
    for metadata_json["cross_link_candidates"].

    Candidates are intended for "## Related on this site" sections demarcated
    by <!-- tealc:auto-start --> / <!-- tealc:auto-end --> markers.
    """
    # ---- Build paper_slug → set(topic_slugs) --------------------------------
    paper_to_topics: dict[str, set[str]] = {}
    for md in sorted(papers_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        topic_slugs = _extract_list_field(text, "topics")
        if topic_slugs:
            paper_to_topics[md.stem] = set(topic_slugs)

    # ---- Invert to topic_slug → set(paper_slugs) ----------------------------
    topic_to_papers: dict[str, set[str]] = {}
    for paper_slug, topics in paper_to_topics.items():
        for t in topics:
            topic_to_papers.setdefault(t, set()).add(paper_slug)

    # ---- Load topic page bodies (for existing-link detection) ---------------
    topic_bodies: dict[str, str] = {}
    existing_topic_slugs: set[str] = set()
    for md in sorted(topics_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        existing_topic_slugs.add(md.stem)
        text = md.read_text(encoding="utf-8", errors="replace")
        _, body = _parse_frontmatter(text)
        topic_bodies[md.stem] = body

    # ---- Load paper page bodies (for existing-link detection) ---------------
    paper_bodies: dict[str, str] = {}
    for md in sorted(papers_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        _, body = _parse_frontmatter(text)
        paper_bodies[md.stem] = body

    # ---- Topic-to-topic candidates ------------------------------------------
    # Only consider topics that actually have a page
    relevant_topics = [t for t in topic_to_papers if t in existing_topic_slugs]

    topic_topic_candidates: list[dict] = []
    checked_tt: set[frozenset] = set()
    for i, t1 in enumerate(sorted(relevant_topics)):
        papers_t1 = topic_to_papers[t1]
        for t2 in sorted(relevant_topics):
            if t1 == t2:
                continue
            pair = frozenset({t1, t2})
            if pair in checked_tt:
                continue
            checked_tt.add(pair)
            papers_t2 = topic_to_papers[t2]
            shared = papers_t1 & papers_t2
            if len(shared) < 2:
                continue
            # Check if t1's body already links to t2 or vice versa
            t1_links_t2 = f"/knowledge/topics/{t2}/" in topic_bodies.get(t1, "")
            t2_links_t1 = f"/knowledge/topics/{t1}/" in topic_bodies.get(t2, "")
            candidates_needed = []
            if not t1_links_t2:
                candidates_needed.append({"from": t1, "to": t2, "direction": "t1→t2"})
            if not t2_links_t1:
                candidates_needed.append({"from": t2, "to": t1, "direction": "t2→t1"})
            if candidates_needed:
                topic_topic_candidates.append({
                    "topic_a": t1,
                    "topic_b": t2,
                    "shared_papers": sorted(shared),
                    "shared_count": len(shared),
                    "missing_links": candidates_needed,
                })

    # Sort by shared count descending
    topic_topic_candidates.sort(key=lambda x: -x["shared_count"])

    # ---- Paper-to-paper candidates ------------------------------------------
    paper_paper_candidates: list[dict] = []
    checked_pp: set[frozenset] = set()
    all_papers = sorted(paper_to_topics.keys())
    for p1 in all_papers:
        topics_p1 = paper_to_topics[p1]
        for p2 in all_papers:
            if p1 == p2:
                continue
            pair = frozenset({p1, p2})
            if pair in checked_pp:
                continue
            checked_pp.add(pair)
            topics_p2 = paper_to_topics[p2]
            shared_topics = topics_p1 & topics_p2
            if len(shared_topics) < 2:
                continue
            # Check if p1's body already links to p2 or vice versa
            p1_links_p2 = f"/knowledge/papers/{p2}/" in paper_bodies.get(p1, "")
            p2_links_p1 = f"/knowledge/papers/{p1}/" in paper_bodies.get(p2, "")
            candidates_needed = []
            if not p1_links_p2:
                candidates_needed.append({"from": p1, "to": p2, "direction": "p1→p2"})
            if not p2_links_p1:
                candidates_needed.append({"from": p2, "to": p1, "direction": "p2→p1"})
            if candidates_needed:
                paper_paper_candidates.append({
                    "paper_a": p1,
                    "paper_b": p2,
                    "shared_topics": sorted(shared_topics),
                    "shared_count": len(shared_topics),
                    "missing_links": candidates_needed,
                })

    paper_paper_candidates.sort(key=lambda x: -x["shared_count"])

    # ---- Compute counts for summary -----------------------------------------
    # topic-to-topic: count unique directed pairs
    n_tt_pairs = len(topic_topic_candidates)
    n_tt_topics = len({c["topic_a"] for c in topic_topic_candidates} |
                      {c["topic_b"] for c in topic_topic_candidates})
    n_pp_pairs = len(paper_paper_candidates)
    n_pp_papers = len({c["paper_a"] for c in paper_paper_candidates} |
                      {c["paper_b"] for c in paper_paper_candidates})

    summary_lines = [
        f"Cross-link candidates (would require human review): "
        f"{n_tt_pairs} topic-to-topic pairs across {n_tt_topics} topics, "
        f"{n_pp_pairs} paper-to-paper pairs across {n_pp_papers} papers"
    ]

    candidates_dict = {
        "topic_topic": topic_topic_candidates,
        "paper_paper": paper_paper_candidates,
    }

    return summary_lines, candidates_dict


def check_orphan_concept_links(
    topics_dir: Path, papers_dir: Path, concepts_dir: Path,
) -> list[str]:
    """Check 10: /knowledge/concepts/<slug>/ references that don't resolve to
    a concept card file.  Also flags class="concept-link" href attributes
    that point at missing slugs (emitted by assets/js/concept-tooltips.js)."""
    existing: set[str] = set()
    if concepts_dir.exists():
        for md in concepts_dir.glob("*.md"):
            if md.name == "index.md":
                continue
            existing.add(md.stem)

    link_re = re.compile(r'/knowledge/concepts/([a-z0-9\-_]+)/')
    href_re = re.compile(r'class="concept-link"[^>]*href="/knowledge/concepts/([a-z0-9\-_]+)/')

    issues: list[str] = []
    for scan_dir in (topics_dir, papers_dir):
        if not scan_dir.exists():
            continue
        for md in sorted(scan_dir.glob("*.md")):
            if md.name == "index.md":
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            _, body = _parse_frontmatter(text)
            seen_in_file: set[str] = set()
            for slug in link_re.findall(body):
                if slug in seen_in_file:
                    continue
                seen_in_file.add(slug)
                if slug not in existing:
                    issues.append(
                        f"{md.name} → /knowledge/concepts/{slug}/ "
                        f"(no concept card)"
                    )
            for slug in href_re.findall(body):
                if slug in seen_in_file:
                    continue
                seen_in_file.add(slug)
                if slug not in existing:
                    issues.append(
                        f"{md.name} → concept-link href /knowledge/concepts/{slug}/ "
                        f"(no concept card)"
                    )
    return issues


def check_method_missing_citations(
    methods_dir: Path, papers_dir: Path,
) -> list[str]:
    """Check 11: method page `appears_in_papers:` frontmatter lists a paper
    slug with no file in knowledge/papers/."""
    if not methods_dir.exists():
        return []
    existing = {md.stem for md in papers_dir.glob("*.md") if md.name != "index.md"} \
        if papers_dir.exists() else set()
    issues: list[str] = []
    for md in sorted(methods_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        entries = _extract_list_field(text, "appears_in_papers")
        for e in entries:
            if not e:
                continue
            # Frontmatter may store DOIs or slugs; normalize DOI → slug
            slug = re.sub(r"[./]", "_", e) if e.startswith("10.") else e
            if slug in existing:
                continue
            issues.append(
                f"{md.name}: appears_in_papers contains {e!r} (no paper page)"
            )
    return issues


def check_paper_method_backlinks(
    methods_dir: Path, papers_dir: Path,
) -> list[str]:
    """Check 13: when a method page's `appears_in_papers:` lists a paper, that
    paper's body should link back to the method page at
    `/knowledge/methods/<method_slug>/`.  Without the reverse link, a reader
    on a paper page has no in-wiki route to the method documentation."""
    if not methods_dir.exists() or not papers_dir.exists():
        return []
    issues: list[str] = []
    for method_md in sorted(methods_dir.glob("*.md")):
        if method_md.name == "index.md":
            continue
        method_slug = method_md.stem
        try:
            mtext = method_md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        papers = _extract_list_field(mtext, "appears_in_papers")
        target = f"/knowledge/methods/{method_slug}/"
        for p_entry in papers:
            if not p_entry:
                continue
            p_slug = re.sub(r"[./]", "_", p_entry) if p_entry.startswith("10.") else p_entry
            paper_md = papers_dir / f"{p_slug}.md"
            if not paper_md.exists():
                continue  # covered by check 11
            try:
                body = paper_md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if target not in body:
                issues.append(
                    f"{paper_md.name} listed in methods/{method_slug}.md "
                    f"appears_in_papers but body does not link back to {target}"
                )
    return issues


_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def check_papers_supporting_format_consistency(topics_dir: Path) -> list[str]:
    """Check 14: a single topic page's `papers_supporting:` should not mix
    identifier formats (raw DOI starting with '10.', bare 64-char SHA256 hex,
    'sha256:'-prefixed string).  refresh_enrichment compares these as raw
    strings when computing topic-topic overlap; format drift silently
    suppresses cross-link discovery."""
    if not topics_dir.exists():
        return []
    issues: list[str] = []
    for md in sorted(topics_dir.glob("*.md")):
        if md.name == "index.md":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        entries = _extract_list_field(text, "papers_supporting")
        doi = [e for e in entries if e.startswith("10.")]
        sha_bare = [e for e in entries if _SHA_RE.match(e)]
        sha_prefixed = [e for e in entries if e.startswith("sha256:")]
        formats_present = sum(1 for x in (doi, sha_bare, sha_prefixed) if x)
        if formats_present > 1:
            issues.append(
                f"{md.name}: papers_supporting mixes formats "
                f"(doi={len(doi)}, sha-bare={len(sha_bare)}, "
                f"sha256:={len(sha_prefixed)}) — refresh_enrichment will "
                f"under-count overlap"
            )
    return issues


def check_orphan_freshness(
    topics_dir: Path, papers_dir: Path, auto_add: bool = True,
) -> tuple[list[str], list[str]]:
    """Check 12: every topic + paper page should have a
    `<!-- tealc:freshness-start -->` region right under its H1.  Missing
    regions are warned; when `auto_add=True`, a minimal banner is inserted
    in-place.

    Returns (warnings, auto_added_paths).
    """
    warnings: list[str] = []
    added: list[str] = []

    def _process(scan_dir: Path, kind: str) -> None:
        if not scan_dir.exists():
            return
        for md in sorted(scan_dir.glob("*.md")):
            if md.name == "index.md":
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if _FRESHNESS_START in text:
                continue
            warnings.append(f"{md.name}: missing {_FRESHNESS_START} region ({kind})")
            if not auto_add:
                continue
            patched = _insert_freshness_banner(text, kind)
            if patched == text:
                continue
            try:
                md.write_text(patched, encoding="utf-8")
                added.append(f"{md.name}: auto-added ({kind})")
            except Exception as exc:
                warnings.append(f"{md.name}: auto-add failed: {exc}")

    _process(topics_dir, "topic")
    _process(papers_dir, "paper")
    return warnings, added


def _insert_freshness_banner(text: str, kind: str) -> str:
    """Insert a minimal freshness banner immediately after the first H1.

    For `kind='topic'`, sources_count is drawn from papers_supporting (if
    present); for `kind='paper'`, the banner still carries a composed-date
    but omits sources_count.  No LLM calls.  Idempotent — returns the text
    unchanged if the marker is already present or no H1 was found.
    """
    if _FRESHNESS_START in text:
        return text

    fm, body = _parse_frontmatter(text)
    today_iso = datetime.now(timezone.utc).date().isoformat()

    if kind == "topic":
        supporting = _extract_list_field(text, "papers_supporting")
        n_sources = len([s for s in supporting if s])
        sources_line = (
            f'<span class="wf-sources">{n_sources} source'
            f'{"s" if n_sources != 1 else ""}</span>\n<span class="wf-dot">·</span>\n'
            if n_sources else ""
        )
    else:
        n_sources = 0
        sources_line = ""

    banner = (
        f"{_FRESHNESS_START}\n"
        '<aside class="wiki-freshness" aria-label="Page provenance">\n'
        '<span class="wf-label">Composed by Tealc</span>\n'
        f'<span class="wf-date">{today_iso}</span>\n'
        '<span class="wf-dot">·</span>\n'
        f"{sources_line}"
        '<span class="wf-review">Last reviewed by Heath: never</span>\n'
        "</aside>\n"
        f"{_FRESHNESS_END}\n"
    )

    # Try to insert after the first H1 in the body; fall back to prepending
    # before the body if no H1 present.
    h1_re = re.compile(r'(^#\s+[^\n]*\n)', re.MULTILINE)
    m = h1_re.search(body)
    if not m:
        return text  # nothing safe to anchor on

    body_idx = text.find(body)  # body is a trimmed substring; this is safe
    if body_idx < 0:
        return text
    insert_at = body_idx + m.end()
    before = text[:insert_at]
    after = text[insert_at:]
    sep = "\n" if not before.endswith("\n\n") else ""
    return before + sep + "\n" + banner + "\n" + after.lstrip("\n")


def _slug_to_doi_heuristic(slug: str) -> str | None:
    """Convert a DOI-derived file slug back to a DOI string.

    File slugs encode DOIs by replacing '/' and '.' with '_'.
    This is not perfectly invertible, but we know DOIs start with '10.'
    and that the registrant follows, so we can reconstruct:
       10_1534_genetics_117_300382 → 10.1534/genetics.117.300382

    Approach: split at first '_', yielding '10' and the rest.
    The next segment (up to the next '_') is the registrant (e.g. '1534').
    Everything after registrant+'_' is the suffix, with '_' → '.'.
    This is a heuristic and may mis-reconstruct some DOIs.
    """
    if not slug.startswith("10_"):
        return None
    # Remove leading "10_"
    rest = slug[3:]  # e.g. "1534_genetics_117_300382"
    underscore = rest.find("_")
    if underscore == -1:
        return f"10.{rest}"
    registrant = rest[:underscore]
    suffix_raw = rest[underscore + 1:]
    # The suffix had both '/' and '.' turned into '_'.
    # We can't know which were '/' vs '.', so return None — caller checks slug form
    # in supporting list first.
    _ = registrant
    _ = suffix_raw
    return None  # can't reliably reconstruct; rely on slug-form match


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def _write_briefing(
    kind: str,
    urgency: str,
    title: str,
    content_md: str,
    metadata: dict,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO briefings (kind, urgency, title, content_md, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            kind,
            urgency,
            title,
            content_md,
            json.dumps(metadata),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(results: dict) -> str:
    lines = ["# Wiki integrity audit\n"]

    def _section(heading: str, items: list[str], error: bool = False) -> None:
        tag = " **[ERROR]**" if error else ""
        lines.append(f"## {heading}{tag}\n")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- (none)")
        lines.append("")

    _section(
        f"1. Missing `category:` on topic pages ({len(results['missing_category'])})",
        results["missing_category"],
    )
    _section(
        f"2. Stub titles on paper pages ({len(results['stub_titles'])})",
        results["stub_titles"],
    )
    _section(
        f"3. Title/h1 mismatches ({len(results['title_h1_mismatch'])})",
        results["title_h1_mismatch"],
    )
    _section(
        f"4. Sub-index files present ({len(results['subindex_files'])})",
        results["subindex_files"],
        error=bool(results["subindex_files"]),
    )
    _section(
        f"5. Broken finding-anchor cross-links ({len(results['broken_anchors'])})",
        results["broken_anchors"],
    )
    _section(
        f"6. Orphan topic references in paper frontmatter ({len(results['orphan_topic_refs'])})",
        results["orphan_topic_refs"],
    )
    _section(
        f"7. Silent paper references in topic bodies ({len(results['silent_paper_refs'])})",
        results["silent_paper_refs"],
    )
    _section(
        f"8. Broken-slug paper links ({len(results['broken_slug_links'])})",
        results["broken_slug_links"],
        error=any("[ERROR]" in item for item in results["broken_slug_links"]),
    )

    # Check 9 is a briefing-only summary (no auto-fix items list)
    lines.append(f"## 9. Cross-link opportunities (suggester)\n")
    for line in results.get("cross_link_summary", []):
        lines.append(f"- {line}")
    if not results.get("cross_link_summary"):
        lines.append("- (no summary available)")
    lines.append("")

    _section(
        f"10. Orphan concept-card links ({len(results['orphan_concept_links'])})",
        results["orphan_concept_links"],
    )
    _section(
        f"11. Method-page citations to missing papers ({len(results['method_missing_citations'])})",
        results["method_missing_citations"],
    )
    _section(
        f"12. Orphan freshness banners ({len(results['orphan_freshness'])})",
        results["orphan_freshness"],
    )
    _section(
        f"13. Missing paper→method backlinks ({len(results['paper_method_backlinks'])})",
        results["paper_method_backlinks"],
    )
    _section(
        f"14. papers_supporting format inconsistency ({len(results['papers_supporting_format'])})",
        results["papers_supporting_format"],
    )
    auto_added = results.get("freshness_auto_added") or []
    if auto_added:
        lines.append("### 12a. Freshness banners auto-added this run")
        lines.append("")
        for item in auto_added:
            lines.append(f"- {item}")
        lines.append("")

    total = sum(
        len(v) for k, v in results.items()
        if k not in ("cross_link_summary", "freshness_auto_added")
    )
    lines.append(f"---\n**Total issues: {total}** (check 9 produces candidates only, not counted as issues)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("wiki_janitor")
def job() -> str:
    """Audit the lab wiki for integrity issues and surface them as a briefing."""

    papers_dir = _PAPERS_DIR
    topics_dir = _TOPICS_DIR

    # Count files (excluding index.md)
    n_papers = len([f for f in papers_dir.glob("*.md") if f.name != "index.md"])
    n_topics = len([f for f in topics_dir.glob("*.md") if f.name != "index.md"])
    n_repos = len([f for f in _REPOS_DIR.glob("*.md") if f.name != "index.md"]) if _REPOS_DIR.exists() else 0

    cross_link_summary, cross_link_candidates = check_cross_link_opportunities(
        topics_dir, papers_dir
    )

    auto_add = _auto_add_freshness_enabled()
    freshness_warnings, freshness_added = check_orphan_freshness(
        topics_dir, papers_dir, auto_add=auto_add,
    )

    results: dict[str, list[str]] = {
        "missing_category":        check_missing_category(topics_dir),
        "stub_titles":             check_stub_titles(papers_dir),
        "title_h1_mismatch":       check_title_h1_mismatch(papers_dir),
        "subindex_files":          check_subindex_files(_WIKI_ROOT),
        "broken_anchors":          check_broken_finding_anchors(topics_dir, papers_dir),
        "orphan_topic_refs":       check_orphan_topic_refs(papers_dir, topics_dir),
        "silent_paper_refs":       check_silent_paper_refs(topics_dir, papers_dir),
        "broken_slug_links":       check_broken_slug_links(topics_dir, papers_dir),
        "cross_link_summary":      cross_link_summary,  # summary strings, not counted as issues
        "orphan_concept_links":    check_orphan_concept_links(topics_dir, papers_dir, _CONCEPTS_DIR),
        "method_missing_citations": check_method_missing_citations(_METHODS_DIR, papers_dir),
        "orphan_freshness":        freshness_warnings,
        "freshness_auto_added":    freshness_added,  # informational; not counted as issues
        "paper_method_backlinks":  check_paper_method_backlinks(_METHODS_DIR, papers_dir),
        "papers_supporting_format": check_papers_supporting_format_consistency(topics_dir),
    }

    # cross_link_summary + freshness_auto_added are informational only
    total_issues = sum(
        len(v) for k, v in results.items()
        if k not in ("cross_link_summary", "freshness_auto_added")
    )
    has_errors = bool(results["subindex_files"]) or any(
        "[ERROR]" in item for item in results["broken_slug_links"]
    )

    content_md = _build_report(results)
    briefing_title = f"Wiki integrity audit — {total_issues} issue{'s' if total_issues != 1 else ''} found"
    urgency = "high" if (total_issues > 0 or has_errors) else "low"

    metadata = {
        "papers_audited": n_papers,
        "topics_audited": n_topics,
        "repos_audited": n_repos,
        "counts": {
            k: len(v) for k, v in results.items()
            if k not in ("cross_link_summary",)
        },
        "total_issues": total_issues,
        "cross_link_candidates": cross_link_candidates,
        "freshness_auto_added": freshness_added,
    }

    _write_briefing(
        kind="wiki_janitor",
        urgency=urgency,
        title=briefing_title,
        content_md=content_md,
        metadata=metadata,
    )

    print(content_md)
    print()

    summary = (
        f"audited: {n_papers} papers, {n_topics} topics, {n_repos} repos; "
        f"{total_issues} issue{'s' if total_issues != 1 else ''} found"
    )
    print(summary)
    return summary


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
