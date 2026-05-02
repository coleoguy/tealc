"""Goals Sheet bidirectional sync job.

Schedule: IntervalTrigger(minutes=5)

Maintains a SQLite mirror of the "Tealc Goals" Google Sheet, which has 5 tabs:
  Goals, Milestones, Today, Decisions, Projects

On first run (or if goals_sheet_id is missing from config.json), bootstraps the
Sheet: creates it, applies headers + formatting, seeds 6 starter goals.

If the Sheet already exists with only 4 tabs, the bootstrap adds the Projects tab
idempotently (checks existing titles, adds if missing).

Conflict rule:
  - SHEET → DB: if Sheet's last_touched_iso is newer than SQLite's, Sheet wins.
  - DB → SHEET: if SQLite row is dirty (tealc_dirty=1), push to Sheet UNLESS
    Heath's last_touched_iso in the DB (from previous sync) is within the past
    1 hour — in that case, defer to preserve Heath's edits ("Heath priority rule").
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from agent.jobs import tracked

from agent.scheduler import DB_PATH

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_AGENT_DIR)
CONFIG_PATH = os.path.normpath(os.path.join(_ROOT, "data", "config.json"))
DEADLINES_PATH = os.path.normpath(os.path.join(_ROOT, "data", "deadlines.json"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _load_deadlines() -> dict:
    """Return dict: name → due_iso for the 3 key deadlines."""
    result = {}
    try:
        with open(DEADLINES_PATH, "r") as f:
            data = json.load(f)
        for d in data.get("deadlines", []):
            result[d["name"]] = d.get("due_iso", "")
    except Exception:
        pass
    return result


def _get_google_service(service_name: str, version: str):
    """Reuse the auth helper from agent.tools."""
    try:
        from agent.tools import _get_google_service as _gs  # noqa: PLC0415
        return _gs(service_name, version)
    except Exception as e:
        return None, str(e)


def _iso_is_within_past_hour(iso_str: str) -> bool:
    """Return True if the ISO timestamp is within the last 60 minutes (UTC-aware)."""
    if not iso_str:
        return False
    try:
        # Parse — handle with or without timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt) < timedelta(hours=1)
    except Exception:
        return False


def _iso_newer_than(a: str, b: str) -> bool:
    """Return True if a > b (both ISO strings; None treated as epoch)."""
    def _parse(s):
        if not s:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    return _parse(a) > _parse(b)


# ---------------------------------------------------------------------------
# DB migration — 4 goals tables
# ---------------------------------------------------------------------------

def _migrate_goals_tables():
    """Apply goals SQLite schema. Safe to call multiple times."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS research_projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT,
            linked_goal_ids TEXT,
            data_dir TEXT,
            output_dir TEXT,
            current_hypothesis TEXT,
            next_action TEXT,
            keywords TEXT,
            linked_artifact_id TEXT,
            last_touched_by TEXT,
            last_touched_iso TEXT,
            notes TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_projects_active
            ON research_projects(status) WHERE status='active';

        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            time_horizon TEXT,
            importance INTEGER,
            nas_relevance TEXT,
            status TEXT,
            success_metric TEXT,
            why TEXT,
            owner TEXT,
            last_touched_by TEXT,
            last_touched_iso TEXT,
            notes TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS milestones_v2 (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            milestone TEXT NOT NULL,
            target_iso TEXT,
            status TEXT,
            notes TEXT,
            last_touched_iso TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS today_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_iso TEXT NOT NULL,
            priority_rank INTEGER,
            description TEXT NOT NULL,
            linked_goal_id TEXT,
            status TEXT,
            notes TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date_iso, priority_rank)
        );

        CREATE TABLE IF NOT EXISTS decisions_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_iso TEXT NOT NULL,
            decision TEXT NOT NULL,
            reasoning TEXT,
            linked_goal_id TEXT,
            decided_by TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Sheet bootstrap
# ---------------------------------------------------------------------------

GOALS_HEADERS = [
    "id", "name", "time_horizon", "importance", "nas_relevance",
    "status", "success_metric", "why", "owner",
    "last_touched_by", "last_touched_iso", "notes",
]

MILESTONES_HEADERS = [
    "id", "goal_id", "milestone", "target_iso", "status", "notes", "last_touched_iso",
]

TODAY_HEADERS = [
    "date", "priority_rank", "description", "linked_goal_id", "status", "notes",
]

DECISIONS_HEADERS = [
    "decided_iso", "decision", "reasoning", "linked_goal_id", "decided_by",
]

PROJECTS_HEADERS = [
    "id", "name", "description", "status", "linked_goal_ids", "data_dir",
    "output_dir", "current_hypothesis", "next_action", "keywords",
    "linked_artifact_id", "last_touched_by", "last_touched_iso", "notes",
]

TAB_NAMES = ["Goals", "Milestones", "Today", "Decisions", "Projects"]


def _bootstrap_sheet() -> str:
    """Create or find 'Tealc Goals' sheet, seed it, and return its ID.
    Writes the ID to data/config.json as goals_sheet_id."""

    sheets_svc, err = _get_google_service("sheets", "v4")
    if err:
        raise RuntimeError(f"Sheets not connected: {err}")
    drive_svc, err2 = _get_google_service("drive", "v3")
    if err2:
        raise RuntimeError(f"Drive not connected: {err2}")

    # 1. Search Drive for existing "Tealc Goals" spreadsheet
    q = "name='Tealc Goals' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    result = drive_svc.files().list(
        q=q,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime)",
    ).execute()
    files = result.get("files", [])

    if files:
        # Use most recently modified if multiple exist
        spreadsheet_id = files[0]["id"]
        created_new = False
        if len(files) > 1:
            # Document the multi-sheet condition; use newest
            pass
    else:
        # 2. Create new spreadsheet with 5 sheets (Goals, Milestones, Today, Decisions, Projects)
        body = {
            "properties": {"title": "Tealc Goals"},
            "sheets": [
                {"properties": {"title": tab}} for tab in TAB_NAMES
            ],
        }
        resp = sheets_svc.spreadsheets().create(body=body).execute()
        spreadsheet_id = resp["spreadsheetId"]
        created_new = True

    # 2b. Idempotent Projects tab: if Sheet exists with <5 tabs, add the missing one
    meta_check = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_titles = {s["properties"]["title"] for s in meta_check["sheets"]}
    if "Projects" not in existing_titles:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "Projects"}}}]},
        ).execute()

    # 3. Apply header rows via values.update (all 5 tabs)
    header_ranges = [
        ("Goals!A1", [GOALS_HEADERS]),
        ("Milestones!A1", [MILESTONES_HEADERS]),
        ("Today!A1", [TODAY_HEADERS]),
        ("Decisions!A1", [DECISIONS_HEADERS]),
        ("Projects!A1", [PROJECTS_HEADERS]),
    ]
    for range_a1, values in header_ranges:
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    # 4. Get sheet IDs for formatting (need numeric sheetId per tab)
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id_map = {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in meta["sheets"]
    }

    # 5. Apply bold headers + freeze row 1 via a single batchUpdate (all 5 tabs)
    format_requests = []
    for tab_name in TAB_NAMES:
        sid = sheet_id_map.get(tab_name, 0)
        # Bold row 0 (header)
        format_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        })
        # Freeze row 1
        format_requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": format_requests},
    ).execute()

    # 6. Seed 6 starter goals (only if Goals tab has ≤1 row)
    existing = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range="Goals!A1:L"
    ).execute()
    existing_rows = existing.get("values", [])
    if len(existing_rows) <= 1:
        deadlines = _load_deadlines()
        now_iso = _now_utc()

        # Build deadline notes for the 3 time-sensitive goals
        google_dl = deadlines.get("Google.org grant", "")
        nih_dl = deadlines.get("NIH MIRA R35 renewal", "")
        stasis_dl = deadlines.get("Chromosomal stasis preprint submission", "")

        google_note = f"Deadline: {google_dl[:10]}" if google_dl else ""
        nih_note = f"Deadline: {nih_dl[:10]}" if nih_dl else ""
        stasis_note = f"Deadline: {stasis_dl[:10]}" if stasis_dl else ""

        seed_rows = [
            # id, name, time_horizon, importance, nas_relevance, status,
            # success_metric, why, owner, last_touched_by, last_touched_iso, notes
            [
                "g_001", "NAS membership", "career", "5", "high", "active",
                "Elected to National Academy of Sciences",
                "Heath's top career goal — drives all strategic decisions",
                "Heath", "Tealc", now_iso, "",
            ],
            [
                "g_002", "Department Head", "career", "4", "med", "active",
                "Appointed as Department Head at TAMU or peer institution",
                "Career leadership goal; builds institutional influence",
                "Heath", "Tealc", now_iso, "",
            ],
            [
                "g_003", "Outstanding mentor", "career", "4", "med", "active",
                "Legendary reputation for training great scientists; strong alumni placement",
                "Mentor excellence directly supports NAS narrative and lab culture",
                "Heath", "Tealc", now_iso, "",
            ],
            [
                "g_004", "Google.org grant submission", "week", "5", "high", "active",
                "Submitted competitive AI-for-biology proposal to Google.org",
                "Critical near-term funding; advances AI-in-biology visibility",
                "joint", "Tealc", now_iso, google_note,
            ],
            [
                "g_005", "NIH MIRA R35 renewal", "month", "5", "high", "active",
                "Submitted strong R35 renewal (R35GM138098) to NIGMS",
                "Primary lab funding renewal; essential for all ongoing research",
                "joint", "Tealc", now_iso, nih_note,
            ],
            [
                "g_006", "Chromosomal stasis paper to top journal", "quarter", "5", "high", "active",
                "Accepted in Nature, Science, or Cell",
                "Flagship NAS paper — 63k karyotypes, 55 clades; must land in CNS-tier journal",
                "joint", "Tealc", now_iso, stasis_note,
            ],
        ]
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="Goals!A2",
            valueInputOption="USER_ENTERED",
            body={"values": seed_rows},
        ).execute()

    # 7. Persist to config.json
    cfg = _load_config()
    cfg["goals_sheet_id"] = spreadsheet_id
    _save_config(cfg)

    return spreadsheet_id


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def _rows_to_dicts(rows: list, headers: list) -> list:
    """Convert list-of-lists (Sheet values) to list-of-dicts, skipping header row."""
    result = []
    for i, row in enumerate(rows[1:], start=2):  # 1-indexed; row 1 = header
        d = {}
        for j, h in enumerate(headers):
            d[h] = row[j] if j < len(row) else ""
        d["_sheet_row"] = i
        result.append(d)
    return result


def _sync_goals(sheets_svc, sid: str, conn: sqlite3.Connection) -> tuple:
    """Pull Goals tab → DB. Push dirty DB rows → Sheet.
    Returns (pulled, pushed, deferred)."""
    pulled = pushed = deferred = 0
    now = _now_utc()

    # Pull from Sheet
    res = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sid, range="Goals!A1:L"
    ).execute()
    rows = res.get("values", [])
    sheet_goals = _rows_to_dicts(rows, GOALS_HEADERS)

    for sg in sheet_goals:
        gid = sg.get("id", "").strip()
        if not gid:
            continue
        existing = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        if existing:
            col_names = [d[0] for d in conn.execute("SELECT * FROM goals LIMIT 0").description]
            ex_dict = dict(zip(col_names, existing))
            # Sheet wins if its last_touched_iso is newer
            sheet_ts = sg.get("last_touched_iso", "")
            db_ts = ex_dict.get("last_touched_iso", "")
            if _iso_newer_than(sheet_ts, db_ts):
                conn.execute(
                    """UPDATE goals SET name=?, time_horizon=?, importance=?, nas_relevance=?,
                       status=?, success_metric=?, why=?, owner=?, last_touched_by=?,
                       last_touched_iso=?, notes=?, sheet_row_index=?, synced_at=?, tealc_dirty=0
                       WHERE id=?""",
                    (
                        sg.get("name"), sg.get("time_horizon"),
                        _safe_int(sg.get("importance")), sg.get("nas_relevance"),
                        sg.get("status"), sg.get("success_metric"),
                        sg.get("why"), sg.get("owner"), sg.get("last_touched_by"),
                        sheet_ts, sg.get("notes"), sg["_sheet_row"], now, gid,
                    ),
                )
                pulled += 1
        else:
            # New row from Sheet — insert
            conn.execute(
                """INSERT OR IGNORE INTO goals
                   (id, name, time_horizon, importance, nas_relevance, status, success_metric,
                    why, owner, last_touched_by, last_touched_iso, notes, sheet_row_index,
                    synced_at, tealc_dirty)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    gid, sg.get("name"), sg.get("time_horizon"),
                    _safe_int(sg.get("importance")), sg.get("nas_relevance"),
                    sg.get("status"), sg.get("success_metric"),
                    sg.get("why"), sg.get("owner"), sg.get("last_touched_by"),
                    sg.get("last_touched_iso"), sg.get("notes"),
                    sg["_sheet_row"], now,
                ),
            )
            pulled += 1

    # Push dirty rows back to Sheet
    dirty_rows = conn.execute(
        "SELECT * FROM goals WHERE tealc_dirty=1"
    ).fetchall()
    if dirty_rows:
        col_names = [d[0] for d in conn.execute("SELECT * FROM goals LIMIT 0").description]
        for row in dirty_rows:
            dr = dict(zip(col_names, row))
            # Heath priority: if Heath touched this row within the last hour, defer
            if (dr.get("last_touched_by") == "Heath" and
                    _iso_is_within_past_hour(dr.get("last_touched_iso", ""))):
                deferred += 1
                continue
            row_idx = dr.get("sheet_row_index")
            if not row_idx:
                # Append as new row
                vals = [[
                    dr["id"], dr.get("name", ""), dr.get("time_horizon", ""),
                    str(dr.get("importance", "")), dr.get("nas_relevance", ""),
                    dr.get("status", ""), dr.get("success_metric", ""),
                    dr.get("why", ""), dr.get("owner", ""),
                    dr.get("last_touched_by", ""), dr.get("last_touched_iso", ""),
                    dr.get("notes", ""),
                ]]
                result = sheets_svc.spreadsheets().values().append(
                    spreadsheetId=sid, range="Goals!A2",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                # Capture the updated range to know the new row index
                updated_range = result.get("updates", {}).get("updatedRange", "")
                new_row = _extract_row_from_range(updated_range)
                conn.execute(
                    "UPDATE goals SET sheet_row_index=?, tealc_dirty=0, synced_at=? WHERE id=?",
                    (new_row, now, dr["id"]),
                )
            else:
                vals = [[
                    dr["id"], dr.get("name", ""), dr.get("time_horizon", ""),
                    str(dr.get("importance", "")), dr.get("nas_relevance", ""),
                    dr.get("status", ""), dr.get("success_metric", ""),
                    dr.get("why", ""), dr.get("owner", ""),
                    dr.get("last_touched_by", ""), dr.get("last_touched_iso", ""),
                    dr.get("notes", ""),
                ]]
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sid,
                    range=f"Goals!A{row_idx}:L{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                conn.execute(
                    "UPDATE goals SET tealc_dirty=0, synced_at=? WHERE id=?",
                    (now, dr["id"]),
                )
            pushed += 1

    conn.commit()
    return pulled, pushed, deferred


def _sync_milestones(sheets_svc, sid: str, conn: sqlite3.Connection) -> tuple:
    """Pull Milestones tab → DB. Push dirty rows → Sheet."""
    pulled = pushed = deferred = 0
    now = _now_utc()

    res = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sid, range="Milestones!A1:G"
    ).execute()
    rows = res.get("values", [])
    sheet_items = _rows_to_dicts(rows, MILESTONES_HEADERS)

    for sm in sheet_items:
        mid = sm.get("id", "").strip()
        if not mid:
            continue
        existing = conn.execute("SELECT * FROM milestones_v2 WHERE id=?", (mid,)).fetchone()
        if existing:
            col_names = [d[0] for d in conn.execute("SELECT * FROM milestones_v2 LIMIT 0").description]
            ex_dict = dict(zip(col_names, existing))
            sheet_ts = sm.get("last_touched_iso", "")
            db_ts = ex_dict.get("last_touched_iso", "")
            if _iso_newer_than(sheet_ts, db_ts):
                conn.execute(
                    """UPDATE milestones_v2 SET goal_id=?, milestone=?, target_iso=?,
                       status=?, notes=?, last_touched_iso=?, sheet_row_index=?,
                       synced_at=?, tealc_dirty=0 WHERE id=?""",
                    (
                        sm.get("goal_id"), sm.get("milestone"), sm.get("target_iso"),
                        sm.get("status"), sm.get("notes"), sheet_ts,
                        sm["_sheet_row"], now, mid,
                    ),
                )
                pulled += 1
        else:
            conn.execute(
                """INSERT OR IGNORE INTO milestones_v2
                   (id, goal_id, milestone, target_iso, status, notes, last_touched_iso,
                    sheet_row_index, synced_at, tealc_dirty)
                   VALUES (?,?,?,?,?,?,?,?,?,0)""",
                (
                    mid, sm.get("goal_id"), sm.get("milestone"), sm.get("target_iso"),
                    sm.get("status"), sm.get("notes"), sm.get("last_touched_iso"),
                    sm["_sheet_row"], now,
                ),
            )
            pulled += 1

    # Push dirty milestones
    dirty_rows = conn.execute("SELECT * FROM milestones_v2 WHERE tealc_dirty=1").fetchall()
    if dirty_rows:
        col_names = [d[0] for d in conn.execute("SELECT * FROM milestones_v2 LIMIT 0").description]
        for row in dirty_rows:
            dr = dict(zip(col_names, row))
            row_idx = dr.get("sheet_row_index")
            vals = [[
                dr["id"], dr.get("goal_id", ""), dr.get("milestone", ""),
                dr.get("target_iso", ""), dr.get("status", ""),
                dr.get("notes", ""), dr.get("last_touched_iso", ""),
            ]]
            if not row_idx:
                result = sheets_svc.spreadsheets().values().append(
                    spreadsheetId=sid, range="Milestones!A2",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                new_row = _extract_row_from_range(
                    result.get("updates", {}).get("updatedRange", "")
                )
                conn.execute(
                    "UPDATE milestones_v2 SET sheet_row_index=?, tealc_dirty=0, synced_at=? WHERE id=?",
                    (new_row, now, dr["id"]),
                )
            else:
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sid,
                    range=f"Milestones!A{row_idx}:G{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                conn.execute(
                    "UPDATE milestones_v2 SET tealc_dirty=0, synced_at=? WHERE id=?",
                    (now, dr["id"]),
                )
            pushed += 1

    conn.commit()
    return pulled, pushed, deferred


def _sync_today(sheets_svc, sid: str, conn: sqlite3.Connection) -> tuple:
    """Pull Today tab → DB for today. Push dirty rows → Sheet."""
    pulled = pushed = deferred = 0
    now = _now_utc()
    today_iso = datetime.now(timezone.utc).date().isoformat()

    res = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sid, range="Today!A1:F"
    ).execute()
    rows = res.get("values", [])
    sheet_items = _rows_to_dicts(rows, TODAY_HEADERS)

    for si in sheet_items:
        date_val = si.get("date", "").strip()
        rank_val = _safe_int(si.get("priority_rank", ""))
        desc = si.get("description", "").strip()
        if not desc:
            continue
        if date_val == today_iso and rank_val is not None:
            existing = conn.execute(
                "SELECT id, sheet_row_index FROM today_plan WHERE date_iso=? AND priority_rank=?",
                (date_val, rank_val),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE today_plan SET description=?, linked_goal_id=?, status=?,
                       notes=?, sheet_row_index=?, synced_at=?, tealc_dirty=0
                       WHERE date_iso=? AND priority_rank=?""",
                    (
                        desc, si.get("linked_goal_id"), si.get("status"),
                        si.get("notes"), si["_sheet_row"], now,
                        date_val, rank_val,
                    ),
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO today_plan
                       (date_iso, priority_rank, description, linked_goal_id, status,
                        notes, sheet_row_index, synced_at, tealc_dirty)
                       VALUES (?,?,?,?,?,?,?,?,0)""",
                    (
                        date_val, rank_val, desc, si.get("linked_goal_id"),
                        si.get("status"), si.get("notes"), si["_sheet_row"], now,
                    ),
                )
            pulled += 1

    # Push dirty today_plan rows
    dirty_rows = conn.execute(
        "SELECT * FROM today_plan WHERE tealc_dirty=1 AND date_iso=?", (today_iso,)
    ).fetchall()
    if dirty_rows:
        col_names = [d[0] for d in conn.execute("SELECT * FROM today_plan LIMIT 0").description]
        for row in dirty_rows:
            dr = dict(zip(col_names, row))
            row_idx = dr.get("sheet_row_index")
            vals = [[
                dr.get("date_iso", ""), str(dr.get("priority_rank", "")),
                dr.get("description", ""), dr.get("linked_goal_id", ""),
                dr.get("status", ""), dr.get("notes", ""),
            ]]
            if not row_idx:
                result = sheets_svc.spreadsheets().values().append(
                    spreadsheetId=sid, range="Today!A2",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                new_row = _extract_row_from_range(
                    result.get("updates", {}).get("updatedRange", "")
                )
                conn.execute(
                    "UPDATE today_plan SET sheet_row_index=?, tealc_dirty=0, synced_at=? WHERE id=?",
                    (new_row, now, dr["id"]),
                )
            else:
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sid,
                    range=f"Today!A{row_idx}:F{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                conn.execute(
                    "UPDATE today_plan SET tealc_dirty=0, synced_at=? WHERE id=?",
                    (now, dr["id"]),
                )
            pushed += 1

    conn.commit()
    return pulled, pushed, deferred


def _sync_decisions(sheets_svc, sid: str, conn: sqlite3.Connection) -> tuple:
    """Pull Decisions tab → DB. Push dirty rows → Sheet (append-only)."""
    pulled = pushed = deferred = 0
    now = _now_utc()

    res = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sid, range="Decisions!A1:E"
    ).execute()
    rows = res.get("values", [])
    sheet_items = _rows_to_dicts(rows, DECISIONS_HEADERS)

    for si in sheet_items:
        decided_iso = si.get("decided_iso", "").strip()
        decision_text = si.get("decision", "").strip()
        if not decided_iso or not decision_text:
            continue
        # Decisions log is append-only — find by (decided_iso, decision) pair
        existing = conn.execute(
            "SELECT id FROM decisions_log WHERE decided_iso=? AND decision=?",
            (decided_iso, decision_text),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO decisions_log
                   (decided_iso, decision, reasoning, linked_goal_id, decided_by,
                    sheet_row_index, synced_at, tealc_dirty)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (
                    decided_iso, decision_text, si.get("reasoning"),
                    si.get("linked_goal_id"), si.get("decided_by"),
                    si["_sheet_row"], now,
                ),
            )
            pulled += 1

    # Push dirty decisions (new rows only — append-only log)
    dirty_rows = conn.execute(
        "SELECT * FROM decisions_log WHERE tealc_dirty=1 AND (sheet_row_index IS NULL OR sheet_row_index=0)"
    ).fetchall()
    if dirty_rows:
        col_names = [d[0] for d in conn.execute("SELECT * FROM decisions_log LIMIT 0").description]
        for row in dirty_rows:
            dr = dict(zip(col_names, row))
            vals = [[
                dr.get("decided_iso", ""), dr.get("decision", ""),
                dr.get("reasoning", ""), dr.get("linked_goal_id", ""),
                dr.get("decided_by", ""),
            ]]
            result = sheets_svc.spreadsheets().values().append(
                spreadsheetId=sid, range="Decisions!A2",
                valueInputOption="USER_ENTERED",
                body={"values": vals},
            ).execute()
            new_row = _extract_row_from_range(
                result.get("updates", {}).get("updatedRange", "")
            )
            conn.execute(
                "UPDATE decisions_log SET sheet_row_index=?, tealc_dirty=0, synced_at=? WHERE id=?",
                (new_row, now, dr["id"]),
            )
            pushed += 1

    conn.commit()
    return pulled, pushed, deferred


def _sync_projects(sheets_svc, sid: str, conn: sqlite3.Connection) -> tuple:
    """Pull Projects tab → DB. Push dirty DB rows (tealc_dirty=1 via last_touched_by='Tealc')
    → Sheet. Skips gracefully when the Projects tab is empty."""
    pulled = pushed = deferred = 0
    now = _now_utc()

    try:
        res = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sid, range="Projects!A1:N"
        ).execute()
    except Exception:
        # Tab may not exist yet on very old sheets; return zeros safely
        return pulled, pushed, deferred

    rows = res.get("values", [])
    if len(rows) <= 1:
        # Header-only or empty — nothing to pull; still push dirty rows
        sheet_projects = []
    else:
        sheet_projects = _rows_to_dicts(rows, PROJECTS_HEADERS)

    for sp in sheet_projects:
        pid = sp.get("id", "").strip()
        if not pid:
            continue
        existing = conn.execute(
            "SELECT * FROM research_projects WHERE id=?", (pid,)
        ).fetchone()
        if existing:
            col_names = [
                d[0] for d in conn.execute(
                    "SELECT * FROM research_projects LIMIT 0"
                ).description
            ]
            ex_dict = dict(zip(col_names, existing))
            sheet_ts = sp.get("last_touched_iso", "")
            db_ts = ex_dict.get("last_touched_iso", "")
            if _iso_newer_than(sheet_ts, db_ts):
                conn.execute(
                    """UPDATE research_projects SET
                       name=?, description=?, status=?, linked_goal_ids=?,
                       data_dir=?, output_dir=?, current_hypothesis=?,
                       next_action=?, keywords=?, linked_artifact_id=?,
                       last_touched_by=?, last_touched_iso=?, notes=?,
                       sheet_row_index=?, synced_at=?
                       WHERE id=?""",
                    (
                        sp.get("name"), sp.get("description"), sp.get("status"),
                        sp.get("linked_goal_ids"), sp.get("data_dir"),
                        sp.get("output_dir"), sp.get("current_hypothesis"),
                        sp.get("next_action"), sp.get("keywords"),
                        sp.get("linked_artifact_id"), sp.get("last_touched_by"),
                        sheet_ts, sp.get("notes"), sp["_sheet_row"], now, pid,
                    ),
                )
                pulled += 1
        else:
            conn.execute(
                """INSERT OR IGNORE INTO research_projects
                   (id, name, description, status, linked_goal_ids, data_dir,
                    output_dir, current_hypothesis, next_action, keywords,
                    linked_artifact_id, last_touched_by, last_touched_iso,
                    notes, sheet_row_index, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid, sp.get("name"), sp.get("description"), sp.get("status"),
                    sp.get("linked_goal_ids"), sp.get("data_dir"),
                    sp.get("output_dir"), sp.get("current_hypothesis"),
                    sp.get("next_action"), sp.get("keywords"),
                    sp.get("linked_artifact_id"), sp.get("last_touched_by"),
                    sp.get("last_touched_iso"), sp.get("notes"),
                    sp["_sheet_row"], now,
                ),
            )
            pulled += 1

    # Push dirty rows (Tealc-created or Tealc-updated projects)
    dirty_rows = conn.execute(
        "SELECT * FROM research_projects WHERE last_touched_by='Tealc' "
        "AND (sheet_row_index IS NULL OR sheet_row_index=0 OR "
        "     synced_at < last_touched_iso)"
    ).fetchall()
    if dirty_rows:
        col_names = [
            d[0] for d in conn.execute(
                "SELECT * FROM research_projects LIMIT 0"
            ).description
        ]
        for row in dirty_rows:
            dr = dict(zip(col_names, row))
            # Heath priority: defer if Heath touched within the last hour
            if (dr.get("last_touched_by") == "Heath" and
                    _iso_is_within_past_hour(dr.get("last_touched_iso", ""))):
                deferred += 1
                continue
            row_idx = dr.get("sheet_row_index")
            vals = [[
                dr["id"],
                dr.get("name", ""),
                dr.get("description", ""),
                dr.get("status", ""),
                dr.get("linked_goal_ids", ""),
                dr.get("data_dir", ""),
                dr.get("output_dir", ""),
                dr.get("current_hypothesis", ""),
                dr.get("next_action", ""),
                dr.get("keywords", ""),
                dr.get("linked_artifact_id", ""),
                dr.get("last_touched_by", ""),
                dr.get("last_touched_iso", ""),
                dr.get("notes", ""),
            ]]
            if not row_idx:
                result = sheets_svc.spreadsheets().values().append(
                    spreadsheetId=sid, range="Projects!A2",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                new_row = _extract_row_from_range(
                    result.get("updates", {}).get("updatedRange", "")
                )
                conn.execute(
                    "UPDATE research_projects SET sheet_row_index=?, synced_at=? WHERE id=?",
                    (new_row, now, dr["id"]),
                )
            else:
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sid,
                    range=f"Projects!A{row_idx}:N{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": vals},
                ).execute()
                conn.execute(
                    "UPDATE research_projects SET synced_at=? WHERE id=?",
                    (now, dr["id"]),
                )
            pushed += 1

    conn.commit()
    return pulled, pushed, deferred


def _extract_row_from_range(range_str: str):
    """Extract integer row number from a range like 'Goals!A7:L7'. Returns None on failure."""
    try:
        # Range format: 'TabName!A7:L7' or 'TabName!A7'
        part = range_str.split("!")[-1]
        # Find first run of digits
        import re
        m = re.search(r"\d+", part)
        return int(m.group()) if m else None
    except Exception:
        return None


def _safe_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Seed SQLite from already-bootstrapped Sheet (on first sync after boot)
# ---------------------------------------------------------------------------

def _seed_db_from_sheet(sheets_svc, sid: str, conn: sqlite3.Connection):
    """Pull the seeded goals from Sheet into SQLite on initial bootstrap."""
    now = _now_utc()
    res = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sid, range="Goals!A1:L"
    ).execute()
    rows = res.get("values", [])
    goals = _rows_to_dicts(rows, GOALS_HEADERS)
    for g in goals:
        gid = g.get("id", "").strip()
        if not gid:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO goals
               (id, name, time_horizon, importance, nas_relevance, status, success_metric,
                why, owner, last_touched_by, last_touched_iso, notes, sheet_row_index,
                synced_at, tealc_dirty)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                gid, g.get("name"), g.get("time_horizon"),
                _safe_int(g.get("importance")), g.get("nas_relevance"),
                g.get("status"), g.get("success_metric"),
                g.get("why"), g.get("owner"), g.get("last_touched_by"),
                g.get("last_touched_iso"), g.get("notes"),
                g["_sheet_row"], now,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("sync_goals_sheet")
def job():
    """Bidirectional sync between 'Tealc Goals' Sheet and SQLite mirror."""
    # 1. Migrate schema
    _migrate_goals_tables()

    # 2. Bootstrap if needed
    cfg = _load_config()
    sid = cfg.get("goals_sheet_id", "")
    if not sid or sid in ("", "PASTE_GOALS_SHEET_ID"):
        sid = _bootstrap_sheet()
        # After bootstrap, seed DB
        sheets_svc, err = _get_google_service("sheets", "v4")
        if err:
            return f"error: Sheet created but Sheets API unavailable: {err}"
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        _seed_db_from_sheet(sheets_svc, sid, conn)
        conn.close()
        return f"bootstrap: created Sheet {sid}; seeded 6 goals; DB populated (5 tabs incl. Projects)"

    # 3. Connect to Sheets API
    sheets_svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"error: Sheets not connected: {err}"

    # 3b. Idempotent Projects tab: add if missing (handles existing 4-tab sheets)
    try:
        meta_check = sheets_svc.spreadsheets().get(spreadsheetId=sid).execute()
        existing_titles = {s["properties"]["title"] for s in meta_check["sheets"]}
        if "Projects" not in existing_titles:
            sheets_svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"addSheet": {"properties": {"title": "Projects"}}}]},
            ).execute()
            # Write header row
            sheets_svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range="Projects!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [PROJECTS_HEADERS]},
            ).execute()
    except Exception:
        pass  # Non-fatal; sync will retry next cycle

    # 4. Sync each tab sequentially. Google Sheets caps writes at 60/min per user;
    # a bulk-dirty state can trip that cap. On 429, stop cleanly — the next cycle
    # (5 min later) will resume. On any other error, re-raise so @tracked records it.
    from googleapiclient.errors import HttpError  # local import keeps scheduler boot light

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    results: dict[str, tuple[int, int, int]] = {}
    quota_hit_at: str | None = None
    try:
        for tab_name, sync_fn in (
            ("goals", _sync_goals),
            ("milestones", _sync_milestones),
            ("today", _sync_today),
            ("decisions", _sync_decisions),
            ("projects", _sync_projects),
        ):
            try:
                results[tab_name] = sync_fn(sheets_svc, sid, conn)
            except HttpError as e:
                if getattr(e, "resp", None) and e.resp.status == 429:
                    quota_hit_at = tab_name
                    break
                raise
    finally:
        conn.close()

    total_pull = sum(v[0] for v in results.values())
    total_push = sum(v[1] for v in results.values())
    total_defer = sum(v[2] for v in results.values())
    suffix = f" (rate-limited at {quota_hit_at}; next cycle will resume)" if quota_hit_at else ""
    return f"sync: pulled={total_pull} pushed={total_push} conflicts_deferred={total_defer}{suffix}"


# ---------------------------------------------------------------------------
# Allow direct invocation for testing / first-run bootstrap
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    result = job()
    print(result)
    sys.exit(0)
