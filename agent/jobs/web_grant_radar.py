"""Web-search-based grant discovery for the researcher.

Complements the API-based grant_radar (NIH RePORTER + NSF Award Search + RSS)
by using Claude's web_search tool to discover the long tail: society awards,
foundation small grants, TAMU internal funding, AI-for-Science seed funding,
named lectureships, workshop grants, etc. — anything not in a structured API.

Inserts into the same `grant_opportunities` table; reuses the same Haiku
scoring (HEATH_PROFILE) and briefing pipeline. Surfaces in the dashboard
Inbox like everything else.

Schedule: weekly, Mondays 7am Central (after grant_radar at 6am).
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic
from agent.jobs import tracked
from agent.scheduler import DB_PATH
from agent.jobs.grant_radar import (
    HEATH_PROFILE, _score_entry, _insert_opportunity,
    _create_briefing, _maybe_notify,
    FIT_THRESHOLD_BRIEFING, FIT_THRESHOLD_NOTIFY,
)

log = logging.getLogger("tealc.web_grant_radar")

SONNET_MODEL = "claude-sonnet-4-6"

DISCOVERY_PROMPT_TEMPLATE = """You are a grant scout for the researcher, a tenured Biology PI. Your job: find smaller awards, fellowships, prizes, internal funding, society awards, and niche calls they would be a strong candidate for. NOT mega-grants like NSF DEB or NIH R01 — those are tracked separately.

WHO HE IS:
{heath_profile}

WHAT YOU'RE SEARCHING THIS RUN:
{category_focus}

TARGET TYPES (focus on these):
- Society awards / prizes / named lectureships (SSE, AGA, ASN, ASIH, GSA, ESA, NABT, ASM, FORCE11, etc.)
- TAMU internal seed grants (T3, X-Grants, AgriLife, IDA, COS seed, T-AGS)
- Foundation small grants under $100k (BWF, Sloan, Pew, Templeton seed, Dreyfus, Dana, Whitehall, M.J. Murdock, Klingenstein, RGK)
- AI-for-Science seed funding for US-based PIs (Schmidt Sciences, AI2050, Open Philanthropy, CZI, Microsoft AI for Good, Convergent Research, Astera, FRO)
- Database/software/data awards (FORCE11, Zenodo, eLife innovation, NumFOCUS, Open Source Awards, Better Scientific Software)
- Travel/lecture/visiting awards, named lectureships, distinguished-speaker invitations
- Workshop/RCN/symposium/conference seed grants (NSF RCN, NIH R13, Burroughs Wellcome workshop, GRC seed, AGA Special Event)
- Mid-career awards for tenured PIs (Guggenheim, Kavli Fellowship, Sloan Mid-Career, AAAS Fellow nomination, NAS Kavli Frontiers)
- Open calls under $100k for genomics / evolution / open science / autonomous AI research

EXCLUDE:
- Standard NIH R01/R21/R35 (tracked separately)
- Standard NSF DEB/IOS program calls (tracked separately)
- Anything requiring trainee status (postdoc fellowships for trainees)
- Anything closed in the past
- Anything not currently accepting applications in 2026
- Funders whose lead-PI eligibility is restricted to non-US countries: UK (ARIA, UKRI, BBSRC, EPSRC, Wellcome Trust UK-lead); EU (ERC, Horizon Europe lead-PI); Germany (DFG); Canada (CIHR, NSERC); Japan (JST, JSPS); Australia (NHMRC, ARC). Heath is at Texas A&M and cannot lead these applications.

USE web_search aggressively. Do 3-5 searches if needed. For promising hits, search the sponsor's website to find exact deadline + URL + eligibility.

OUTPUT FORMAT — return ONLY a valid JSON array. No preamble. No closing remarks. No markdown fences. Begin response with `[` and end with `]`. Each object has these exact fields:

[
  {{
    "title": "<grant/award name>",
    "sponsor": "<funder/society/institution>",
    "url": "https://...",
    "amount": "<e.g. '$50k', 'up to $25k', null if unknown>",
    "deadline_iso": "<YYYY-MM-DD or null if unknown>",
    "eligibility_note": "<who can apply — e.g. 'tenured faculty in evolutionary biology, US citizen, mid-career'>",
    "why_fits": "<1 sentence on why Heath specifically fits>"
  }}
]

If no good candidates found after searching, return []. Do not invent candidates."""

QUERIES = [
    {
        "category": "society_awards",
        "focus": "Society awards, prizes, named lectureships in evolutionary biology, genetics, or phylogenetics that Heath could be nominated for or self-apply to in 2026 (SSE Dobzhansky Prize, AGA awards, GSA Genetics Society awards, ASN Presidents Award, ASIH Stoye Award, etc.)",
    },
    {
        "category": "tamu_internal",
        "focus": "TAMU (Texas A&M University) internal funding programs with 2026 deadlines: T3 (Tier 3) grants, X-Grants, AgriLife internal, College of Arts & Sciences seed funds, Institute for Data Science seed grants, Hagler Institute, T-AGS faculty fellows.",
    },
    {
        "category": "foundation_small_grants",
        "focus": "Foundation seed grants and small awards (under $100k) for evolutionary biology, comparative genomics, or open science with 2026 deadlines: Burroughs Wellcome Career Awards, Sloan Research Fellowship, Pew Biomedical Scholars, Templeton Foundation small grants, Dreyfus Foundation, Whitehall Foundation, M.J. Murdock Charitable Trust, Klingenstein-Simons.",
    },
    {
        "category": "ai_for_science_seed",
        "focus": "AI-for-Science fellowships and seed awards in 2026: Schmidt Sciences AI2050, Schmidt Polymathic Fellowship, Open Philanthropy AI grants, CZI Open Science, Microsoft AI for Good academic, Convergent Research FROs, Astera Institute, NSF Convergence Accelerator, NSF AI Institutes (PI roles).",
    },
    {
        "category": "database_software_awards",
        "focus": "Awards/grants for open scientific databases, software tools, FAIR data, reproducibility in 2026: FORCE11 awards, Zenodo Community grants, eLife innovation, NumFOCUS small development grants, Better Scientific Software, Open Source Awards, NIH ODSS data ecosystem, NSF DBI sustaining/innovation.",
    },
    {
        "category": "midcareer_awards",
        "focus": "Mid-career awards for tenured PIs in evolutionary biology, comparative biology, or genomics with 2026 deadlines: Guggenheim Fellowship (natural sciences), Kavli Fellowships, AAAS Fellow nomination process, NAS Kavli Frontiers of Science, Simons Investigator MMLS, Radcliffe Institute Fellowship, Center for Advanced Study fellowships.",
    },
    {
        "category": "workshop_rcn",
        "focus": "Workshop, RCN, conference, and meeting grants for 2026: NSF RCN (Research Coordination Networks), NIH R13 conference grants, Burroughs Wellcome Innovations in Regulatory Science workshop grants, AGA Special Event, GSA conference grants, NSF small workshop grants, GRC/Gordon Research Conference seed.",
    },
    {
        "category": "applied_ai_research",
        "focus": "Funding for autonomous AI agents in research, LLMs for scientific literature, AI scientists, automated discovery, agentic systems for science with 2026 deadlines: Schmidt Futures, Convergent Research, Future of Research Organizations, Anthropic research grants, OpenAI Researcher Access Program (with funding), IARPA programs touching science automation.",
    },
]


def _extract_json_array(raw: str) -> list:
    """Lenient JSON-array extraction — strips fences, finds outer brackets."""
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    # Find outermost array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    candidate = text[start:end + 1]
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _discover(client: Anthropic, category: str, focus: str) -> list[dict]:
    """One Sonnet + web_search call. Returns a list of candidate dicts."""
    system = DISCOVERY_PROMPT_TEMPLATE.format(
        heath_profile=HEATH_PROFILE,
        category_focus=focus,
    )
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            system=system,
            messages=[{
                "role": "user",
                "content": f"Find grants/awards in category '{category}'. Focus: {focus}\n\nReturn a JSON array of candidates only.",
            }],
        )
    except Exception as exc:
        log.warning("Sonnet/web_search call failed for %s: %s", category, exc)
        return []

    text_blocks = [getattr(b, "text", None) for b in response.content if getattr(b, "type", "") == "text"]
    text_blocks = [t for t in text_blocks if t]
    if not text_blocks:
        log.warning("No text response from Sonnet for %s", category)
        return []

    candidates = _extract_json_array(text_blocks[-1])
    if not candidates:
        log.warning("JSON parse returned empty for %s. Raw[:200]: %r", category, text_blocks[-1][:200])
    return candidates


def _process_candidate(conn: sqlite3.Connection, client: Anthropic, category: str, cand: dict) -> bool:
    """Score + insert one web-discovered candidate. Returns True if newly inserted."""
    title = (cand.get("title") or "").strip()
    url = (cand.get("url") or "").strip()
    if not title or not url:
        return False

    # Cross-source dedup: skip if any existing row has this URL
    existing = conn.execute(
        "SELECT id FROM grant_opportunities WHERE url=?",
        (url,),
    ).fetchone()
    if existing:
        return False

    # Build description from candidate fields for Haiku scoring
    desc_parts = [
        f"Sponsor: {cand.get('sponsor') or 'unknown'}",
        f"Amount: {cand.get('amount') or 'unspecified'}",
        f"Deadline: {cand.get('deadline_iso') or 'unspecified'}",
        f"Eligibility: {cand.get('eligibility_note') or 'see source'}",
        f"Why Tealc thinks Heath fits: {cand.get('why_fits') or ''}",
    ]
    desc = "\n".join(desc_parts)

    j = _score_entry(client, title, desc, url)
    if j is None:
        return False

    # If Haiku didn't extract a deadline, use the candidate's deadline_iso
    if not j.get("deadline_iso") and cand.get("deadline_iso"):
        j["deadline_iso"] = cand["deadline_iso"]

    fit_score = float(j.get("fit", 0))
    source = f"web:{category}"
    inserted = _insert_opportunity(conn, source, title, url, desc, j)
    if not inserted:
        return False

    if fit_score >= FIT_THRESHOLD_BRIEFING:
        try:
            _create_briefing(conn, title, url, j)
            conn.commit()
            log.info("High-fit web opportunity (%s): %.2f — %s", category, fit_score, title[:60])
        except Exception as exc:
            log.warning("Briefing insert failed: %s", exc)
    if fit_score >= FIT_THRESHOLD_NOTIFY:
        _maybe_notify(title, url, j)
    return True


@tracked("web_grant_radar")
def job(force: bool = False, categories: list[str] | None = None) -> str:
    """Run the web_grant_radar pipeline.

    `categories` (optional): restrict to a subset of QUERIES["category"].
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    client = Anthropic()
    new_count = 0
    by_category: dict[str, int] = {}

    queries_to_run = QUERIES
    if categories:
        wanted = set(categories)
        queries_to_run = [q for q in QUERIES if q["category"] in wanted]

    log.info("web_grant_radar: running %d categories", len(queries_to_run))

    for q in queries_to_run:
        category = q["category"]
        focus = q["focus"]
        log.info("web_grant_radar: category=%s", category)
        try:
            candidates = _discover(client, category, focus)
        except Exception as exc:
            log.warning("Discover failed for %s: %s", category, exc)
            continue
        log.info("web_grant_radar: %s returned %d candidates", category, len(candidates))

        cat_count = 0
        for cand in candidates:
            try:
                if _process_candidate(conn, client, category, cand):
                    new_count += 1
                    cat_count += 1
            except Exception as exc:
                log.warning("Candidate processing failed for %s: %s", category, exc)
        by_category[category] = cat_count

    conn.close()
    summary = ", ".join(f"{k}={v}" for k, v in by_category.items())
    return f"web_grant_radar: new={new_count} ({summary})"
