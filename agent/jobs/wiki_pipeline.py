"""Wiki pipeline — ingests one paper end-to-end into the lab wiki.

Flow (per paper):
    1. Resolve source (Drive file_id, local PDF path, or DOI→Europe PMC).
    2. Extract text; fingerprint SHA256; cache PDF bytes locally.
    3. Extractor agent (Opus 4.7 + finding_extractor.md) proposes 3–4 findings
       with verbatim quotes + teaching-mode reasoning + counter-argument.
    4. DETERMINISTIC verify — each quote must appear verbatim in the paper
       text. Non-matches are dropped immediately (TraitTrawler discipline).
    5. MODEL verify — Verifier agent (Sonnet + finding_verifier.md) reviews
       surviving candidates on four dimensions (quote-finding fit, page
       accuracy, reasoning honesty, counter specificity). Emits accept /
       revise / reject per finding.
    6. Apply revisions, drop rejects.
    7. Upsert paper_findings + topics rows in data/agent.db.
    8. Generate paper page markdown → knowledge/papers/<slug>.md
    9. For each touched topic, update knowledge/topics/<slug>.md via the
       topic_page_writer agent (Sonnet).
   10. Stage all writes via website_git.stage_files() (path-allowlisted).
   11. Log every model call to cost_tracking + output_ledger.
   12. Run critic.py (wiki_edit rubric) on each generated page.
   13. If dry_run: return diff + ledger summary; do NOT commit.
       If not dry_run: commit_and_push via website_git with [tealc] prefix
       and the 4-tuple in the commit body.

Entry points:
    run_on_drive_pdf(file_id, dry_run=True, ...)    → for Heath's Drive folder
    run_on_local_pdf(pdf_path, doi=None, ...)       → for local PDFs
    run_on_doi(doi, dry_run=True, ...)              → Europe PMC path

All three return a structured dict with {status, findings_accepted, findings_
rejected, pages_written, diff, errors, cost_usd}.

Manual run:
    python -m agent.jobs.wiki_pipeline --drive-file-id <ID> [--execute]
    python -m agent.jobs.wiki_pipeline --local-pdf <PATH> --doi <DOI> [--execute]
    python -m agent.jobs.wiki_pipeline --doi <DOI> [--execute]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.critic import critic_pass  # noqa: E402
from agent.jobs.website_git import (  # noqa: E402
    stage_files, commit_and_push, website_repo_path, EditNote,
    PathNotAllowed, RepoNotFound, PullConflict, PushBlocked,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPTS_DIR = os.path.join(_PROJECT_ROOT, "agent", "prompts")
_WIKI_PDFS_DIR = os.path.join(_PROJECT_ROOT, "data", "wiki_pdfs")
_TOPICS_FILE = os.path.join(_PROJECT_ROOT, "data", "research_topics.json")

_WEBSITE_PAPERS = "knowledge/papers"
_WEBSITE_TOPICS = "knowledge/topics"

_EXTRACTOR_MODEL = "claude-sonnet-4-6"  # switched 2026-04-21; Opus was overkill
_VERIFIER_MODEL = "claude-sonnet-4-6"
_TOPIC_WRITER_MODEL = "claude-sonnet-4-6"

_JOB_NAME = "wiki_pipeline"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class FindingRecord:
    """One finding, as it sits in the pipeline before landing in paper_findings."""
    finding_idx: int
    finding_text: str
    quote: str
    page: Optional[str]
    reasoning: str
    counter: str
    topic_tags: list[str]


@dataclass
class PipelineResult:
    """Structured return value of run_pipeline() / run_on_*()."""
    status: str = "unknown"             # "success" | "dry_run" | "partial" | "error"
    doi: Optional[str] = None
    fingerprint: Optional[str] = None
    paper_slug: Optional[str] = None
    findings_proposed: int = 0
    findings_after_deterministic: int = 0
    findings_accepted: int = 0
    findings_revised: int = 0
    findings_rejected: int = 0
    topics_touched: list[str] = field(default_factory=list)
    paths_written: list[str] = field(default_factory=list)
    diff: str = ""
    committed_sha: Optional[str] = None
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_summary_str(self) -> str:
        parts = [
            f"status={self.status}",
            f"doi={self.doi or '-'}",
            f"proposed={self.findings_proposed}",
            f"verbatim_ok={self.findings_after_deterministic}",
            f"accepted={self.findings_accepted}",
            f"revised={self.findings_revised}",
            f"rejected={self.findings_rejected}",
            f"topics={len(self.topics_touched)}",
            f"files={len(self.paths_written)}",
            f"cost≈${self.cost_usd:.3f}",
        ]
        if self.committed_sha:
            parts.append(f"sha={self.committed_sha[:8]}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Prompt + topic loading
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    """Load a prompt template from agent/prompts/<name>."""
    path = os.path.join(_PROMPTS_DIR, name)
    with open(path, encoding="utf-8") as f:
        return f.read()


class _JSONParseFailure(Exception):
    """Raised when a model response can't be parsed as JSON after all rescue
    strategies. Carries `raw` (the full response) and `snippet` (first 400 chars)
    so upstream error handlers can log something diagnostic instead of a bare
    JSONDecodeError."""
    def __init__(self, message: str, raw: str):
        self.raw = raw or ""
        self.snippet = self.raw[:400].replace("\n", "\\n")
        super().__init__(f"{message} | raw[:400]={self.snippet!r}")


def _robust_json_parse(raw: str) -> dict:
    """Extract the first JSON object from a model response.

    Handles the three failure modes we've actually seen in production:
      (a) model wraps its JSON in ```json ... ``` fences
      (b) model adds prose before the JSON ("Here is the updated page: {...}")
      (c) model uses smart quotes (\u201c\u201d) inside prose keys

    Strategy (in order; returns on first success):
      1. Plain json.loads(raw)
      2. Strip all lines that are/begin with ``` fences, retry
      3. Extract first balanced {...} object via brace-counting, retry
      4. Normalize smart quotes to ASCII on all prior candidates, retry

    Raises _JSONParseFailure with the raw text attached on total failure.
    """
    if not raw or not raw.strip():
        raise _JSONParseFailure("empty response from model", raw)

    attempts: list[str] = [raw]

    # Strip markdown fences (``` or ```json on their own lines, anywhere in the text)
    if "```" in raw:
        lines = raw.splitlines()
        fenced = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        ).strip()
        if fenced and fenced not in attempts:
            attempts.append(fenced)

    # Extract first balanced {...} via brace-counting (string-aware)
    start = raw.find("{")
    if start >= 0:
        depth = 0
        end: Optional[int] = None
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
        if end is not None:
            candidate = raw[start:end + 1]
            if candidate not in attempts:
                attempts.append(candidate)

    # Normalize smart quotes on all prior candidates
    for candidate in list(attempts):
        normalized = (candidate
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'"))
        if normalized != candidate and normalized not in attempts:
            attempts.append(normalized)

    last_err: Optional[Exception] = None
    for attempt in attempts:
        try:
            result = json.loads(attempt)
            if isinstance(result, dict):
                return result
            last_err = ValueError(
                f"parsed JSON is {type(result).__name__}, not dict"
            )
        except json.JSONDecodeError as e:
            last_err = e
    raise _JSONParseFailure(
        f"could not parse JSON after {len(attempts)} strategies; "
        f"last error: {last_err}",
        raw,
    )


def _load_research_topics() -> dict:
    """Load data/research_topics.json. Returns {} on error."""
    try:
        with open(_TOPICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _known_topic_slugs() -> set[str]:
    """Return the set of known canonical topic slugs from research_topics.json."""
    data = _load_research_topics()
    return {t.get("slug", "") for t in data.get("topics", []) if t.get("slug")}


# ---------------------------------------------------------------------------
# Slug + fingerprint helpers
# ---------------------------------------------------------------------------

def doi_to_slug(doi: str) -> str:
    """Convert a DOI to a filesystem-safe slug."""
    doi = (doi or "").strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    if doi.startswith("http://dx.doi.org/"):
        doi = doi[len("http://dx.doi.org/"):]
    # Lowercase + replace non-alphanumeric with underscore
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", doi.lower()).strip("_")
    return slug or "unknown"


def title_to_slug(title: str, max_len: int = 60) -> str:
    """Convert a paper title (or Drive filename sans extension) to a slug.

    Used when no DOI is available. Preserves Drive-filename conventions like
    '2022 why not' → '2022_why_not'. Caps at max_len chars.
    """
    base = (title or "").strip()
    # Strip common extensions
    for ext in (".pdf", ".PDF"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", base.lower()).strip("_")
    slug = slug[:max_len].rstrip("_")
    return slug or "unknown"


def _pick_paper_slug(meta: dict) -> str:
    """Pick the best slug for a paper given its metadata. Prefer DOI; fall back
    to title (for Drive-sourced papers with filenames); last resort fingerprint.
    """
    if meta.get("doi"):
        return doi_to_slug(meta["doi"])
    if meta.get("title"):
        candidate = title_to_slug(meta["title"])
        if candidate and candidate != "unknown":
            return candidate
    fingerprint = meta.get("fingerprint_sha256") or ""
    if fingerprint:
        return fingerprint[:16]  # short fingerprint prefix, still unique
    return "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Source resolution: Drive / local / DOI
# ---------------------------------------------------------------------------

def _fetch_drive_filename(file_id: str) -> str:
    """Fetch a Drive file's name without downloading the body. Empty string on error.

    Note: _get_google_service returns (service, error_str) — must be unpacked.
    """
    try:
        from agent.tools import _get_google_service  # noqa: PLC0415
        service, err = _get_google_service("drive", "v3")
        if service is None or err:
            return ""
        info = service.files().get(
            fileId=file_id, fields="name", supportsAllDrives=True,
        ).execute()
        return info.get("name", "") or ""
    except Exception:
        return ""


def _resolve_drive_source(file_id: str) -> tuple[bytes, dict]:
    """Download a PDF from Drive and return (bytes, metadata_from_drive).

    Also fetches the Drive filename so downstream slug/title logic has a
    sensible fallback when no DOI is supplied.
    """
    from agent.tools import download_drive_pdf  # noqa: PLC0415
    os.makedirs(_WIKI_PDFS_DIR, exist_ok=True)
    drive_name = _fetch_drive_filename(file_id)
    # Write to a temp path first, then move to canonical fingerprint path.
    tmp_path = os.path.join(_WIKI_PDFS_DIR, f"_incoming_{file_id}.pdf")
    result = download_drive_pdf.invoke({"file_id": file_id, "output_path": tmp_path})
    if "error" in result:
        raise RuntimeError(f"Drive download failed: {result}")
    with open(tmp_path, "rb") as f:
        pdf_bytes = f.read()
    # Move to canonical fingerprint path
    fingerprint = sha256_bytes(pdf_bytes)
    canonical = os.path.join(_WIKI_PDFS_DIR, f"{fingerprint}.pdf")
    if not os.path.exists(canonical):
        os.replace(tmp_path, canonical)
    else:
        os.remove(tmp_path)
    meta = {
        "source": "drive",
        "drive_file_id": file_id,
        "drive_filename": drive_name,
        "local_pdf_path": canonical,
        "fingerprint_sha256": fingerprint,
    }
    if drive_name:
        # Strip extension; becomes the default title if caller doesn't pass one.
        meta["title_from_filename"] = drive_name.rsplit(".", 1)[0] if "." in drive_name else drive_name
    return pdf_bytes, meta


def _resolve_local_source(pdf_path: str, doi: Optional[str] = None) -> tuple[bytes, dict]:
    """Read a local PDF file; optionally tag with a DOI supplied by the caller."""
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    fingerprint = sha256_bytes(pdf_bytes)
    os.makedirs(_WIKI_PDFS_DIR, exist_ok=True)
    canonical = os.path.join(_WIKI_PDFS_DIR, f"{fingerprint}.pdf")
    if not os.path.exists(canonical):
        with open(canonical, "wb") as f:
            f.write(pdf_bytes)
    meta = {
        "source": "local",
        "original_path": pdf_path,
        "local_pdf_path": canonical,
        "fingerprint_sha256": fingerprint,
        "doi": doi,
    }
    return pdf_bytes, meta


def _resolve_doi_source(doi: str) -> tuple[str, dict]:
    """Fetch full-text XML from Europe PMC by DOI. Returns (full_text, meta)."""
    from agent.apis import europe_pmc  # noqa: PLC0415
    # Search for the DOI to get the PMCID
    results = europe_pmc.search_full_text(f"DOI:{doi}", limit=1)
    if not results:
        raise RuntimeError(
            f"No open-access full text available via Europe PMC for DOI {doi}. "
            f"Fall back to local PDF ingestion via run_on_local_pdf()."
        )
    hit = results[0]
    pmcid = hit.get("pmcid", "")
    extracted = europe_pmc.fetch_and_extract(pmcid)
    if not extracted or not extracted.get("full_text"):
        raise RuntimeError(f"Could not fetch full-text XML for PMCID {pmcid}")
    full_text = extracted["full_text"]
    fingerprint = sha256_bytes(full_text.encode("utf-8"))
    meta = {
        "source": "europe_pmc",
        "doi": doi,
        "pmcid": pmcid,
        "title": hit.get("title"),
        "authors": hit.get("authors"),
        "journal": hit.get("journal"),
        "publication_year": hit.get("pub_year"),
        "fingerprint_sha256": fingerprint,
    }
    return full_text, meta


# ---------------------------------------------------------------------------
# PDF text extraction (reuses tools._read_pdf)
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_path: str) -> str:
    """Extract readable text from a PDF via the existing _read_pdf helper."""
    from agent.tools import _read_pdf  # noqa: PLC0415
    return _read_pdf(pdf_path)


# ---------------------------------------------------------------------------
# Extractor agent (Opus 4.7)
# ---------------------------------------------------------------------------

def _run_extractor(client: Anthropic, paper_meta: dict,
                   paper_text: str, max_chars: int = 60000) -> tuple[list[dict], float]:
    """Call the extractor agent. Returns (findings_list, usd_cost)."""
    system_prompt = _load_prompt("finding_extractor.md")
    known_topics = sorted(_known_topic_slugs())

    user_msg = (
        f"Paper metadata:\n{json.dumps(paper_meta, indent=2)}\n\n"
        f"Known lab topic slugs (prefer these, coin new only when truly novel):\n"
        f"{', '.join(known_topics) or '(none configured)'}\n\n"
        f"Paper text:\n{paper_text[:max_chars]}"
    )
    if len(paper_text) > max_chars:
        user_msg += f"\n\n[NOTE: paper text truncated at {max_chars} chars of {len(paper_text)}]"

    msg = client.messages.create(
        model=_EXTRACTOR_MODEL,
        max_tokens=4000,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    record_call(job_name=_JOB_NAME, model=_EXTRACTOR_MODEL, usage=usage)

    raw = msg.content[0].text
    parsed = _robust_json_parse(raw)
    findings = parsed.get("findings", [])
    cost = _estimate_cost_usd(_EXTRACTOR_MODEL, usage)
    return findings, cost


# ---------------------------------------------------------------------------
# Deterministic verify (non-negotiable, no model call)
# ---------------------------------------------------------------------------

# Unicode punctuation + ligature normalizations. PDFs often encode em-dashes,
# smart quotes, and ligatures (fi, fl) while the model "tidies" them up into
# ASCII equivalents in its returned quote. Without normalizing both sides, the
# deterministic-verify step rejects every finding with "no verbatim match" even
# when the content is substantively identical.
_UNICODE_PUNCT_MAP = {
    "\u2014": "-",   # em-dash
    "\u2013": "-",   # en-dash
    "\u2212": "-",   # minus sign
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote (apostrophe)
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u00ab": '"',   # left guillemet
    "\u00bb": '"',   # right guillemet
    "\u2026": "...", # ellipsis
    "\u00a0": " ",   # non-breaking space
    "\u2009": " ",   # thin space
    "\u200a": " ",   # hair space
    "\u200b": "",    # zero-width space
    "\u00ad": "",    # soft hyphen
    "\ufb00": "ff",  # ligature ff
    "\ufb01": "fi",  # ligature fi
    "\ufb02": "fl",  # ligature fl
    "\ufb03": "ffi", # ligature ffi
    "\ufb04": "ffl", # ligature ffl
}


def _normalize_for_compare(s: str) -> str:
    """Lowercase, collapse whitespace, normalize Unicode punctuation and
    ligatures to ASCII equivalents. Used by _deterministic_verify so
    PDF-extracted text matches model-returned quotes even when one side has
    smart quotes/em-dashes and the other has plain ASCII."""
    if not s:
        return ""
    for uc, ac in _UNICODE_PUNCT_MAP.items():
        s = s.replace(uc, ac)
    return re.sub(r"\s+", " ", s).strip().lower()


def _deterministic_verify(findings: list[dict], paper_text: str) -> list[dict]:
    """Drop any finding whose quote does not appear verbatim in paper_text,
    after Unicode-normalizing both sides."""
    paper_norm = _normalize_for_compare(paper_text)
    surviving: list[dict] = []
    for f in findings:
        quote = (f or {}).get("quote", "")
        q_norm = _normalize_for_compare(quote)
        if not q_norm:
            continue
        if q_norm in paper_norm:
            surviving.append(f)
    return surviving


# V1 Phase 2b — digit-substring drift check.
# 2026-04-23: BLOCKING MODE enabled. After the corpus validator fell from 536
# to 45 claim fails (under the 50 threshold), the defensive guard is live:
# findings that fail the numeric-substring check (with tilde-approximation
# zone) are DROPPED from the commit instead of being logged as warnings.
#
# Now readable from the Control tab: the flag lives in
# data/tealc_config.json under jobs.wiki_pipeline.digit_substring_blocking.
# The module constant below is the fallback default used only if the config
# is unreadable or the key is missing — matches current production state.
_DIGIT_SUBST_BLOCKING_DEFAULT = True
_DIGIT_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _digit_subst_blocking_enabled() -> bool:
    """Read jobs.wiki_pipeline.digit_substring_blocking from tealc_config.json.
    Fall back to _DIGIT_SUBST_BLOCKING_DEFAULT on any config-load error so the
    pipeline never crashes on a missing/corrupt config."""
    try:
        from agent.config import load_config  # noqa: PLC0415
        cfg = load_config()
        job_cfg = (cfg.get("jobs") or {}).get("wiki_pipeline") or {}
        val = job_cfg.get("digit_substring_blocking")
        if isinstance(val, bool):
            return val
    except Exception:
        pass
    return _DIGIT_SUBST_BLOCKING_DEFAULT


def _digit_substring_check(findings: list[dict]) -> list[dict]:
    """For each finding, verify every numeric in finding_text appears as a
    substring of the verbatim quote (thousand-commas stripped). When the
    claim contains '~' / 'approximately' / 'about', permit +/-10% deviation
    per numeric vs any numeric in the quote (tilde-approximation zone).

    Returns a list of failure dicts — one per finding that failed. Never
    drops findings in V1; caller decides what to do with the failures.
    """
    failures: list[dict] = []
    for i, f in enumerate(findings):
        claim = (f or {}).get("finding_text") or ""
        quote = (f or {}).get("quote") or ""
        if not claim or not quote:
            continue
        claim_n = _normalize_for_compare(claim).replace(",", "")
        quote_n = _normalize_for_compare(quote).replace(",", "")
        claim_nums = _DIGIT_NUM_RE.findall(claim_n)
        if not claim_nums:
            continue
        allow_tilde = any(
            tok in claim.lower() for tok in ("~", "approximately", "about ", "roughly")
        )
        missing = None
        for num in claim_nums:
            if num in quote_n:
                continue
            matched = False
            if allow_tilde:
                try:
                    v = float(num)
                    for qn in _DIGIT_NUM_RE.findall(quote_n):
                        qv = float(qn)
                        if qv == 0:
                            continue
                        if abs(v - qv) / qv <= 0.10:
                            matched = True
                            break
                except ValueError:
                    pass
            if not matched:
                missing = num
                break
        if missing is not None:
            failures.append({
                "finding_idx": i,
                "claim": claim[:240],
                "quote": quote[:240],
                "missing_numeric": missing,
                "allow_tilde": allow_tilde,
            })
    return failures


# ---------------------------------------------------------------------------
# Verifier agent (Sonnet)
# ---------------------------------------------------------------------------

def _run_verifier(client: Anthropic, findings: list[dict],
                  paper_meta: dict, paper_text: str,
                  max_chars: int = 60000) -> tuple[dict, float]:
    """Call the verifier agent. Returns (verdict_dict, usd_cost)."""
    system_prompt = _load_prompt("finding_verifier.md")

    # Index findings so the verifier can refer to them by finding_idx
    indexed = []
    for i, f in enumerate(findings):
        indexed.append({
            "finding_idx": i,
            **{k: f.get(k) for k in ("finding_text", "quote", "page",
                                     "reasoning", "counter", "topic_tags")},
        })

    user_msg = (
        f"Paper metadata:\n{json.dumps(paper_meta, indent=2)}\n\n"
        f"Candidate findings (deterministic-verified as verbatim-in-text):\n"
        f"{json.dumps(indexed, indent=2)}\n\n"
        f"Paper text:\n{paper_text[:max_chars]}"
    )

    msg = client.messages.create(
        model=_VERIFIER_MODEL,
        max_tokens=4000,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    record_call(job_name=_JOB_NAME, model=_VERIFIER_MODEL, usage=usage)

    raw = msg.content[0].text
    verdict = _robust_json_parse(raw)
    cost = _estimate_cost_usd(_VERIFIER_MODEL, usage)
    return verdict, cost


def _apply_verdict(findings: list[dict], verdict: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (accepted, revised, rejected) finding dicts.

    accepted: findings whose finding_idx appears in verdict["accepted"].
    revised:  findings whose finding_idx appears in verdict["revised"], with
              proposed_edits applied (null fields left unchanged).
    rejected: findings whose finding_idx appears in verdict["rejected"].
    """
    accepted, revised, rejected = [], [], []
    for entry in verdict.get("accepted", []) or []:
        idx = entry.get("finding_idx")
        if idx is not None and 0 <= idx < len(findings):
            accepted.append(findings[idx])
    for entry in verdict.get("revised", []) or []:
        idx = entry.get("finding_idx")
        edits = entry.get("proposed_edits") or {}
        if idx is not None and 0 <= idx < len(findings):
            patched = dict(findings[idx])
            for field_name in ("finding_text", "quote", "page",
                               "reasoning", "counter", "topic_tags"):
                new_val = edits.get(field_name)
                if new_val is not None:
                    patched[field_name] = new_val
            revised.append(patched)
    for entry in verdict.get("rejected", []) or []:
        idx = entry.get("finding_idx")
        if idx is not None and 0 <= idx < len(findings):
            rejected.append(findings[idx])
    return accepted, revised, rejected


# ---------------------------------------------------------------------------
# Topic page writer agent (Sonnet)
# ---------------------------------------------------------------------------

def _run_topic_writer(client: Anthropic, topic_slug: str, topic_title: str,
                      existing_body: str, new_findings: list[dict],
                      paper_meta: dict) -> tuple[dict, float]:
    """Call the topic-page writer. Returns (result_dict, usd_cost).

    result_dict keys: body_md, edit_note{what_changed, why_changed,
    evidence_quote, counter_argument}.
    """
    system_prompt = _load_prompt("topic_page_writer.md")

    paper_permalink = paper_meta.get("paper_permalink") or "/knowledge/papers/unknown/"
    # Pre-format the per-finding anchors so the writer does not have to guess.
    # Each finding in new_findings has a 1-based position in the paper's
    # finding list; the anchors are #finding-1, #finding-2, ...
    findings_with_anchors = []
    for i, f in enumerate(new_findings, start=1):
        fc = dict(f)
        fc["anchor"] = f"{paper_permalink}#finding-{i}"
        fc["suggested_link_markdown"] = (
            f"[{paper_meta.get('title') or paper_meta.get('doi') or 'source'}, "
            f"Finding {i}]({paper_permalink}#finding-{i})"
        )
        findings_with_anchors.append(fc)

    # Voice exemplars: pull Heath's own prose for the topic domain so the
    # writer can match register rather than landing in generic AI-assistant
    # voice. Soft dependency — if retrieval fails or returns nothing, we
    # proceed without the block.
    voice_block = ""
    try:
        from agent.tools import retrieve_voice_exemplars  # noqa: PLC0415
        voice_query = (
            f"scientific topic page on {topic_title or topic_slug.replace('_', ' ')}, "
            f"synthesis of findings with hedging and contradictions"
        )
        voice_block = retrieve_voice_exemplars.invoke({"query": voice_query, "k": 4})
    except Exception:
        voice_block = ""

    voice_section = ""
    if voice_block:
        voice_section = (
            f"VOICE EXEMPLARS — match the register below when writing prose:\n"
            f"{voice_block}\n\n"
        )

    user_msg = (
        f"{voice_section}"
        f"Topic slug: {topic_slug}\n"
        f"Topic title: {topic_title}\n\n"
        f"Paper permalink for cross-links: {paper_permalink}\n"
        f"Finding anchors on that page: #finding-1, #finding-2, ... (1-based, in "
        f"the order below)\n\n"
        f"Existing topic page body (empty if new topic):\n"
        f"```\n{existing_body}\n```\n\n"
        f"New findings to fold in (each carries 'anchor' and "
        f"'suggested_link_markdown' — USE these exact links, don't invent "
        f"DOI-based URLs):\n{json.dumps(findings_with_anchors, indent=2)}\n\n"
        f"Source paper metadata:\n{json.dumps(paper_meta, indent=2)}"
    )

    msg = client.messages.create(
        model=_TOPIC_WRITER_MODEL,
        # 6000 handles rich topic pages that accumulate findings across multiple
        # papers. Previous 3000 cap was causing mid-string truncation that
        # surfaced as JSONDecodeError "Unterminated string starting at...".
        max_tokens=6000,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    record_call(job_name=_JOB_NAME, model=_TOPIC_WRITER_MODEL, usage=usage)

    raw = msg.content[0].text
    # Delimiter protocol: <<<EDIT_NOTE_JSON>>> {json} <<<BODY_MD_BEGIN>>> body <<<BODY_MD_END>>>.
    # Avoids the JSON-escape failures that plague markdown-in-JSON-string
    # outputs (unescaped quotes, newlines, em-dashes, code fences in body).
    result = _parse_topic_writer_output(raw)
    cost = _estimate_cost_usd(_TOPIC_WRITER_MODEL, usage)
    return result, cost


def _parse_topic_writer_output(raw: str) -> dict:
    """Split the topic writer's delimiter-protocol response into structured
    output. Returns {"body_md": str, "edit_note": dict}.

    Falls back to the old robust JSON parser if delimiters aren't present,
    for backward compatibility with any stray call that didn't see the new
    prompt (shouldn't happen in normal operation but belt-and-suspenders).
    """
    edit_marker = "<<<EDIT_NOTE_JSON>>>"
    body_begin = "<<<BODY_MD_BEGIN>>>"
    body_end = "<<<BODY_MD_END>>>"

    if edit_marker in raw and body_begin in raw:
        # New protocol path
        after_edit = raw.split(edit_marker, 1)[1]
        edit_json_part, after_body_begin = after_edit.split(body_begin, 1)
        edit_note = _robust_json_parse(edit_json_part.strip())
        if body_end in after_body_begin:
            body_md = after_body_begin.split(body_end, 1)[0].strip()
        else:
            body_md = after_body_begin.strip()
        return {"body_md": body_md, "edit_note": edit_note}

    # Fallback: old JSON-only protocol (for transitional compatibility)
    return _robust_json_parse(raw)


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def _paper_key(meta: dict) -> str:
    """Canonical identity for a paper in paper_findings.doi.

    Uses real DOI if available, else 'sha256:<fingerprint>' pseudo-DOI so
    DOI-less papers (e.g. Drive file-id ingests with no metadata) still get a
    unique per-paper key and the UNIQUE(doi, finding_idx) constraint holds.
    """
    doi = (meta.get("doi") or "").strip()
    if doi:
        return doi
    fp = (meta.get("fingerprint_sha256") or "").strip()
    if fp:
        return f"sha256:{fp}"
    return ""


def _upsert_paper_findings(meta: dict, findings: list[dict]) -> None:
    """Insert (or replace) paper_findings rows for this paper. Clears prior
    rows by the paper's canonical key (real DOI or sha256: pseudo-DOI) so
    re-runs stay idempotent. No-ops if neither DOI nor fingerprint is known.
    """
    key = _paper_key(meta)
    if not key:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("DELETE FROM paper_findings WHERE doi=?", (key,))
    for idx, f in enumerate(findings):
        conn.execute(
            """INSERT INTO paper_findings
               (doi, finding_idx, finding_text, quote, page, reasoning,
                counter, topic_tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                key, idx,
                f.get("finding_text", ""),
                f.get("quote", ""),
                str(f.get("page") or "") or None,
                f.get("reasoning", ""),
                f.get("counter", ""),
                ",".join(f.get("topic_tags", []) or []),
                now,
            ),
        )
    conn.commit()
    conn.close()


def _upsert_topics(slugs: list[str]) -> None:
    """Ensure a topics row exists for each slug."""
    now = datetime.now(timezone.utc).isoformat()
    known = _load_research_topics().get("topics", [])
    title_by_slug = {t.get("slug"): t.get("title", t.get("slug")) for t in known}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    for slug in slugs:
        title = title_by_slug.get(slug, slug.replace("_", " ").title())
        conn.execute(
            """INSERT OR IGNORE INTO topics (slug, title, last_updated, created_at)
               VALUES (?, ?, ?, ?)""",
            (slug, title, now, now),
        )
        conn.execute(
            "UPDATE topics SET last_updated=? WHERE slug=?",
            (now, slug),
        )
    conn.commit()
    conn.close()


def _upsert_literature_note_with_fingerprint(meta: dict, findings: list[dict]) -> None:
    """Keep the existing literature_notes table in sync with our processing.
    Single row per (project_id=NULL, doi). Stores the SHA256 fingerprint.

    For papers without a real DOI, stores 'sha256:<fp>' as a pseudo-DOI so the
    UNIQUE(project_id, doi) constraint still holds and the row is findable on
    subsequent ingest attempts (dedup guard).
    """
    doi = _paper_key(meta)
    if not doi:
        return
    now = datetime.now(timezone.utc).isoformat()
    # Brief markdown digest of findings for the findings_md column
    findings_md = "\n\n".join(
        f"- **{f.get('finding_text', '')}**\n  > {f.get('quote', '')[:200]}"
        for f in findings
    ) or "(no findings)"
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO literature_notes
           (project_id, doi, title, authors, journal, publication_year,
            raw_abstract, extracted_findings_md, relevance_to_project,
            pdf_fingerprint, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, doi) DO UPDATE SET
             pdf_fingerprint=excluded.pdf_fingerprint,
             extracted_findings_md=excluded.extracted_findings_md""",
        (
            None, doi,
            meta.get("title", "") or "(unknown)",
            meta.get("authors"),
            meta.get("journal"),
            meta.get("publication_year"),
            None,
            findings_md,
            None,
            meta.get("fingerprint_sha256"),
            now,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Markdown composition
# ---------------------------------------------------------------------------

def _compose_paper_page(meta: dict, findings: list[dict]) -> str:
    """Build the paper-page markdown body from metadata + accepted findings."""
    doi = meta.get("doi", "")
    slug = meta.get("paper_slug") or _pick_paper_slug(meta)
    title = meta.get("title") or doi or "Untitled paper"
    topics_all = sorted({t for f in findings for t in (f.get("topic_tags") or [])})

    # doi is always quoted so empty string (no real DOI) is explicit as "" in
    # YAML rather than a bare `doi:` which parses as null. WIKI_HANDOFF.md
    # requires explicit empty string.
    yaml_lines = [
        "---",
        "layout: default",
        f'title: "{_esc(title)}"',
        f'doi: "{doi}"',
        f"fingerprint_sha256: {meta.get('fingerprint_sha256', '')}",
        f'authors: "{_esc(meta.get("authors") or "")}"',
        f'journal: "{_esc(meta.get("journal") or "")}"',
        f"year: {meta.get('publication_year') or ''}",
        f"topics: [{', '.join(topics_all)}]",
        f"tier: canon",
        f"ingested_at: {datetime.now(timezone.utc).isoformat()}",
        f"permalink: /knowledge/papers/{slug}/",
        "---",
        "",
    ]

    body = [
        f"# {title}",
        "",
        "## Summary",
        "",
        f"_Ingested {datetime.now(timezone.utc).strftime('%Y-%m-%d')}. "
        f"{len(findings)} findings extracted and verified._",
        "",
        "## Findings worth citing",
        "",
    ]
    for i, f in enumerate(findings, start=1):
        # Explicit HTML anchor so topic pages can link to /#finding-N reliably,
        # independent of whatever slugification the Markdown renderer does with
        # the heading text.
        body.append(f'<a id="finding-{i}"></a>')
        body.append(f"### Finding {i} — {f.get('finding_text', '')}")
        body.append("")
        body.append(f"> {f.get('quote', '')}")
        page = f.get("page")
        if page:
            body.append(f"— p. {page}")
        body.append("")
        body.append(f"*Why this is citable:* {f.get('reasoning', '')}")
        body.append("")
        body.append(f"*Counter / limitation:* {f.get('counter', '')}")
        body.append("")
        tags = f.get("topic_tags") or []
        if tags:
            topic_links = ", ".join(
                f"[{t}](/knowledge/topics/{t}/)" for t in tags
            )
            body.append(f"*Topics:* {topic_links}")
            body.append("")

    return "\n".join(yaml_lines + body)


def _esc(s: str) -> str:
    """Escape a string for YAML double-quoted scalar."""
    return (s or "").replace('\\', '\\\\').replace('"', '\\"').replace("\n", " ")


def _read_existing_topic_page(
    repo: str, slug: str,
) -> tuple[str, list[str], str]:
    """Return (existing_body, existing_papers_supporting, existing_category).

    existing_body: the page content *below* the YAML frontmatter (or empty if
        the page doesn't exist). NEVER includes the frontmatter — the whole
        point is to pass clean body text to the topic writer so it doesn't
        accidentally treat old frontmatter as prose and double up YAML blocks
        on the next rewrite.
    existing_papers_supporting: DOIs currently listed in the frontmatter's
        papers_supporting array, or [] if the field is absent / page missing.
        Used to union with the new paper's DOI so the list accumulates across
        ingests instead of overwriting.
    existing_category: the category: value from the frontmatter, or "" if
        the field is absent / page missing. WIKI_HANDOFF.md requires topic
        pages to carry a category; on update we always preserve the existing
        value rather than let a fresh lookup clobber a hand-assigned one.
    """
    path = os.path.join(repo, _WEBSITE_TOPICS, f"{slug}.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return ("", [], "")

    body = text
    papers_supporting: list[str] = []
    category = ""

    if text.startswith("---"):
        # Find the closing --- of the first (top-level) frontmatter block.
        end = text.find("\n---", 3)
        if end >= 0:
            fm = text[3:end]
            body = text[end + 4:].lstrip("\n")
            for raw_line in fm.splitlines():
                line = raw_line.strip()
                if line.startswith("papers_supporting:"):
                    list_part = line.split(":", 1)[1].strip()
                    if list_part.startswith("[") and list_part.endswith("]"):
                        list_part = list_part[1:-1]
                    papers_supporting = [
                        p.strip().strip('"').strip("'")
                        for p in list_part.split(",")
                        if p.strip()
                    ]
                elif line.startswith("category:"):
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        category = val

    return (body, papers_supporting, category)


# ---------------------------------------------------------------------------
# Non-destructive update protocol: tealc:auto markers
#
# Topic page bodies have an "auto region" demarcated by HTML comment markers.
# The topic_page_writer regenerates ONLY that region on each update; anything
# outside the markers (human-added notes, cross-links the janitor maintains,
# hand-curated "Related" sections) is preserved verbatim. See
# WIKI_HANDOFF.md and wiki_janitor.py's module docstring for the contract.
#
# Legacy pages (no markers) are treated as fully-auto for backward compat;
# on their first update the pipeline wraps them in markers.
# ---------------------------------------------------------------------------

_MARKER_START = "<!-- tealc:auto-start -->"
_MARKER_END = "<!-- tealc:auto-end -->"

# Second marker pair for enrichment content (cross-links, external DOI links,
# author cross-refs). Managed by agent/jobs/refresh_enrichment.py, NOT the
# topic writer. Deterministic regeneration from DB state + paper frontmatter.
# Lives below tealc:auto-end (on topic pages) or at EOF (on paper pages).
_RELATED_START = "<!-- tealc:related-start -->"
_RELATED_END = "<!-- tealc:related-end -->"


def splice_related_region(full_body: str, new_related_content: str) -> str:
    """Replace the content between <!-- tealc:related-start/end --> in
    full_body with new_related_content. Inserts a fresh region at EOF if no
    markers are present.

    Content outside the markers is preserved verbatim. Always ends the file
    with a single trailing newline.
    """
    start_idx = full_body.find(_RELATED_START)
    end_idx = full_body.find(_RELATED_END)

    if start_idx >= 0 and end_idx > start_idx:
        # Replace existing region
        before = full_body[:start_idx].rstrip("\n")
        after = full_body[end_idx + len(_RELATED_END):].lstrip("\n")
        parts: list[str] = []
        if before:
            parts.append(before)
            parts.append("")
        parts.append(_RELATED_START)
        if new_related_content.strip():
            parts.append(new_related_content.strip())
        parts.append(_RELATED_END)
        if after:
            parts.append("")
            parts.append(after)
        return "\n".join(parts).rstrip("\n") + "\n"

    # No existing markers — append a fresh region at EOF
    base = full_body.rstrip("\n")
    parts = [base, "", _RELATED_START]
    if new_related_content.strip():
        parts.append(new_related_content.strip())
    parts.append(_RELATED_END)
    return "\n".join(parts) + "\n"


def _split_body_by_markers(body: str) -> tuple[str, str, str, bool]:
    """Split a topic page body into (before, inside, after, had_markers).

    before: content above <!-- tealc:auto-start --> (preserved on update)
    inside: content between start and end markers (regenerated on update)
    after:  content below <!-- tealc:auto-end --> (preserved on update)
    had_markers: False if the body had no markers (legacy page); in that case
        before="", inside=body, after="" so the whole body is treated as auto.
    """
    if _MARKER_START not in body or _MARKER_END not in body:
        return ("", body, "", False)

    start_idx = body.find(_MARKER_START)
    end_idx = body.find(_MARKER_END, start_idx + len(_MARKER_START))
    if start_idx < 0 or end_idx < 0:
        return ("", body, "", False)

    before = body[:start_idx]
    inside = body[start_idx + len(_MARKER_START):end_idx]
    after = body[end_idx + len(_MARKER_END):]
    return (before, inside, after, True)


def _splice_auto_region(new_auto_body: str, existing_full_body: str) -> str:
    """Produce the final topic-page body after a writer update.

    Takes the topic writer's newly-generated 'auto region' content and the
    existing full body. Preserves any human/janitor content outside the
    markers; replaces only what's inside. Legacy pages (no markers) get
    wrapped in markers on this first write — their existing content is
    dropped because it has already been regenerated as new_auto_body.
    """
    before, _, after, _ = _split_body_by_markers(existing_full_body)

    before_clean = before.rstrip("\n")
    inside_clean = new_auto_body.strip("\n")
    after_clean = after.lstrip("\n")

    parts: list[str] = []
    if before_clean:
        parts.append(before_clean)
        parts.append("")  # blank line between before-content and marker
    parts.append(_MARKER_START)
    if inside_clean:
        parts.append(inside_clean)
    parts.append(_MARKER_END)
    if after_clean:
        parts.append("")  # blank line between marker and after-content
        parts.append(after_clean)

    # Always end with a single trailing newline
    return "\n".join(parts).rstrip("\n") + "\n"


# Slug → category map, mirrors WIKI_HANDOFF.md's "Complete category map"
# section. Update in lockstep with that doc. Missing slugs fall back to
# _CATEGORY_FALLBACK (the handoff's documented catch-all).
_CATEGORY_MAP: dict[str, str] = {
    # Sex chromosomes
    "sex_chromosome_evolution":         "Sex chromosomes",
    "fragile_y_hypothesis":             "Sex chromosomes",
    "sex_linkage_mutation":             "Sex chromosomes",
    "y_naught_asymmetry":               "Sex chromosomes",
    "sexual_antagonism":                "Sex chromosomes",
    "sexually_antagonistic_selection":  "Sex chromosomes",
    "meiotic_drive":                    "Sex chromosomes",
    "haplodiploidy_evolution":          "Sex chromosomes",
    # Karyotype evolution
    "karyotype_evolution":              "Karyotype evolution",
    "karyotype_evolution_overview":     "Karyotype evolution",
    "chromosome_number_evolution":      "Karyotype evolution",
    "chromosome_number_optima":         "Karyotype evolution",
    "centromere_type":                  "Karyotype evolution",
    "centromere_evolution":             "Karyotype evolution",
    "holocentric_chromosomes":          "Karyotype evolution",
    "chromosome_fusion":                "Karyotype evolution",
    "karyotype_database":               "Karyotype evolution",
    # Genome structure
    "genome_structure_evolution":       "Genome structure",
    "genome_assembly":                  "Genome structure",
    "genome_dynamics":                  "Genome structure",
    "transposable_elements":            "Genome structure",
    "microsatellite_evolution":         "Genome structure",
    "genetic_architecture":             "Genome structure",
    # Insects & Coleoptera
    "coleoptera":                       "Insects & Coleoptera",
    "coleoptera_genomics":              "Insects & Coleoptera",
    "coleoptera_karyotype":             "Insects & Coleoptera",
    "bee_genomics":                     "Insects & Coleoptera",
    "bee_phylogenomics":                "Insects & Coleoptera",
    "insect_genomics":                  "Insects & Coleoptera",
    "Tribolium":                        "Insects & Coleoptera",
    # Speciation & macroevolution
    "ring_species":                     "Speciation & macroevolution",
    "speciation":                       "Speciation & macroevolution",
    "phylloscopus":                     "Speciation & macroevolution",
    "avian_evolution":                  "Speciation & macroevolution",
    "avian_hybridization":              "Speciation & macroevolution",
    "Galliformes":                      "Speciation & macroevolution",
    "uce_phylogenetics":                "Speciation & macroevolution",
    "diversification_rates":            "Speciation & macroevolution",
    "hybridization":                    "Speciation & macroevolution",
    "postzygotic_isolation":            "Speciation & macroevolution",
    "reproductive_isolation":           "Speciation & macroevolution",
    "domestication":                    "Speciation & macroevolution",
    "domestication_genomics":           "Speciation & macroevolution",
    "life_history_evolution":           "Speciation & macroevolution",
    "convergent_evolution":             "Speciation & macroevolution",
    # Population genetics
    "demographic_inference":            "Population genetics",
    "conservation_genomics":            "Population genetics",
    "conservation_genetics":            "Population genetics",
    "sequencing_methods":               "Population genetics",
    "coalescent_simulation":            "Population genetics",
    "isolation_by_distance":            "Population genetics",
    "effective_population_size":        "Population genetics",
    "divergence_time_estimation":       "Population genetics",
    "population_genetics":              "Population genetics",
    # Quantitative genetics & epistasis
    "epistasis":                        "Quantitative genetics & epistasis",
    "line_cross_analysis":              "Quantitative genetics & epistasis",
    "quantitative_genetics":            "Quantitative genetics & epistasis",
    "quantitative_genetics_methods":    "Quantitative genetics & epistasis",
    "artificial_selection":             "Quantitative genetics & epistasis",
    "dispersal":                        "Quantitative genetics & epistasis",
    "selection_and_drift":              "Quantitative genetics & epistasis",
    "trait_definition":                 "Quantitative genetics & epistasis",
    "comparative_methods":              "Quantitative genetics & epistasis",
    "ancestral_state_reconstruction":   "Quantitative genetics & epistasis",
    # Bioinformatics & tools
    "bioinformatics_tools":             "Bioinformatics & tools",
    "model_organism_databases":         "Bioinformatics & tools",
    "cavefish_genomics":                "Bioinformatics & tools",
    "circadian_rhythm_evolution":       "Bioinformatics & tools",
}
_CATEGORY_FALLBACK = "Bioinformatics & tools"


def _category_for_slug(slug: str) -> str:
    """Look up the canonical category for a topic slug, falling back per the
    WIKI_HANDOFF convention."""
    return _CATEGORY_MAP.get(slug, _CATEGORY_FALLBACK)


def _compose_topic_page(slug: str, body_from_writer: str,
                        papers_supporting: list[str],
                        existing_category: str = "") -> str:
    """Wrap the writer's body_md in YAML frontmatter.

    category emission order of preference:
      1. existing_category (passed by caller from _read_existing_topic_page)
      2. _category_for_slug(slug) lookup in the WIKI_HANDOFF map
      3. _CATEGORY_FALLBACK (via the lookup function)

    Never emits an empty category — WIKI_HANDOFF marks uncategorized pages as
    invisible on the landing page.
    """
    known = _load_research_topics().get("topics", [])
    meta = next((t for t in known if t.get("slug") == slug), {})
    title = meta.get("title") or slug.replace("_", " ").title()
    category = (existing_category or "").strip() or _category_for_slug(slug)
    yaml_lines = [
        "---",
        "layout: default",
        f'title: "{_esc(title)}"',
        f"topic_slug: {slug}",
        f"last_updated: {datetime.now(timezone.utc).isoformat()}",
        f"papers_supporting: [{', '.join(papers_supporting)}]",
        f"permalink: /knowledge/topics/{slug}/",
        f'category: "{_esc(category)}"',
        "---",
        "",
    ]
    return "\n".join(yaml_lines) + (body_from_writer or "")


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

_MODEL_PRICING_PER_MTOK = {
    # USD per million tokens. Keep in sync with Anthropic pricing page.
    "claude-opus-4-7":     {"in": 15.0, "out": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6":   {"in": 3.0,  "out": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":    {"in": 1.0,  "out": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
}


def _estimate_cost_usd(model: str, usage: dict) -> float:
    p = _MODEL_PRICING_PER_MTOK.get(model, _MODEL_PRICING_PER_MTOK["claude-sonnet-4-6"])
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    cw_tok = usage.get("cache_creation_input_tokens", 0) or 0
    cr_tok = usage.get("cache_read_input_tokens", 0) or 0
    return (
        in_tok * p["in"] / 1_000_000 +
        out_tok * p["out"] / 1_000_000 +
        cw_tok * p["cache_write"] / 1_000_000 +
        cr_tok * p["cache_read"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def _run_pipeline_core(meta: dict, paper_text: str,
                       dry_run: bool = True) -> PipelineResult:
    """Run the full pipeline given pre-resolved paper text + metadata.

    Returns a PipelineResult. Never raises on per-step failures — errors are
    collected into result.errors and the pipeline continues where possible.
    """
    result = PipelineResult()
    result.doi = meta.get("doi")
    result.fingerprint = meta.get("fingerprint_sha256")
    result.paper_slug = _pick_paper_slug(meta)
    # Stash the chosen slug in meta so downstream prompts have it without
    # having to re-derive from potentially-empty DOI.
    meta["paper_slug"] = result.paper_slug
    meta["paper_permalink"] = f"/knowledge/papers/{result.paper_slug}/"

    client = Anthropic()

    # 1. Extract findings.
    try:
        findings_raw, cost_extract = _run_extractor(client, meta, paper_text)
        result.findings_proposed = len(findings_raw)
        result.cost_usd += cost_extract
    except Exception as e:
        result.errors.append(f"extractor failed: {e}")
        result.status = "error"
        return result

    # 2. Deterministic verify.
    findings_after_det = _deterministic_verify(findings_raw, paper_text)
    result.findings_after_deterministic = len(findings_after_det)
    if not findings_after_det:
        result.errors.append(
            f"deterministic verify dropped all {len(findings_raw)} findings "
            f"(no verbatim quote matched the paper text)"
        )
        result.status = "error"
        return result

    # 3. Model verify.
    try:
        verdict, cost_verify = _run_verifier(client, findings_after_det, meta, paper_text)
        result.cost_usd += cost_verify
    except Exception as e:
        result.errors.append(f"verifier failed: {e}")
        # Conservative fallback: accept only deterministic-verified findings.
        accepted = findings_after_det
        revised, rejected = [], []
    else:
        accepted, revised, rejected = _apply_verdict(findings_after_det, verdict)

    final_findings = accepted + revised
    result.findings_accepted = len(accepted)
    result.findings_revised = len(revised)
    result.findings_rejected = len(rejected)

    if not final_findings:
        result.errors.append("verifier rejected all findings")
        result.status = "error"
        return result

    # 4. DB writes. Always attempt, as long as we have *some* key — either
    # a real DOI or a fingerprint. No-DOI papers get 'sha256:<fp>' as their
    # canonical key so subsequent ingests hit the dedup guard.
    if _paper_key(meta):
        try:
            _upsert_paper_findings(meta, final_findings)
            _upsert_literature_note_with_fingerprint(meta, final_findings)
        except Exception as e:
            result.errors.append(f"DB write failed: {e}")

    topic_slugs_touched = sorted({
        t for f in final_findings for t in (f.get("topic_tags") or [])
    })
    result.topics_touched = topic_slugs_touched
    if topic_slugs_touched:
        try:
            _upsert_topics(topic_slugs_touched)
        except Exception as e:
            result.errors.append(f"topics upsert failed: {e}")

    # 4b. Digit-substring drift check (V1 Phase 2b).
    # Flag any finding whose numeric claims don't substring-match the verbatim
    # quote. Mode is config-driven: jobs.wiki_pipeline.digit_substring_blocking
    # in data/tealc_config.json. Toggle from the Control tab.
    _blocking = _digit_subst_blocking_enabled()
    try:
        _digit_fails = _digit_substring_check(final_findings)
    except Exception as e:
        _digit_fails = []
        print(f"[wiki_pipeline] digit_substring_check raised: {e}")
    if _digit_fails:
        for fail in _digit_fails:
            try:
                record_output(
                    kind="wiki_warning",
                    job_name=f"{_JOB_NAME}.digit_substring_drift_check",
                    model="none",
                    project_id=None,
                    content_md=(
                        f"Finding #{fail['finding_idx']} in "
                        f"{meta.get('doi') or meta.get('paper_slug')}:\n"
                        f"- missing numeric: {fail['missing_numeric']}\n"
                        f"- tilde_allowed: {fail['allow_tilde']}\n"
                        f"- claim: {fail['claim']}\n"
                        f"- quote: {fail['quote']}\n"
                    ),
                    tokens_in=0,
                    tokens_out=0,
                    provenance={
                        "paper_doi": meta.get("doi"),
                        "paper_slug": meta.get("paper_slug"),
                        "check": "digit_substring_ok",
                        "mode": "blocking" if _blocking else "dry_run",
                    },
                )
            except Exception as e:
                print(f"[wiki_pipeline] ledger write failed for digit-drift: {e}")
        msg = (
            f"digit_substring_drift_check: {len(_digit_fails)} finding(s) "
            f"flagged ({'BLOCKING' if _blocking else 'dry-run'})"
        )
        print(f"[wiki_pipeline] {msg}")
        result.errors.append(msg)
        if _blocking:
            bad_idx = {f["finding_idx"] for f in _digit_fails}
            final_findings = [
                f for i, f in enumerate(final_findings) if i not in bad_idx
            ]
            if not final_findings:
                result.status = "error"
                return result

    # 5. Compose paper page markdown.
    try:
        paper_md = _compose_paper_page(meta, final_findings)
    except Exception as e:
        result.errors.append(f"paper page compose failed: {e}")
        result.status = "partial"
        return result

    # 6. Update each touched topic page via the topic writer agent.
    try:
        repo = website_repo_path()
    except RepoNotFound as e:
        result.errors.append(f"website repo not found: {e}")
        result.status = "partial"
        return result

    files_to_stage: dict[str, str] = {
        f"{_WEBSITE_PAPERS}/{result.paper_slug}.md": paper_md,
    }
    topic_edit_notes: dict[str, EditNote] = {}

    # Findings for topic writer: thin down to the fields it needs
    writer_findings_by_topic: dict[str, list[dict]] = {}
    for f in final_findings:
        for t in (f.get("topic_tags") or []):
            writer_findings_by_topic.setdefault(t, []).append({
                "doi": meta.get("doi"),
                "finding_text": f.get("finding_text"),
                "quote": f.get("quote"),
                "page": f.get("page"),
                "reasoning": f.get("reasoning"),
                "counter": f.get("counter"),
                "source_paper_title": meta.get("title") or meta.get("doi"),
            })

    for slug, findings_for_topic in writer_findings_by_topic.items():
        existing, existing_papers, existing_category = _read_existing_topic_page(repo, slug)
        # Split body into preserved regions (outside markers) and auto region
        # (inside markers). The writer sees ONLY the auto region as its
        # "existing body" input, and its output gets spliced back into the
        # auto region while the before/after regions stay verbatim.
        _before, old_auto, _after, _had = _split_body_by_markers(existing)
        try:
            known = _load_research_topics().get("topics", [])
            topic_meta = next((t for t in known if t.get("slug") == slug), {})
            topic_title = topic_meta.get("title") or slug.replace("_", " ").title()

            writer_result, cost_writer = _run_topic_writer(
                client, slug, topic_title, old_auto, findings_for_topic, meta,
            )
            result.cost_usd += cost_writer

            new_auto_body = writer_result.get("body_md", "")
            body_md = _splice_auto_region(new_auto_body, existing)
            edit_note_raw = writer_result.get("edit_note", {}) or {}
            edit_note = EditNote(
                what_changed=edit_note_raw.get("what_changed", ""),
                why_changed=edit_note_raw.get("why_changed", ""),
                evidence_quote=edit_note_raw.get("evidence_quote", ""),
                counter_argument=edit_note_raw.get("counter_argument", ""),
            )
            if not edit_note.is_complete():
                result.errors.append(
                    f"topic {slug}: edit_note incomplete, skipping commit"
                )
                continue
            topic_edit_notes[slug] = edit_note

            # Union the existing papers_supporting (parsed from the old
            # frontmatter above) with the current paper's DOI so the list
            # accumulates across ingests rather than overwriting.
            # Prefer real DOI in papers_supporting; fall back to sha256: pseudo-
            # DOI so no-DOI papers still appear in the topic's paper-count.
            current_key = _paper_key(meta)
            papers_supporting = sorted(set(existing_papers) | {current_key} - {""})
            files_to_stage[f"{_WEBSITE_TOPICS}/{slug}.md"] = _compose_topic_page(
                slug, body_md, papers_supporting, existing_category,
            )
        except Exception as e:
            result.errors.append(f"topic {slug} writer failed: {e}")

    # 7. Stage files (dry-run-safe; always produces a diff without committing).
    try:
        stage_result = stage_files(files_to_stage, repo_path=repo)
        result.paths_written = stage_result.paths_written
        result.diff = stage_result.diff
    except PathNotAllowed as e:
        result.errors.append(f"path allowlist rejected: {e}")
        result.status = "partial"
        return result
    except Exception as e:
        result.errors.append(f"stage_files failed: {e}")
        result.status = "partial"
        return result

    # 8. Run critic on each generated page.
    try:
        for rel_path, content in files_to_stage.items():
            crit = critic_pass(content, rubric_name="wiki_edit")
            _log_wiki_edit(
                meta=meta, rel_path=rel_path, content=content,
                edit_note=topic_edit_notes.get(
                    os.path.basename(rel_path).rsplit(".", 1)[0],
                    EditNote(
                        what_changed=f"Ingested paper {meta.get('doi') or meta.get('fingerprint_sha256', '')[:12]}",
                        why_changed="New paper ingestion via wiki_pipeline.",
                        evidence_quote=(final_findings[0].get("quote") if final_findings else ""),
                        counter_argument=(final_findings[0].get("counter") if final_findings else ""),
                    ),
                ),
                critic_score=crit.get("score"),
                critic_notes=crit.get("overall_notes"),
            )
    except Exception as e:
        result.errors.append(f"critic/ledger logging failed: {e}")

    # 9. Commit or dry-run.
    if dry_run:
        result.status = "dry_run"
        return result

    # Non-dry-run: commit + push. Paper-page commit carries an overall edit note.
    overall_note = EditNote(
        what_changed=(
            f"Ingested {meta.get('title') or meta.get('doi') or result.paper_slug}; "
            f"{result.findings_accepted + result.findings_revised} findings verified; "
            f"touched {len(topic_slugs_touched)} topic(s)."
        ),
        why_changed="Wiki pipeline run: new paper ingestion produced verified findings and topic-page updates.",
        evidence_quote=(final_findings[0].get("quote") if final_findings else ""),
        counter_argument=(
            final_findings[0].get("counter") if final_findings
            else "Automated ingest — Heath has not reviewed the output."
        ),
    )
    try:
        sha = commit_and_push(
            message=f"add {result.paper_slug} ({result.findings_accepted + result.findings_revised} findings)",
            edit_note=overall_note,
            paths=stage_result.paths_written,
            repo_path=repo,
        )
        result.committed_sha = sha
        result.status = "success"
    except PullConflict as e:
        result.errors.append(f"pull conflict: {e}")
        result.status = "partial"
    except PushBlocked as e:
        result.errors.append(f"push blocked (privacy hook likely): {e}")
        result.status = "partial"
    except Exception as e:
        result.errors.append(f"commit/push failed: {e}")
        result.status = "partial"

    return result


def _log_wiki_edit(meta: dict, rel_path: str, content: str, edit_note: EditNote,
                   critic_score: Any = None, critic_notes: Any = None) -> None:
    """Log one wiki edit to output_ledger with full teaching-mode provenance."""
    try:
        provenance = {
            "rel_path": rel_path,
            "paper_doi": meta.get("doi"),
            "paper_fingerprint": meta.get("fingerprint_sha256"),
            "paper_title": meta.get("title"),
            "source": meta.get("source"),
            "edit_note": {
                "what_changed": edit_note.what_changed,
                "why_changed": edit_note.why_changed,
                "evidence_quote": edit_note.evidence_quote,
                "counter_argument": edit_note.counter_argument,
            },
            "critic_score": critic_score,
            "critic_notes": critic_notes,
        }
        record_output(
            kind="wiki_edit",
            job_name=_JOB_NAME,
            model=_EXTRACTOR_MODEL,
            project_id=None,
            content_md=content[:8000],  # cap size; full content lives in git
            tokens_in=0,
            tokens_out=0,
            provenance=provenance,
        )
    except Exception as e:
        print(f"[wiki_pipeline] ledger write failed for {rel_path}: {e}")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _check_already_ingested(
    doi: Optional[str], fingerprint: Optional[str],
) -> tuple[bool, str]:
    """Return (already_ingested, reason). Checks multiple dedup paths:
    1. paper_findings by real DOI (if caller supplied one)
    2. paper_findings by 'sha256:<fingerprint>' pseudo-DOI (for Drive papers)
    3. literature_notes by pdf_fingerprint column
    Any hit returns True.
    """
    if not doi and not fingerprint:
        return (False, "")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        if doi:
            row = conn.execute(
                "SELECT COUNT(*) FROM paper_findings WHERE doi=?", (doi,),
            ).fetchone()
            if row and row[0] > 0:
                return (True, f"doi={doi} already has {row[0]} findings "
                              f"in paper_findings")
        if fingerprint:
            # (a) sha256:<fp> pseudo-DOI lookup in paper_findings
            pseudo = f"sha256:{fingerprint}"
            row = conn.execute(
                "SELECT COUNT(*) FROM paper_findings WHERE doi=?", (pseudo,),
            ).fetchone()
            if row and row[0] > 0:
                return (True, f"fingerprint {fingerprint[:12]}… already has "
                              f"{row[0]} findings in paper_findings (no-DOI path)")
            # (b) literature_notes fingerprint column
            row = conn.execute(
                "SELECT doi FROM literature_notes WHERE pdf_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row:
                prev_doi = row[0] or "(no DOI)"
                return (True, f"fingerprint {fingerprint[:12]}… already in "
                              f"literature_notes (doi={prev_doi})")
    finally:
        conn.close()
    return (False, "")


def _already_ingested_result(meta: dict, reason: str) -> PipelineResult:
    """Build a cheap no-op PipelineResult indicating the paper was already
    ingested. No API calls made; returned immediately from the three entry
    points when the guard trips."""
    result = PipelineResult()
    result.status = "already_ingested"
    result.doi = meta.get("doi")
    result.fingerprint = meta.get("fingerprint_sha256")
    result.paper_slug = _pick_paper_slug(meta)
    result.errors.append(f"skipped: {reason} (pass force=True to re-ingest)")
    return result


def run_on_drive_pdf(file_id: str, doi: Optional[str] = None,
                     title: Optional[str] = None,
                     dry_run: bool = True,
                     force: bool = False) -> PipelineResult:
    """Ingest a PDF from Google Drive by file ID.

    If the paper is already in the DB (by DOI or SHA256 fingerprint) the
    pipeline returns early with status='already_ingested' and ZERO API calls.
    Pass force=True to bypass and re-ingest.
    """
    pdf_bytes, meta = _resolve_drive_source(file_id)
    if doi:
        meta["doi"] = doi
    if title:
        meta["title"] = title
    elif meta.get("title_from_filename"):
        # Use the Drive filename as a title fallback so permalinks are human-
        # readable even when no DOI is supplied.
        meta["title"] = meta["title_from_filename"]

    if not force:
        already, reason = _check_already_ingested(
            meta.get("doi"), meta.get("fingerprint_sha256"),
        )
        if already:
            return _already_ingested_result(meta, reason)

    paper_text = _extract_pdf_text(meta["local_pdf_path"])
    return _run_pipeline_core(meta, paper_text, dry_run=dry_run)


def run_on_local_pdf(pdf_path: str, doi: Optional[str] = None,
                     title: Optional[str] = None,
                     dry_run: bool = True,
                     force: bool = False) -> PipelineResult:
    """Ingest a local PDF file. DOI is optional but recommended for DB/URL keying.
    Skips re-ingestion unless force=True."""
    pdf_bytes, meta = _resolve_local_source(pdf_path, doi=doi)
    if title:
        meta["title"] = title

    if not force:
        already, reason = _check_already_ingested(
            meta.get("doi"), meta.get("fingerprint_sha256"),
        )
        if already:
            return _already_ingested_result(meta, reason)

    paper_text = _extract_pdf_text(meta["local_pdf_path"])
    return _run_pipeline_core(meta, paper_text, dry_run=dry_run)


def run_on_doi(doi: str, dry_run: bool = True,
               force: bool = False) -> PipelineResult:
    """Ingest a paper via its DOI using Europe PMC full-text XML.

    Raises a clear error if the paper is not OA on Europe PMC; fall back to
    run_on_local_pdf() or run_on_drive_pdf() with the downloaded PDF.

    Skips re-ingestion unless force=True.
    """
    # DOI guard can run before source resolution (cheaper — no network)
    if not force:
        already, reason = _check_already_ingested(doi, None)
        if already:
            return _already_ingested_result({"doi": doi}, reason)

    full_text, meta = _resolve_doi_source(doi)
    return _run_pipeline_core(meta, full_text, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Manual run
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest one paper into the lab wiki.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--doi", help="Fetch via Europe PMC by DOI")
    src.add_argument("--drive-file-id", help="Google Drive file ID (PDF)")
    src.add_argument("--local-pdf", help="Path to a local PDF file")
    parser.add_argument("--title", help="Override paper title (local / Drive paths)")
    parser.add_argument("--source-doi", help="DOI to attach (local / Drive paths)")
    parser.add_argument("--execute", action="store_true",
                        help="Commit + push. Default is dry-run only.")
    args = parser.parse_args(argv)

    dry_run = not args.execute

    if args.doi:
        result = run_on_doi(args.doi, dry_run=dry_run)
    elif args.drive_file_id:
        result = run_on_drive_pdf(args.drive_file_id, doi=args.source_doi,
                                  title=args.title, dry_run=dry_run)
    else:
        result = run_on_local_pdf(args.local_pdf, doi=args.source_doi,
                                  title=args.title, dry_run=dry_run)

    print(result.to_summary_str())
    if result.errors:
        print("\nErrors:")
        for e in result.errors:
            print(f"  - {e}")
    if dry_run and result.diff:
        print("\n--- DIFF PREVIEW (truncated at 3000 chars) ---")
        print(result.diff[:3000])
    return 0 if result.status in ("dry_run", "success") else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
