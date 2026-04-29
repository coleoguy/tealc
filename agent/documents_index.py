"""Documents index — categorized view of every doc Tealc creates or maintains.

Called by the /api/documents endpoint on the HQ dashboard (localhost:8001).
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "data"))
_DB_PATH = os.path.join(_DATA, "agent.db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mtime_iso(path: str) -> str:
    """Return UTC ISO timestamp for a path's mtime, or empty string on error."""
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _safe_json(s) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _dir_stats(dir_path: str) -> tuple[int, int]:
    """Return (file_count, largest_file_bytes) for a directory (non-recursive)."""
    count = 0
    largest = 0
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file():
                count += 1
                try:
                    sz = entry.stat().st_size
                    if sz > largest:
                        largest = sz
                except Exception:
                    pass
    except Exception:
        pass
    return count, largest


def _dir_newest_mtime(dir_path: str) -> str:
    """Return UTC ISO of the most-recently-modified file in a directory."""
    newest = 0.0
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file():
                try:
                    mt = entry.stat().st_mtime
                    if mt > newest:
                        newest = mt
                except Exception:
                    pass
    except Exception:
        pass
    if newest == 0.0:
        return _mtime_iso(dir_path)
    return datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Category 1: Drafts & docs in Google Drive
# ---------------------------------------------------------------------------

def _cat_drafts_in_drive() -> dict:
    items: list[dict] = []

    # --- Source A: overnight_drafts table ---
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT id, project_id, source_artifact_title, drafted_section, "
                "       draft_doc_url, reasoning, created_at, reviewed_at, outcome "
                "FROM overnight_drafts "
                "ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()

        # Pull matching output_ledger rows once so we can attach critic_score to drafts.
        try:
            conn2 = _db()
            ledger_map: dict = {}
            try:
                lrows = conn2.execute(
                    "SELECT id, project_id, critic_score, provenance_json "
                    "FROM output_ledger WHERE kind='grant_draft' "
                    "ORDER BY created_at DESC LIMIT 100"
                ).fetchall()
                for lr in lrows:
                    prov = _safe_json(lr["provenance_json"])
                    key = prov.get("source_artifact_id") or prov.get("draft_id")
                    if key is not None:
                        ledger_map[str(key)] = lr["critic_score"]
            except Exception:
                pass
            finally:
                conn2.close()
        except Exception:
            ledger_map = {}

        for r in rows:
            section = r["drafted_section"] or ""
            art_title = r["source_artifact_title"] or ""
            title = f"{section} — {art_title}" if art_title else section or f"Draft #{r['id']}"
            reasoning = r["reasoning"] or ""
            critic_score = ledger_map.get(str(r["id"]))
            items.append({
                "title": title,
                "subcategory": "Overnight grant drafts",
                "description": reasoning[:120],
                "modified_iso": r["created_at"] or "",
                "link_type": "external_url",
                "link": r["draft_doc_url"] or "",
                "extra": {
                    "reviewed": r["reviewed_at"] is not None,
                    "outcome": r["outcome"] or None,
                    "critic_score": critic_score,
                    "project_id": r["project_id"] or "",
                    "draft_id": r["id"],
                },
            })
    except Exception:
        pass

    # --- Source B: output_ledger rows whose provenance_json contains a doc_url ---
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT id, kind, project_id, content_md, provenance_json, created_at, critic_score "
                "FROM output_ledger "
                "WHERE provenance_json IS NOT NULL AND provenance_json != '' "
                "ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()

        ledger_items: list[dict] = []
        for r in rows:
            prov = _safe_json(r["provenance_json"])
            doc_url = prov.get("doc_url") or ""
            if not doc_url:
                continue
            kind = r["kind"] or ""
            project_id = r["project_id"] or ""
            created = r["created_at"] or ""
            date_part = created[:10] if created else ""
            if kind == "nas_case_packet":
                subcat = "NAS case packets"
            else:
                subcat = "Other Drive documents"
            title = f"{kind} — {project_id} — {date_part}".strip(" —")
            content = r["content_md"] or ""
            ledger_items.append({
                "title": title,
                "subcategory": subcat,
                "description": content[:120],
                "modified_iso": created,
                "link_type": "external_url",
                "link": doc_url,
                "extra": {"ledger_id": r["id"], "kind": kind, "project_id": project_id,
                          "critic_score": r["critic_score"]},
            })

        # Sort ledger items newest-first, cap at 30
        ledger_items.sort(key=lambda x: x["modified_iso"], reverse=True)
        items.extend(ledger_items[:30])
    except Exception:
        pass

    # Final sort: overnight drafts already came newest-first; merge by modified_iso
    items.sort(key=lambda x: x["modified_iso"] or "", reverse=True)

    return {
        "key": "drafts_in_drive",
        "title": "Drafts & docs in Google Drive",
        "description": "Grant section drafts and case packets Tealc wrote to Google Docs.",
        "priority_note": "Review these first — they're the primary feedback loop.",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Category 2: Analysis outputs
# ---------------------------------------------------------------------------

def _walk_run_dirs(root: str, script_names: list[str], subcat: str, cap: int = 20) -> list[dict]:
    """Yield items for each <run_id>/ subdir that contains a recognised script."""
    items: list[dict] = []
    if not os.path.isdir(root):
        return items
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.stat().st_mtime, reverse=True)
    except Exception:
        return items

    for entry in entries:
        if not entry.is_dir():
            continue
        dir_path = entry.path
        # Check if the dir contains a known script file
        has_script = any(os.path.isfile(os.path.join(dir_path, s)) for s in script_names)
        if not has_script:
            continue
        n_files, largest = _dir_stats(dir_path)
        newest_mtime = _dir_newest_mtime(dir_path)
        try:
            total_bytes = sum(os.path.getsize(os.path.join(dp, f))
                              for dp, _, fs in os.walk(dir_path) for f in fs)
        except Exception:
            total_bytes = 0
        items.append({
            "title": entry.name,
            "subcategory": subcat,
            "description": f"{n_files} files, largest = {largest:,} bytes",
            "modified_iso": newest_mtime,
            "link_type": "local_file",
            "link": os.path.abspath(dir_path),
            "extra": {"size_bytes": total_bytes, "file_count": n_files},
        })
        if len(items) >= cap:
            break
    return items


def _walk_plot_files(root: str, subcat: str, cap: int = 20) -> list[dict]:
    """Yield items for each file (or dir) in the nas_case_plots directory."""
    items: list[dict] = []
    if not os.path.isdir(root):
        return items
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.stat().st_mtime, reverse=True)
    except Exception:
        return items

    for entry in entries:
        abs_path = os.path.abspath(entry.path)
        if entry.is_file():
            try:
                size = entry.stat().st_size
            except Exception:
                size = 0
            items.append({
                "title": entry.name,
                "subcategory": subcat,
                "description": f"{size:,} bytes",
                "modified_iso": _mtime_iso(entry.path),
                "link_type": "local_file",
                "link": abs_path,
                "extra": {},
            })
        elif entry.is_dir():
            n_files, largest = _dir_stats(entry.path)
            items.append({
                "title": entry.name,
                "subcategory": subcat,
                "description": f"{n_files} files, largest = {largest:,} bytes",
                "modified_iso": _dir_newest_mtime(entry.path),
                "link_type": "local_file",
                "link": abs_path,
                "extra": {},
            })
        if len(items) >= cap:
            break
    return items


def _cat_analysis_outputs() -> dict:
    r_root = os.path.join(_DATA, "r_runs")
    py_root = os.path.join(_DATA, "py_runs")
    nas_root = os.path.join(_DATA, "nas_case_plots")

    r_items = _walk_run_dirs(r_root, ["analysis.R", "script.R"], "R analyses", cap=20)
    py_items = _walk_run_dirs(py_root, ["script.py", "analysis.py"], "Python analyses", cap=20)
    nas_items = _walk_plot_files(nas_root, "NAS case plots", cap=20)

    all_items = r_items + py_items + nas_items
    all_items.sort(key=lambda x: x["modified_iso"] or "", reverse=True)

    return {
        "key": "analysis_outputs",
        "title": "Analysis outputs",
        "description": "R/Python run directories and NAS case plots produced by Tealc jobs.",
        "priority_note": "Check for new plots or scripts worth reviewing.",
        "items": all_items,
    }


# ---------------------------------------------------------------------------
# Category 3: Reproducibility bundles
# ---------------------------------------------------------------------------

def _cat_bundles() -> dict:
    bundles_dir = os.path.join(_DATA, "r_runs", "bundles")
    items: list[dict] = []
    if os.path.isdir(bundles_dir):
        try:
            entries = sorted(os.scandir(bundles_dir), key=lambda e: e.stat().st_mtime, reverse=True)
        except Exception:
            entries = []
        for entry in entries:
            if not entry.is_file():
                continue
            if not entry.name.endswith(".tar.gz"):
                continue
            try:
                size = entry.stat().st_size
            except Exception:
                size = 0
            items.append({
                "title": entry.name,
                "subcategory": "Reproducibility bundles",
                "description": f"{size:,} bytes",
                "modified_iso": _mtime_iso(entry.path),
                "link_type": "local_file",
                "link": os.path.abspath(entry.path),
                "extra": {"size_bytes": size},
            })
            if len(items) >= 20:
                break

    return {
        "key": "bundles",
        "title": "Reproducibility bundles",
        "description": "Tar archives ready to upload to Zenodo/OSF for a DOI.",
        "priority_note": "Archive before submitting a manuscript.",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Category 4: System state files Heath edits
# ---------------------------------------------------------------------------

_HEATH_EDIT_FILES = [
    ("data/deadlines.json",       "Upcoming deadlines",            "Edit to add/remove deadlines watched by deadline_countdown and nightly_grant_drafter."),
    ("data/vip_senders.json",     "VIP email senders",             "Emails from these addresses/domains get critical-urgency briefings."),
    ("data/lab_people.json",      "Lab people (privacy)",          "Names that get redacted from the public aquarium feed."),
    ("data/grant_sources.json",   "Grant radar feeds",             "RSS/feeds the Monday grant radar scans."),
    ("data/config.json",          "Sheets/legacy config",          "Goals Sheet ID + legacy settings."),
    ("data/tealc_config.json",    "Tealc control panel state",     "JSON behind the Control tab — usually edit via UI."),
    ("data/heath_preferences.md", "Heath's preferences (living doc)", "Weekly preference_consolidator writes here."),
]


def _cat_heath_edits() -> dict:
    items: list[dict] = []
    for rel_path, title, description in _HEATH_EDIT_FILES:
        abs_path = os.path.join(_PROJECT_ROOT, rel_path)
        if not os.path.isfile(abs_path):
            continue
        items.append({
            "title": title,
            "subcategory": "Config & state",
            "description": description,
            "modified_iso": _mtime_iso(abs_path),
            "link_type": "local_file",
            "link": abs_path,
            "extra": {"rel_path": rel_path},
        })

    return {
        "key": "heath_edits",
        "title": "System state files Heath edits",
        "description": "Config/state files Tealc reads and Heath occasionally edits directly.",
        "priority_note": "Low frequency but critical — wrong values affect multiple jobs.",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Category 5: Auto-generated system docs
# ---------------------------------------------------------------------------

_SYSTEM_DOCS = [
    ("REPLICATION.md",                 "Replication guide",          "The exact-steps doc for reproducing Tealc elsewhere."),
    ("TEALC_SYSTEM.md",                "Tealc system doc",           "Architecture + versioned capability summary."),
    ("IMPLEMENTATION_PLAN.md",         "Implementation plan",        "Completed and planned work."),
    ("BACKLOG.md",                     "Backlog",                    "Parked ideas — Tier 1-3 + API integrations."),
    ("google_grant_followup_brief.md", "Google.org followup brief",  "The claims→deployed-components narrative."),
    ("docs/TEALC_V2_HELPERS.md",       "v2 helper module reference", "API summaries for ledger/critic/bundle/cost modules."),
]


def _cat_system_docs() -> dict:
    items: list[dict] = []
    for rel_path, title, description in _SYSTEM_DOCS:
        abs_path = os.path.join(_PROJECT_ROOT, rel_path)
        items.append({
            "title": title,
            "subcategory": "System docs",
            "description": description,
            "modified_iso": _mtime_iso(abs_path) if os.path.isfile(abs_path) else "",
            "link_type": "local_file",
            "link": abs_path,
            "extra": {"exists": os.path.isfile(abs_path), "rel_path": rel_path},
        })

    return {
        "key": "system_docs",
        "title": "Auto-generated system docs",
        "description": "Architecture, replication, and planning documents maintained by Tealc.",
        "priority_note": "Occasional reads — check after major capability changes.",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Category 6: Recent logs (only if errors present)
# ---------------------------------------------------------------------------

_LOG_FILES = [
    ("data/scheduler.log",           "Scheduler log"),
    ("data/scheduler.stdout.log",    "Scheduler stdout"),
    ("data/dashboard_server.log",    "Dashboard server log"),
    ("data/aquarium_push_errors.log", "Aquarium push errors"),
]

_ERROR_SIGNALS = ("ERROR", "error", "Traceback")


def _log_item(rel_path: str, title: str, always_include: bool = False) -> Optional[dict]:
    abs_path = os.path.join(_PROJECT_ROOT, rel_path)
    if not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return None

    tail = lines[-50:] if len(lines) > 50 else lines
    tail_text = "".join(tail)
    error_count = sum(1 for ln in tail if any(sig in ln for sig in _ERROR_SIGNALS))

    if not always_include and error_count == 0:
        return None

    if error_count > 0:
        description = f"{error_count} recent error line(s) in last {len(tail)} lines"
    else:
        description = f"{len(lines)} total lines (no recent errors)"

    return {
        "title": title,
        "subcategory": "Logs",
        "description": description,
        "modified_iso": _mtime_iso(abs_path),
        "link_type": "local_file",
        "link": abs_path,
        "extra": {
            "error_count": error_count,
            "tail_lines": len(tail),
            "total_lines": len(lines),
        },
    }


def _cat_logs() -> dict:
    items: list[dict] = []
    for i, (rel_path, title) in enumerate(_LOG_FILES):
        # aquarium_push_errors.log is always included if it exists (index 3)
        always = (i == 3)
        item = _log_item(rel_path, title, always_include=always)
        if item:
            items.append(item)

    return {
        "key": "logs",
        "title": "Recent logs",
        "description": "Log files — only shown when errors are present (except aquarium push errors).",
        "priority_note": "Debug surface — check when jobs are failing.",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_documents_index() -> dict:
    """Assemble a categorized, interaction-frequency-ordered view of every doc Tealc creates or maintains.

    Returns a dict with shape:
    {
        "generated_at": iso,
        "categories": [
            {
                "key": "...",
                "title": "...",
                "description": "...",
                "priority_note": "...",
                "items": [ { "title", "subcategory", "description", "modified_iso",
                              "link_type", "link", "extra" }, ... ]
            },
            ...  # 6 categories in interaction-frequency order
        ]
    }
    """
    categories = [
        _cat_drafts_in_drive(),
        _cat_analysis_outputs(),
        _cat_bundles(),
        _cat_heath_edits(),
        _cat_system_docs(),
        _cat_logs(),
    ]

    return {
        "generated_at": _now_iso(),
        "categories": categories,
    }


if __name__ == "__main__":
    result = build_documents_index()
    print(f"categories: {len(result['categories'])}")
    for cat in result["categories"]:
        print(f"  - {cat['key']}: {len(cat['items'])} items")
