"""Grant deadline radar — runs weekly on Mondays at 6am Central.

Scans RSS feeds, NIH RePORTER, and NSF Award Search for grant opportunities,
scores each one against Heath's research profile using Haiku, and surfaces
high-fit opportunities as briefings.

Run manually:
    cd "$HOME/Google Drive/My Drive/00-Lab-Agent"
    python -m agent.jobs.grant_radar
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic
from agent.jobs import tracked
from agent.scheduler import DB_PATH

log = logging.getLogger("tealc.grant_radar")

# Optional feedparser — warn and continue if not installed
try:
    import feedparser as _feedparser
    _FEEDPARSER_OK = True
except ImportError:
    _feedparser = None  # type: ignore[assignment]
    _FEEDPARSER_OK = False
    log.warning(
        "feedparser not installed — RSS feeds will be skipped. "
        "Install with: pip install feedparser>=6.0.10"
    )

SOURCES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "grant_sources.json"
)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

HEATH_PROFILE = """PI — Biology. Two co-equal lanes: evolutionary genomics AND AI-for-Science.

CORE EXPERTISE
- Genome structure evolution: sex chromosomes, karyotype evolution, chromosome dynamics, dosage compensation, epistasis, speciation
- Comparative phylogenetics across arthropods, vertebrates, plants — broad taxon scope, NOT single-organism
- Methods: BiSSE/BAMM/PGLS in R; package author; big-data karyotype databases (6 published, including Tree of Sex)

BUILDING (AI-for-Science lane)
- Tealc: autonomous AI postdoc agent — Chainlit + LangGraph + Claude + SQLite; 50+ scheduled jobs producing real research artifacts
- TraitTrawler: literature-mining at 94-96% accuracy via Sonnet swarms with grounded verbatim-quote validators
- Five Sonnet-swarm dissertation-scale databases planned (Pseudoautosomal Region Atlas, Karyotype Uncertainty Ledger v2, Dosage Compensation Atlas, Epistasis Outcome Ledger, Domestication Catalog)
- Google.org Impact Challenge finalist for autonomous AI scientist + blinded external peer-review experiment

SCORE HIGH (0.6-1.0) — direct fits
- NSF DEB / IOS evolutionary or comparative biology; NSF DBI biological infrastructure
- NSF AI Institutes; NSF CSSI / OAC software-and-cyberinfrastructure; NSF Convergence Accelerator; NAIRR; CISE programs touching biology
- NIH NIGMS R35 MIRA; NHGRI R01/R35; Common Fund Bridge2AI; ODSS data-science programs
- USDA NIFA AFRI Genomics; NASA Astrobiology / Exobiology; DOE Office of Science (AI for Science, SciDAC, KBase, ASCR)
- HHMI, Sloan, BWF, Pew, Schmidt Sciences / AI2050, Open Philanthropy, CZI, Templeton, Dreyfus
- Anything funding: AI agents for research, autonomous experimentation, foundation models for biology, LLMs in science, knowledge extraction from scientific literature, open scientific databases, AI benchmarks for science

SCORE MEDIUM (0.3-0.6) — plausible adjacent fits
- Career-development / mid-career awards (R35-stage)
- Research infrastructure, open-source scientific software, open data, FAIR data
- Method development in genomics or comparative biology
- Computational biology, bioinformatics, machine learning for genomics, network biology
- AI ethics / responsible AI in science (tangential but feasible)
- Cross-disciplinary "team science" or "convergence" calls touching evolution + AI

SCORE LOW (<0.3) — poor fit
- Single-organism deep molecular biology with no comparative angle
- Wet-lab bench molecular / clinical / patient cohort work
- Direct human health, disease intervention, drug discovery without a methods/AI angle
- Pure CS theory or AI safety policy disconnected from scientific applications


HARD ELIGIBILITY GATES — score MUST be 0.0 if any of these apply:
- PI-eligibility geography excludes the United States. Examples: UK-only funders
  (ARIA, UKRI, BBSRC, EPSRC, MRC-UK, Wellcome Trust UK-lead track); EU-only
  funders (ERC, Horizon Europe lead-PI); Germany-only (DFG); Canada-only
  (CIHR, NSERC); Japan-only (JST, JSPS); Australia-only (NHMRC, ARC).
  Heath is at Texas A&M — he cannot be lead PI on these. Partnership or
  collaborator roles are NOT solo applications. Score 0.0, full stop.
- Trainee-only awards (graduate fellowships, postdoc fellowships requiring
  trainee status). Heath is a tenured Associate Professor.
- Citizenship-restricted awards Heath does not qualify for.
- Deadlines already passed.
When a HARD GATE applies: {"fit": 0.0, "reasoning": "INELIGIBLE: <reason>",
"deadline_iso": null}. Do NOT rationalize. Do NOT score above 0.0 because the
funder seems thematically aligned. Do NOT propose partnership workarounds.

Score 0.0-1.0 strictly. Reasoning must cite which lane the grant fits. Output ONLY valid JSON:
{"fit": float, "reasoning": str, "deadline_iso": str|null}"""

# Keywords to filter NIH/NSF results before sending to Haiku.
# Broad on purpose — cheaper to score irrelevant titles via Haiku than to miss
# good fits. The Haiku layer (HEATH_PROFILE) handles fine-grained scoring.
NIH_KEYWORDS = [
    # Evolutionary genomics core
    "evolution", "evolutionary", "genome", "genomic", "genomics",
    "chromosome", "karyotype", "phylogen", "comparative",
    "speciation", "macroevolution", "diversification",
    "sex chromosome", "y chromosome", "x chromosome",
    "dosage compensation", "recombination", "epistasis",
    "biodiversity", "taxonomy", "cytogenetic",
    "population genomics", "comparative genomics", "phylogenomics",
    "genome assembly", "genome evolution", "phylogenetics",
    "trait evolution", "ancestral state",
    # AI / computational / methods (Heath's second lane)
    "artificial intelligence", "machine learning", "deep learning",
    "neural network", "foundation model", "language model",
    "natural language processing", "large language model",
    "knowledge extraction", "literature mining", "text mining",
    "ai agent", "agentic", "autonomous",
    "ai for science", "scientific discovery", "automated discovery",
    "ai scientist", "self-driving lab",
    # Computational biology / infrastructure
    "computational biology", "bioinformatics", "data science",
    "open data", "open source", "scientific software",
    "research infrastructure", "data integration",
    "fair data", "metadata standard",
    "reproducibility", "benchmark",
]

# NSF program element codes (precise filter — known good codes only)
NSF_PROGRAM_ELEMENTS = [
    "7374",  # Evolutionary Processes (DEB)
    "1174",  # Systematics & Biodiversity Science (DEB)
    "7375",  # Phylogenetic Systematics (DEB)
    "7659",  # Symbiosis, Defense and Self-Recognition (IOS)
    "7657",  # Behavioral Systems (IOS)
    "1165",  # ABI Innovation (DBI) — bioinformatics tools
    "7723",  # CSSI (OAC) — sustained scientific software
]

# NSF keyword searches (catches AI Institutes, Convergence Accelerator, NAIRR,
# CSSI calls, AI-for-Science programs that don't share a single program element).
NSF_KEYWORD_QUERIES = [
    "AI Institute",
    "AI for science",
    "autonomous research",
    "foundation model biology",
    "Convergence Accelerator",
    "comparative genomics",
    "phylogenetic",
    "karyotype",
    "biodiversity informatics",
    "scientific software infrastructure",
]

# Fit-score thresholds (Haiku is conservative; lower threshold catches more)
FIT_THRESHOLD_BRIEFING = 0.3   # write a briefing row
FIT_THRESHOLD_NOTIFY   = 0.7   # also fire desktop notification


def _current_fiscal_year() -> int:
    """US federal FY starts Oct 1. FY 2026 = Oct 2025 – Sep 2026."""
    now = datetime.now(timezone.utc)
    return now.year if now.month < 10 else now.year + 1


def _seven_days_ago_str() -> str:
    """Return date string 7 days ago in MM/DD/YYYY format (NSF API format)."""
    d = datetime.now(timezone.utc) - timedelta(days=7)
    return d.strftime("%m/%d/%Y")


_PREFERENCES_PATH = os.path.join(_PROJECT_ROOT, "data", "heath_preferences.md")


def _load_dismiss_context(limit: int = 15) -> str:
    """Pull the most recent dismissed grants (title + reason) from
    preference_signals so Haiku learns what NOT to surface.

    Returns a markdown block, or empty string if no signal data.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        # Handle both legacy int target_id and new "grant_<id>" string target_id.
        rows = conn.execute(
            """
            SELECT g.title, p.user_reason, p.captured_at
            FROM preference_signals p
            LEFT JOIN grant_opportunities g
              ON g.id = CAST(REPLACE(COALESCE(p.target_id, ''), 'grant_', '') AS INTEGER)
            WHERE p.signal_type = 'dismiss'
              AND p.target_kind IN ('grant_opportunity', 'grant')
              AND COALESCE(p.user_reason, '') != ''
            ORDER BY p.captured_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.warning("dismiss-context query failed: %s", exc)
        return ""
    if not rows:
        return ""
    lines = ["RECENT USER DISMISSALS — down-score titles/scopes resembling these:"]
    for title, reason, _captured in rows:
        if not reason:
            continue
        title_short = (title or "(unknown title)")[:90]
        lines.append(f'- "{title_short}" — reason: {reason[:80]}')
    if len(lines) == 1:
        return ""
    lines.append(
        "RULE: if a candidate matches one of these patterns (same scope, same"
        " eligibility blocker, same reason), score it lower than its surface"
        " match would suggest. Do NOT rationalize past Heath's stated reasons."
    )
    return "\n".join(lines)


def _load_consolidated_preferences() -> str:
    """Read the top section of heath_preferences.md (most recent week's
    consolidated dismiss/adopt patterns) so Haiku honors learned rules."""
    try:
        with open(_PREFERENCES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except (FileNotFoundError, OSError):
        return ""
    # Take only the first ~3000 chars (most recent week's section is at the top).
    # The consolidator prepends new sections, so newest is on top.
    snippet = content[:3000].strip()
    if not snippet or "_No consolidated preferences yet" in snippet:
        return ""
    return f"LEARNED PREFERENCES (from past dismissals/adoptions — honor these):\n\n{snippet}"


def _score_entry(client: Anthropic, title: str, desc: str, url: str) -> Optional[dict]:
    """Score an opportunity with Haiku. Returns parsed JSON dict or None.

    The system prompt is HEATH_PROFILE plus two appendices that close the
    feedback loop:
      1. Recent dismissed titles + reasons (last 15) — learns within a single run
      2. Consolidated preferences from past weeks — durable rules
    """
    system_parts = [HEATH_PROFILE]
    learned = _load_consolidated_preferences()
    if learned:
        system_parts.append(learned)
    recent = _load_dismiss_context(limit=15)
    if recent:
        system_parts.append(recent)
    system = "\n\n---\n\n".join(system_parts)

    try:
        judgement = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": f"{title}\n\n{desc}\n\n{url}"}],
        )
        raw_text = judgement.content[0].text
    except Exception as exc:
        log.warning("Haiku API error for [%s]: %s", title[:60], exc)
        return None

    try:
        # Strip markdown code fences if present (model sometimes wraps JSON)
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop first line (```json or ```) and last line (```)
            inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
            text = inner.strip()
        return json.loads(text)
    except Exception:
        log.warning("Haiku JSON parse failed for [%s]: %r", title[:60], raw_text[:200])
        return None


def _insert_opportunity(
    conn: sqlite3.Connection,
    source: str,
    title: str,
    url: str,
    desc: str,
    j: dict,
) -> bool:
    """Insert into grant_opportunities. Returns True if newly inserted."""
    fit_score   = float(j.get("fit", 0))
    deadline    = j.get("deadline_iso") or None
    reasoning   = j.get("reasoning", "")

    try:
        conn.execute(
            "INSERT OR IGNORE INTO grant_opportunities "
            "(source, program, title, deadline_iso, url, description, "
            " fit_score, fit_reasoning, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source,
                source,
                title,
                deadline,
                url,
                desc[:500],
                fit_score,
                reasoning,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return conn.execute("SELECT changes()").fetchone()[0] > 0
    except Exception as exc:
        log.warning("DB insert failed for [%s]: %s", title[:60], exc)
        return False


def _create_briefing(conn: sqlite3.Connection, title: str, url: str, j: dict):
    """Insert a briefing row for a high-fit opportunity."""
    fit       = j.get("fit", 0)
    reasoning = j.get("reasoning", "")
    deadline  = j.get("deadline_iso") or "unknown"
    body = (
        f"**{title}**\n\n"
        f"Fit score: {fit:.2f} — {reasoning}\n\n"
        f"Deadline: {deadline}\n\n"
        f"{url}"
    )
    conn.execute(
        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
        "VALUES ('grant_radar', 'info', ?, ?, ?)",
        (
            f"Funding opportunity: {title[:60]}",
            body,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _maybe_notify(title: str, url: str, j: dict):
    """Fire desktop notification for very high-fit opportunities (>= 0.8)."""
    try:
        from agent.notify import notify  # noqa: PLC0415
        reasoning = j.get("reasoning", "")
        deadline  = j.get("deadline_iso") or "unknown"
        body = f"Fit {j.get('fit', 0):.2f} | Deadline: {deadline} | {reasoning[:120]}"
        notify("warn", "High-fit grant", f"{title[:70]}\n{body}")
    except Exception as exc:
        log.warning("notify() failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Source legs
# ---------------------------------------------------------------------------

def _collect_rss(conn: sqlite3.Connection, client: Anthropic, sources: dict) -> int:
    """Collect from RSS feeds. Returns new_count."""
    if not _FEEDPARSER_OK:
        log.warning("Skipping RSS leg: feedparser not available")
        return 0

    new_count = 0
    for feed in sources.get("rss_feeds", []):
        feed_name   = feed.get("name", "unknown")
        feed_url    = feed.get("url", "")
        filter_terms = feed.get("filter_terms", [])

        try:
            d = _feedparser.parse(
                feed_url,
                request_headers={"User-Agent": "Mozilla/5.0 Tealc"},
            )
        except Exception as exc:
            log.warning("Feed fetch failed [%s]: %s", feed_name, exc)
            continue

        entries = d.get("entries", [])
        if not entries:
            log.info("Feed [%s] returned 0 entries", feed_name)

        for entry in entries[:50]:
            title = entry.get("title", "").strip()
            url   = entry.get("link", "").strip()
            desc  = (entry.get("summary") or entry.get("description") or "")[:1500]

            if not title or not url:
                continue

            combined = (title + " " + desc).lower()
            if filter_terms and not any(t.lower() in combined for t in filter_terms):
                continue

            existing = conn.execute(
                "SELECT id FROM grant_opportunities WHERE source=? AND title=?",
                (feed_name, title),
            ).fetchone()
            if existing:
                continue

            j = _score_entry(client, title, desc, url)
            if j is None:
                continue

            fit_score = float(j.get("fit", 0))
            inserted  = _insert_opportunity(conn, feed_name, title, url, desc, j)
            if not inserted:
                continue

            new_count += 1
            if fit_score >= FIT_THRESHOLD_BRIEFING:
                try:
                    _create_briefing(conn, title, url, j)
                    conn.commit()
                    log.info("High-fit RSS opportunity: %.2f — %s", fit_score, title[:60])
                except Exception as exc:
                    log.warning("Briefing insert failed: %s", exc)
            if fit_score >= FIT_THRESHOLD_NOTIFY:
                _maybe_notify(title, url, j)

    return new_count


def _collect_nih_grants(conn: sqlite3.Connection, client: Anthropic) -> int:
    """Collect from NIH RePORTER API. Returns new_count."""
    try:
        from agent.apis.grants import nih_search_awards  # noqa: PLC0415
    except ImportError as exc:
        log.warning("NIH API import failed: %s", exc)
        return 0

    current_fy = _current_fiscal_year()
    log.info("NIH leg: querying FY %d, activity codes R01/R35/R21/R15", current_fy)

    try:
        awards = nih_search_awards(
            fiscal_years=[current_fy],
            activity_codes=["R01", "R35", "R21", "R15"],
            limit=50,
        )
    except Exception as exc:
        log.warning("NIH API call failed: %s", exc)
        return 0

    log.info("NIH leg: %d raw results", len(awards))
    new_count = 0

    for award in awards:
        title = (award.get("project_title") or "").strip()
        abstract = (award.get("abstract_text") or "").strip()
        appl_id = award.get("appl_id") or ""
        project_num = award.get("project_num") or str(appl_id)

        if not title:
            continue

        # Build URL
        url = (
            f"https://reporter.nih.gov/project-details/{appl_id}"
            if appl_id
            else f"https://reporter.nih.gov/search/{project_num}"
        )

        # Filter by keyword match
        combined = (title + " " + abstract).lower()
        if not any(kw.lower() in combined for kw in NIH_KEYWORDS):
            continue

        # Skip if already seen
        existing = conn.execute(
            "SELECT id FROM grant_opportunities WHERE source='NIH RePORTER' AND title=?",
            (title,),
        ).fetchone()
        if existing:
            continue

        desc = abstract[:1500]
        j = _score_entry(client, title, desc, url)
        if j is None:
            continue

        fit_score = float(j.get("fit", 0))
        inserted  = _insert_opportunity(conn, "NIH RePORTER", title, url, desc, j)
        if not inserted:
            continue

        new_count += 1
        if fit_score >= FIT_THRESHOLD_BRIEFING:
            try:
                _create_briefing(conn, title, url, j)
                conn.commit()
                log.info("High-fit NIH opportunity: %.2f — %s", fit_score, title[:60])
            except Exception as exc:
                log.warning("Briefing insert failed: %s", exc)
        if fit_score >= FIT_THRESHOLD_NOTIFY:
            _maybe_notify(title, url, j)

    return new_count


def _process_nsf_award(conn: sqlite3.Connection, client: Anthropic, award: dict) -> bool:
    """Score + insert one NSF award. Returns True if newly inserted."""
    title    = (award.get("title") or "").strip()
    abstract = (award.get("abstract_text") or "").strip()
    award_id = award.get("award_id") or ""

    if not title:
        return False

    url = (
        f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_id}"
        if award_id
        else "https://www.nsf.gov/awardsearch/"
    )

    # Skip if already seen
    existing = conn.execute(
        "SELECT id FROM grant_opportunities WHERE source='NSF Award Search' AND title=?",
        (title,),
    ).fetchone()
    if existing:
        return False

    desc = abstract[:1500]
    j = _score_entry(client, title, desc, url)
    if j is None:
        return False

    fit_score = float(j.get("fit", 0))
    inserted  = _insert_opportunity(conn, "NSF Award Search", title, url, desc, j)
    if not inserted:
        return False

    if fit_score >= FIT_THRESHOLD_BRIEFING:
        try:
            _create_briefing(conn, title, url, j)
            conn.commit()
            log.info("High-fit NSF opportunity: %.2f — %s", fit_score, title[:60])
        except Exception as exc:
            log.warning("Briefing insert failed: %s", exc)
    if fit_score >= FIT_THRESHOLD_NOTIFY:
        _maybe_notify(title, url, j)
    return True


def _collect_nsf_grants(conn: sqlite3.Connection, client: Anthropic) -> int:
    """Collect from NSF Award Search API via program-element + keyword legs."""
    try:
        from agent.apis.grants import nsf_search_awards  # noqa: PLC0415
    except ImportError as exc:
        log.warning("NSF API import failed: %s", exc)
        return 0

    date_start = _seven_days_ago_str()
    new_count = 0

    # Leg 1: program-element search (precise targeting)
    log.info("NSF program-element leg: querying since %s for %d codes",
             date_start, len(NSF_PROGRAM_ELEMENTS))
    for prog_elem in NSF_PROGRAM_ELEMENTS:
        try:
            awards = nsf_search_awards(
                program_element=prog_elem,
                date_start=date_start,
                limit=25,
            )
        except Exception as exc:
            log.warning("NSF API call failed for element %s: %s", prog_elem, exc)
            continue
        log.info("NSF element %s: %d raw results", prog_elem, len(awards))
        for award in awards:
            if _process_nsf_award(conn, client, award):
                new_count += 1

    # Leg 2: keyword search (catches AI Institutes, Convergence Accelerator,
    # NAIRR, CSSI calls — programs not pinned to a single element code)
    log.info("NSF keyword leg: querying %d AI/science queries since %s",
             len(NSF_KEYWORD_QUERIES), date_start)
    for query in NSF_KEYWORD_QUERIES:
        try:
            awards = nsf_search_awards(
                query=query,
                date_start=date_start,
                limit=25,
            )
        except Exception as exc:
            log.warning("NSF API call failed for query %r: %s", query, exc)
            continue
        log.info("NSF keyword %r: %d raw results", query, len(awards))
        for award in awards:
            if _process_nsf_award(conn, client, award):
                new_count += 1

    return new_count


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("grant_radar")
def job(force: bool = False):
    """Main grant radar job — fetches feeds + APIs, scores, stores results."""
    if not os.path.exists(SOURCES_PATH):
        log.warning("grant_sources.json not found at %s — skipping", SOURCES_PATH)
        return "skipped: no sources file"

    sources = json.load(open(SOURCES_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    client = Anthropic()

    # Run each leg independently — one failure must not kill the others
    rss_count = 0
    try:
        rss_count = _collect_rss(conn, client, sources)
    except Exception as exc:
        log.warning("RSS leg crashed: %s", exc)

    nih_count = 0
    try:
        nih_count = _collect_nih_grants(conn, client)
    except Exception as exc:
        log.warning("NIH leg crashed: %s", exc)

    nsf_count = 0
    try:
        nsf_count = _collect_nsf_grants(conn, client)
    except Exception as exc:
        log.warning("NSF leg crashed: %s", exc)

    conn.commit()
    conn.close()

    new_total = rss_count + nih_count + nsf_count
    summary = (
        f"new_opportunities={new_total} "
        f"(rss={rss_count} nih={nih_count} nsf={nsf_count})"
    )
    log.info("Grant radar complete: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    job()
