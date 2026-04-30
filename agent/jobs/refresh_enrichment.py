"""Refresh wiki enrichment sections: cross-links + external DOI + author crosswalk.

Every paper page and topic page in the lab's GitHub Pages /knowledge/ gets a
deterministic enrichment section written into the `tealc:related-*` marker
region. Content between those markers is rewritten each run; content outside
them (including the tealc:auto region on topic pages) is preserved.

For paper pages, the enrichment section includes:
  - "Read the paper" — external doi.org link, only when the DOI is real
  - "Related papers on this site" — other papers sharing ≥2 topic_tags, ranked
    by overlap count
  - "Other papers by these authors" — links to author index pages for any
    co-author who has an author page (2+ papers in the wiki)

For topic pages, the enrichment section includes:
  - "Related topics on this site" — other topics sharing ≥2 papers, ranked by
    overlap count

Always deterministic. Zero LLM cost. Idempotent — re-running produces byte-
identical output unless DB / frontmatter has changed.

Manual run (recommended after adding a new paper):
    PYTHONPATH=/path/to/00-Lab-Agent ~/.lab-agent-venv/bin/python \\
        -m agent.jobs.refresh_enrichment

Scheduled: registered as a weekly cron in agent/scheduler.py — runs every
Tuesday 8am Central (after wiki_janitor Monday).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.jobs.website_git import website_repo_path  # noqa: E402
from agent.jobs.wiki_pipeline import splice_related_region  # noqa: E402


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Frontmatter values are strings.
    List fields (topics, papers_supporting) stay as raw list strings for the
    caller to split. Empty dict if frontmatter is missing/malformed."""
    if not text.startswith("---"):
        return ({}, text)
    end = text.find("\n---", 3)
    if end < 0:
        return ({}, text)
    fm_raw = text[3:end]
    body = text[end + 4:]
    fm: dict = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if ":" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes for scalars; keep [..] list strings intact
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        fm[key] = val
    return (fm, body)


def _parse_list_field(raw: str) -> list[str]:
    """Parse a YAML list-in-line like '[a, b, c]' into ['a','b','c']."""
    if not raw:
        return []
    v = raw.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip().strip('"').strip("'") for x in v.split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Author parsing (mirrors what the author-index agent did)
# ---------------------------------------------------------------------------

_AUTHOR_SPLIT = re.compile(r"\s*[,;]\s*|\s+and\s+")


def _parse_authors(authors_raw: str) -> list[tuple[str, str]]:
    """Split an authors string into (surname, initials) tuples.

    Supports "Surname I" and "First Surname" and "First M Surname" forms.
    Skips consortium-style names (single token, or containing 'Consortium').
    """
    out: list[tuple[str, str]] = []
    if not authors_raw:
        return out
    for piece in _AUTHOR_SPLIT.split(authors_raw):
        piece = piece.strip()
        if not piece:
            continue
        if "consortium" in piece.lower() or "group" in piece.lower():
            continue
        tokens = piece.split()
        if len(tokens) < 2:
            # single token — could be "Smith" but ambiguous; skip
            continue
        # Distinguish two author-order conventions. Look at FIRST token too,
        # not just last, to avoid misreading "H Smith" as surname=H.
        last = tokens[-1]
        first = tokens[0]
        last_raw = last.replace(".", "")
        first_raw = first.replace(".", "")
        last_is_initials = bool(
            re.fullmatch(r"[A-Z]{1,4}", last_raw)
        )
        first_is_initial_only = bool(
            re.fullmatch(r"[A-Z]{1,2}", first_raw)
        )

        if last_is_initials and not first_is_initial_only:
            # "Jonika MM" / "Smith H" — surname first, initials last
            surname = first
            initials = last_raw
        else:
            # "Michelle Jonika" / "H Smith" / "Michelle M Jonika" — surname last
            surname = last
            # Build initials from first-token + middle tokens (first letters)
            initials_list: list[str] = []
            for tok in tokens[:-1]:
                tok_clean = tok.replace(".", "").strip()
                if tok_clean:
                    initials_list.append(tok_clean[0].upper())
            initials = "".join(initials_list)
        out.append((surname, initials))
    return out


def _author_slug(surname: str, initials: str) -> str:
    surname_norm = re.sub(r"[^a-zA-Z]", "", surname).lower()
    initials_norm = re.sub(r"[^a-zA-Z]", "", initials).lower()
    if initials_norm:
        return f"{surname_norm}_{initials_norm}"
    return surname_norm


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------

@dataclass
class PaperInfo:
    slug: str
    path: str
    title: str
    year: str
    authors_raw: str
    doi: str
    fingerprint: str
    topic_tags: list[str] = field(default_factory=list)
    author_tuples: list[tuple[str, str]] = field(default_factory=list)
    cite_short: str = ""

    @property
    def permalink(self) -> str:
        return f"/knowledge/papers/{self.slug}/"


@dataclass
class TopicInfo:
    slug: str
    path: str
    title: str
    category: str
    papers_supporting: list[str] = field(default_factory=list)

    @property
    def permalink(self) -> str:
        return f"/knowledge/topics/{self.slug}/"


def _short_cite(p: PaperInfo) -> str:
    """Build a short 'Jonika et al. 2020' style citation from parsed authors."""
    if not p.author_tuples:
        return p.title[:60] + "…" if len(p.title) > 60 else p.title
    surnames = [sn for sn, _ in p.author_tuples]
    year = p.year or ""
    if len(surnames) == 1:
        base = surnames[0]
    elif len(surnames) == 2:
        base = f"{surnames[0]} & {surnames[1]}"
    else:
        base = f"{surnames[0]} et al."
    return f"{base} {year}".strip()


def _load_papers(repo: str) -> dict[str, PaperInfo]:
    papers_dir = os.path.join(repo, "knowledge", "papers")
    out: dict[str, PaperInfo] = {}
    for name in sorted(os.listdir(papers_dir)):
        if not name.endswith(".md") or name == "index.md":
            continue
        path = os.path.join(papers_dir, name)
        slug = name[:-3]
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm, _ = _parse_frontmatter(text)
        if not fm:
            continue
        p = PaperInfo(
            slug=slug, path=path,
            title=fm.get("title", "") or "",
            year=fm.get("year", "") or "",
            authors_raw=fm.get("authors", "") or "",
            doi=fm.get("doi", "") or "",
            fingerprint=fm.get("fingerprint_sha256", "") or "",
            topic_tags=_parse_list_field(fm.get("topics", "")),
            author_tuples=_parse_authors(fm.get("authors", "") or ""),
        )
        p.cite_short = _short_cite(p)
        out[slug] = p
    return out


def _load_topics(repo: str) -> dict[str, TopicInfo]:
    topics_dir = os.path.join(repo, "knowledge", "topics")
    out: dict[str, TopicInfo] = {}
    for name in sorted(os.listdir(topics_dir)):
        if not name.endswith(".md") or name == "index.md":
            continue
        path = os.path.join(topics_dir, name)
        slug = name[:-3]
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm, _ = _parse_frontmatter(text)
        out[slug] = TopicInfo(
            slug=slug, path=path,
            title=fm.get("title", "") or slug.replace("_", " ").title(),
            category=fm.get("category", "") or "",
            papers_supporting=_parse_list_field(fm.get("papers_supporting", "")),
        )
    return out


def _load_author_slugs(repo: str) -> set[str]:
    """Author index pages that already exist — only link to those."""
    authors_dir = os.path.join(repo, "knowledge", "authors")
    if not os.path.isdir(authors_dir):
        return set()
    return {
        name[:-3]
        for name in os.listdir(authors_dir)
        if name.endswith(".md") and name != "index.md"
    }


# ---------------------------------------------------------------------------
# Enrichment composition
# ---------------------------------------------------------------------------

def _paper_related_section(
    paper: PaperInfo,
    all_papers: dict[str, PaperInfo],
    author_slugs: set[str],
) -> str:
    """Compose the tealc:related region content for a paper page."""
    parts: list[str] = []

    # 1. External DOI link (only if real DOI — skip sha256: pseudo-DOIs)
    if paper.doi and not paper.doi.startswith("sha256:") and paper.doi != "":
        parts.append("## Read the paper")
        parts.append("")
        parts.append(f"[doi.org/{paper.doi}](https://doi.org/{paper.doi})")
        parts.append("")

    # 2. Related papers — papers sharing ≥2 topic_tags
    my_topics = set(paper.topic_tags)
    candidates: list[tuple[int, PaperInfo]] = []
    for other_slug, other in all_papers.items():
        if other_slug == paper.slug:
            continue
        overlap = len(my_topics & set(other.topic_tags))
        if overlap >= 2:
            candidates.append((overlap, other))
    candidates.sort(key=lambda kv: (-kv[0], kv[1].year, kv[1].slug))

    if candidates:
        parts.append("## Related papers on this site")
        parts.append("")
        for n, other in candidates[:10]:  # cap at top 10
            shared = sorted(my_topics & set(other.topic_tags))
            shared_str = ", ".join(shared[:3]) + (" …" if len(shared) > 3 else "")
            parts.append(
                f"- [{other.cite_short}]({other.permalink}) — {n} shared topic"
                f"{'s' if n != 1 else ''}"
                f" ({shared_str})"
            )
        parts.append("")

    # 3. Other papers by these authors (only if author has an index page)
    author_links: list[str] = []
    seen_slugs: set[str] = set()
    for surname, initials in paper.author_tuples:
        slug = _author_slug(surname, initials)
        # Match slug with initials; fall back to surname-only slug
        if slug in author_slugs and slug not in seen_slugs:
            author_links.append(f"- [{surname} {initials}](/knowledge/authors/{slug}/)")
            seen_slugs.add(slug)
        elif surname.lower() in author_slugs and surname.lower() not in seen_slugs:
            author_links.append(f"- [{surname}](/knowledge/authors/{surname.lower()}/)")
            seen_slugs.add(surname.lower())

    if author_links:
        parts.append("## Other papers by these authors")
        parts.append("")
        parts.extend(author_links)
        parts.append("")

    return "\n".join(parts).strip()


def _build_paper_canon_map(
    all_papers: dict[str, "PaperInfo"],
) -> dict[str, str]:
    """Build a lookup that normalizes every observed papers_supporting entry
    form (raw DOI, bare 64-char SHA256, 'sha256:'-prefixed) to a single
    canonical key for set-intersection.

    Canonical preference: fingerprint SHA (most papers carry it).  Fall back
    to DOI when fingerprint is missing.  Unknown entries pass through
    unchanged via dict.get(e, e) at the call site.
    """
    canon: dict[str, str] = {}
    for slug, p in all_papers.items():
        sha = (p.fingerprint or "").strip().lower()
        doi = (p.doi or "").strip()
        key = sha if sha else doi
        if not key:
            continue
        if doi:
            canon[doi] = key
        if sha:
            canon[sha] = key
            canon[f"sha256:{sha}"] = key
    return canon


def _topic_related_section(
    topic: TopicInfo,
    all_topics: dict[str, TopicInfo],
    all_papers: dict[str, "PaperInfo"],
) -> str:
    """Compose the tealc:related region content for a topic page.

    Uses papers_supporting sets to compute topic-topic overlap (≥2 shared
    papers). Ranked by overlap count descending.

    papers_supporting entries across the wiki mix three identifier formats:
    raw DOI ("10.1234/foo"), bare 64-char SHA256 hex, and "sha256:"-prefixed
    strings.  We canonicalize every entry to the paper's SHA256 fingerprint
    (looked up against papers/*.md frontmatter) before set-intersection so
    that a topic listing a paper by DOI and another topic listing the same
    paper by SHA256 are correctly recognized as sharing that paper.
    """
    parts: list[str] = []
    canon = _build_paper_canon_map(all_papers)

    def _canon_set(entries: list[str]) -> set[str]:
        return {canon.get(e, e) for e in entries}

    my_papers = _canon_set(topic.papers_supporting)

    candidates: list[tuple[int, TopicInfo]] = []
    for other_slug, other in all_topics.items():
        if other_slug == topic.slug:
            continue
        overlap = len(my_papers & _canon_set(other.papers_supporting))
        if overlap >= 2:
            candidates.append((overlap, other))
    candidates.sort(key=lambda kv: (-kv[0], kv[1].slug))

    if candidates:
        parts.append("## Related topics on this site")
        parts.append("")
        for n, other in candidates[:12]:
            parts.append(
                f"- [{other.title}]({other.permalink}) — {n} shared paper"
                f"{'s' if n != 1 else ''}"
            )

    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _apply_enrichment(repo: str) -> dict:
    papers = _load_papers(repo)
    topics = _load_topics(repo)
    author_slugs = _load_author_slugs(repo)

    stats = {
        "papers_updated": 0,
        "papers_unchanged": 0,
        "topics_updated": 0,
        "topics_unchanged": 0,
    }

    # Paper pages
    for slug, paper in papers.items():
        new_section = _paper_related_section(paper, papers, author_slugs)
        with open(paper.path, encoding="utf-8") as f:
            text = f.read()
        new_text = splice_related_region(text, new_section)
        if new_text != text:
            with open(paper.path, "w", encoding="utf-8") as f:
                f.write(new_text)
            stats["papers_updated"] += 1
        else:
            stats["papers_unchanged"] += 1

    # Topic pages
    for slug, topic in topics.items():
        new_section = _topic_related_section(topic, topics, papers)
        with open(topic.path, encoding="utf-8") as f:
            text = f.read()
        new_text = splice_related_region(text, new_section)
        if new_text != text:
            with open(topic.path, "w", encoding="utf-8") as f:
                f.write(new_text)
            stats["topics_updated"] += 1
        else:
            stats["topics_unchanged"] += 1

    stats["papers_total"] = len(papers)
    stats["topics_total"] = len(topics)
    stats["author_pages_found"] = len(author_slugs)
    return stats


@tracked("refresh_enrichment")
def job() -> str:
    repo = website_repo_path()
    stats = _apply_enrichment(repo)
    return (
        f"enrichment refreshed: "
        f"{stats['papers_updated']}/{stats['papers_total']} papers updated, "
        f"{stats['topics_updated']}/{stats['topics_total']} topics updated, "
        f"{stats['author_pages_found']} author pages referenced"
    )


if __name__ == "__main__":
    import json
    repo = website_repo_path()
    stats = _apply_enrichment(repo)
    print(json.dumps(stats, indent=2))
