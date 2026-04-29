"""
mine_project_leads.py
=====================
One-shot batch job: mines Google Docs linked to research_projects to extract
first-author, project type, and status metadata, then writes results to the DB.

Usage:
    python -m agent.jobs.mine_project_leads

Fields updated in research_projects:
    lead_student_id, lead_name, project_type, paper_status,
    journal, grant_status, agency, program,
    last_touched_by, last_touched_iso
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "data", "agent.db")

CREDS_PATH = os.path.join(BASE_DIR, "google_credentials.json")
TOKEN_PATH  = os.path.join(BASE_DIR, "data", "google_token.json")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

ANTHROPIC_MODEL = "claude-sonnet-4-6"
BATCH_SIZE      = 5
MAX_EXCERPT     = 3000


# ── Google auth ────────────────────────────────────────────────────────────────
def _get_drive_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Google credentials not valid. Run authenticate_google.py first.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def read_drive_file(service, file_id: str) -> str:
    """Export Google Doc or DOCX as plain text; returns text or error string.
    NOTE: supportsAllDrives=True is required for files in Shared Drives."""
    try:
        meta = service.files().get(
            fileId=file_id, fields="name,mimeType", supportsAllDrives=True
        ).execute()
        mime = meta.get("mimeType", "")
        if mime == "application/vnd.google-apps.document":
            content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = content.decode("utf-8") if isinstance(content, bytes) else content
            return f"**{meta['name']}**\n\n{text}"
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            # Download and convert DOCX via mammoth
            try:
                import mammoth
                raw = service.files().get_media(fileId=file_id).execute()
                with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp_f:
                    tmp_f.write(raw)
                    tmp_path = tmp_f.name
                try:
                    with open(tmp_path, "rb") as f:
                        result = mammoth.convert_to_markdown(f)
                    return f"**{meta['name']}**\n\n{result.value}"
                finally:
                    os.unlink(tmp_path)
            except ImportError:
                return f"[DOCX — mammoth not installed]"
        elif mime.startswith("text/"):
            content = service.files().get_media(fileId=file_id).execute()
            text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
            return f"**{meta['name']}**\n\n{text}"
        else:
            return f"[Unsupported mime: {mime}]"
    except Exception as e:
        return f"[Error: {e}]"


def find_best_manuscript_doc(service, folder_id: str) -> Optional[dict]:
    """
    Given a project folder ID, look for the best manuscript document.
    Strategy:
    1. Look for a 'manuscript' subfolder → drill in, find title_page or revised_manuscript.
    2. Look for any .docx or Google Doc with manuscript-like name in root.
    3. Return first Google Doc found.
    Returns dict with 'id' and 'mimeType', or None.
    """
    files = search_drive_in_folder(service, folder_id, max_results=20)

    # Look for manuscript/Manuscript subfolder
    ms_folder = next(
        (f for f in files if "manuscript" in f["name"].lower() and "folder" in f.get("mimeType", "")),
        None
    )
    if ms_folder:
        ms_files = search_drive_in_folder(service, ms_folder["id"], max_results=20)
        # Priority: title_page > revised_manuscript > manuscript > first doc
        priority_keys = ["title_page", "title page", "revised_manuscript", "revised manuscript", "manuscript"]
        for kw in priority_keys:
            doc = next(
                (f for f in ms_files if kw in f["name"].lower() and (
                    "document" in f.get("mimeType", "") or "word" in f.get("mimeType", "")
                )),
                None
            )
            if doc:
                return doc
        # First document in ms folder
        doc = next(
            (f for f in ms_files if "document" in f.get("mimeType", "") or "word" in f.get("mimeType", "")),
            None
        )
        if doc:
            return doc

    # Look for manuscript/paper named doc in root
    doc = next(
        (f for f in files if (
            any(kw in f["name"].lower() for kw in ["manuscript", "paper", "draft"]) and
            ("document" in f.get("mimeType", "") or "word" in f.get("mimeType", ""))
        )),
        None
    )
    if doc:
        return doc

    # Any doc in root
    return next(
        (f for f in files if "document" in f.get("mimeType", "") or "word" in f.get("mimeType", "")),
        None
    )


def search_drive_in_folder(service, folder_id: str, max_results: int = 5) -> list[dict]:
    """Find manuscript-looking files in a Drive folder."""
    try:
        query = (
            f"'{folder_id}' in parents and trashed=false and ("
            "name contains 'manuscript' or name contains 'draft' or "
            "name contains 'paper' or name contains 'writing' or "
            "mimeType='application/vnd.google-apps.document'"
            ")"
        )
        resp = service.files().list(
            q=query,
            fields="files(id,name,mimeType,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=max_results,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return resp.get("files", [])
    except Exception as e:
        return []


# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def load_students(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, full_name, short_name FROM students WHERE status='active'"
    ).fetchall()
    return [dict(r) for r in rows]


def load_projects_with_docs(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT id, name, description, linked_artifact_id, data_dir
        FROM research_projects
        WHERE status='active'
          AND linked_artifact_id != ''
          AND linked_artifact_id IS NOT NULL
    """).fetchall()
    return [dict(r) for r in rows]


def load_projects_without_docs(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT id, name, description, data_dir
        FROM research_projects
        WHERE status='active'
          AND (linked_artifact_id IS NULL OR linked_artifact_id = '')
          AND data_dir != ''
          AND data_dir IS NOT NULL
    """).fetchall()
    return [dict(r) for r in rows]


def get_current_project_type(conn, project_id: str):
    row = conn.execute(
        "SELECT project_type FROM research_projects WHERE id=?", (project_id,)
    ).fetchone()
    return row["project_type"] if row else None


# ── LLM extraction ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You extract metadata from research documents. Input: a JSON array of {project_id, project_name, excerpt} objects where excerpt is the first ~3000 chars of a Google Doc tied to a research project. Output a JSON array of the same length with:
{
  "project_id": "<echoed>",
  "is_manuscript": bool,
  "is_grant": bool,
  "first_author_name": "Jane Doe" or null,
  "all_authors": ["First1 Last1", "First2 Last2"] or null,
  "paper_status": "submitted|under_review|revision_resubmit|revision_new_journal|accepted|in_press|published" or null,
  "journal": "Nature Genetics" or null,
  "grant_status": "in_prep|submitted|under_review|awarded|declined|deferred" or null,
  "agency": "NIH|NSF|Google.org|..." or null,
  "program": "R35 MIRA|DEB|..." or null,
  "confidence": 0.0-1.0,
  "reasoning": "<1 sentence>"
}
Rules:
- First author is ALWAYS the first name in the author list. Do not guess based on last name alphabetizing.
- If the excerpt shows "DRAFT" / "in preparation" → paper_status='in_prep' (treat as paper even without a journal).
- If excerpt says "Submitted to <journal>" → paper_status='submitted', journal=that name.
- If excerpt says "Revised for <journal>" → paper_status='revision_resubmit' or 'revision_new_journal' depending on context.
- If no clear first-author line found, set first_author_name=null, confidence<0.3.
- If it looks like a grant (NIH/NSF/agency template, Specific Aims heading, budget) → is_grant=true.
Output ONLY the JSON array, no preamble."""


def call_sonnet(batch: list[dict], client) -> tuple[list[dict], dict]:
    """Call claude-sonnet-4-6 with a batch of {project_id, project_name, excerpt}."""
    user_msg = json.dumps(batch, indent=2)
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    usage = {
        "input_tokens":  resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    raw = resp.content[0].text.strip()
    # strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    results = json.loads(raw)
    return results, usage


# ── name matching ──────────────────────────────────────────────────────────────
def match_author(first_author: str | None, students: list[dict]) -> tuple[int | None, str | None]:
    """
    Returns (lead_student_id, lead_name).
    If matched to a student: (student_id, None).
    If Heath: (None, "Heath").
    If unknown: (None, first_author).
    """
    if not first_author:
        return None, None

    name_lower = first_author.lower().strip()

    # Heath check
    if any(tok in name_lower for tok in ["heath", "blackmon", "h. blackmon"]):
        return None, "Heath"

    # Exact match on full_name
    for s in students:
        if s["full_name"].lower() == name_lower:
            return s["id"], None

    # Token overlap — 2+ consecutive tokens match
    fa_tokens = name_lower.split()
    for s in students:
        s_tokens = s["full_name"].lower().split()
        # look for 2 consecutive tokens from fa_tokens in s_tokens
        for i in range(len(fa_tokens) - 1):
            pair = fa_tokens[i:i+2]
            for j in range(len(s_tokens) - 1):
                if s_tokens[j:j+2] == pair:
                    return s["id"], None
        # single token short_name match as fallback
        if s.get("short_name") and s["short_name"].lower() in fa_tokens:
            # require at least a last-name fragment too
            s_last = s["full_name"].lower().split()[-1]
            if s_last in fa_tokens or len(fa_tokens) == 1:
                return s["id"], None

    # No match — external / alumni
    return None, first_author


# ── cost tracking ──────────────────────────────────────────────────────────────
total_input_tokens  = 0
total_output_tokens = 0
sonnet_calls        = 0

def record_cost(usage: dict):
    global total_input_tokens, total_output_tokens, sonnet_calls
    total_input_tokens  += usage.get("input_tokens",  0)
    total_output_tokens += usage.get("output_tokens", 0)
    sonnet_calls        += 1
    try:
        from agent import cost_tracking
        cost_tracking.record_call(
            job_name="mine_project_leads",
            model=ANTHROPIC_MODEL,
            usage=usage,
        )
    except Exception:
        pass


# ── DB write-back ──────────────────────────────────────────────────────────────
def update_project(conn, project_id: str, extracted: dict, lead_student_id, lead_name: str | None,
                   current_type: str | None):
    now_iso = datetime.now(timezone.utc).isoformat()

    # Determine project_type — only set if currently NULL
    if current_type is not None:
        new_type = current_type
    elif extracted.get("is_grant"):
        new_type = "grant"
    elif extracted.get("is_manuscript"):
        new_type = "paper"
    else:
        new_type = None

    # Only write non-None values
    def v(val):
        return val if val is not None else None

    conn.execute("""
        UPDATE research_projects
        SET lead_student_id  = COALESCE(?, lead_student_id),
            lead_name        = COALESCE(?, lead_name),
            project_type     = COALESCE(?, project_type),
            paper_status     = COALESCE(?, paper_status),
            journal          = COALESCE(?, journal),
            grant_status     = COALESCE(?, grant_status),
            agency           = COALESCE(?, agency),
            program          = COALESCE(?, program),
            last_touched_by  = 'Tealc-mine',
            last_touched_iso = ?
        WHERE id = ?
    """, (
        lead_student_id,
        v(lead_name),
        v(new_type),
        v(extracted.get("paper_status")),
        v(extracted.get("journal")),
        v(extracted.get("grant_status")),
        v(extracted.get("agency")),
        v(extracted.get("program")),
        now_iso,
        project_id,
    ))
    conn.commit()


# ── main ───────────────────────────────────────────────────────────────────────
def run():
    from anthropic import Anthropic
    client = Anthropic()

    t_start = time.time()
    conn = get_db()
    students = load_students(conn)
    print(f"Loaded {len(students)} active students.")

    drive_svc = _get_drive_service()
    print("Drive service authenticated OK.")

    # ── Source 1: projects with linked_artifact_id ─────────────────────────────
    projects_with_docs  = load_projects_with_docs(conn)
    projects_without    = load_projects_without_docs(conn)

    print(f"\nSource 1: {len(projects_with_docs)} projects with linked docs")
    print(f"Source 2: {len(projects_without)} projects without linked docs\n")

    # Deduplicate linked_artifact_ids (p_059/060/066 share same doc, p_050/081 share too)
    seen_artifact_ids: set[str] = set()
    # We still process each project row, but we only read the doc once per file_id

    doc_cache: dict[str, str] = {}  # file_id -> text excerpt

    def fetch_excerpt(file_id: str) -> str:
        if file_id in doc_cache:
            return doc_cache[file_id]
        text = read_drive_file(drive_svc, file_id)
        excerpt = text[:MAX_EXCERPT]
        doc_cache[file_id] = excerpt
        return excerpt

    # Build batch items for Source 1
    batch_items: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for p in projects_with_docs:
        file_id = p["linked_artifact_id"]
        excerpt = fetch_excerpt(file_id)
        if excerpt.startswith("[Error") or excerpt.startswith("[Unsupported"):
            skipped.append((p["id"], f"Doc unreadable: {excerpt[:80]}"))
            continue
        batch_items.append({
            "project_id":   p["id"],
            "project_name": p["name"],
            "excerpt":      excerpt,
        })

    # Source 2: projects without linked docs
    for p in projects_without:
        dd = p.get("data_dir", "")
        # Is it a Drive folder ID (no slashes)?
        if not dd or "/" in dd or "\\" in dd:
            skipped.append((p["id"], f"data_dir is local path: {dd[:60]}"))
            continue
        # Try to find a manuscript file in that folder
        files = search_drive_in_folder(drive_svc, dd, max_results=5)
        if not files:
            skipped.append((p["id"], f"No manuscript files found in Drive folder {dd}"))
            continue
        # Pick the first (most recently modified)
        chosen = files[0]
        excerpt = fetch_excerpt(chosen["id"])
        if excerpt.startswith("[Error") or excerpt.startswith("[Unsupported"):
            skipped.append((p["id"], f"File {chosen['name']} unreadable"))
            continue
        batch_items.append({
            "project_id":   p["id"],
            "project_name": p["name"],
            "excerpt":      excerpt,
        })

    print(f"Batch items to process: {len(batch_items)}")
    print(f"Pre-skipped: {len(skipped)}")

    # ── Sonnet batching ────────────────────────────────────────────────────────
    all_results: list[dict] = []

    for i in range(0, len(batch_items), BATCH_SIZE):
        batch = batch_items[i:i+BATCH_SIZE]
        ids   = [b["project_id"] for b in batch]
        print(f"\nSonnet call {i//BATCH_SIZE + 1}: projects {ids}")
        try:
            results, usage = call_sonnet(batch, client)
            record_cost(usage)
            print(f"  tokens in={usage['input_tokens']} out={usage['output_tokens']}")
            all_results.extend(results)
        except Exception as e:
            print(f"  ERROR in Sonnet call: {e}")
            for b in batch:
                skipped.append((b["project_id"], f"Sonnet error: {e}"))

    # ── Match authors & write to DB ────────────────────────────────────────────
    # Track output rows for the filled sheet
    output_rows: list[dict] = []

    for extracted in all_results:
        pid = extracted.get("project_id")
        if not pid:
            continue
        first_author = extracted.get("first_author_name")
        lead_sid, lead_nm = match_author(first_author, students)

        current_type = get_current_project_type(conn, pid)
        update_project(conn, pid, extracted, lead_sid, lead_nm, current_type)

        # Figure out display lead
        if lead_sid:
            matched_student = next((s for s in students if s["id"] == lead_sid), None)
            lead_display = matched_student["full_name"] if matched_student else f"[id={lead_sid}]"
        elif lead_nm:
            lead_display = lead_nm
        else:
            lead_display = f"?? {first_author}" if first_author else "—"

        proj_name = next(
            (p["name"] for p in projects_with_docs + projects_without if p["id"] == pid), pid
        )
        ptype = "grant" if extracted.get("is_grant") else ("paper" if extracted.get("is_manuscript") else "—")
        status = extracted.get("paper_status") or extracted.get("grant_status") or "—"
        journal_agency = extracted.get("journal") or extracted.get("agency") or "—"
        confidence = extracted.get("confidence", 0.0)

        output_rows.append({
            "pid":          pid,
            "name":         proj_name,
            "type":         ptype,
            "lead":         lead_display,
            "lead_sid":     lead_sid,
            "status":       status,
            "journal":      journal_agency,
            "confidence":   confidence,
            "reasoning":    extracted.get("reasoning", ""),
        })
        print(f"  {pid}: {proj_name[:30]} → {lead_display} ({ptype}, {status})")

    t_end = time.time()
    elapsed = t_end - t_start

    # ── Print filled sheet ─────────────────────────────────────────────────────
    print("\n\n" + "="*80)
    print("# Lead-mining results\n")
    header = "| Project ID | Project name | Type | Lead → | Stu_ID | Status | Journal/Agency | Confidence |"
    sep    = "|---|---|---|---|---|---|---|---|"
    print(header)
    print(sep)
    for r in sorted(output_rows, key=lambda x: x["pid"]):
        name_trunc    = r["name"][:30]
        lead_trunc    = r["lead"][:25]
        journal_trunc = r["journal"][:20]
        sid_str       = str(r["lead_sid"]) if r["lead_sid"] else "—"
        print(f"| {r['pid']} | {name_trunc} | {r['type']} | {lead_trunc} | {sid_str} | {r['status']} | {journal_trunc} | {r['confidence']:.2f} |")

    # Counts
    student_leads  = sum(1 for r in output_rows if r["lead_sid"] is not None)
    external_leads = sum(1 for r in output_rows if r["lead_sid"] is None and r["lead"] not in ("—", ""))
    papers         = sum(1 for r in output_rows if r["type"] == "paper")
    grants         = sum(1 for r in output_rows if r["type"] == "grant")

    print(f"\n## Summary")
    print(f"- Projects processed: {len(output_rows)}")
    print(f"- Leads assigned (student match): {student_leads}")
    print(f"- Leads assigned (Heath/external): {external_leads}")
    print(f"- Typed as paper: {papers}")
    print(f"- Typed as grant: {grants}")
    print(f"- Skipped (no Doc accessible): {len(skipped)}")
    print(f"\n## Skipped projects")
    for sid, reason in skipped:
        print(f"- {sid}: {reason}")

    # Cost summary
    # claude-sonnet-4-6 pricing: $3/MTok input, $15/MTok output (approx)
    cost_usd = (total_input_tokens / 1_000_000) * 3.0 + (total_output_tokens / 1_000_000) * 15.0
    print(f"\n## Cost")
    print(f"- Sonnet calls: {sonnet_calls}")
    print(f"- Total input tokens: {total_input_tokens:,}")
    print(f"- Total output tokens: {total_output_tokens:,}")
    print(f"- Estimated cost: ${cost_usd:.4f}")
    print(f"- Elapsed time: {elapsed:.1f}s")

    conn.close()
    return output_rows, skipped


if __name__ == "__main__":
    run()
