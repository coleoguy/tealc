#!/usr/bin/env python3
"""enrich_projects_from_drive.py — One-shot enrichment migration.

For each research_project in SQLite, fuzzy-match against the lab's
Drive project folders.  On a match:
  • Set data_dir = folder ID
  • Find most-recently-modified .gdoc/.docx → set linked_artifact_id
  • If keywords or current_hypothesis are missing, Sonnet reads the artifact
    and extracts them.

Drive folders with NO matching existing project are added as new
research_projects with status='proposed'.

IDEMPOTENT: re-running adds 0 changes when everything is already mapped.
READ-ONLY on Drive — no files are modified.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

# Resolve root early so we can load .env before importing anthropic
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.dirname(_SCRIPT_DIR)
_ROOT = os.path.dirname(_AGENT_DIR)

sys.path.insert(0, _ROOT)

# Load .env FIRST so ANTHROPIC_API_KEY is in os.environ before anthropic is imported
try:
    from dotenv import load_dotenv  # noqa: PLC0415
    _env_path = os.path.join(_ROOT, ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=True)
except ImportError:
    pass  # python-dotenv not installed — rely on env already set

import anthropic  # noqa: E402 (must come after dotenv load)

DB_PATH = os.path.join(_ROOT, "data", "agent.db")

# Lab shared drive / Projects folder
LAB_DRIVE = "0AKI1NlwWUostUk9PVA"
PROJECTS_FOLDER = "1TkR4etnfq0WnVjx44iN9rrUHeZN4oDLS"

# mimeTypes that count as "documents"
DOC_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("enrich_projects")

# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


def _drive_service():
    from agent.tools import _get_google_service  # noqa: PLC0415
    svc, err = _get_google_service("drive", "v3")
    if err:
        raise RuntimeError(f"Drive not connected: {err}")
    return svc


def _list_project_folders(svc):
    """Return list of dicts: id, name, modifiedTime — all non-meta subfolders."""
    folders = svc.files().list(
        q=(
            f"'{PROJECTS_FOLDER}' in parents "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        ),
        corpora="drive",
        driveId=LAB_DRIVE,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=200,
        fields="files(id, name, modifiedTime)",
    ).execute().get("files", [])

    # Filter meta folders
    meta_re = re.compile(r"^\d{4} abandoned\??$", re.IGNORECASE)
    meta_names = {"Example", "Open Dissertation Projects"}
    active = []
    skipped = []
    for f in folders:
        name = f["name"]
        if meta_re.match(name) or name in meta_names:
            skipped.append(name)
        else:
            active.append(f)

    if skipped:
        log.info("Skipped meta folders: %s", skipped)

    return active, skipped


def _list_folder_docs(svc, folder_id):
    """Return list of files (id, name, mimeType, modifiedTime) in a folder,
    sorted newest-first, filtered to document mimeTypes only."""
    files = svc.files().list(
        q=(
            f"'{folder_id}' in parents "
            "and trashed=false "
            "and (mimeType='application/vnd.google-apps.document' "
            "or mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document')"
        ),
        corpora="drive",
        driveId=LAB_DRIVE,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=100,
        fields="files(id, name, mimeType, modifiedTime)",
    ).execute().get("files", [])

    # Sort newest first
    files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return files


def _read_artifact_text(svc, file_id, mime, max_chars=3000):
    """Return up to max_chars of plain text from a Drive document."""
    try:
        if mime == "application/vnd.google-apps.document":
            # Note: export() does not accept supportsAllDrives — auth covers shared drives
            content = svc.files().export(
                fileId=file_id, mimeType="text/plain",
            ).execute()
            text = content.decode("utf-8") if isinstance(content, bytes) else content
        else:
            # .docx — download and parse with python-docx
            import tempfile  # noqa: PLC0415
            raw = svc.files().get_media(
                fileId=file_id, supportsAllDrives=True
            ).execute()
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            try:
                import docx  # noqa: PLC0415
                doc = docx.Document(tmp_path)
                text = "\n".join(p.text for p in doc.paragraphs)
            except Exception:
                text = ""
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except Exception as exc:
        log.warning("Could not read artifact %s: %s", file_id, exc)
        return ""
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize(s):
    """Lowercase, strip punctuation, collapse whitespace, strip accents."""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = _PUNCT_RE.sub(" ", s.lower())
    return " ".join(s.split())


def _jaccard(a_words, b_words):
    s1, s2 = set(a_words), set(b_words)
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


def _fuzzy_match(project_name, folders):
    """Return (best_folder_dict, confidence_label) or (None, None).

    Strategy (in order of priority):
    1. Exact normalized match
    2. Substring match (shorter ≥ 4 chars, contained in longer)
    3. Jaccard on word sets > 0.5
    If multiple candidates tie at any level, return the most-recently-modified one.
    """
    pn = _normalize(project_name)
    pw = pn.split()

    exact = []
    substring = []
    jaccard_hits = []

    for f in folders:
        fn = _normalize(f["name"])
        fw = fn.split()

        # 1. Exact
        if pn == fn:
            exact.append(f)
            continue

        # 2. Substring (shorter ≥ 4 chars must be contained in longer)
        shorter, longer = (pn, fn) if len(pn) <= len(fn) else (fn, pn)
        if len(shorter) >= 4 and shorter in longer:
            substring.append(f)
            continue

        # Also check word-level containment (all words of shorter in longer set)
        sp, lp = (pw, fw) if len(pw) <= len(fw) else (fw, pw)
        if len(sp) >= 1 and set(sp).issubset(set(lp)) and len(" ".join(sp)) >= 4:
            substring.append(f)
            continue

        # 3. Jaccard
        j = _jaccard(pw, fw)
        if j > 0.5:
            jaccard_hits.append((j, f))

    def _newest(lst):
        return max(lst, key=lambda f: f.get("modifiedTime", ""))

    if exact:
        if len(exact) > 1:
            log.warning("Multiple exact matches for '%s' — picking newest", project_name)
        return _newest(exact), "exact"

    if substring:
        if len(substring) > 1:
            log.warning("Multiple substring matches for '%s' — picking newest", project_name)
        return _newest(substring), "substring"

    if jaccard_hits:
        jaccard_hits.sort(key=lambda x: x[0], reverse=True)
        top_score = jaccard_hits[0][0]
        top_tier = [f for sc, f in jaccard_hits if sc == top_score]
        if len(top_tier) > 1:
            log.warning("Ambiguous Jaccard matches for '%s' — picking newest", project_name)
        return _newest(top_tier), f"jaccard({top_score:.2f})"

    return None, None


# ---------------------------------------------------------------------------
# Sonnet extraction
# ---------------------------------------------------------------------------

_ANTHROPIC_CLIENT = None


def _get_client():
    global _ANTHROPIC_CLIENT
    if _ANTHROPIC_CLIENT is None:
        _ANTHROPIC_CLIENT = anthropic.Anthropic()
    return _ANTHROPIC_CLIENT


EXTRACT_SYSTEM = (
    "From the project name and the artifact text below, extract for the "
    "research project: "
    "(1) current_hypothesis: 2-3 sentences stating the testable claim or the project's "
    "central question. "
    "(2) keywords: 5-10 comma-separated topical scientific terms suitable for OpenAlex "
    "literature search. "
    "Output JSON only: {\"current_hypothesis\": \"...\", \"keywords\": \"...\"}. "
    "If the artifact doesn't make the hypothesis clear, base it on the project name."
)


def _sonnet_extract(project_name, description, artifact_text):
    """Call Sonnet 4.6 and return (current_hypothesis, keywords) strings."""
    client = _get_client()
    user_content = (
        f"Project name: {project_name}\n"
        f"Description: {description or '(none)'}\n\n"
        f"Artifact excerpt:\n{artifact_text or '(no artifact text available)'}"
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        hyp = data.get("current_hypothesis", "").strip()
        kw = data.get("keywords", "").strip()
        return hyp, kw
    except Exception as exc:
        log.warning("Sonnet extract failed for '%s': %s", project_name, exc)
        return "", ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _load_projects(conn):
    return conn.execute(
        "SELECT * FROM research_projects ORDER BY id"
    ).fetchall()


def _update_project(conn, project_id, data_dir="", linked_artifact_id="",
                    current_hypothesis="", keywords=""):
    """Direct SQLite update (same logic as update_research_project tool)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT * FROM research_projects WHERE id=?", (project_id,)
    ).fetchone()
    if not row:
        log.error("Project %s not found in DB", project_id)
        return

    p = dict(row)
    if data_dir:
        p["data_dir"] = data_dir
    if linked_artifact_id:
        p["linked_artifact_id"] = linked_artifact_id
    if current_hypothesis:
        p["current_hypothesis"] = current_hypothesis
    if keywords:
        p["keywords"] = keywords

    conn.execute(
        """UPDATE research_projects SET
           data_dir=?, linked_artifact_id=?, current_hypothesis=?, keywords=?,
           last_touched_by='Tealc', last_touched_iso=?, synced_at=?
           WHERE id=?""",
        (
            p.get("data_dir") or None,
            p.get("linked_artifact_id") or None,
            p.get("current_hypothesis") or None,
            p.get("keywords") or None,
            now_iso, now_iso, project_id,
        ),
    )
    conn.commit()


def _next_project_id(conn):
    row = conn.execute(
        "SELECT id FROM research_projects ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "p_001"
    last = row["id"]
    try:
        num = int(last.split("_")[1]) + 1
    except (IndexError, ValueError):
        num = 1
    return f"p_{num:03d}"


def _add_project(conn, name, description, current_hypothesis, keywords,
                 data_dir, linked_artifact_id):
    """Insert a new 'proposed' project."""
    now_iso = datetime.now(timezone.utc).isoformat()
    new_id = _next_project_id(conn)
    conn.execute(
        """INSERT INTO research_projects
           (id, name, description, status, linked_goal_ids, data_dir,
            output_dir, current_hypothesis, next_action, keywords,
            linked_artifact_id, last_touched_by, last_touched_iso, synced_at)
           VALUES (?,?,?,'proposed',NULL,?,NULL,?,NULL,?,?,?,?,?)""",
        (
            new_id, name, description or None,
            data_dir or None,
            current_hypothesis or None,
            keywords or None,
            linked_artifact_id or None,
            "Tealc", now_iso, now_iso,
        ),
    )
    conn.commit()
    log.info("  → Added proposed project %s: '%s'", new_id, name)
    return new_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    log.info("=" * 60)
    log.info("Enrich projects from Drive — starting")
    log.info("=" * 60)

    # 1. Connect to Drive
    svc = _drive_service()

    # 2. List Drive project folders
    log.info("Phase 1: Listing Drive project folders …")
    folders, skipped_meta = _list_project_folders(svc)
    log.info("Active Drive folders: %d (skipped meta: %s)", len(folders), skipped_meta)

    # 3. Load existing projects
    conn = _get_db()
    projects = _load_projects(conn)
    log.info("Existing research_projects in DB: %d", len(projects))

    # Track which folder IDs get matched
    matched_folder_ids = set()

    # Counters
    already_done = 0
    newly_enriched = 0
    skipped_no_match = 0
    uncertain_pairs = []  # (project_name, folder_name, confidence)
    strong_matches = []   # (project_name, folder_name, confidence)

    # 4. Phase 2: Match existing projects → folders
    log.info("Phase 2: Matching existing projects to Drive folders …")

    for proj in projects:
        pid = proj["id"]
        pname = proj["name"]

        # Idempotency: skip if data_dir + keywords + current_hypothesis are all set.
        # linked_artifact_id is not required — some folders have no documents.
        has_dir = bool(proj["data_dir"] and str(proj["data_dir"]).strip())
        has_kw = bool(proj["keywords"] and str(proj["keywords"]).strip())
        has_hyp = bool(proj["current_hypothesis"] and str(proj["current_hypothesis"]).strip())

        if has_dir and has_kw and has_hyp:
            log.info("SKIP  %s (%s) — already fully enriched", pid, pname)
            already_done += 1
            matched_folder_ids.add(proj["data_dir"])
            continue

        try:
            folder, confidence = _fuzzy_match(pname, folders)

            if folder is None:
                log.info("NO_MATCH  %s (%s)", pid, pname)
                skipped_no_match += 1
                continue

            folder_id = folder["id"]
            folder_name = folder["name"]
            matched_folder_ids.add(folder_id)

            log.info(
                "MATCH  %s (%s) → '%s' [%s]",
                pid, pname, folder_name, confidence
            )

            # Record match quality
            if confidence == "exact" or confidence.startswith("substring"):
                strong_matches.append((pname, folder_name, confidence))
            else:
                uncertain_pairs.append((pname, folder_name, confidence))

            # data_dir
            new_data_dir = folder_id if not has_dir else (proj["data_dir"] or folder_id)

            # Find most-recent doc in folder
            docs = _list_folder_docs(svc, folder_id)
            artifact_id = ""
            artifact_mime = ""
            artifact_text = ""

            if docs:
                best_doc = docs[0]
                artifact_id = best_doc["id"]
                artifact_mime = best_doc["mimeType"]
                log.info(
                    "  artifact: '%s' (%s)",
                    best_doc["name"], artifact_mime.split(".")[-1]
                )
            else:
                log.info("  No documents found in folder '%s'", folder_name)

            new_artifact_id = artifact_id if not has_artifact else (proj["linked_artifact_id"] or artifact_id)

            # Sonnet extraction if keywords/hypothesis missing
            has_kw = bool(proj["keywords"] and proj["keywords"].strip())
            has_hyp = bool(proj["current_hypothesis"] and proj["current_hypothesis"].strip())

            new_hyp = proj["current_hypothesis"] or ""
            new_kw = proj["keywords"] or ""

            if (not has_kw or not has_hyp) and artifact_id:
                log.info("  Running Sonnet extraction …")
                artifact_text = _read_artifact_text(svc, artifact_id, artifact_mime, 3000)
                extracted_hyp, extracted_kw = _sonnet_extract(
                    pname,
                    proj["description"] or "",
                    artifact_text,
                )
                if not has_hyp and extracted_hyp:
                    new_hyp = extracted_hyp
                if not has_kw and extracted_kw:
                    new_kw = extracted_kw

            elif not has_kw or not has_hyp:
                # No artifact — extract from name only
                log.info("  Running Sonnet extraction (name-only, no artifact) …")
                extracted_hyp, extracted_kw = _sonnet_extract(
                    pname,
                    proj["description"] or "",
                    "",
                )
                if not has_hyp and extracted_hyp:
                    new_hyp = extracted_hyp
                if not has_kw and extracted_kw:
                    new_kw = extracted_kw

            # Write update
            _update_project(
                conn,
                project_id=pid,
                data_dir=new_data_dir,
                linked_artifact_id=new_artifact_id,
                current_hypothesis=new_hyp,
                keywords=new_kw,
            )
            newly_enriched += 1

        except Exception as exc:
            log.error("ERROR processing project %s (%s): %s", pid, pname, exc)
            continue

    # 5. Phase 3: Add unmatched Drive folders as proposed projects
    log.info("Phase 3: Adding unmatched Drive folders as proposed projects …")
    new_proposed = []

    for folder in folders:
        folder_id = folder["id"]
        folder_name = folder["name"]

        if folder_id in matched_folder_ids:
            continue

        log.info("UNMATCHED FOLDER  '%s' → adding as proposed", folder_name)

        try:
            docs = _list_folder_docs(svc, folder_id)
            artifact_id = ""
            artifact_mime = ""
            artifact_text = ""

            if docs:
                best_doc = docs[0]
                artifact_id = best_doc["id"]
                artifact_mime = best_doc["mimeType"]
                artifact_text = _read_artifact_text(svc, artifact_id, artifact_mime, 1500)

            extracted_hyp, extracted_kw = _sonnet_extract(
                folder_name, "", artifact_text
            )

            # Generate description from hypothesis if we have it
            description = (
                extracted_hyp[:200] if extracted_hyp else f"Proposed project from Drive folder: {folder_name}"
            )

            new_id = _add_project(
                conn,
                name=folder_name,
                description=description,
                current_hypothesis=extracted_hyp,
                keywords=extracted_kw,
                data_dir=artifact_id and folder_id or folder_id,
                linked_artifact_id=artifact_id,
            )
            new_proposed.append(folder_name)
            matched_folder_ids.add(folder_id)

        except Exception as exc:
            log.error("ERROR adding proposed project for folder '%s': %s", folder_name, exc)
            continue

    conn.close()

    # 6. Phase 4: Trigger Goals Sheet sync
    log.info("Phase 4: Triggering Goals Sheet sync …")
    try:
        from agent.jobs.sync_goals_sheet import job as sync_job  # noqa: PLC0415
        result = sync_job()
        log.info("Sync result: %s", result)
    except Exception as exc:
        log.warning("Sync trigger failed (non-fatal): %s", exc)

    # 7. Summary
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  already_enriched (skipped): %d", already_done)
    log.info("  newly_enriched:             %d", newly_enriched)
    log.info("  no_match (unresolved):      %d", skipped_no_match)
    log.info("  new_proposed_projects:      %d", len(new_proposed))
    log.info("  meta_folders_skipped:       %s", skipped_meta)
    log.info("")
    log.info("TOP STRONG MATCHES:")
    for pname, fname, conf in strong_matches[:10]:
        log.info("  '%s' → '%s' [%s]", pname, fname, conf)
    log.info("")
    log.info("UNCERTAIN MATCHES (Heath should verify):")
    for pname, fname, conf in uncertain_pairs:
        log.info("  '%s' → '%s' [%s]", pname, fname, conf)
    log.info("")
    log.info("NEW PROPOSED PROJECTS:")
    for name in new_proposed:
        log.info("  '%s'", name)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
