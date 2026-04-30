"""
One-shot migration: Lab Status Tracking → Tealc data model.

Sources (READ-ONLY):
  Sheet 11tgJC4axlhdHhUGT_S5U2Oq-AQIaRMhKsC6Byiy4Flc
    "Lab Projects" tab  → research_projects table
    "PhDs" tab          → milestones table (per student)

Safe to re-run: fully idempotent.

Layout of "Lab Projects" tab:
  Row 1: meta-instructions (skip)
  Row 2: header: Primary Person | Projects: match google drive | Status |
                 Notes to or from Heath | github repository | Target Journal | Notes
  Rows 3–48:  active-student projects (7-column layout)
  Row 48: banner: "To my knowledge the following projects are currently open..."
  Row 49: sub-header: Open Projects | Status | Notes / Claimed by?
  Rows 50–end: open/unowned projects (3-column layout: A=name, B=status, C=notes)
  Rows 89+: a few more student entries resume with the original 7-column layout
             (student-led project rows resume here)
"""

import sys
import os
import sqlite3
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from agent.tools import _get_google_service, DB_PATH
from agent.scheduler import _migrate

LAB_STATUS_SHEET_ID = "11tgJC4axlhdHhUGT_S5U2Oq-AQIaRMhKsC6Byiy4Flc"
TODAY_ISO = datetime.now(timezone.utc).date().isoformat()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_status(raw: str) -> str:
    """Map free-text status to one of {active, paused, done, dropped}."""
    r = raw.strip().lower()
    done_kw = ["published", "accepted", "in press", "submitted finalized"]
    dropped_kw = ["abandoned", "cancelled"]
    paused_kw = ["waiting", "need to", "stalled", "on hold"]
    active_kw = [
        "submitted", "in review", "writing", "in progress", "data analysis",
        "drafting", "analysis", "extractions", "exploring", "revising",
        "resubmitting", "nearing completion", "collecting", "development",
        "generating", "fresh look", "proof stage", "ready",
    ]
    for kw in done_kw:
        if kw in r:
            return "done"
    for kw in dropped_kw:
        if kw in r:
            return "dropped"
    for kw in paused_kw:
        if kw in r:
            return "paused"
    for kw in active_kw:
        if kw in r:
            return "active"
    return "active"


def _next_project_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM research_projects ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "p_001"
    last = row[0]
    try:
        num = int(last.split("_")[1]) + 1
        return f"p_{num:03d}"
    except Exception:
        return "p_001"


def _get_projects_db() -> sqlite3.Connection:
    _migrate()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_student_db() -> sqlite3.Connection:
    _migrate()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _resolve_student(conn, name: str):
    """Return (id, full_name) for closest name match, or (None, None)."""
    name_l = name.strip().lower()
    for query in [
        "SELECT id, full_name FROM students WHERE LOWER(full_name)=?",
        "SELECT id, full_name FROM students WHERE LOWER(short_name)=?",
        "SELECT id, full_name FROM students WHERE LOWER(full_name) LIKE ?",
    ]:
        arg = name_l if "LIKE" not in query else f"%{name_l}%"
        row = conn.execute(query, (arg,)).fetchone()
        if row:
            return row
    return None, None


def _insert_project(conn, name, description, status_raw, notes_str, row_label):
    """Insert a project if it doesn't already exist. Returns 'added'|'exists'|'error'."""
    name_key = name[:40].lower()
    existing = conn.execute(
        "SELECT id FROM research_projects WHERE LOWER(SUBSTR(name,1,40))=?",
        (name_key,)
    ).fetchone()
    if existing:
        print(f"SKIPPED EXISTS: {name}")
        return "exists"

    status = _normalize_status(status_raw) if status_raw.strip() else "active"
    new_id = _next_project_id(conn)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO research_projects
           (id, name, description, status, linked_goal_ids, data_dir,
            output_dir, current_hypothesis, next_action, keywords,
            linked_artifact_id, last_touched_by, last_touched_iso, notes, synced_at)
           VALUES (?,?,?,'active',NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?,?,?)""",
        (new_id, name, description or None, "Tealc", now_iso, notes_str or None, now_iso),
    )
    conn.commit()
    if status != "active":
        conn.execute(
            "UPDATE research_projects SET status=?, last_touched_iso=?, synced_at=? WHERE id=?",
            (status, now_iso, now_iso, new_id),
        )
        conn.commit()

    print(f"ADDED: {name} (status={status}, id={new_id})")
    return "added"


# ---------------------------------------------------------------------------
# Migration 1: Lab Projects → research_projects
# ---------------------------------------------------------------------------

# Names that are section dividers / sub-headers, not real projects
_SKIP_NAMES = {
    "open projects", "status", "notes / claimed by?",
    "primary person", "projects: match google drive",
}

# Marker text indicating start of the "Open Projects" sub-section
_OPEN_PROJECTS_MARKER = "open projects"


def migrate_lab_projects(service):
    print("\n=== Migration 1: Lab Projects → research_projects ===")

    result = service.spreadsheets().values().get(
        spreadsheetId=LAB_STATUS_SHEET_ID,
        range="Lab Projects!A1:Z200",
    ).execute()
    raw_rows = result.get("values", [])
    print(f"Total rows from sheet: {len(raw_rows)}")

    # ── Find real header (row with "Primary Person") ──
    header_idx = None
    for i, row in enumerate(raw_rows):
        if row and row[0].strip().lower() == "primary person":
            header_idx = i
            break
    if header_idx is None:
        print("ERROR: Could not find primary-section header row.")
        return 0, 0, 0

    # ── Find "Open Projects" sub-header ──
    open_proj_idx = None
    for i, row in enumerate(raw_rows):
        if row and row[0].strip().lower() == _OPEN_PROJECTS_MARKER:
            open_proj_idx = i
            break
    print(f"Primary header at row {header_idx+1}, Open Projects sub-header at row {open_proj_idx+1 if open_proj_idx else 'N/A'}")

    conn = _get_projects_db()

    # Pull student roster for primary-person enrichment
    students_by_short = {}
    for sid, full_name, short_name in conn.execute(
        "SELECT id, full_name, short_name FROM students"
    ).fetchall():
        students_by_short[short_name.strip().lower()] = full_name

    added = 0
    skipped_exists = 0
    skipped_empty = 0

    # ── Section A: Primary-section rows (header+1 through open_proj_idx-1 or end) ──
    # These use: col0=primary_person, col1=project, col2=status, col3=notes_heath,
    #            col4=github, col5=journal, col6=notes
    primary_end = open_proj_idx if open_proj_idx else len(raw_rows)
    current_person = ""

    for abs_i in range(header_idx + 1, primary_end):
        row = raw_rows[abs_i]
        row_label = f"row {abs_i + 1}"

        def _c(col, r=row):
            return (r[col] if col < len(r) else "").strip()

        person_raw = _c(0)
        project_raw = _c(1)
        status_raw = _c(2)
        notes1_raw = _c(3)
        github_raw = _c(4)
        journal_raw = _c(5)
        notes2_raw = _c(6)

        if person_raw:
            current_person = person_raw

        # Skip totally empty rows
        if not any([project_raw, status_raw, notes1_raw, github_raw, journal_raw, notes2_raw]):
            print(f"SKIPPED EMPTY: {row_label}")
            skipped_empty += 1
            continue

        # Skip section marker rows with no project
        if not project_raw:
            if person_raw.lower().startswith("to my knowledge"):
                print(f"SKIPPED EMPTY: {row_label} (section banner)")
            else:
                print(f"SKIPPED EMPTY: {row_label} (no project name)")
            skipped_empty += 1
            continue

        # Parse project name / description
        if ":" in project_raw:
            colon_idx = project_raw.index(":")
            name = project_raw[:colon_idx].strip()
            description = project_raw[colon_idx + 1:].strip()
        else:
            name = project_raw.strip()
            description = ""

        if not name or name.lower() in _SKIP_NAMES:
            print(f"SKIPPED EMPTY: {row_label} (name is section header)")
            skipped_empty += 1
            continue

        # Build notes
        notes_parts = []
        if current_person:
            sid, full_name = _resolve_student(conn, current_person)
            notes_parts.append(f"Primary author: {full_name if full_name else current_person}")
        if github_raw:
            notes_parts.append(f"GitHub: {github_raw}")
        if journal_raw:
            notes_parts.append(f"Target journal: {journal_raw}")
        if notes1_raw:
            notes_parts.append(f"Notes: {notes1_raw}")
        if notes2_raw and notes2_raw != notes1_raw:
            notes_parts.append(f"Notes2: {notes2_raw}")
        notes_str = "\n".join(notes_parts)

        try:
            result = _insert_project(conn, name, description, status_raw, notes_str, row_label)
            if result == "added":
                added += 1
            elif result == "exists":
                skipped_exists += 1
        except Exception as exc:
            print(f"ERROR {row_label}: {exc}")

    # ── Section B: "Open Projects" rows ──
    # Layout: col0=project_name, col1=status_or_note, col2=notes
    # The sub-header row itself is open_proj_idx; data starts at open_proj_idx+1.
    if open_proj_idx is not None:
        # Find where "Open Projects" section ends (look for next named-person rows after row 88)
        # We know rows 89+ resume the primary layout (Rachel K, etc.)
        # Detect end of open-projects section: first row where col0 looks like a person name
        # (i.e., it has content but is NOT a project description and the next rows resume col1 layout)
        # Simplest heuristic: open projects end when we see a row where col1 has a person-section
        # indicator. Actually just handle rows 89+ as primary-section.
        # Find the "resume primary section" boundary by looking for rows after the open section
        # that start with a name found in students table or have col1 as a project description.
        resume_primary_idx = None
        for i in range(open_proj_idx + 1, len(raw_rows)):
            row = raw_rows[i]
            if not row:
                continue
            # If col0 looks like a student name (matches students table) and col1 is non-empty
            # with a real project name (not a status keyword), treat as resume of primary section.
            col0 = (row[0] if row else "").strip()
            col1 = (row[1] if len(row) > 1 else "").strip()
            sid, _ = _resolve_student(conn, col0) if col0 else (None, None)
            if sid is not None and col1:
                resume_primary_idx = i
                break
            # Also catch the case where col0 is something like "Rachel K" that matches partially
            if col0 and col1 and len(col1) > 10 and not col1.lower().startswith("status"):
                # If col0 has a surname initial pattern like "Rachel K", treat as person
                parts = col0.split()
                if len(parts) == 2 and len(parts[1]) <= 2:
                    resume_primary_idx = i
                    break

        print(f"\nOpen Projects section: rows {open_proj_idx+2} to "
              f"{(resume_primary_idx) if resume_primary_idx else len(raw_rows)}")

        open_end = resume_primary_idx if resume_primary_idx else len(raw_rows)
        for abs_i in range(open_proj_idx + 1, open_end):
            row = raw_rows[abs_i]
            row_label = f"row {abs_i + 1}"

            def _c2(col, r=row):
                return (r[col] if col < len(r) else "").strip()

            name = _c2(0)
            status_raw = _c2(1)
            notes_raw = _c2(2)
            extra_notes = _c2(3)  # col D (occasionally populated)

            if not name:
                print(f"SKIPPED EMPTY: {row_label}")
                skipped_empty += 1
                continue

            if name.lower() in _SKIP_NAMES:
                print(f"SKIPPED EMPTY: {row_label} (sub-header: {name})")
                skipped_empty += 1
                continue

            notes_parts = []
            notes_parts.append("Section: Open/Unowned Projects")
            if status_raw and not _normalize_status(status_raw):
                notes_parts.append(f"Notes: {status_raw}")
            if notes_raw:
                notes_parts.append(f"Notes: {notes_raw}")
            if extra_notes:
                notes_parts.append(f"Notes: {extra_notes}")
            notes_str = "\n".join(notes_parts)

            # Status comes from col B; if it looks like a note (long free text), status=active
            if len(status_raw) > 30 or " " in status_raw[:10]:
                # Likely a note, not a status keyword
                effective_status = "active"
                if status_raw and "notes" not in notes_str:
                    notes_str = f"Section: Open/Unowned Projects\nNotes: {status_raw}"
                    if notes_raw:
                        notes_str += f"\nNotes: {notes_raw}"
            else:
                effective_status = _normalize_status(status_raw) if status_raw else "active"

            description = ""

            try:
                result = _insert_project(conn, name, description, effective_status if status_raw else "", notes_str, row_label)
                if result == "added":
                    added += 1
                elif result == "exists":
                    skipped_exists += 1
            except Exception as exc:
                print(f"ERROR {row_label}: {exc}")

        # ── Section C: Resume primary section after open-projects ──
        if resume_primary_idx is not None:
            current_person = ""
            for abs_i in range(resume_primary_idx, len(raw_rows)):
                row = raw_rows[abs_i]
                if not row:
                    print(f"SKIPPED EMPTY: row {abs_i+1}")
                    skipped_empty += 1
                    continue
                row_label = f"row {abs_i + 1}"

                def _c3(col, r=row):
                    return (r[col] if col < len(r) else "").strip()

                col0 = _c3(0)
                col1 = _c3(1)

                # Detect rows that use the open-project layout (col A = project name, not a person).
                # Heuristic: if col0 is present and col1 looks like a status keyword or long note
                # (not a detailed project description), AND col0 doesn't match any student, treat as
                # open-project-layout row.
                sid_check, _ = _resolve_student(conn, col0) if col0 else (None, None)
                col0_is_person = (sid_check is not None) or (
                    col0 and len(col0.split()) == 2 and len(col0.split()[-1]) <= 2
                )

                if col0 and not col0_is_person and col1 and len(col1) < 30:
                    # Open-project-layout: col0=name, col1=status, col2=notes
                    name = col0
                    status_raw = col1
                    notes_raw = _c3(2)
                    extra_notes = _c3(3)
                    if name.lower() in _SKIP_NAMES:
                        print(f"SKIPPED EMPTY: {row_label} (section header)")
                        skipped_empty += 1
                        continue
                    notes_parts = ["Section: Open/Unowned Projects"]
                    if notes_raw:
                        notes_parts.append(f"Notes: {notes_raw}")
                    if extra_notes:
                        notes_parts.append(f"Notes: {extra_notes}")
                    notes_str = "\n".join(notes_parts)
                    try:
                        result = _insert_project(conn, name, "", status_raw, notes_str, row_label)
                        if result == "added":
                            added += 1
                        elif result == "exists":
                            skipped_exists += 1
                    except Exception as exc:
                        print(f"ERROR {row_label}: {exc}")
                    continue

                person_raw = col0
                project_raw = col1
                status_raw = _c3(2)
                notes1_raw = _c3(3)
                github_raw = _c3(4)
                journal_raw = _c3(5)
                notes2_raw = _c3(6)

                if person_raw and not person_raw.lower().startswith("to my"):
                    current_person = person_raw

                if not any([project_raw, status_raw, notes1_raw, github_raw, journal_raw, notes2_raw]):
                    print(f"SKIPPED EMPTY: {row_label}")
                    skipped_empty += 1
                    continue

                if not project_raw:
                    print(f"SKIPPED EMPTY: {row_label} (no project name)")
                    skipped_empty += 1
                    continue

                if ":" in project_raw:
                    colon_idx = project_raw.index(":")
                    name = project_raw[:colon_idx].strip()
                    description = project_raw[colon_idx + 1:].strip()
                else:
                    name = project_raw.strip()
                    description = ""

                if not name or name.lower() in _SKIP_NAMES:
                    print(f"SKIPPED EMPTY: {row_label}")
                    skipped_empty += 1
                    continue

                notes_parts = []
                if current_person:
                    sid, full_name = _resolve_student(conn, current_person)
                    notes_parts.append(f"Primary author: {full_name if full_name else current_person}")
                if github_raw:
                    notes_parts.append(f"GitHub: {github_raw}")
                if journal_raw:
                    notes_parts.append(f"Target journal: {journal_raw}")
                if notes1_raw:
                    notes_parts.append(f"Notes: {notes1_raw}")
                if notes2_raw and notes2_raw != notes1_raw:
                    notes_parts.append(f"Notes2: {notes2_raw}")
                notes_str = "\n".join(notes_parts)

                try:
                    result = _insert_project(conn, name, description, status_raw, notes_str, row_label)
                    if result == "added":
                        added += 1
                    elif result == "exists":
                        skipped_exists += 1
                except Exception as exc:
                    print(f"ERROR {row_label}: {exc}")

    conn.close()
    print(f"\nprojects: added={added} skipped_existing={skipped_exists} skipped_empty={skipped_empty}")
    return added, skipped_exists, skipped_empty


# ---------------------------------------------------------------------------
# Migration 2: PhDs tab → milestones (per student)
# ---------------------------------------------------------------------------

COMPLETION_VALUES = {
    "passed", "submitted", "published", "accepted", "defended", "pass",
    "in press", "graduated",
}

PHD_COL_KINDS = {
    "status": "paper_submission",
    "proposal satus": "proposal",   # typo in sheet header
    "proposal status": "proposal",
    "qualifying exam": "qualifying_exam",
    "dissertation": "dissertation",
    "defense presentation": "defense",
}


def _is_complete(value: str) -> bool:
    v = value.strip().lower()
    for kw in COMPLETION_VALUES:
        if kw in v:
            return True
    return False


def migrate_phd_milestones(service):
    print("\n=== Migration 2: PhDs tab → milestones ===")

    result = service.spreadsheets().values().get(
        spreadsheetId=LAB_STATUS_SHEET_ID,
        range="PhDs!A1:Z100",
    ).execute()
    raw_rows = result.get("values", [])

    if not raw_rows:
        print("No data found in PhDs tab.")
        return 0, 0, []

    header = [h.strip() for h in raw_rows[0]]
    while len(header) < 7:
        header.append("")

    milestone_cols = {}
    for i, h in enumerate(header):
        hl = h.strip().lower()
        if hl in PHD_COL_KINDS:
            milestone_cols[i] = PHD_COL_KINDS[hl]

    print(f"PhDs header: {header}")
    print(f"Milestone columns: {milestone_cols}")

    conn = _get_student_db()
    added = 0
    skipped_exists = 0
    unmatched_names = []
    current_sid = None
    current_full_name = ""

    for row_i, row in enumerate(raw_rows[1:], start=2):
        try:
            def _cell(col, r=row):
                return (r[col] if col < len(r) else "").strip()

            name_raw = _cell(0)
            item_raw = _cell(1)

            if name_raw:
                sid, full_name = _resolve_student(conn, name_raw)
                if sid is None:
                    if name_raw not in unmatched_names:
                        unmatched_names.append(name_raw)
                    print(f"SKIPPED NO STUDENT MATCH: {name_raw}")
                    current_sid = None
                    current_full_name = ""
                else:
                    current_sid = sid
                    current_full_name = full_name

            if current_sid is None or not item_raw:
                continue

            for col_idx, kind in milestone_cols.items():
                value = _cell(col_idx)
                if not value:
                    continue

                notes_str = f"{item_raw}: {value}"

                existing = conn.execute(
                    "SELECT id FROM milestones WHERE student_id=? AND kind=? AND notes=?",
                    (current_sid, kind, notes_str),
                ).fetchone()
                if existing:
                    print(f"SKIPPED EXISTS: [{current_full_name}] {kind}: {notes_str[:60]}")
                    skipped_exists += 1
                    continue

                completed_iso = TODAY_ISO if _is_complete(value) else None

                conn.execute(
                    "INSERT INTO milestones(student_id, kind, target_iso, completed_iso, notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (current_sid, kind, None, completed_iso, notes_str),
                )
                conn.commit()
                print(f"ADDED MILESTONE: [{current_full_name}] {kind}: {notes_str[:60]}"
                      f"{' (completed)' if completed_iso else ''}")
                added += 1

        except Exception as exc:
            print(f"ERROR row {row_i}: {exc}")

    conn.close()
    print(f"\nmilestones: added={added} skipped_existing={skipped_exists}")
    if unmatched_names:
        print(f"UNMATCHED STUDENT NAMES (not in students table): {unmatched_names}")
    return added, skipped_exists, unmatched_names


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"=== Tealc Migration: Lab Status Tracking → SQLite ===")
    print(f"Date: {TODAY_ISO}")
    print(f"DB: {DB_PATH}")

    service, err = _get_google_service("sheets", "v4")
    if err:
        print(f"ERROR: Could not connect to Google Sheets: {err}")
        sys.exit(1)

    migrate_lab_projects(service)
    migrate_phd_milestones(service)

    print("\n=== Migration complete ===")


if __name__ == "__main__":
    main()
