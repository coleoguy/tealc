"""
Data schemas for the Tealc blinded evaluation harness.

These dataclasses define the wire format for all evaluation artifacts.
EvalInput is what reviewers receive; AnswerKey is private and never distributed.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Optional


@dataclasses.dataclass
class EvalInput:
    """A single blinded output item sent to an external reviewer."""
    blinded_id: str          # Fresh UUID4; primary key for reviewer scoring
    kind: str                # One of: grant_draft, hypothesis, analysis, literature_synthesis
    content: str             # Cleaned, de-identified output text
    domain: str              # One of the four rubric domains
    created_iso: str         # Rounded to Monday-of-week in UTC (YYYY-MM-DD)
    context_hint: Optional[str]  # Sanitized short phrase, e.g. "eukaryotic karyotype evolution"


@dataclasses.dataclass
class ReviewRecord:
    """A completed review submitted by one external reviewer for one EvalInput."""
    blinded_id: str
    reviewer_id: str
    domain: str
    scores: dict[str, int]   # Keys: rigor, novelty, grounding, clarity, feasibility; values 1-5
    qualitative_notes: str
    flags: list[str]
    submitted_iso: str       # ISO 8601 datetime string


@dataclasses.dataclass
class EvalBatch:
    """Metadata envelope for a collection of EvalInput items."""
    batch_id: str
    created_iso: str
    domain: str
    kind_filter: Optional[str]
    date_range: tuple[str, str]   # (since_iso, until_iso)
    entries: list[EvalInput]


@dataclasses.dataclass
class AnswerKey:
    """Private mapping from blinded_id to original ledger_id. Never distributed to reviewers."""
    batch_id: str
    mapping: dict[str, int]  # blinded_id -> original ledger_id (int)


# ---------------------------------------------------------------------------
# Module-level serialization helpers
# ---------------------------------------------------------------------------

def to_jsonl(objs: list) -> str:
    """Serialize a list of dataclass instances to JSONL (one JSON object per line)."""
    lines = []
    for obj in objs:
        lines.append(json.dumps(dataclasses.asdict(obj)))
    return "\n".join(lines)


def from_jsonl(text: str, cls) -> list:
    """Deserialize JSONL text into a list of instances of the given dataclass."""
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        # Reconstruct nested dataclasses where needed
        if cls is EvalInput:
            results.append(EvalInput(**data))
        elif cls is ReviewRecord:
            results.append(ReviewRecord(**data))
        elif cls is EvalBatch:
            data["entries"] = [EvalInput(**e) for e in data.get("entries", [])]
            data["date_range"] = tuple(data["date_range"])
            results.append(EvalBatch(**data))
        elif cls is AnswerKey:
            results.append(AnswerKey(**data))
        else:
            # Generic fallback: attempt direct construction
            results.append(cls(**data))
    return results
