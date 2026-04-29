"""data_introspect.py — helpers for locating and describing project data dirs.

Two public functions:
  inspect_project_data(project_id, max_depth=3, max_files=200) -> dict
  propose_data_dir(project_id) -> dict

Stdlib only; no pip dependencies.
"""
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent.scheduler import DB_PATH

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_HOME = Path.home()

# Ordered list of root directories to search when proposing a data_dir.
_SEARCH_ROOTS = [
    _HOME / "Google Drive" / "My Drive" / "00-Lab-Agent" / "data",
    _HOME / "Google Drive" / "My Drive",
    _HOME / "Desktop" / "GitHub" / "coleoguy.github.io",
    _HOME / "Desktop" / "GitHub" / "coleoguy",
    _HOME / "Research",
]


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_project(project_id: str):
    with _open_db() as conn:
        return conn.execute(
            "SELECT * FROM research_projects WHERE id = ?", (project_id,)
        ).fetchone()


def _iso(ts: float) -> str:
    """Convert a POSIX timestamp to ISO-8601 string (UTC)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# inspect_project_data
# ---------------------------------------------------------------------------

def inspect_project_data(
    project_id: str,
    max_depth: int = 3,
    max_files: int = 200,
) -> dict:
    """Walk a project's data_dir and return a structured description.

    Returns a dict with keys:
        project_id, project_name, data_dir, exists, total_files, total_bytes,
        tree, summary_by_extension, most_recent_iso
    """
    row = _fetch_project(project_id)
    if row is None:
        return {
            "project_id": project_id,
            "project_name": None,
            "data_dir": None,
            "exists": False,
            "total_files": 0,
            "total_bytes": 0,
            "tree": [],
            "summary_by_extension": {},
            "most_recent_iso": None,
            "error": "project_id not found in research_projects",
        }

    name = row["name"]
    data_dir = row["data_dir"] or ""

    empty_result: dict = {
        "project_id": project_id,
        "project_name": name,
        "data_dir": data_dir or None,
        "exists": False,
        "total_files": 0,
        "total_bytes": 0,
        "tree": [],
        "summary_by_extension": {},
        "most_recent_iso": None,
    }

    if not data_dir:
        return empty_result

    root = Path(data_dir)
    if not root.exists():
        return empty_result

    # Walk up to max_depth; collect files + dirs.
    entries: list[dict] = []
    total_files = 0
    total_bytes = 0
    ext_stats: dict[str, dict] = {}

    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root):
        current_path = Path(dirpath)
        depth = len(current_path.parts) - root_depth
        if depth > max_depth:
            dirnames.clear()
            continue

        # Record directory entry (not root itself)
        if current_path != root:
            try:
                st = current_path.stat()
                rel = str(current_path.relative_to(root))
                entries.append({
                    "path": rel,
                    "bytes": 0,
                    "modified_iso": _iso(st.st_mtime),
                    "kind": "dir",
                })
            except OSError:
                pass

        for fname in filenames:
            fpath = current_path / fname
            try:
                st = fpath.stat()
            except OSError:
                continue
            size = st.st_size
            mtime = st.st_mtime
            rel = str(fpath.relative_to(root))
            ext = fpath.suffix.lower() if fpath.suffix else "(no ext)"

            total_files += 1
            total_bytes += size

            # Extension summary
            if ext not in ext_stats:
                ext_stats[ext] = {"count": 0, "bytes": 0}
            ext_stats[ext]["count"] += 1
            ext_stats[ext]["bytes"] += size

            entries.append({
                "path": rel,
                "bytes": size,
                "modified_iso": _iso(mtime),
                "kind": "file",
            })

    # Sort by modified_iso descending, then trim to max_files (files only for trimming).
    entries.sort(key=lambda e: e["modified_iso"], reverse=True)
    file_entries = [e for e in entries if e["kind"] == "file"]
    dir_entries = [e for e in entries if e["kind"] == "dir"]
    trimmed = (file_entries[:max_files] + dir_entries)
    trimmed.sort(key=lambda e: e["modified_iso"], reverse=True)

    most_recent = file_entries[0]["modified_iso"] if file_entries else None

    return {
        "project_id": project_id,
        "project_name": name,
        "data_dir": str(root),
        "exists": True,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "tree": trimmed,
        "summary_by_extension": ext_stats,
        "most_recent_iso": most_recent,
    }


# ---------------------------------------------------------------------------
# propose_data_dir
# ---------------------------------------------------------------------------

def _tokenise_project(row):
    """Return (keyword_tokens, significant_name_words) from a project row.

    keyword_tokens: every non-empty token from the keywords field (comma/space split).
    significant_name_words: words >= 5 chars from name that aren't stop words.
    """
    _STOP = {
        "about", "after", "again", "along", "among", "analysis", "between",
        "during", "effect", "effects", "evolution", "genome", "genomics",
        "model", "models", "other", "project", "research", "study", "their",
        "there", "these", "those", "under", "using", "which", "with",
    }

    kw_raw = row["keywords"] or ""
    # Split on comma, semicolon, whitespace
    kw_tokens = [
        t.strip().lower()
        for t in re.split(r"[,;\s]+", kw_raw)
        if len(t.strip()) >= 3
    ]

    name_words = [
        w.lower()
        for w in re.split(r"\W+", row["name"] or "")
        if len(w) >= 5 and w.lower() not in _STOP
    ]

    return kw_tokens, name_words


def _word_boundary_match(token: str, text: str) -> bool:
    """True if token appears as a whole word (case-insensitive) in text."""
    try:
        return bool(re.search(r"\b" + re.escape(token) + r"\b", text, re.IGNORECASE))
    except re.error:
        return False


def _score_directory(
    dirpath: Path,
    kw_tokens: list[str],
    name_words: list[str],
    now_ts: float,
):
    """Score a candidate directory.

    Returns (score, match_reasons, file_count, most_recent_iso).
    score == 0.0 means no match.
    """
    dir_name = dirpath.name

    hits: list[str] = []

    # Check directory name against tokens and name words
    all_tokens = list(set(kw_tokens + name_words))
    for tok in all_tokens:
        if _word_boundary_match(tok, dir_name):
            hits.append(tok)

    # Shallow scan of immediate files — check filenames for keyword hits
    try:
        immediate_files = list(dirpath.iterdir())
    except PermissionError:
        return 0.0, [], 0, None

    file_count = 0
    most_recent_ts = None

    for item in immediate_files:
        if item.is_file():
            file_count += 1
            try:
                mt = item.stat().st_mtime
                if most_recent_ts is None or mt > most_recent_ts:
                    most_recent_ts = mt
            except OSError:
                pass
            # Check filename for keyword hits (only kw_tokens for filename matching)
            for tok in kw_tokens:
                if _word_boundary_match(tok, item.stem):
                    if tok not in hits:
                        hits.append(tok)
        elif item.is_dir():
            pass  # Count only direct files for density calc

    if not hits:
        return 0.0, [], file_count, None

    # Recency factor: days-old capped at 730; newer = higher factor
    most_recent_iso = None
    recency_factor = 0.5  # default if no files
    if most_recent_ts is not None:
        most_recent_iso = _iso(most_recent_ts)
        days_old = max(0.0, (now_ts - most_recent_ts) / 86400)
        recency_factor = 1.0 / (1.0 + days_old / 90.0)  # halves at ~90 days old

    # Density: hits per directory-name token (rough relevance)
    denom = max(1, file_count)
    density = len(hits) / denom

    score = density * recency_factor * (1 + len(hits))  # weight by raw hit count too
    return score, hits, file_count, most_recent_iso


def propose_data_dir(project_id: str) -> dict:
    """Propose candidate data directories for a project that lacks data_dir.

    Returns a dict with keys:
        project_id, project_name, current_data_dir, candidates
    where each candidate has: path, match_reason, file_count, most_recent_iso.
    """
    row = _fetch_project(project_id)
    if row is None:
        return {
            "project_id": project_id,
            "project_name": None,
            "current_data_dir": None,
            "candidates": [],
            "error": "project_id not found in research_projects",
        }

    name = row["name"]
    current_data_dir = row["data_dir"] or None

    kw_tokens, name_words = _tokenise_project(row)
    if not kw_tokens and not name_words:
        return {
            "project_id": project_id,
            "project_name": name,
            "current_data_dir": current_data_dir,
            "candidates": [],
        }

    now_ts = datetime.now(tz=timezone.utc).timestamp()
    scored: list[tuple[float, dict]] = []
    seen: set[str] = set()  # avoid duplicate paths

    for root in _SEARCH_ROOTS:
        if not root.exists():
            continue
        root_depth = len(root.parts)

        for dirpath, dirnames, _filenames in os.walk(root):
            current = Path(dirpath)
            depth = len(current.parts) - root_depth

            if depth > 3:
                dirnames.clear()
                continue

            if depth == 0:
                # Don't score the root itself; descend into it
                continue

            canonical = str(current.resolve())
            if canonical in seen:
                dirnames.clear()
                continue
            seen.add(canonical)

            score, hits, fc, mri = _score_directory(
                current, kw_tokens, name_words, now_ts
            )
            if score > 0:
                reason = f"keyword match: {', '.join(hits[:4])}"
                scored.append((score, {
                    "path": canonical,
                    "match_reason": reason,
                    "file_count": fc,
                    "most_recent_iso": mri,
                }))

    # Sort by score descending, keep top 5
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [item for _score, item in scored[:5]]

    return {
        "project_id": project_id,
        "project_name": name,
        "current_data_dir": current_data_dir,
        "candidates": candidates,
    }
