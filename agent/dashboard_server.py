"""Tealc HQ — private local dashboard server.

Serves on 127.0.0.1:8001 (localhost ONLY — never bind 0.0.0.0).
Three-tab dashboard: tasks, activity, abilities.

Start:   bash scripts/start_dashboard.sh
Stop:    bash scripts/stop_dashboard.sh
Direct:  python -m agent.dashboard_server
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from typing import Optional  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(_HERE, "..", "data"))
PUBLIC_DIR = os.path.normpath(os.path.join(_HERE, "..", "public"))
# Operational DB is owned by the scheduler. Use its resolution (TEALC_DB_PATH
# env var → default to ~/Library/Application Support/tealc/agent.db) so the
# dashboard reads/writes the same DB the scheduled jobs do. JSON state files
# (dashboard_state.json, abilities.json) still live alongside this script.
from agent.scheduler import DB_PATH  # noqa: E402

app = FastAPI(title="Tealc HQ", docs_url=None, redoc_url=None)

# Mount public static files if the directory exists
if os.path.isdir(PUBLIC_DIR):
    app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Static / JSON file endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    html = os.path.join(PUBLIC_DIR, "dashboard.html")
    if os.path.isfile(html):
        return FileResponse(html)
    return JSONResponse({"message": "Tealc HQ — dashboard.html not yet built"})


@app.get("/api/state")
async def state():
    p = os.path.join(DATA_DIR, "dashboard_state.json")
    try:
        with open(p) as f:
            return JSONResponse(json.load(f))
    except FileNotFoundError:
        return JSONResponse({"error": "state not yet generated — run publish_dashboard job"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/abilities")
async def abilities():
    p = os.path.join(DATA_DIR, "abilities.json")
    try:
        with open(p) as f:
            return JSONResponse(json.load(f))
    except FileNotFoundError:
        return JSONResponse({"error": "abilities not yet generated — run publish_abilities job"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/prereg_ledger")
async def api_prereg_ledger():
    try:
        with open(os.path.join(DATA_DIR, "dashboard_state.json")) as f:
            state = json.load(f)
        return JSONResponse(state.get("prereg_ledger", []))
    except FileNotFoundError:
        return JSONResponse({"error": "state not yet generated — run publish_dashboard job"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# On-demand job execution — force-runs a scheduled job now, bypassing its
# working-hours guard via FORCE_RUN=1.  Localhost only (the server binds
# 127.0.0.1) so no auth is needed beyond that.
# ---------------------------------------------------------------------------

class RunJobRequest(BaseModel):
    job_name: str
    verbose: bool = False
    dry_run: Optional[bool] = None
    target: Optional[str] = None


@app.get("/api/jobs")
async def list_jobs():
    """Return the list of job names available for /api/run_job."""
    try:
        from agent.jobs import list_available_jobs  # noqa: PLC0415
        return JSONResponse({"jobs": list_available_jobs()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/inbox")
async def inbox():
    """Unified inbox: all items awaiting Heath's review, sorted by urgency.

    Returns the pre-computed inbox from dashboard_state.json (written every
    minute by publish_dashboard).  Falls back to live computation if the
    state file is missing.

    POST-CACHE FILTER: applies a fresh `inbox_dismissals` filter on top of
    the cached items so user actions (rate_inbox_item, dismiss_inbox_item)
    take effect on the very next request, not after the next 1-minute
    publish_dashboard tick. Without this, the user clicks Good/OK/Bad,
    the card disappears optimistically via the frontend, then reappears
    on the next 30-second dashboard auto-refresh — bad UX.
    """
    inbox_data = None
    try:
        with open(os.path.join(DATA_DIR, "dashboard_state.json")) as f:
            state = json.load(f)
        inbox_data = state.get("inbox")
        if inbox_data is not None:
            inbox_data["generated_at"] = state.get("generated_at", _now_iso())
    except FileNotFoundError:
        pass
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Fallback: compute live
    if inbox_data is None:
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = _sqlite3.Row
            from agent.jobs.publish_dashboard import _inbox as _compute_inbox
            inbox_data = _compute_inbox(conn)
            conn.close()
            inbox_data["generated_at"] = _now_iso()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # Apply fresh inbox_dismissals filter (cheap — single indexed SELECT).
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        dismissed_keys = {
            (row[0], row[1])
            for row in conn.execute("SELECT kind, target_id FROM inbox_dismissals")
        }
        conn.close()
        if dismissed_keys and inbox_data.get("items"):
            before = len(inbox_data["items"])
            inbox_data["items"] = [
                i for i in inbox_data["items"]
                if (i.get("kind"), str(i.get("id"))) not in dismissed_keys
            ]
            after = len(inbox_data["items"])
            if before != after:
                # Recompute the inbox_summary counts so the badge stays consistent.
                inbox_data.setdefault("inbox_summary", {})
                inbox_data["inbox_summary"]["total_pending"] = after
                # Recount by_kind too
                by_kind: dict = {}
                for it in inbox_data["items"]:
                    k = it.get("kind", "?")
                    by_kind[k] = by_kind.get(k, 0) + 1
                inbox_data["inbox_summary"]["by_kind"] = by_kind
    except Exception as exc:
        # Filter is best-effort — never fail the request because of it.
        print(f"[/api/inbox] post-cache dismissal filter failed: {exc}")

    return JSONResponse(inbox_data)


@app.get("/api/reviewer_circle")
async def reviewer_circle():
    """Reviewer circle state: invitations, correlations, config status."""
    try:
        with open(os.path.join(DATA_DIR, "dashboard_state.json")) as f:
            state = json.load(f)
        rc_data = state.get("reviewer_circle")
        if rc_data is not None:
            rc_data["generated_at"] = state.get("generated_at", _now_iso())
            return JSONResponse(rc_data)
    except FileNotFoundError:
        pass
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Fallback: compute live
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = _sqlite3.Row
        from agent.jobs.publish_dashboard import _reviewer_circle as _compute_rc
        result = _compute_rc(conn)
        conn.close()
        result["generated_at"] = _now_iso()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/run_job")
async def run_job(req: RunJobRequest):
    """Force-run a scheduled job now.  Job output is the job's own return
    string (usually a short summary).  Blocks until the job completes.
    """
    import asyncio  # noqa: PLC0415
    from agent.jobs import run_job_now, list_available_jobs  # noqa: PLC0415

    known = set(list_available_jobs())
    if req.job_name not in known:
        raise HTTPException(
            status_code=404,
            detail=f"unknown job {req.job_name!r}; see GET /api/jobs",
        )

    started = _now_iso()
    try:
        result = await asyncio.to_thread(
            run_job_now,
            req.job_name,
            verbose=req.verbose,
            dry_run=req.dry_run,
            target=req.target,
        )
    except Exception as e:
        return JSONResponse(
            {"ok": False, "job_name": req.job_name, "started_at": started,
             "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "job_name": req.job_name,
        "started_at": started,
        "finished_at": _now_iso(),
        "result": str(result)[:4000],
    })


# ---------------------------------------------------------------------------
# Action endpoint
# ---------------------------------------------------------------------------

class ActionRequest(BaseModel):
    action: str
    target_id: int | str | None = None  # string allowed for prefixed inbox IDs (e.g. "grant_27")
    reason: str | None = None
    outcome: str | None = None
    kind: str | None = None  # used by dismiss_inbox_item to scope the dismissal
    rating: str | None = None  # used by rate_inbox_item: "good" | "ok" | "bad"


_VALID_ACTIONS = {
    "complete_briefing",
    "defer_briefing",
    "review_draft",
    "adopt_hypothesis",
    "reject_hypothesis",
    "investigate_hypothesis",
    "create_project_folder",
    "dismiss_grant_opportunity",
    "dismiss_inbox_item",
    "rate_inbox_item",
    "defer_stalled_goal",
}

_VALID_OUTCOMES = {"accepted", "edited", "rejected"}
_VALID_REJECT_REASONS = {"out of lab scope", "flawed logic", "not novel"}

# Lab Drive "Projects" root — canonical location for student-led paper
# projects.  Used by create_project_folder to materialize a new project
# subtree from a hypothesis proposal.
_LAB_PROJECTS_ROOT = os.environ.get(
    "LAB_PROJECTS_ROOT",
    os.path.expanduser("~/Library/CloudStorage/GoogleDrive/Shared drives/Lab/Projects")
)
_PROJECT_SUBDIRS = ("analysis", "data", "figures", "manuscript")


@app.post("/api/action")
async def action(req: ActionRequest):
    if req.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action!r}. Valid: {sorted(_VALID_ACTIONS)}")

    try:
        conn = _db()
        now = _now_iso()

        if req.action == "complete_briefing":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for complete_briefing")
            conn.execute(
                "UPDATE briefings SET acknowledged_at=? WHERE id=?",
                (now, req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "adopt", "briefing", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "complete_briefing", "id": req.target_id})

        elif req.action == "defer_briefing":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for defer_briefing")
            # Write a decisions_log row then acknowledge the briefing
            try:
                conn.execute(
                    "INSERT INTO decisions_log(kind, ref_id, reason, decided_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("defer_briefing", req.target_id, req.reason or "", now),
                )
            except Exception:
                pass  # decisions_log may not exist; still acknowledge
            conn.execute(
                "UPDATE briefings SET acknowledged_at=? WHERE id=?",
                (now, req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "defer", "briefing", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "defer_briefing", "id": req.target_id})

        elif req.action == "review_draft":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for review_draft")
            if req.outcome not in _VALID_OUTCOMES:
                raise HTTPException(
                    status_code=400,
                    detail=f"outcome must be one of {sorted(_VALID_OUTCOMES)}, got {req.outcome!r}",
                )
            # overnight_drafts has no `notes` column; reason is captured in
            # preference_signals.user_reason + output_ledger.user_reason below.
            conn.execute(
                "UPDATE overnight_drafts SET reviewed_at=?, outcome=? WHERE id=?",
                (now, req.outcome, req.target_id),
            )
            conn.commit()
            # Map outcome -> signal_type
            _outcome_to_signal = {"accepted": "adopt", "edited": "revise", "rejected": "reject"}
            _signal_type = _outcome_to_signal.get(req.outcome, req.outcome)
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, _signal_type, "overnight_draft", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            # Propagate the outcome to output_ledger.user_action via the
            # overnight_draft_id back-link that nightly_grant_drafter writes
            # into provenance_json.
            try:
                conn.execute(
                    "UPDATE output_ledger SET user_action=?, user_reason=?, user_action_at=? "
                    "WHERE json_extract(provenance_json, '$.overnight_draft_id') = ?",
                    (req.outcome, req.reason, now, req.target_id),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] ledger user_action update failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "review_draft", "id": req.target_id, "outcome": req.outcome})

        elif req.action == "adopt_hypothesis":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for adopt_hypothesis")
            conn.execute(
                "UPDATE hypothesis_proposals SET status='adopted', reviewed_at=? WHERE id=?",
                (now, req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "adopt", "hypothesis", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            try:
                conn.execute(
                    "UPDATE output_ledger SET user_action='adopted', user_reason=?, user_action_at=? "
                    "WHERE kind='hypothesis' AND json_extract(provenance_json, '$.hypothesis_id') = ?",
                    (req.reason, now, req.target_id),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] ledger user_action update failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "adopt_hypothesis", "id": req.target_id})

        elif req.action == "investigate_hypothesis":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for investigate_hypothesis")
            conn.execute(
                "UPDATE hypothesis_proposals SET status='investigating', reviewed_at=? WHERE id=?",
                (now, req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "investigate", "hypothesis", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "investigate_hypothesis", "id": req.target_id})

        elif req.action == "create_project_folder":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for create_project_folder")
            folder_name = (req.reason or "").strip()
            if not folder_name:
                raise HTTPException(status_code=400, detail="reason=<folder_name> required for create_project_folder")
            # Sanitize: no path traversal, no slashes
            if "/" in folder_name or ".." in folder_name or folder_name.startswith("."):
                raise HTTPException(status_code=400, detail="invalid folder name")
            target_path = os.path.join(_LAB_PROJECTS_ROOT, folder_name)
            if os.path.exists(target_path):
                raise HTTPException(status_code=409, detail=f"folder already exists: {folder_name}")
            if not os.path.isdir(_LAB_PROJECTS_ROOT):
                raise HTTPException(status_code=500, detail=f"Projects root not found: {_LAB_PROJECTS_ROOT}")
            try:
                os.makedirs(target_path, exist_ok=False)
                for sub in _PROJECT_SUBDIRS:
                    os.makedirs(os.path.join(target_path, sub), exist_ok=True)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"mkdir failed: {exc}")
            conn.execute(
                "UPDATE hypothesis_proposals SET status='promoted_to_project', reviewed_at=?, human_review=? WHERE id=?",
                (now, f"Promoted to project folder: {folder_name}", req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "promote", "hypothesis", req.target_id, f"project_folder={folder_name}"),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            # Trigger a lab-projects sync soon so the new folder becomes a DB row.
            # Best-effort; the daily 3:30am sync will catch it anyway.
            try:
                import threading  # noqa: PLC0415
                from agent.jobs import run_job_now  # noqa: PLC0415
                threading.Thread(
                    target=lambda: run_job_now("sync_lab_projects"),
                    daemon=True,
                ).start()
            except Exception as exc:
                print(f"[dashboard] sync_lab_projects kick failed: {exc}")
            conn.close()
            return JSONResponse({
                "ok": True,
                "action": "create_project_folder",
                "id": req.target_id,
                "folder": folder_name,
                "path": target_path,
            })

        elif req.action == "reject_hypothesis":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for reject_hypothesis")
            conn.execute(
                "UPDATE hypothesis_proposals SET status='rejected', reviewed_at=? WHERE id=?",
                (now, req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "reject", "hypothesis", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            try:
                conn.execute(
                    "UPDATE output_ledger SET user_action='rejected', user_reason=?, user_action_at=? "
                    "WHERE kind='hypothesis' AND json_extract(provenance_json, '$.hypothesis_id') = ?",
                    (req.reason, now, req.target_id),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] ledger user_action update failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "reject_hypothesis", "id": req.target_id})

        elif req.action == "dismiss_grant_opportunity":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for dismiss_grant_opportunity")
            # accept either a bare int or a prefixed string like "grant_27"
            raw_id = str(req.target_id)
            grant_pk = raw_id.replace("grant_", "") if raw_id.startswith("grant_") else raw_id
            try:
                grant_pk_int = int(grant_pk)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"unparseable target_id: {req.target_id!r}")
            # Update BOTH dismissed (boolean) and dismissed_at (timestamp) so
            # the legacy and new inbox queries both honor the dismissal.
            conn.execute(
                "UPDATE grant_opportunities SET dismissed=1, dismissed_at=?, dismiss_reason=? WHERE id=?",
                (now, req.reason or "", grant_pk_int),
            )
            conn.commit()
            # Also record in the generic inbox_dismissals table so the unified
            # inbox honors this regardless of source-table column drift.
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO inbox_dismissals (kind, target_id, dismissed_at, reason) VALUES (?, ?, ?, ?)",
                    ("grant", f"grant_{grant_pk_int}", now, req.reason or ""),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] inbox_dismissals write failed: {exc}")
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "dismiss", "grant_opportunity", grant_pk_int, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "dismiss_grant_opportunity", "id": grant_pk_int})

        elif req.action == "rate_inbox_item":
            # Quality rating: good/ok/bad. The actual feedback channel that
            # teaches Tealc what the user actually values. Three writes:
            #   1. preference_signals — consumed by reranker Haiku prompts
            #      (paper_radar, grant_radar, weekly_hypothesis_generator)
            #   2. output_ledger.user_action — for kind="ledger" only;
            #      maps good→adopted, ok→ignored, bad→rejected so retrieval-
            #      quality jobs can correlate critic scores with user judgment
            #   3. inbox_dismissals — removes the card from the unified inbox
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for rate_inbox_item")
            kind = (req.kind or "").strip()
            if not kind:
                raise HTTPException(status_code=400, detail="kind required for rate_inbox_item")
            rating = (req.rating or "").strip().lower()
            if rating not in ("good", "ok", "bad"):
                raise HTTPException(status_code=400, detail="rating must be 'good', 'ok', or 'bad'")
            tid = str(req.target_id)
            signal_type = f"rate_{rating}"

            # 1. Preference signal
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, signal_type, kind, tid, req.reason),
                )
            except Exception as exc:
                print(f"[dashboard] preference signal write failed: {exc}")

            # 2. Ledger user_action update — only for kind=ledger
            if kind == "ledger" and tid.startswith("ledger_"):
                user_action_map = {"good": "adopted", "ok": "ignored", "bad": "rejected"}
                try:
                    from agent.ledger import update_user_action  # noqa: PLC0415
                    ledger_row_id = int(tid.replace("ledger_", ""))
                    update_user_action(ledger_row_id, user_action_map[rating], req.reason or rating)
                except Exception as exc:
                    print(f"[dashboard] update_user_action failed for {tid}: {exc}")

            # 3. Dismiss from inbox so the card disappears
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO inbox_dismissals (kind, target_id, dismissed_at, reason) VALUES (?, ?, ?, ?)",
                    (kind, tid, now, f"rated_{rating}"),
                )
                conn.commit()
            except Exception as exc:
                conn.close()
                raise HTTPException(status_code=500, detail=f"inbox_dismissals write failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "rate_inbox_item", "kind": kind, "id": tid, "rating": rating})

        elif req.action == "dismiss_inbox_item":
            # Generic soft-dismiss for any inbox item. Stored in a separate
            # inbox_dismissals table so the unified inbox query LEFT JOINs
            # against it — never touches source rows.
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for dismiss_inbox_item")
            kind = (req.kind or "").strip()
            if not kind:
                raise HTTPException(status_code=400, detail="kind required for dismiss_inbox_item")
            tid = str(req.target_id)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO inbox_dismissals (kind, target_id, dismissed_at, reason) VALUES (?, ?, ?, ?)",
                    (kind, tid, now, req.reason or ""),
                )
                conn.commit()
            except Exception as exc:
                conn.close()
                raise HTTPException(status_code=500, detail=f"inbox_dismissals write failed: {exc}")
            # Best-effort preference signal
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "dismiss", kind, tid, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] preference signal write failed: {exc}")
            # Bonus: if it was a grant, mirror the column update so the legacy
            # query honors it too.
            if kind == "grant" and tid.startswith("grant_"):
                try:
                    grant_pk_int = int(tid.replace("grant_", ""))
                    conn.execute(
                        "UPDATE grant_opportunities SET dismissed=1, dismissed_at=?, dismiss_reason=? WHERE id=?",
                        (now, req.reason or "", grant_pk_int),
                    )
                    conn.commit()
                except Exception:
                    pass
            conn.close()
            return JSONResponse({"ok": True, "action": "dismiss_inbox_item", "kind": kind, "id": tid})

        elif req.action == "defer_stalled_goal":
            if req.target_id is None:
                raise HTTPException(status_code=400, detail="target_id required for defer_stalled_goal")
            # Write a decisions_log row and snooze last_touched_iso to now
            try:
                conn.execute(
                    "INSERT INTO decisions_log(kind, ref_id, reason, decided_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("defer_stalled_goal", req.target_id, req.reason or "", now),
                )
            except Exception:
                pass
            conn.execute(
                "UPDATE goals SET last_touched_iso=? WHERE id=?",
                (now, req.target_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO preference_signals(captured_at, signal_type, target_kind, target_id, user_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "defer", "goal", req.target_id, req.reason),
                )
                conn.commit()
            except Exception as exc:
                print(f"[dashboard] signal-write failed: {exc}")
            conn.close()
            return JSONResponse({"ok": True, "action": "defer_stalled_goal", "id": req.target_id})

        else:
            # Unreachable given the guard above, but be safe
            raise HTTPException(status_code=400, detail=f"Unhandled action: {req.action}")

    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Documents index endpoint
# ---------------------------------------------------------------------------

@app.get("/api/documents")
async def documents():
    """Returns the documents index."""
    from agent.documents_index import build_documents_index  # noqa: PLC0415
    try:
        return JSONResponse(build_documents_index())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


class RevealRequest(BaseModel):
    path: str


@app.post("/api/reveal")
async def reveal(req: RevealRequest):
    """Open a local file's enclosing folder in macOS Finder with the file selected.

    Safety: path must be under the project root.
    Returns {ok: True} or an error.
    """
    import subprocess as _sub  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    project_root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), ".."))
    abs_path = _os.path.abspath(req.path)
    if not abs_path.startswith(project_root):
        return JSONResponse({"error": "path not under project root"}, status_code=400)
    if not _os.path.exists(abs_path):
        return JSONResponse({"error": "file not found"}, status_code=404)
    try:
        _sub.run(["open", "-R", abs_path], check=False, timeout=3)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Goals + Milestones endpoints
# ---------------------------------------------------------------------------

_NAS_RELEVANCE_ORDER = {"high": 2, "med": 1, "low": 0}
_VALID_NAS_RELEVANCE = {"high", "med", "low"}
_VALID_STATUSES = {"proposed", "active", "paused", "done", "retired"}


def _new_goal_id():
    import uuid
    return f"g_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


def _new_milestone_id():
    import uuid
    return f"m_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


def _goal_row_to_dict(row, columns) -> dict:
    return dict(zip(columns, row))


def _compute_days_since(iso_str: Optional[str]) -> Optional[int]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return int(delta.total_seconds() // 86400)
    except Exception:
        return None


class GoalCreate(BaseModel):
    name: str
    time_horizon: Optional[str] = None
    importance: Optional[int] = 3
    nas_relevance: Optional[str] = "med"
    status: Optional[str] = "active"
    success_metric: Optional[str] = None
    why: Optional[str] = None
    notes: Optional[str] = None
    owner: Optional[str] = "Heath"


class GoalUpdate(BaseModel):
    name: Optional[str] = None
    time_horizon: Optional[str] = None
    importance: Optional[int] = None
    nas_relevance: Optional[str] = None
    status: Optional[str] = None
    success_metric: Optional[str] = None
    why: Optional[str] = None
    notes: Optional[str] = None
    owner: Optional[str] = None


class MilestoneCreate(BaseModel):
    goal_id: str
    milestone: str
    target_iso: Optional[str] = None
    notes: Optional[str] = None


class MilestoneUpdate(BaseModel):
    milestone: Optional[str] = None
    target_iso: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/goals")
async def get_goals():
    """Return all goals sorted by importance DESC, nas_relevance DESC, last_touched_iso DESC.

    Each goal includes its milestones (sorted by target_iso ASC NULLS LAST).
    days_since_touch is integer days since last_touched_iso, or null if never touched.
    All statuses are included (active, proposed, paused, retired, done); frontend filters.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        goal_rows = conn.execute(
            "SELECT id, name, time_horizon, importance, nas_relevance, status, "
            "success_metric, why, owner, last_touched_by, last_touched_iso, notes "
            "FROM goals"
        ).fetchall()

        milestone_rows = conn.execute(
            "SELECT id, goal_id, milestone, target_iso, status, notes, last_touched_iso "
            "FROM milestones_v2 "
            "ORDER BY CASE WHEN target_iso IS NULL THEN 1 ELSE 0 END ASC, target_iso ASC"
        ).fetchall()
        conn.close()

        # Build milestone lookup by goal_id
        milestones_by_goal: dict = {}
        for m in milestone_rows:
            gid = m["goal_id"]
            milestones_by_goal.setdefault(gid, []).append({
                "id": m["id"],
                "goal_id": m["goal_id"],
                "milestone": m["milestone"],
                "target_iso": m["target_iso"],
                "status": m["status"],
                "notes": m["notes"],
                "last_touched_iso": m["last_touched_iso"],
            })

        goals = []
        for g in goal_rows:
            gid = g["id"]
            goals.append({
                "id": gid,
                "name": g["name"],
                "time_horizon": g["time_horizon"],
                "importance": g["importance"],
                "nas_relevance": g["nas_relevance"],
                "status": g["status"],
                "success_metric": g["success_metric"],
                "why": g["why"],
                "owner": g["owner"],
                "last_touched_by": g["last_touched_by"],
                "last_touched_iso": g["last_touched_iso"],
                "notes": g["notes"],
                "days_since_touch": _compute_days_since(g["last_touched_iso"]),
                "milestones": milestones_by_goal.get(gid, []),
            })

        def _ts(iso: Optional[str]) -> int:
            """Convert ISO string to epoch int for sorting (0 if missing, sorts last)."""
            if not iso:
                return 0
            try:
                return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
            except Exception:
                return 0

        # Sort: importance DESC, nas_relevance DESC (high>med>low), last_touched_iso DESC
        goals.sort(key=lambda g: (
            -(g["importance"] or 0),
            -_NAS_RELEVANCE_ORDER.get(g["nas_relevance"] or "", 0),
            -_ts(g["last_touched_iso"]),
        ))

        return JSONResponse({
            "generated_at": _now_iso(),
            "goals": goals,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/goals")
async def create_goal(req: GoalCreate):
    """Create a new goal. Only 'name' is required.

    id auto-generated as g_<YYYYMMDD>_<rand6>.
    status defaults to 'active'. owner defaults to 'Heath'.
    time_horizon suggestions: week / month / quarter / year / career.
    """
    try:
        if req.importance is not None and not (1 <= req.importance <= 5):
            return JSONResponse({"error": "importance must be 1–5"}, status_code=400)
        if req.nas_relevance is not None and req.nas_relevance not in _VALID_NAS_RELEVANCE:
            return JSONResponse({"error": "nas_relevance must be 'high', 'med', or 'low'"}, status_code=400)
        if req.status is not None and req.status not in _VALID_STATUSES:
            return JSONResponse({"error": f"status must be one of {sorted(_VALID_STATUSES)}"}, status_code=400)

        gid = _new_goal_id()
        now = _now_iso()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO goals(id, name, time_horizon, importance, nas_relevance, status, "
            "success_metric, why, owner, last_touched_by, last_touched_iso, notes, "
            "synced_at, tealc_dirty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                gid,
                req.name,
                req.time_horizon,
                req.importance if req.importance is not None else 3,
                req.nas_relevance if req.nas_relevance is not None else "med",
                req.status if req.status is not None else "active",
                req.success_metric,
                req.why,
                req.owner if req.owner is not None else "Heath",
                "Heath",
                now,
                req.notes,
                now,
                0,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, time_horizon, importance, nas_relevance, status, "
            "success_metric, why, owner, last_touched_by, last_touched_iso, notes "
            "FROM goals WHERE id=?",
            (gid,),
        ).fetchone()
        conn.close()

        return JSONResponse({
            "id": row[0],
            "name": row[1],
            "time_horizon": row[2],
            "importance": row[3],
            "nas_relevance": row[4],
            "status": row[5],
            "success_metric": row[6],
            "why": row[7],
            "owner": row[8],
            "last_touched_by": row[9],
            "last_touched_iso": row[10],
            "notes": row[11],
            "days_since_touch": _compute_days_since(row[10]),
            "milestones": [],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.patch("/api/goals/{goal_id}")
async def update_goal(goal_id: str, req: GoalUpdate):
    """Update mutable fields of an existing goal. Body is a partial dict.

    Always refreshes last_touched_by='Heath', last_touched_iso=now(), tealc_dirty=0.
    Returns the updated goal dict (same shape as GET /api/goals items).
    """
    try:
        if req.importance is not None and not (1 <= req.importance <= 5):
            return JSONResponse({"error": "importance must be 1–5"}, status_code=400)
        if req.nas_relevance is not None and req.nas_relevance not in _VALID_NAS_RELEVANCE:
            return JSONResponse({"error": "nas_relevance must be 'high', 'med', or 'low'"}, status_code=400)
        if req.status is not None and req.status not in _VALID_STATUSES:
            return JSONResponse({"error": f"status must be one of {sorted(_VALID_STATUSES)}"}, status_code=400)

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        existing = conn.execute("SELECT id FROM goals WHERE id=?", (goal_id,)).fetchone()
        if existing is None:
            conn.close()
            return JSONResponse({"error": f"goal {goal_id!r} not found"}, status_code=404)

        now = _now_iso()
        fields = req.model_dump(exclude_none=True)
        set_clauses = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())

        # Always refresh touch metadata
        if set_clauses:
            set_clauses += ", last_touched_by=?, last_touched_iso=?, tealc_dirty=?"
        else:
            set_clauses = "last_touched_by=?, last_touched_iso=?, tealc_dirty=?"
        values += ["Heath", now, 0, goal_id]

        conn.execute(f"UPDATE goals SET {set_clauses} WHERE id=?", values)
        conn.commit()

        row = conn.execute(
            "SELECT id, name, time_horizon, importance, nas_relevance, status, "
            "success_metric, why, owner, last_touched_by, last_touched_iso, notes "
            "FROM goals WHERE id=?",
            (goal_id,),
        ).fetchone()
        milestone_rows = conn.execute(
            "SELECT id, goal_id, milestone, target_iso, status, notes, last_touched_iso "
            "FROM milestones_v2 WHERE goal_id=? "
            "ORDER BY CASE WHEN target_iso IS NULL THEN 1 ELSE 0 END ASC, target_iso ASC",
            (goal_id,),
        ).fetchall()
        conn.close()

        milestones = [
            {
                "id": m[0], "goal_id": m[1], "milestone": m[2],
                "target_iso": m[3], "status": m[4], "notes": m[5],
                "last_touched_iso": m[6],
            }
            for m in milestone_rows
        ]

        return JSONResponse({
            "id": row[0],
            "name": row[1],
            "time_horizon": row[2],
            "importance": row[3],
            "nas_relevance": row[4],
            "status": row[5],
            "success_metric": row[6],
            "why": row[7],
            "owner": row[8],
            "last_touched_by": row[9],
            "last_touched_iso": row[10],
            "notes": row[11],
            "days_since_touch": _compute_days_since(row[10]),
            "milestones": milestones,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/goals/{goal_id}")
async def archive_goal(goal_id: str):
    """Archive a goal by setting status='retired' (no actual row deletion).

    Also writes a decisions_log row recording the retirement.
    Returns {"ok": true, "archived_id": goal_id}.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        existing = conn.execute("SELECT id FROM goals WHERE id=?", (goal_id,)).fetchone()
        if existing is None:
            conn.close()
            return JSONResponse({"error": f"goal {goal_id!r} not found"}, status_code=404)

        now = _now_iso()
        conn.execute(
            "UPDATE goals SET status='retired', last_touched_by='Heath', "
            "last_touched_iso=?, tealc_dirty=0 WHERE id=?",
            (now, goal_id),
        )
        try:
            conn.execute(
                "INSERT INTO decisions_log(decided_iso, decision, decided_by, synced_at, tealc_dirty) "
                "VALUES (?,?,?,?,?)",
                (now, f"retired goal {goal_id}", "Heath", now, 0),
            )
        except Exception:
            pass  # decisions_log schema mismatch — don't block the archive
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "archived_id": goal_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/milestones")
async def create_milestone(req: MilestoneCreate):
    """Add a milestone to an existing goal.

    goal_id and milestone text are required.
    id auto-generated as m_<YYYYMMDD>_<rand6>. status defaults to 'open'.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        goal_exists = conn.execute("SELECT id FROM goals WHERE id=?", (req.goal_id,)).fetchone()
        if goal_exists is None:
            conn.close()
            return JSONResponse({"error": f"goal {req.goal_id!r} not found"}, status_code=404)

        mid = _new_milestone_id()
        now = _now_iso()
        conn.execute(
            "INSERT INTO milestones_v2(id, goal_id, milestone, target_iso, status, notes, "
            "last_touched_iso, synced_at, tealc_dirty) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, req.goal_id, req.milestone, req.target_iso, "open", req.notes, now, now, 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, goal_id, milestone, target_iso, status, notes, last_touched_iso "
            "FROM milestones_v2 WHERE id=?",
            (mid,),
        ).fetchone()
        conn.close()

        return JSONResponse({
            "id": row[0],
            "goal_id": row[1],
            "milestone": row[2],
            "target_iso": row[3],
            "status": row[4],
            "notes": row[5],
            "last_touched_iso": row[6],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.patch("/api/milestones/{milestone_id}")
async def update_milestone(milestone_id: str, req: MilestoneUpdate):
    """Update mutable fields of a milestone (milestone text, target_iso, status, notes).

    Always refreshes last_touched_iso=now().
    Returns the updated milestone dict.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        existing = conn.execute(
            "SELECT id FROM milestones_v2 WHERE id=?", (milestone_id,)
        ).fetchone()
        if existing is None:
            conn.close()
            return JSONResponse({"error": f"milestone {milestone_id!r} not found"}, status_code=404)

        now = _now_iso()
        fields = req.model_dump(exclude_none=True)
        set_clauses = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())

        if set_clauses:
            set_clauses += ", last_touched_iso=?"
        else:
            set_clauses = "last_touched_iso=?"
        values += [now, milestone_id]

        conn.execute(f"UPDATE milestones_v2 SET {set_clauses} WHERE id=?", values)
        conn.commit()

        row = conn.execute(
            "SELECT id, goal_id, milestone, target_iso, status, notes, last_touched_iso "
            "FROM milestones_v2 WHERE id=?",
            (milestone_id,),
        ).fetchone()
        conn.close()

        return JSONResponse({
            "id": row[0],
            "goal_id": row[1],
            "milestone": row[2],
            "target_iso": row[3],
            "status": row[4],
            "notes": row[5],
            "last_touched_iso": row[6],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/milestones/{milestone_id}")
async def delete_milestone(milestone_id: str):
    """Actually delete a milestone row (milestones are freer to discard than goals).

    Returns {"ok": true, "deleted_id": milestone_id}.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        existing = conn.execute(
            "SELECT id FROM milestones_v2 WHERE id=?", (milestone_id,)
        ).fetchone()
        if existing is None:
            conn.close()
            return JSONResponse({"error": f"milestone {milestone_id!r} not found"}, status_code=404)

        conn.execute("DELETE FROM milestones_v2 WHERE id=?", (milestone_id,))
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "deleted_id": milestone_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Knowledge Map (resource catalog) endpoints
# ---------------------------------------------------------------------------

CATEGORY_META = [
    ("research_project", "Research projects",
     "Every active project and the Doc / data folder that anchors it."),
    ("google_doc", "Project documents",
     "Google Docs you edit — manuscripts, grant sections, drafts."),
    ("google_sheet", "Databases & spreadsheets",
     "Karyotype DBs, Tree of Sex, Goals sheet, budgets."),
    ("drive_folder", "Drive folders", "Shared-drive destinations."),
    ("local_dir", "Local directories", "Paths on this machine — data, outputs, code."),
    ("grant", "Active grants",
     "Grants in flight — applications, renewals, preprint submissions."),
    ("github_repo", "GitHub repos", "Code repos."),
    ("email_contact", "People & VIPs",
     "Students, alumni, mentors, program officers, editors."),
    ("external_url", "Other URLs", "External sites and references."),
    ("other", "Miscellaneous", ""),
]

_CATEGORY_KEYS = [k for k, _, _ in CATEGORY_META]


class ResourceCreate(BaseModel):
    kind: str
    handle: str
    display_name: str
    purpose: Optional[str] = ""
    tags: Optional[list[str]] = []
    linked_project_ids: Optional[list[str]] = []
    linked_goal_ids: Optional[list[str]] = []
    linked_person_ids: Optional[list[str]] = []
    notes: Optional[str] = ""


class ResourceUpdate(BaseModel):
    kind: Optional[str] = None
    handle: Optional[str] = None
    display_name: Optional[str] = None
    purpose: Optional[str] = None
    tags: Optional[list[str]] = None
    linked_project_ids: Optional[list[str]] = None
    linked_goal_ids: Optional[list[str]] = None
    linked_person_ids: Optional[list[str]] = None
    notes: Optional[str] = None
    status: Optional[str] = None


@app.get("/api/catalog")
async def get_catalog(status: str = "confirmed", kind: Optional[str] = None):
    """status: 'all' | 'proposed' | 'confirmed' | 'dismissed'. Default 'confirmed'."""
    try:
        from agent.knowledge_map import load_catalog  # noqa: PLC0415
        items = load_catalog(status=status if status != "all" else None)
        if kind:
            items = [it for it in items if it.get("kind") == kind]

        # Count unconfirmed (proposed) and stale items
        all_items = load_catalog(status=None)
        unconfirmed_count = sum(1 for it in all_items if it.get("status") == "proposed")

        from datetime import timezone as _tz  # noqa: PLC0415
        now_utc = datetime.now(_tz.utc)
        stale_threshold = 90 * 86400  # 90 days in seconds
        stale_count = 0
        for it in all_items:
            lc = it.get("last_confirmed_iso")
            if lc:
                try:
                    dt = datetime.fromisoformat(lc)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    age = (now_utc - dt).total_seconds()
                    if age > stale_threshold:
                        stale_count += 1
                except Exception:
                    pass

        # Build category buckets
        by_kind: dict = {k: [] for k in _CATEGORY_KEYS}
        for it in items:
            k = it.get("kind", "other")
            bucket = k if k in by_kind else "other"
            by_kind[bucket].append(it)

        # Sort each bucket by display_name ASC
        for bucket in by_kind.values():
            bucket.sort(key=lambda x: (x.get("display_name") or "").lower())

        categories = []
        for key, title, description in CATEGORY_META:
            bucket_items = by_kind.get(key, [])
            if bucket_items or True:  # always include category (frontend handles empty)
                categories.append({
                    "key": key,
                    "title": title,
                    "description": description,
                    "items": bucket_items,
                })

        return JSONResponse({
            "generated_at": _now_iso(),
            "categories": categories,
            "unconfirmed_count": unconfirmed_count,
            "stale_count": stale_count,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/catalog")
async def create_catalog_resource(req: ResourceCreate):
    """Create a new resource (proposed_by='heath', confirmed immediately)."""
    try:
        from agent.knowledge_map import add_resource  # noqa: PLC0415
        result = add_resource(
            kind=req.kind,
            handle=req.handle,
            display_name=req.display_name,
            purpose=req.purpose or "",
            tags=req.tags or [],
            linked_project_ids=req.linked_project_ids or [],
            linked_goal_ids=req.linked_goal_ids or [],
            linked_person_ids=req.linked_person_ids or [],
            notes=req.notes or "",
            proposed_by="heath",
            status="confirmed",
        )
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.patch("/api/catalog/{res_id}")
async def update_catalog_resource(res_id: str, req: ResourceUpdate):
    """Partial update of a resource."""
    try:
        from agent.knowledge_map import update_resource  # noqa: PLC0415
        updates = req.model_dump(exclude_none=True)
        result = update_resource(res_id, **updates)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/catalog/{res_id}/confirm")
async def confirm_catalog_resource(res_id: str):
    """One-click confirm a proposed resource."""
    try:
        from agent.knowledge_map import confirm_resource  # noqa: PLC0415
        result = confirm_resource(res_id)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/catalog/{res_id}")
async def dismiss_catalog_resource(res_id: str):
    """Soft dismiss — sets status='dismissed'."""
    try:
        from agent.knowledge_map import dismiss_resource  # noqa: PLC0415
        result = dismiss_resource(res_id)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Projects endpoints
# ---------------------------------------------------------------------------

_VALID_PROJECT_STATUSES = {"active", "paused", "done", "archived"}


def _new_project_id() -> str:
    import uuid
    return f"p_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


def _make_artifact_url(artifact_id: Optional[str]) -> str:
    if not artifact_id:
        return ""
    if "/" in artifact_id:
        return ""
    return f"https://docs.google.com/document/d/{artifact_id}/edit"


def _make_data_dir_url(data_dir: Optional[str]) -> str:
    if not data_dir:
        return ""
    if "/" in data_dir or "\\" in data_dir:
        return ""
    import re
    # Drive folder IDs: alphanumeric + dashes/underscores, typically ~28+ chars
    if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", data_dir):
        return f"https://drive.google.com/drive/folders/{data_dir}"
    return ""


def _build_project_dict(
    row: sqlite3.Row,
    students_by_id: dict,
    conn: sqlite3.Connection,
) -> dict:
    """Build a full project dict conforming to the shape contract."""
    project_id = row["id"]

    # Lead resolution
    lead_student_id = row["lead_student_id"]
    lead_name_col = row["lead_name"] if "lead_name" in row.keys() else None
    lead_name = ""
    lead_role = ""
    if lead_student_id and lead_student_id in students_by_id:
        s = students_by_id[lead_student_id]
        lead_name = s["full_name"] or ""
        lead_role = s["role"] or ""
    elif lead_name_col:
        lead_name = lead_name_col

    # URL synthesis
    artifact_id = row["linked_artifact_id"] or ""
    data_dir = row["data_dir"] or ""
    artifact_url = _make_artifact_url(artifact_id)
    data_dir_url = _make_data_dir_url(data_dir)

    # days_since_touch
    days_since = _compute_days_since(row["last_touched_iso"])

    # recent_activity
    unreviewed_drafts = conn.execute(
        "SELECT count(*) FROM overnight_drafts WHERE project_id=? AND reviewed_at IS NULL",
        (project_id,),
    ).fetchone()[0]

    pending_hypotheses = conn.execute(
        "SELECT count(*) FROM hypothesis_proposals WHERE project_id=? AND status='proposed'",
        (project_id,),
    ).fetchone()[0]

    literature_notes_last_30d = conn.execute(
        "SELECT count(*) FROM literature_notes WHERE project_id=? AND created_at > datetime('now', '-30 days')",
        (project_id,),
    ).fetchone()[0]

    ledger_entries_last_30d = conn.execute(
        "SELECT count(*) FROM output_ledger WHERE project_id=? AND created_at > datetime('now', '-30 days')",
        (project_id,),
    ).fetchone()[0]

    last_draft_row = conn.execute(
        "SELECT created_at, draft_doc_url FROM overnight_drafts WHERE project_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    last_draft_at = last_draft_row[0] if last_draft_row else None
    last_draft_doc_url = last_draft_row[1] if last_draft_row else None

    # resource_ids: resources whose linked_project_ids contains this project_id
    resource_rows = conn.execute(
        "SELECT id FROM resource_catalog WHERE linked_project_ids LIKE ?",
        (f"%{project_id}%",),
    ).fetchall()
    resource_ids = [r[0] for r in resource_rows]

    # linked_goal_ids — stored as comma-separated string
    linked_goal_ids_raw = row["linked_goal_ids"] or ""
    linked_goal_ids = [g.strip() for g in linked_goal_ids_raw.split(",") if g.strip()]

    # Type-specific fields (echo from DB, default None)
    def _row_get(r, key):
        try:
            return r[key]
        except (IndexError, KeyError):
            return None

    return {
        "id": project_id,
        "name": row["name"],
        "description": row["description"] or "",
        "status": row["status"] or "active",
        "lead_student_id": lead_student_id,
        "lead_name": lead_name,
        "lead_role": lead_role,
        "current_hypothesis": row["current_hypothesis"] or "",
        "next_action": row["next_action"] or "",
        "keywords": row["keywords"] or "",
        "notes": row["notes"] or "",
        "data_dir": data_dir,
        "output_dir": row["output_dir"] or "",
        "linked_artifact_id": artifact_id,
        "linked_artifact_url": artifact_url,
        "data_dir_url": data_dir_url,
        "last_touched_iso": row["last_touched_iso"],
        "days_since_touch": days_since,
        "recent_activity": {
            "last_draft_at": last_draft_at,
            "last_draft_doc_url": last_draft_doc_url,
            "unreviewed_drafts": unreviewed_drafts,
            "pending_hypotheses": pending_hypotheses,
            "literature_notes_last_30d": literature_notes_last_30d,
            "ledger_entries_last_30d": ledger_entries_last_30d,
        },
        "resource_ids": resource_ids,
        "linked_goal_ids": linked_goal_ids,
        # Type classification fields
        "project_type": _row_get(row, "project_type"),
        "journal": _row_get(row, "journal"),
        "paper_status": _row_get(row, "paper_status"),
        "agency": _row_get(row, "agency"),
        "program": _row_get(row, "program"),
        "grant_status": _row_get(row, "grant_status"),
        # Lifecycle + wiki visibility
        "stage": _row_get(row, "stage"),
        "include_in_wiki": int(_row_get(row, "include_in_wiki") or 0),
    }


_VALID_PROJECT_TYPES = {"paper", "grant", "software", "database", "teaching", "general", "other"}
_VALID_PAPER_STATUSES = {
    "submitted", "under_review", "revision_resubmit", "revision_new_journal",
    "accepted", "in_press", "published",
}
_VALID_GRANT_STATUSES = {
    "in_prep", "submitted", "under_review", "awarded", "declined", "deferred",
}
_VALID_STAGES = {
    "in_development", "data_collection", "finalizing_manuscript", "under_review", "published",
}

# Main journals dropdown (frontend renders these + an "Other (write-in)" entry).
# Stored value is the journal name string; "Other" lets the user type free text.
MAIN_JOURNALS = [
    "Journal of Heredity",
    "Evolution",
    "Genetics",
    "Heredity",
    "G3: Genes, Genomes, Genetics",
    "PeerJ",
    "Genes",
    "Genome Biology and Evolution",
    "Molecular Biology and Evolution",
    "Systematic Biology",
    "Nature Communications",
    "PLOS Genetics",
    "Ecology and Evolution",
    "Chromosome Research",
    "bioRxiv",
]


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None        # 'active'|'paused'|'done'|'archived'
    lead_student_id: Optional[int] = None
    lead_name: Optional[str] = None
    current_hypothesis: Optional[str] = None
    next_action: Optional[str] = None
    keywords: Optional[str] = None
    notes: Optional[str] = None
    data_dir: Optional[str] = None
    output_dir: Optional[str] = None
    linked_artifact_id: Optional[str] = None
    linked_goal_ids: Optional[str] = None   # comma-separated
    # Type classification fields
    project_type: Optional[str] = None     # 'paper'|'grant'|'software'|'database'|'teaching'|'general'|'other'
    journal: Optional[str] = None
    paper_status: Optional[str] = None     # 'submitted'|'under_review'|'revision_resubmit'|'revision_new_journal'|'accepted'|'in_press'|'published'
    agency: Optional[str] = None           # free text (NIH, NSF, Google.org, etc.)
    program: Optional[str] = None          # free text
    grant_status: Optional[str] = None     # 'in_prep'|'submitted'|'under_review'|'awarded'|'declined'|'deferred'
    # Lifecycle stage (drives wiki visibility: published projects are filtered out)
    stage: Optional[str] = None            # 'in_development'|'data_collection'|'finalizing_manuscript'|'under_review'|'published'
    include_in_wiki: Optional[int] = None  # 1 or 0; manual override for the public projects page


_PROJECT_COLUMNS = (
    "id, name, description, status, linked_goal_ids, data_dir, output_dir, "
    "current_hypothesis, next_action, keywords, linked_artifact_id, "
    "last_touched_by, last_touched_iso, notes, "
    "lead_student_id, lead_name, "
    "project_type, journal, paper_status, agency, program, grant_status, "
    "stage, include_in_wiki"
)


@app.get("/api/projects")
async def get_projects(status: str = "active"):
    """Return projects filtered by status (all|active|paused|done), sorted by last_touched_iso DESC, name ASC.

    Also returns the full students list so the frontend can render the lead picker.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        valid_filter = {"all", "active", "paused", "done"}
        if status not in valid_filter:
            conn.close()
            return JSONResponse(
                {"error": f"status must be one of {sorted(valid_filter)}"}, status_code=400
            )

        if status == "all":
            project_rows = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM research_projects "
                "ORDER BY CASE WHEN last_touched_iso IS NULL THEN 1 ELSE 0 END ASC, "
                "last_touched_iso DESC, name ASC"
            ).fetchall()
        else:
            project_rows = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM research_projects WHERE status=? "
                "ORDER BY CASE WHEN last_touched_iso IS NULL THEN 1 ELSE 0 END ASC, "
                "last_touched_iso DESC, name ASC",
                (status,),
            ).fetchall()

        student_rows = conn.execute(
            "SELECT id, full_name, short_name, role FROM students ORDER BY full_name ASC"
        ).fetchall()

        students_by_id = {
            s["id"]: {"full_name": s["full_name"], "short_name": s["short_name"], "role": s["role"]}
            for s in student_rows
        }

        projects = [_build_project_dict(row, students_by_id, conn) for row in project_rows]

        students = [
            {
                "id": s["id"],
                "full_name": s["full_name"],
                "short_name": s["short_name"],
                "role": s["role"],
            }
            for s in student_rows
        ]

        conn.close()
        return JSONResponse({
            "generated_at": _now_iso(),
            "projects": projects,
            "students": students,
            "journals": MAIN_JOURNALS,
            "stages": sorted(_VALID_STAGES),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _validate_project_type_fields(req: "ProjectUpdate"):
    """Return an error string if any type-classification field is invalid, else None."""
    if req.project_type is not None and req.project_type not in _VALID_PROJECT_TYPES:
        return f"project_type must be one of {sorted(_VALID_PROJECT_TYPES)}"
    if req.paper_status is not None and req.paper_status not in _VALID_PAPER_STATUSES:
        return f"paper_status must be one of {sorted(_VALID_PAPER_STATUSES)}"
    if req.grant_status is not None and req.grant_status not in _VALID_GRANT_STATUSES:
        return f"grant_status must be one of {sorted(_VALID_GRANT_STATUSES)}"
    if req.stage is not None and req.stage not in _VALID_STAGES:
        return f"stage must be one of {sorted(_VALID_STAGES)}"
    if req.include_in_wiki is not None and req.include_in_wiki not in (0, 1):
        return "include_in_wiki must be 0 or 1"
    return None


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, req: ProjectUpdate):
    """Update editable fields of an existing project.

    Always sets last_touched_by='Heath', last_touched_iso=now().
    Returns the full updated project dict.
    """
    try:
        if req.status is not None and req.status not in _VALID_PROJECT_STATUSES:
            return JSONResponse(
                {"error": f"status must be one of {sorted(_VALID_PROJECT_STATUSES)}"}, status_code=400
            )
        type_err = _validate_project_type_fields(req)
        if type_err:
            return JSONResponse({"error": type_err}, status_code=400)

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        existing = conn.execute(
            "SELECT id FROM research_projects WHERE id=?", (project_id,)
        ).fetchone()
        if existing is None:
            conn.close()
            return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)

        now = _now_iso()
        fields = req.model_dump(exclude_none=True)
        set_clauses = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())

        if set_clauses:
            set_clauses += ", last_touched_by=?, last_touched_iso=?"
        else:
            set_clauses = "last_touched_by=?, last_touched_iso=?"
        values += ["Heath", now, project_id]

        conn.execute(f"UPDATE research_projects SET {set_clauses} WHERE id=?", values)
        conn.commit()

        row = conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM research_projects WHERE id=?",
            (project_id,),
        ).fetchone()

        student_rows = conn.execute(
            "SELECT id, full_name, short_name, role FROM students"
        ).fetchall()
        students_by_id = {
            s["id"]: {"full_name": s["full_name"], "short_name": s["short_name"], "role": s["role"]}
            for s in student_rows
        }

        result = _build_project_dict(row, students_by_id, conn)
        conn.close()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/projects")
async def create_project(req: ProjectUpdate):
    """Create a new project. 'name' is required. status defaults to 'active'.

    id auto-generated as p_<YYYYMMDD>_<uuid6>.
    Returns the full new project dict.
    """
    try:
        if req.name is None:
            return JSONResponse({"error": "'name' is required"}, status_code=400)
        if req.status is not None and req.status not in _VALID_PROJECT_STATUSES:
            return JSONResponse(
                {"error": f"status must be one of {sorted(_VALID_PROJECT_STATUSES)}"}, status_code=400
            )
        type_err = _validate_project_type_fields(req)
        if type_err:
            return JSONResponse({"error": type_err}, status_code=400)

        pid = _new_project_id()
        now = _now_iso()

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        conn.execute(
            "INSERT INTO research_projects("
            "id, name, description, status, linked_goal_ids, data_dir, output_dir, "
            "current_hypothesis, next_action, keywords, linked_artifact_id, "
            "last_touched_by, last_touched_iso, notes, lead_student_id, lead_name, synced_at, "
            "project_type, journal, paper_status, agency, program, grant_status, "
            "stage, include_in_wiki"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                req.name,
                req.description,
                req.status if req.status is not None else "active",
                req.linked_goal_ids,
                req.data_dir,
                req.output_dir,
                req.current_hypothesis,
                req.next_action,
                req.keywords,
                req.linked_artifact_id,
                "Heath",
                now,
                req.notes,
                req.lead_student_id,
                req.lead_name,
                now,
                req.project_type,
                req.journal,
                req.paper_status,
                req.agency,
                req.program,
                req.grant_status,
                req.stage,
                req.include_in_wiki if req.include_in_wiki is not None else 1,
            ),
        )
        conn.commit()

        row = conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM research_projects WHERE id=?",
            (pid,),
        ).fetchone()

        student_rows = conn.execute(
            "SELECT id, full_name, short_name, role FROM students"
        ).fetchall()
        students_by_id = {
            s["id"]: {"full_name": s["full_name"], "short_name": s["short_name"], "role": s["role"]}
            for s in student_rows
        }

        result = _build_project_dict(row, students_by_id, conn)
        conn.close()
        return JSONResponse(result, status_code=201)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Auto-classify projects by type (one-shot heuristic helper)
# ---------------------------------------------------------------------------

def _auto_classify_projects() -> dict:
    """Scan research_projects and set project_type (and agency/program/journal) for obvious cases.

    Only updates rows where project_type IS NULL.
    Returns {"classified": N, "by_type": {type: count}}.
    """
    import re as _re

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute(
        "SELECT id, name FROM research_projects WHERE project_type IS NULL"
    ).fetchall()

    classified = 0
    by_type: dict = {}

    for pid, name in rows:
        if not name:
            continue

        project_type = None
        journal = None
        agency = None
        program_val = None

        # Grant pattern — check first (more specific)
        if _re.search(r"grant|mira|nih|nsf|google\.org|sloan|pew", name, _re.IGNORECASE):
            project_type = "grant"
            # Populate agency from obvious keyword matches
            if _re.search(r"\bnih\b", name, _re.IGNORECASE):
                agency = "NIH"
            elif _re.search(r"\bnsf\b", name, _re.IGNORECASE):
                agency = "NSF"
            elif _re.search(r"google\.org", name, _re.IGNORECASE):
                agency = "Google.org"
            elif _re.search(r"\bsloan\b", name, _re.IGNORECASE):
                agency = "Sloan Foundation"
            elif _re.search(r"\bpew\b", name, _re.IGNORECASE):
                agency = "Pew Charitable Trusts"
            # MIRA is an NIH mechanism
            if _re.search(r"\bmira\b", name, _re.IGNORECASE):
                agency = agency or "NIH"
                program_val = "MIRA"

        # Teaching / curriculum pattern
        elif _re.search(r"CURE|teaching|course|curriculum", name, _re.IGNORECASE):
            project_type = "teaching"

        # Database pattern
        elif _re.search(r"database|(?<!\w)db(?!\w)|karyotype|tree of sex|epistasis", name, _re.IGNORECASE):
            project_type = "database"

        # Paper pattern — venue names or explicit "paper"
        elif _re.search(r"\bpaper\b|bioRxiv|Nature|Science|Cell|PNAS", name, _re.IGNORECASE):
            project_type = "paper"
            venue_match = _re.search(r"bioRxiv|Nature|Science|Cell|PNAS", name, _re.IGNORECASE)
            if venue_match:
                journal = venue_match.group(0)

        if project_type is None:
            continue

        # Build SET clause dynamically
        set_parts = ["project_type=?"]
        vals: list = [project_type]
        if journal is not None:
            set_parts.append("journal=?")
            vals.append(journal)
        if agency is not None:
            set_parts.append("agency=?")
            vals.append(agency)
        if program_val is not None:
            set_parts.append("program=?")
            vals.append(program_val)
        vals.append(pid)

        conn.execute(
            f"UPDATE research_projects SET {', '.join(set_parts)} WHERE id=?",
            vals,
        )
        classified += 1
        by_type[project_type] = by_type.get(project_type, 0) + 1

    conn.commit()
    conn.close()
    return {"classified": classified, "by_type": by_type}


@app.post("/api/projects/auto_classify")
async def auto_classify_projects():
    """One-shot heuristic classifier: sets project_type (and agency/journal) for obvious rows.

    Only touches rows where project_type IS NULL — safe to re-run.
    Returns {"classified": N, "by_type": {"grant": N, "paper": N, ...}}.
    """
    try:
        result = _auto_classify_projects()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Settings (Control panel) endpoints
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    from agent.config import load_config  # noqa: PLC0415
    return load_config()


class SettingsUpdate(BaseModel):
    jobs: dict | None = None
    thresholds: dict | None = None
    personality: dict | None = None
    active_preset: str | None = None
    preset_to_apply: str | None = None


@app.post("/api/settings")
async def update_settings(req: SettingsUpdate):
    from agent.config import load_config, save_config, apply_preset  # noqa: PLC0415
    cfg = load_config()
    if req.preset_to_apply:
        cfg = apply_preset(req.preset_to_apply)
        cfg["active_preset"] = req.preset_to_apply
    if req.jobs:
        cfg.setdefault("jobs", {}).update(req.jobs)
    if req.thresholds:
        cfg.setdefault("thresholds", {}).update(req.thresholds)
    if req.personality:
        cfg.setdefault("personality", {}).update(req.personality)
    save_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# /restart — hit by the "RESTART" pseudo-element link on the chat sidebar.
# Kills chainlit + scheduler, sleeps briefly for ports to clear, spawns the
# canonical start scripts. Does NOT touch the dashboard itself (this process)
# because that would kill the request handler. Returns a small status page
# that auto-redirects to localhost:8000 after ~12 s.
# ---------------------------------------------------------------------------

@app.get("/restart")
def restart_tealc(target: str = "all"):
    import os
    import signal
    import subprocess
    import time as _time
    from fastapi.responses import HTMLResponse

    LAB_DIR = (
        "/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/"
        "My Drive/00-Lab-Agent"
    )

    if target not in {"all", "chat", "scheduler"}:
        target = "all"

    killed: list[str] = []
    started: list[str] = []
    errors: list[str] = []

    if target in ("all", "chat"):
        try:
            r = subprocess.run(
                ["lsof", "-ti:8000"], capture_output=True, text=True, timeout=5
            )
            for pid_s in r.stdout.split():
                try:
                    os.kill(int(pid_s), signal.SIGTERM)
                    killed.append(f"chainlit pid {pid_s}")
                except ProcessLookupError:
                    pass
        except Exception as e:
            errors.append(f"chainlit kill: {e}")

    if target in ("all", "scheduler"):
        sched_pid_file = os.path.join(LAB_DIR, "data", "scheduler.pid")
        try:
            with open(sched_pid_file) as f:
                sched_pid = int(f.read().strip())
            os.kill(sched_pid, signal.SIGTERM)
            killed.append(f"scheduler pid {sched_pid}")
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass
        except Exception as e:
            errors.append(f"scheduler kill: {e}")
        try:
            os.remove(sched_pid_file)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    _time.sleep(1.5)

    if target in ("all", "scheduler"):
        try:
            subprocess.Popen(
                ["bash", os.path.join(LAB_DIR, "scripts", "start_scheduler.sh")],
                cwd=LAB_DIR,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            started.append("scheduler")
        except Exception as e:
            errors.append(f"scheduler start: {e}")

    if target in ("all", "chat"):
        try:
            subprocess.Popen(
                ["bash", os.path.join(LAB_DIR, "run.sh")],
                cwd=LAB_DIR,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            started.append("chainlit")
        except Exception as e:
            errors.append(f"chainlit start: {e}")

    body = f"""<!doctype html>
<html><head><title>Restarting Tealc</title>
<meta http-equiv="refresh" content="12;url=http://localhost:8000/">
<style>
  body {{ font-family: ui-monospace, 'JetBrains Mono', monospace;
         max-width: 540px; margin: 60px auto; padding: 0 20px;
         color: #1e1713; background: #faf6ec; }}
  h1 {{ font-size: 22px; color: #500000; margin-bottom: 4px; }}
  .sub {{ color: #7a6d60; font-size: 12px; margin-bottom: 24px; }}
  ul {{ font-size: 13px; color: #5a4c46; }} li {{ margin: 4px 0; }}
  .err {{ color: #b22222; }}
  .note {{ color: #7a6d60; font-size: 12px; margin-top: 24px; }}
  a {{ color: #500000; }}
</style></head>
<body>
<h1>Restarting Tealc</h1>
<div class="sub">target = {target}</div>
<p><b>Killed:</b></p>
<ul>{''.join(f'<li>{k}</li>' for k in killed) or '<li>nothing was running</li>'}</ul>
<p><b>Spawned (detached):</b></p>
<ul>{''.join(f'<li>{s}</li>' for s in started)}</ul>
{('<p><b class="err">Errors:</b></p><ul>' + ''.join(f'<li class="err">{e}</li>' for e in errors) + '</ul>') if errors else ''}
<p class="note">This page auto-redirects to the chat in ~12 s.
If the chat doesn't load, refresh <a href="http://localhost:8000/">localhost:8000</a> manually.</p>
</body></html>"""
    return HTMLResponse(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")
