"""Tealc Knowledge Map — resource catalog helper module.

Provides CRUD + search over the resource_catalog table which tracks
where all of Heath's information lives (Drive folders, Google Docs, GitHub
repos, email contacts, external URLs, grants, etc.).

All functions open fresh connections with WAL mode and close on exit.
"""
import json
import os
import random
import re
import sqlite3
import string
from datetime import datetime, timezone
from typing import Optional

from agent.scheduler import DB_PATH

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_KIND_PREFIX = {
    "google_doc":     "doc",
    "google_sheet":   "sht",
    "drive_folder":   "dir",
    "local_dir":      "dir",
    "github_repo":    "git",
    "email_contact":  "eml",
    "external_url":   "url",
    "grant":          "grn",
    "research_project": "prj",
    "other":          "oth",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(kind: str) -> str:
    prefix = _KIND_PREFIX.get(kind, "oth")
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand6 = "".join(random.choices(string.hexdigits[:16].lower(), k=6))
    return f"r_{prefix}_{date_str}_{rand6}"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Split comma-separated list fields into Python lists for convenience
    for field in ("linked_project_ids", "linked_goal_ids", "linked_person_ids", "tags"):
        raw = d.get(field) or ""
        d[field] = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
    return d


def _list_to_str(lst: Optional[list]) -> str:
    if not lst:
        return ""
    return ", ".join(str(x) for x in lst)


def _is_drive_id(value: str) -> bool:
    """Heuristic: Google Drive folder/file IDs are ~28-33 chars, alphanumeric + dashes + underscores, no slashes."""
    if not value:
        return False
    if "/" in value or "\\" in value:
        return False
    if not re.match(r'^[A-Za-z0-9_\-]+$', value):
        return False
    return 20 <= len(value) <= 44


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_catalog(status: Optional[str] = "confirmed") -> list:
    """Return all resource_catalog rows matching status. Pass None for all."""
    conn = _get_conn()
    try:
        if status is None:
            rows = conn.execute("SELECT * FROM resource_catalog ORDER BY kind, display_name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM resource_catalog WHERE status=? ORDER BY kind, display_name",
                (status,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_resource(resource_id: str) -> Optional[dict]:
    """Fetch one row by id."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM resource_catalog WHERE id=?", (resource_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def add_resource(
    kind: str,
    handle: str,
    display_name: str,
    purpose: str = "",
    tags: Optional[list] = None,
    linked_project_ids: Optional[list] = None,
    linked_goal_ids: Optional[list] = None,
    linked_person_ids: Optional[list] = None,
    notes: str = "",
    proposed_by: str = "heath",
) -> dict:
    """Insert a new resource row and return it."""
    now = _now_iso()
    resource_id = _gen_id(kind)

    if proposed_by == "heath":
        status = "confirmed"
        last_confirmed_iso = now
    else:
        status = "proposed"
        last_confirmed_iso = None

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO resource_catalog
               (id, kind, handle, display_name, purpose, tags,
                linked_project_ids, linked_goal_ids, linked_person_ids,
                status, proposed_by, last_confirmed_iso, last_used_iso,
                notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
            (
                resource_id, kind, handle, display_name, purpose,
                _list_to_str(tags),
                _list_to_str(linked_project_ids),
                _list_to_str(linked_goal_ids),
                _list_to_str(linked_person_ids),
                status, proposed_by, last_confirmed_iso,
                notes, now, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM resource_catalog WHERE id=?", (resource_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_resource(resource_id: str, **fields) -> Optional[dict]:
    """Partial update. Returns updated row or None if not found."""
    allowed = {
        "kind", "handle", "display_name", "purpose", "tags",
        "linked_project_ids", "linked_goal_ids", "linked_person_ids",
        "notes", "status",
    }
    list_fields = {"tags", "linked_project_ids", "linked_goal_ids", "linked_person_ids"}

    now = _now_iso()
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in list_fields and isinstance(v, list):
            updates[k] = _list_to_str(v)
        else:
            updates[k] = v

    if not updates:
        return get_resource(resource_id)

    updates["updated_at"] = now
    if updates.get("status") == "confirmed":
        updates["last_confirmed_iso"] = now

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [resource_id]

    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE resource_catalog SET {set_clause} WHERE id=?", values
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM resource_catalog WHERE id=?", (resource_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def confirm_resource(resource_id: str) -> Optional[dict]:
    """Shortcut: set status='confirmed', update timestamps."""
    return update_resource(resource_id, status="confirmed")


def dismiss_resource(resource_id: str) -> Optional[dict]:
    """Soft-delete: set status='dismissed'."""
    return update_resource(resource_id, status="dismissed")


def find_resource(query: str, kind: Optional[str] = None, limit: int = 10) -> list:
    """Fuzzy search across display_name, purpose, tags, handle. Stamps last_used_iso."""
    query_lower = query.lower()
    tokens = query_lower.split()

    conn = _get_conn()
    try:
        if kind:
            rows = conn.execute(
                "SELECT * FROM resource_catalog WHERE kind=? AND status != 'dismissed'",
                (kind,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM resource_catalog WHERE status != 'dismissed'"
            ).fetchall()

        scored = []
        for row in rows:
            d = _row_to_dict(row)
            name_l = (d.get("display_name") or "").lower()
            purpose_l = (d.get("purpose") or "").lower()
            tags_l = " ".join(d.get("tags") or []).lower()
            handle_l = (d.get("handle") or "").lower()

            score = 0
            for tok in tokens:
                if tok in name_l:
                    score += 4
                if tok in purpose_l:
                    score += 2
                if tok in tags_l:
                    score += 2
                if tok in handle_l:
                    score += 1

            if score > 0:
                scored.append((score, d))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [d for _, d in scored[:limit]]

        # Stamp last_used_iso on matched rows
        now = _now_iso()
        for d in results:
            conn.execute(
                "UPDATE resource_catalog SET last_used_iso=? WHERE id=?",
                (now, d["id"])
            )
        conn.commit()
        return results
    finally:
        conn.close()


def categories_overview() -> list:
    """Returns a summary grouped by kind with counts per status."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT kind,
                      SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) AS confirmed,
                      SUM(CASE WHEN status='proposed'  THEN 1 ELSE 0 END) AS proposed,
                      SUM(CASE WHEN status='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM resource_catalog
               GROUP BY kind
               ORDER BY kind"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

def seed_from_existing_state(overwrite: bool = False) -> dict:
    """Auto-populate the catalog from everything Tealc already knows.

    If overwrite=False and a resource with the same (kind, handle) exists, skip.
    Returns {'added': N, 'skipped_existing': M, 'by_kind': {...}}.
    """
    added = 0
    skipped = 0
    by_kind: dict = {}

    def _try_add(kind, handle, display_name, purpose="", tags=None, linked_project_ids=None,
                  linked_goal_ids=None, linked_person_ids=None, notes=""):
        nonlocal added, skipped
        # Normalize handle for dedup check
        norm_handle = (handle or "").strip()

        conn = _get_conn()
        try:
            # When handle is a placeholder, dedup on (kind, handle, display_name)
            # so multiple contacts/DBs with <TO-BE-FILLED> each get their own row
            if norm_handle == "<TO-BE-FILLED>":
                existing = conn.execute(
                    "SELECT id FROM resource_catalog WHERE kind=? AND handle=? AND display_name=?",
                    (kind, norm_handle, display_name)
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id FROM resource_catalog WHERE kind=? AND handle=?",
                    (kind, norm_handle)
                ).fetchone()
        finally:
            conn.close()

        if existing and not overwrite:
            skipped += 1
            return

        add_resource(
            kind=kind,
            handle=norm_handle,
            display_name=display_name,
            purpose=purpose,
            tags=tags or [],
            linked_project_ids=linked_project_ids or [],
            linked_goal_ids=linked_goal_ids or [],
            linked_person_ids=linked_person_ids or [],
            notes=notes,
            proposed_by="tealc",
        )
        added += 1
        by_kind[kind] = by_kind.get(kind, 0) + 1

    # ------------------------------------------------------------------
    # a) research_projects
    # ------------------------------------------------------------------
    conn = _get_conn()
    try:
        projects = conn.execute(
            "SELECT id, name, status, linked_artifact_id, data_dir, output_dir, linked_goal_ids FROM research_projects"
        ).fetchall()
    finally:
        conn.close()

    for proj in projects:
        p = dict(proj)
        pid = p["id"]
        pname = p["name"]
        goal_ids_raw = p.get("linked_goal_ids") or ""
        goal_ids = [g.strip() for g in goal_ids_raw.split(",") if g.strip()]

        artifact_id = (p.get("linked_artifact_id") or "").strip()
        if artifact_id:
            _try_add(
                kind="google_doc",
                handle=f"https://docs.google.com/document/d/{artifact_id}/edit",
                display_name=f"{pname} — project artifact",
                purpose=f"Primary Google Doc for research project {pname}",
                tags=["project-artifact"],
                linked_project_ids=[pid],
                linked_goal_ids=goal_ids,
            )

        data_dir = (p.get("data_dir") or "").strip()
        if data_dir:
            if _is_drive_id(data_dir):
                kind = "drive_folder"
                handle = f"https://drive.google.com/drive/folders/{data_dir}"
            else:
                kind = "local_dir"
                handle = data_dir
            _try_add(
                kind=kind,
                handle=handle,
                display_name=f"{pname} — data",
                purpose=f"Data directory for research project {pname}",
                tags=["project-data"],
                linked_project_ids=[pid],
                linked_goal_ids=goal_ids,
            )

        output_dir = (p.get("output_dir") or "").strip()
        if output_dir:
            if _is_drive_id(output_dir):
                kind = "drive_folder"
                handle = f"https://drive.google.com/drive/folders/{output_dir}"
            else:
                kind = "local_dir"
                handle = output_dir
            _try_add(
                kind=kind,
                handle=handle,
                display_name=f"{pname} — output",
                purpose=f"Output directory for research project {pname}",
                tags=["project-output"],
                linked_project_ids=[pid],
                linked_goal_ids=goal_ids,
            )

    # ------------------------------------------------------------------
    # b) Goals Sheet from config.json
    # ------------------------------------------------------------------
    config_path = os.path.normpath(os.path.join(os.path.dirname(DB_PATH), "config.json"))
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            gsid = cfg.get("goals_sheet_id", "").strip()
            if gsid and "PASTE" not in gsid and "PLACEHOLDER" not in gsid:
                _try_add(
                    kind="google_sheet",
                    handle=f"https://docs.google.com/spreadsheets/d/{gsid}/edit",
                    display_name="Tealc Goals Sheet",
                    purpose="Goals/milestones/decisions/projects mirror — used by export_state_to_sheet",
                    tags=["goals", "sheet", "mirror"],
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # c) Known karyotype databases
    # ------------------------------------------------------------------
    karyotype_dbs = [
        ("Coleoptera karyotype DB", "8000+ records"),
        ("Diptera karyotype DB", "lab karyotype database"),
        ("Amphibia karyotype DB", "lab karyotype database"),
        ("Mammalia karyotype DB", "lab karyotype database"),
        ("Polyneoptera karyotype DB", "lab karyotype database"),
        ("Drosophila karyotype DB", "lab karyotype database"),
        ("Tree of Sex", "15000+ records"),
        ("Epistasis DB", "1600+ records"),
    ]
    for db_name, description in karyotype_dbs:
        _try_add(
            kind="google_sheet",
            handle="<TO-BE-FILLED>",
            display_name=db_name,
            purpose=f"Lab karyotype database ({description})",
            tags=["database", "karyotype"],
        )

    # ------------------------------------------------------------------
    # d) Active grants from deadlines.json
    # ------------------------------------------------------------------
    deadlines_path = os.path.normpath(os.path.join(os.path.dirname(DB_PATH), "deadlines.json"))
    if os.path.exists(deadlines_path):
        try:
            with open(deadlines_path) as f:
                dl_data = json.load(f)
            for dl in dl_data.get("deadlines", []):
                if dl.get("kind") == "grant":
                    _try_add(
                        kind="grant",
                        handle="<TO-BE-FILLED>",
                        display_name=dl["name"],
                        purpose=f"Active grant application — due {dl.get('due_iso', 'TBD')}",
                        tags=["grant", "active"],
                    )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # e) Active students from students table
    # ------------------------------------------------------------------
    conn = _get_conn()
    try:
        students = conn.execute(
            "SELECT id, full_name, role, email, primary_project FROM students WHERE status='active'"
        ).fetchall()
    finally:
        conn.close()

    for s in students:
        sd = dict(s)
        email = (sd.get("email") or "").strip()
        handle = email if email else "<TO-BE-FILLED>"
        role = sd.get("role") or "Lab member"
        primary = sd.get("primary_project") or ""
        purpose = f"{role} — {primary}" if primary else role
        _try_add(
            kind="email_contact",
            handle=handle,
            display_name=sd["full_name"],
            purpose=purpose,
            tags=["lab-member", role.lower()],
            linked_person_ids=[str(sd["id"])],
        )

    # ------------------------------------------------------------------
    # f) Alumni — lab_people.json index > 25
    # ------------------------------------------------------------------
    lab_people_path = os.path.normpath(os.path.join(os.path.dirname(DB_PATH), "lab_people.json"))
    if os.path.exists(lab_people_path):
        try:
            with open(lab_people_path) as f:
                lp = json.load(f)
            names = lp.get("names", [])
            # Index > 25 means alumni per current seed (0-based: index >= 26)
            # The first 26 entries cover active lab + first alumni, indices 26+ are alumni
            alumni_names = names[26:]
            for name in alumni_names:
                _try_add(
                    kind="email_contact",
                    handle="<TO-BE-FILLED>",
                    display_name=name,
                    purpose="Lab alumnus — now PI / postdoc; potential collaborator",
                    tags=["alumni", "collaborator"],
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # g) Standing resources
    # ------------------------------------------------------------------
    _researcher_github = os.environ.get("RESEARCHER_GITHUB", "https://github.com/your-org")
    _researcher_site = os.environ.get("RESEARCHER_PUBLIC_SITE", "https://your-org.github.io")
    standing = [
        ("github_repo", _researcher_github, "Researcher GitHub",
         "Main GitHub profile", ["github", "profile"]),
        ("github_repo", os.environ.get("RESEARCHER_GITHUB", "https://github.com/your-org/your-tool"), "TraitTrawler",
         "Multi-agent literature mining system — predecessor of Tealc", ["github", "ai-project"]),
        ("external_url", _researcher_site, "Lab website",
         "Public lab website", ["lab", "website"]),
        ("external_url", "https://orcid.org/0000-0002-5433-4036", "ORCID profile",
         "Researcher ORCID", ["profile", "orcid"]),
        ("external_url", _researcher_site + "/tealc.html", "Tealc public aquarium",
         "Public activity feed", ["tealc", "public"]),
    ]
    for kind, handle, display_name, purpose, tags in standing:
        _try_add(kind=kind, handle=handle, display_name=display_name, purpose=purpose, tags=tags)

    # ------------------------------------------------------------------
    # h) PhD/Postdoc mentors
    # ------------------------------------------------------------------
    mentors = [
        ("Jeff Demuth", "Former PhD advisor at UT Arlington"),
        ("Emma Goldberg", "Former postdoc advisor at U Minnesota"),
        ("Yaniv Brandvain", "Former postdoc advisor at U Minnesota"),
    ]
    for name, desc in mentors:
        _try_add(
            kind="email_contact",
            handle="<TO-BE-FILLED>",
            display_name=name,
            purpose=f"Former advisor — likely NAS letter writer / reference ({desc})",
            tags=["mentor", "vip", "letter-writer"],
        )

    # ------------------------------------------------------------------
    # i) VIP senders from vip_senders.json
    # ------------------------------------------------------------------
    vip_path = os.path.normpath(os.path.join(os.path.dirname(DB_PATH), "vip_senders.json"))
    if os.path.exists(vip_path):
        try:
            with open(vip_path) as f:
                vip_data = json.load(f)
            for entry in vip_data.get("vip_senders", []):
                email = (entry.get("email") or "").strip()
                label = (entry.get("label") or "").strip()
                note = (entry.get("note") or "").strip()
                if not email or "PLACEHOLDER" in email:
                    continue
                _try_add(
                    kind="email_contact",
                    handle=email,
                    display_name=label,
                    purpose=note,
                    tags=["vip"],
                )
        except Exception:
            pass

    return {"added": added, "skipped_existing": skipped, "by_kind": by_kind}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = seed_from_existing_state()
    print(f"Seeded: {result}")
