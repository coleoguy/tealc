"""Weekly database health check — runs Saturday 3am Central.

Scans each registered Google Sheet in data/known_sheets.json, applies
consistency rules, and produces a "needs curation" briefing for Heath.

Rules applied (pure Python, no Anthropic API — $0/run):
  1. empty_critical_field  — first column is blank
  2. duplicate_primary     — first-column value appears more than once
  3. trailing_whitespace   — any cell has leading/trailing whitespace
  4. placeholder_values    — 'TBD', 'TODO', '???', 'unknown' in numeric fields
  5. outlier_chromosome_counts — chromosomal headers; count >500, non-integer, or negative

Run manually:
    cd "$HOME/Google Drive/My Drive/00-Lab-Agent"
    python -m agent.jobs.weekly_database_health
"""
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from agent.jobs import tracked
from agent.scheduler import DB_PATH

log = logging.getLogger("tealc.weekly_database_health")

KNOWN_SHEETS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "known_sheets.json"
)

# Placeholder sentinel — sheets without a real ID
_PLACEHOLDER = "PASTE_ID"

# Per-run cap: take only the first 100 issues across all categories per sheet
FLAG_CAP = 100

# Headers that indicate karyotype / chromosome-count columns
_CHROMO_HEADER_RE = re.compile(
    r"chromosome|2n|haploid|n=", re.IGNORECASE
)

# Placeholder strings that should never appear in numeric columns
_PLACEHOLDER_STRINGS = {"tbd", "todo", "???", "unknown"}


# ---------------------------------------------------------------------------
# Helper: get an authenticated Google Sheets service (mirrors tools.py pattern)
# ---------------------------------------------------------------------------
def _get_sheets_service():
    """Return (service, error_str). Replicates the auth pattern in tools.py."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        _HERE = os.path.dirname(os.path.abspath(__file__))
        TOKEN_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "data", "google_token.json"))
        CREDS_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "google_credentials.json"))

        SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                return None, "Google token not found or expired. Re-run authenticate_google.py."
        return build("sheets", "v4", credentials=creds), None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Consistency rules
# ---------------------------------------------------------------------------

def _detect_chromo_columns(headers: list[str]) -> set[int]:
    """Return column indices whose header mentions chromosome/karyotype counts."""
    return {
        i for i, h in enumerate(headers)
        if _CHROMO_HEADER_RE.search(str(h))
    }


def _apply_rules(rows: list[list]) -> dict[str, list[dict]]:
    """Run all 5 rules on the raw row data. Returns {category: [flags]}."""
    if not rows:
        return {}

    headers = rows[0] if rows else []
    data_rows = rows[1:]  # skip header

    flags: dict[str, list[dict]] = defaultdict(list)
    total_flagged = 0

    # Pre-compute: which columns are numeric (heuristic: >50% of values parse as float)
    numeric_cols: set[int] = set()
    if data_rows:
        n_sample = min(50, len(data_rows))
        num_cols = len(headers)
        for col_idx in range(num_cols):
            vals = [
                data_rows[r][col_idx]
                for r in range(n_sample)
                if col_idx < len(data_rows[r])
            ]
            numeric_count = sum(
                1 for v in vals
                if re.match(r"^-?[\d.]+$", str(v).strip())
            )
            if vals and numeric_count / len(vals) >= 0.5:
                numeric_cols.add(col_idx)

    chromo_cols = _detect_chromo_columns(headers)

    # Track primary key occurrences for duplicate detection
    primary_counts: dict[str, list[int]] = defaultdict(list)

    for row_idx, row in enumerate(data_rows, start=2):  # 1-indexed, row 1 = header
        if total_flagged >= FLAG_CAP:
            break

        snippet = str(row[:6])[:120]

        # Rule 1: empty_critical_field (first column blank)
        primary = str(row[0]).strip() if row else ""
        if not primary:
            if total_flagged < FLAG_CAP:
                flags["empty_critical_field"].append(
                    {"row_idx": row_idx, "snippet": snippet}
                )
                total_flagged += 1

        # Collect primary for duplicate check
        if primary:
            primary_counts[primary].append(row_idx)

        # Rule 3: trailing_whitespace (any cell)
        for cell in row:
            s = str(cell)
            if s != s.strip() and s.strip():
                if total_flagged < FLAG_CAP:
                    flags["trailing_whitespace"].append(
                        {"row_idx": row_idx, "snippet": snippet}
                    )
                    total_flagged += 1
                break  # one flag per row

        # Rule 4: placeholder_values in numeric columns
        for col_idx in numeric_cols:
            if col_idx < len(row):
                cell_val = str(row[col_idx]).strip().lower()
                if cell_val in _PLACEHOLDER_STRINGS:
                    if total_flagged < FLAG_CAP:
                        flags["placeholder_values"].append(
                            {"row_idx": row_idx, "snippet": snippet}
                        )
                        total_flagged += 1
                    break

        # Rule 5: outlier_chromosome_counts
        for col_idx in chromo_cols:
            if col_idx < len(row):
                raw = str(row[col_idx]).strip()
                if not raw:
                    continue
                try:
                    val = float(raw)
                    is_outlier = val > 500 or val < 0 or val != int(val)
                except ValueError:
                    is_outlier = True  # non-numeric in a chromo column
                if is_outlier:
                    if total_flagged < FLAG_CAP:
                        flags["outlier_chromosome_counts"].append(
                            {"row_idx": row_idx, "snippet": snippet}
                        )
                        total_flagged += 1
                    break

    # Rule 2: duplicate_primary — post-loop (needs full scan to detect)
    seen_dups: set[str] = set()
    for primary_val, row_indices in primary_counts.items():
        if len(row_indices) > 1 and total_flagged < FLAG_CAP:
            if primary_val not in seen_dups:
                seen_dups.add(primary_val)
                for ridx in row_indices[:5]:  # cap per-value dup rows
                    if total_flagged < FLAG_CAP:
                        flags["duplicate_primary"].append(
                            {"row_idx": ridx, "snippet": f"primary={primary_val!r}"}
                        )
                        total_flagged += 1

    return dict(flags)


# ---------------------------------------------------------------------------
# Briefing renderer
# ---------------------------------------------------------------------------

def _render_briefing(results: list[dict]) -> str:
    """Render markdown briefing from per-sheet results."""
    lines = ["# Weekly Database Health Report\n"]
    for r in results:
        name = r["sheet_name"]
        sid = r["spreadsheet_id"]
        total = r["total_rows"]
        flagged = r["flagged_count"]
        url = f"https://docs.google.com/spreadsheets/d/{sid}"
        lines.append(f"\n## [{name}]({url})")
        lines.append(f"- Rows scanned: {total} | Flags: {flagged}")
        if flagged == 0:
            lines.append("- No issues found.")
            continue
        summary = r.get("flagged_summary", {})
        for category, flag_list in summary.items():
            lines.append(f"\n### {category} ({len(flag_list)} rows)")
            for f in flag_list[:5]:  # show 3-5 examples
                lines.append(f"  - Row {f['row_idx']}: `{f['snippet']}`")
            if len(flag_list) > 5:
                lines.append(f"  - …and {len(flag_list) - 5} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("weekly_database_health")
def job(restrict_to_sheet: str = ""):
    """Run the weekly database health check.

    Args:
        restrict_to_sheet: If set, only check this one sheet (for manual runs).
    """
    # --- Step 1: Load known_sheets.json ---
    try:
        with open(KNOWN_SHEETS_PATH) as f:
            known_sheets: dict = json.load(f)
    except Exception as e:
        log.warning(f"Could not read known_sheets.json: {e}")
        return "no_sheets_configured"

    if not known_sheets:
        return "no_sheets_configured"

    # Check for all-placeholder case
    real_sheets = {
        name: sid
        for name, sid in known_sheets.items()
        if sid and sid != _PLACEHOLDER
    }
    if not real_sheets:
        log.info("weekly_database_health: all sheets are placeholders — skipping")
        return "no_sheets_configured"

    if restrict_to_sheet:
        if restrict_to_sheet in real_sheets:
            real_sheets = {restrict_to_sheet: real_sheets[restrict_to_sheet]}
        else:
            return f"sheet '{restrict_to_sheet}' not found or is a placeholder"

    # --- Step 2: Get Sheets API service ---
    svc, err = _get_sheets_service()
    if err:
        log.warning(f"weekly_database_health: Sheets API unavailable — {err}")
        return f"sheets_api_error: {err}"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_iso = datetime.now(timezone.utc).isoformat()

    per_sheet_results: list[dict] = []

    for sheet_name, spreadsheet_id in real_sheets.items():
        try:
            # --- Step 2b: Skip if already run today ---
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            already = conn.execute(
                "SELECT 1 FROM database_health_runs "
                "WHERE run_iso LIKE ? AND sheet_name=?",
                (f"{today_str}%", sheet_name),
            ).fetchone()
            conn.close()

            if already:
                log.info(f"weekly_database_health: {sheet_name} already checked today — skipping")
                continue

            log.info(f"weekly_database_health: checking {sheet_name} ({spreadsheet_id})")

            # --- Step 2c: Read rows via Sheets API ---
            resp = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range="A1:Z10000",
            ).execute()
            rows = resp.get("values", [])
            total_rows = max(0, len(rows) - 1)  # subtract header

            # --- Step 2d: Apply rules ---
            flagged_summary = _apply_rules(rows)
            flagged_count = sum(len(v) for v in flagged_summary.values())

            # --- Step 2g: Insert row ---
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """INSERT OR IGNORE INTO database_health_runs
                   (run_iso, sheet_name, spreadsheet_id, total_rows,
                    flagged_count, flagged_summary_json, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_iso,
                    sheet_name,
                    spreadsheet_id,
                    total_rows,
                    flagged_count,
                    json.dumps(flagged_summary),
                    None,
                ),
            )
            conn.commit()
            conn.close()

            per_sheet_results.append({
                "sheet_name": sheet_name,
                "spreadsheet_id": spreadsheet_id,
                "total_rows": total_rows,
                "flagged_count": flagged_count,
                "flagged_summary": flagged_summary,
            })

            log.info(
                f"weekly_database_health: {sheet_name} — "
                f"{total_rows} rows, {flagged_count} flags"
            )

            # Rate-limit: 200ms between sheets
            time.sleep(0.2)

        except Exception as e:
            log.error(f"weekly_database_health: error checking {sheet_name}: {e}")
            # Insert error row so we don't re-attempt today
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """INSERT OR IGNORE INTO database_health_runs
                       (run_iso, sheet_name, spreadsheet_id, total_rows,
                        flagged_count, flagged_summary_json, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_iso, sheet_name, spreadsheet_id, 0, 0, "{}", f"error: {str(e)[:200]}"),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            continue

    # --- Step 3: Create briefing if any flags found ---
    k_sheets = len(per_sheet_results)
    n_total = sum(r["flagged_count"] for r in per_sheet_results)
    briefing_created = False

    if n_total > 0:
        content_md = _render_briefing(per_sheet_results)
        flagged_sheets = sum(1 for r in per_sheet_results if r["flagged_count"] > 0)
        title = f"Weekly database health — {n_total} flags across {flagged_sheets} sheet(s)"
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """INSERT INTO briefings
                   (kind, urgency, title, content_md, created_at)
                   VALUES ('database_health', 'info', ?, ?, ?)""",
                (title, content_md, run_iso),
            )
            conn.commit()
            conn.close()
            briefing_created = True
            log.info(f"weekly_database_health: briefing created — {title}")
        except Exception as e:
            log.error(f"weekly_database_health: could not create briefing: {e}")

    summary = (
        f"checked={k_sheets} "
        f"flagged_total={n_total} "
        f"briefing={'yes' if briefing_created else 'no'}"
    )
    log.info(f"weekly_database_health: {summary}")
    return summary


# ---------------------------------------------------------------------------
# Manual entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from agent.scheduler import _migrate
    _migrate()
    result = job()
    print(result)
