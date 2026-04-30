"""
Blinding logic for the Tealc evaluation harness.

blind_entry() takes a raw ledger output dict and returns an EvalInput with all
identifying information removed or replaced with stable pseudonyms.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from evaluations.schema import EvalInput


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEALC_PATTERN = re.compile(r"\bTealc\b|\bTEALC\b|\btealc\b")
_MODEL_PATTERN = re.compile(
    r"claude[-_](?:opus|sonnet|haiku)[-_0-9.]*"
    r"|sonnet\s+4\.6"
    r"|opus\s+4\.7"
    r"|haiku\s+4\.5",
    re.IGNORECASE,
)
_PROJECT_ID_PATTERN = re.compile(
    r"project_id\s*=\s*\S+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Extended DENY_PATTERNS — backported from agent/privacy.py
# Strips data_dir paths, grant codes, person names, and email addresses that
# the original blind.py missed.  Patterns are applied in blind_entry() as
# simple regex substitutions before the text is handed to reviewers.
# ---------------------------------------------------------------------------

# Absolute filesystem paths (any leading /Users/, /home/, /data/, Windows C:\)
_DATA_DIR_PATTERN = re.compile(
    r"(?:/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+)"
    r"(?:/[^\s,;\"'`\n]*)?",
    re.IGNORECASE,
)
_WINDOWS_PATH_PATTERN = re.compile(
    r"[A-Za-z]:\\(?:[^\s,;\"'`\n]+)?",
    re.IGNORECASE,
)

# Grant / funding codes (NIH, NSF, DOD, Google.org, CPRIT, Sloan, Pew, Templeton, Keck)
_GRANT_CODE_PATTERN = re.compile(
    r"\b(?:R01|R35|R21|R03|P01|U01|T32|K99|K01|F32|R00)"
    r"[-_]?[A-Z]{2}[0-9]{6,}\b"      # NIH style: R01-HG012345
    r"|(?:NSF[-\s]?DEB[-\s]?[0-9]+)"  # NSF-DEB-1234567
    r"|(?:Google\.org\s+Grant\s+[A-Z0-9/-]+)",
    re.IGNORECASE,
)

# Grant *program* keywords that should be vague-ified in reviewer-facing text
_GRANT_PROGRAM_PATTERN = re.compile(
    r"\b(?:MIRA|R35|R01|R21|NIGMS|NHGRI|CPRIT|NSF\s+DEB|DOD|USDA|RateScape"
    r"|Google\.org|Sloan|Pew|Templeton|Keck)\b",
    re.IGNORECASE,
)

# Email addresses (any @-containing token not caught by lab-people logic)
_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# "Heath Blackmon" and institutional variants
_PI_NAME_PATTERN = re.compile(
    r"\bheath\s+blackmon\b|\bblackmon\b|\bheath\b(?=\s+blackmon)",
    re.IGNORECASE,
)

# TAMU / institution identifiers
_INSTITUTION_PATTERN = re.compile(
    r"\bTexas\s+A&M\b|\bTAMU\b|\btamu\.edu\b",
    re.IGNORECASE,
)

_LAB_PEOPLE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "data",
    "lab_people.json",
)

# Alphabet of pseudonym labels (Person_A .. Person_Z, then Person_AA, etc.)
_PSEUDONYM_LABELS = [f"Person_{chr(65 + i)}" for i in range(26)]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def load_lab_people() -> list[str]:
    """Return the list of lab-member name strings from data/lab_people.json."""
    path = os.path.normpath(_LAB_PEOPLE_PATH)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("names", [])


def blind_entry(
    entry: dict,
    pseudonym_map: Optional[dict[str, str]] = None,
) -> EvalInput:
    """
    Blind a single ledger output dict and return an EvalInput.

    Parameters
    ----------
    entry:
        A dict as returned by agent.ledger.query_outputs(). Expected keys
        include at least 'content', 'kind', and optionally 'domain',
        'created_iso', 'context_hint', 'ledger_id', 'project_id'.
    pseudonym_map:
        Shared mutable dict for stable pseudonym assignment across multiple
        calls in the same batch. Pass the same dict for every entry in a batch.
        If None, a fresh dict is created (pseudonyms won't be stable across
        calls).

    Returns
    -------
    EvalInput with blinded_id, cleaned content, and rounded timestamp.
    """
    if pseudonym_map is None:
        pseudonym_map = {}

    content = entry.get("content", "")
    # Also check content_md key (some callers use content_md)
    if not content:
        content = entry.get("content_md", "")

    # 1. Strip filesystem paths (data_dir — privacy bug fix)
    content = _DATA_DIR_PATTERN.sub("[PATH_REDACTED]", content)
    content = _WINDOWS_PATH_PATTERN.sub("[PATH_REDACTED]", content)

    # 2. Strip grant codes and program keywords (privacy bug fix)
    content = _GRANT_CODE_PATTERN.sub("[GRANT_CODE_REDACTED]", content)
    content = _GRANT_PROGRAM_PATTERN.sub("[GRANT_PROGRAM]", content)

    # 3. Strip email addresses (privacy bug fix)
    content = _EMAIL_PATTERN.sub("[EMAIL_REDACTED]", content)

    # 4. Strip PI name variants (privacy bug fix)
    content = _PI_NAME_PATTERN.sub("the PI", content)

    # 5. Strip institution identifiers (privacy bug fix)
    content = _INSTITUTION_PATTERN.sub("[INSTITUTION]", content)

    # 6. Strip Tealc name
    content = _TEALC_PATTERN.sub("the system", content)

    # 7. Strip model names
    content = _MODEL_PATTERN.sub("the model", content)

    # 8. Replace lab-people names with stable pseudonyms
    lab_people = load_lab_people()
    # Sort longest first so full names are caught before first names
    for name in sorted(lab_people, key=len, reverse=True):
        if name in content:
            if name not in pseudonym_map:
                idx = len([k for k in pseudonym_map if not k.startswith("project_")])
                label = _PSEUDONYM_LABELS[idx] if idx < len(_PSEUDONYM_LABELS) else f"Person_{idx}"
                pseudonym_map[name] = label
            content = content.replace(name, pseudonym_map[name])

    # 9. Replace project IDs (pattern-based and known IDs from the entry)
    def _project_label(key: str) -> str:
        """Return or create a stable Project_* label for the given key."""
        project_keys = [k for k in pseudonym_map if k.startswith("project_")]
        if key not in pseudonym_map:
            idx = len(project_keys)
            greek = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon",
                     "Zeta", "Eta", "Theta", "Iota", "Kappa"]
            pseudonym_map[key] = f"Project_{greek[idx]}" if idx < len(greek) else f"Project_{idx}"
        return pseudonym_map[key]

    def _replace_project_id_match(m: re.Match) -> str:
        raw = m.group(0)
        return _project_label(f"project_{raw}")

    content = _PROJECT_ID_PATTERN.sub(_replace_project_id_match, content)

    # Also blind a known project_id field from the entry dict itself
    known_project_id = entry.get("project_id")
    if known_project_id:
        key = f"project_known_{known_project_id}"
        label = _project_label(key)
        content = content.replace(str(known_project_id), label)

    # 10. Round created_iso to Monday of the week in UTC
    created_iso = entry.get("created_iso", "")
    rounded_date = _round_to_monday(created_iso)

    # 11. Sanitize context_hint
    raw_hint = entry.get("context_hint", None)
    context_hint = _sanitize_hint(raw_hint) if raw_hint else None

    return EvalInput(
        blinded_id=str(uuid.uuid4()),
        kind=entry.get("kind", "analysis"),
        content=content,
        domain=entry.get("domain", ""),
        created_iso=rounded_date,
        context_hint=context_hint,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _round_to_monday(iso_str: str) -> str:
    """
    Round an ISO 8601 datetime string to the Monday of that week (UTC).
    Returns YYYY-MM-DD. Falls back to today's Monday on parse failure.
    """
    try:
        # Accept both date-only and full datetime strings
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        dt = datetime.now(tz=timezone.utc)
    # weekday(): Monday=0 ... Sunday=6
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def _sanitize_hint(hint: str) -> str:
    """
    Remove any obvious identifiers from a context hint string and truncate.
    Strips Tealc/model names; keeps it short (max 80 chars).
    """
    hint = _TEALC_PATTERN.sub("the system", hint)
    hint = _MODEL_PATTERN.sub("the model", hint)
    return hint.strip()[:80]


# ---------------------------------------------------------------------------
# Sanity check — run as: python -m evaluations.blind
# Asserts that the extended DENY_PATTERNS correctly redact sensitive tokens.
# ---------------------------------------------------------------------------

def _run_sanity_check() -> None:
    """Assert that sensitive tokens in a synthetic text are properly redacted."""
    SAMPLE = (
        "The analysis was run on /Users/labuser/data/project_xyz/results.csv "
        "under grant R01-HG012345 funded by NHGRI. "
        "Researcher Name at State University wrote this. "
        "Contact them at researcher@example.org or researcher@gmail.com. "
        "The Tealc system used claude-opus-4-5 for the critic pass. "
        "Lab Collaborator contributed the phylogenetic tree. "
        "project_id = proj_42"
    )
    entry = {
        "content": SAMPLE,
        "kind": "analysis",
        "domain": "macroevolution",
        "created_iso": "2026-04-27",
    }
    result = blind_entry(entry)
    text = result.content

    forbidden = [
        "/Users/blackmon",
        "R01-HG012345",
        "blackmon@tamu.edu",
        "hblackmon@gmail.com",
        "Tealc",
        "claude-opus",
        "Texas A&M",
    ]
    # Lab Collaborator would be caught by lab_people.json if present; skip if file absent
    failures = [tok for tok in forbidden if tok.lower() in text.lower()]
    if failures:
        raise AssertionError(
            f"blind.py sanity check FAILED — these tokens survived blinding: {failures}\n"
            f"Blinded text:\n{text}"
        )
    print("blind.py sanity check PASSED — all sensitive tokens redacted.")
    print(f"Blinded snippet: {text[:200]}...")


if __name__ == "__main__":
    _run_sanity_check()
