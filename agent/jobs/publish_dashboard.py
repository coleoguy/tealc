"""Publish Tealc's full task/briefing state to the local HQ dashboard.

Writes data/dashboard_state.json — consumed by agent/dashboard_server.py
which serves the private localhost:8001 dashboard.

Recommended schedule (Wave 5): IntervalTrigger(minutes=1)

Run manually to test:
    python -m agent.jobs.publish_dashboard
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
DASHBOARD_PATH = os.path.join(_DATA, "dashboard_state.json")
HEARTBEAT_PATH = os.path.join(_DATA, "scheduler_heartbeat.json")
PID_PATH = os.path.join(_DATA, "scheduler.pid")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _age_seconds(iso: str) -> int | None:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return None


def _scheduler_status() -> dict:
    alive = False
    age_seconds = -1
    pid = ""
    try:
        with open(HEARTBEAT_PATH) as f:
            hb = json.load(f)
        age = _age_seconds(hb.get("alive_at", ""))
        if age is not None:
            age_seconds = age
            alive = age < 120
    except Exception:
        pass
    try:
        with open(PID_PATH) as f:
            pid = f.read().strip()
    except Exception:
        pass
    return {"alive": alive, "age_seconds": age_seconds, "pid": pid}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def _safe_json(s) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _meeting_briefs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, content_md, created_at, metadata_json "
        "FROM briefings "
        "WHERE kind='meeting_prep' AND acknowledged_at IS NULL "
        "ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "",
            "content_md": r["content_md"] or "",
            "created_at": r["created_at"] or "",
            "metadata": _safe_json(r["metadata_json"]),
        }
        for r in rows
    ]


def _drafts_unreviewed(conn: sqlite3.Connection) -> list[dict]:
    # Overnight drafts not yet reviewed, joined with project name
    try:
        rows = conn.execute(
            "SELECT od.id, od.project_id, od.section, od.doc_url, "
            "       od.critic_score, od.created_at, "
            "       COALESCE(p.name, od.project_id) AS project_name "
            "FROM overnight_drafts od "
            "LEFT JOIN research_projects p ON p.id = od.project_id "
            "WHERE od.reviewed_at IS NULL "
            "ORDER BY od.created_at DESC LIMIT 20"
        ).fetchall()
    except Exception:
        return []

    result = []
    for r in rows:
        draft_id = r["id"]
        # Try to find a matching briefing that references this draft
        briefing_id = None
        try:
            brow = conn.execute(
                "SELECT id FROM briefings "
                "WHERE kind='overnight_draft' AND acknowledged_at IS NULL "
                "  AND metadata_json LIKE ? LIMIT 1",
                (f'%{draft_id}%',),
            ).fetchone()
            if brow:
                briefing_id = brow["id"]
        except Exception:
            pass

        result.append({
            "id": draft_id,
            "project_name": r["project_name"] or r["project_id"] or "",
            "section": r["section"] or "",
            "doc_url": r["doc_url"] or "",
            "critic_score": r["critic_score"],
            "created_at": r["created_at"] or "",
            "briefing_id": briefing_id,
        })
    return result


def _hypotheses_pending(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT hp.id, hp.project_id, hp.hypothesis_md, "
            "       hp.novelty_score, hp.feasibility_score, hp.proposed_at, "
            "       COALESCE(p.name, hp.project_id) AS project_name "
            "FROM hypothesis_proposals hp "
            "LEFT JOIN research_projects p ON p.id = hp.project_id "
            "WHERE hp.status = 'pending' "
            "ORDER BY hp.proposed_at DESC LIMIT 20"
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "id": r["id"],
            "project_id": r["project_id"] or "",
            "project_name": r["project_name"] or r["project_id"] or "",
            "hypothesis_md": r["hypothesis_md"] or "",
            "novelty_score": r["novelty_score"],
            "feasibility_score": r["feasibility_score"],
            "proposed_iso": r["proposed_at"] or "",
        }
        for r in rows
    ]


def _stalled_goals(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, name, importance, nas_relevance, last_touched_iso, "
            "       CAST(julianday('now') - julianday(COALESCE(last_touched_iso, '2000-01-01')) AS INTEGER) AS days_stale "
            "FROM goals "
            "WHERE status='active' AND importance >= 4 AND nas_relevance='high' "
            "  AND (last_touched_iso IS NULL "
            "    OR julianday('now') - julianday(last_touched_iso) > 21) "
            "ORDER BY days_stale DESC"
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "id": str(r["id"]),
            "name": r["name"] or "",
            "importance": r["importance"],
            "nas_relevance": r["nas_relevance"] or "",
            "last_touched_iso": r["last_touched_iso"] or "",
            "days_stale": r["days_stale"] or 0,
        }
        for r in rows
    ]


def _review_invitations(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, title, content_md, created_at, metadata_json "
            "FROM briefings "
            "WHERE kind='review_invitation' AND acknowledged_at IS NULL "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "id": r["id"],
            "title": r["title"] or "",
            "content_md": r["content_md"] or "",
            "created_at": r["created_at"] or "",
            "metadata": _safe_json(r["metadata_json"]),
        }
        for r in rows
    ]


def _other_briefings(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, kind, urgency, title, content_md, created_at "
            "FROM briefings "
            "WHERE surfaced_at IS NULL "
            "  AND kind NOT IN ('meeting_prep', 'overnight_draft', 'review_invitation') "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "id": r["id"],
            "kind": r["kind"] or "",
            "urgency": r["urgency"] or "",
            "title": r["title"] or "",
            "content_md": r["content_md"] or "",
            "created_at": r["created_at"] or "",
        }
        for r in rows
    ]


def _pending_intentions(conn: sqlite3.Connection) -> int:
    try:
        (n,) = conn.execute(
            "SELECT count(*) FROM intentions WHERE status='pending'"
        ).fetchone()
        return int(n or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Inbox aggregator
# ---------------------------------------------------------------------------

def _inbox(conn: sqlite3.Connection) -> dict:
    """Unified list of items awaiting Heath's review, sorted by urgency."""
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    items: list[dict] = []

    # ── Drafts ──────────────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, source_artifact_title, drafted_section, draft_doc_url, created_at "
            "FROM overnight_drafts WHERE reviewed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        for r in rows:
            try:
                created = datetime.fromisoformat((r["created_at"] or "").replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=_tz.utc)
                age_days = (now - created).total_seconds() / 86400
            except Exception:
                age_days = 0
            urgency = 5 if age_days >= 7 else (4 if age_days >= 3 else 3)
            title = r["source_artifact_title"] or r["drafted_section"] or f"Draft #{r['id']}"
            items.append({
                "id": f"draft_{r['id']}",
                "kind": "draft",
                "title": title[:120],
                "summary": (r["drafted_section"] or "")[:200],
                "created_at": r["created_at"] or "",
                "urgency": urgency,
                "link_text": "Open Google Doc",
                "link_url": r["draft_doc_url"] or "",
                "action_hint": "Approve, edit, or reject",
            })
    except Exception:
        pass

    # ── Hypotheses ──────────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, hypothesis_md, novelty_score, feasibility_score, proposed_iso "
            "FROM hypothesis_proposals WHERE human_review IS NULL "
            "  AND (novelty_score IS NULL OR novelty_score >= 3) "
            "ORDER BY proposed_iso DESC LIMIT 30"
        ).fetchall()
        for r in rows:
            score = r["novelty_score"] or 0
            urgency = 4 if score >= 4 else 3
            hyp_text = (r["hypothesis_md"] or "")
            title = hyp_text[:80] + ("…" if len(hyp_text) > 80 else "")
            items.append({
                "id": f"hyp_{r['id']}",
                "kind": "hypothesis",
                "title": title,
                "summary": hyp_text[:200],
                "created_at": r["proposed_iso"] or "",
                "urgency": urgency,
                "link_text": "View",
                "link_url": f"/dashboard#hypotheses?id={r['id']}",
                "action_hint": "Adopt or reject",
            })
    except Exception:
        pass

    # ── Grants ──────────────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, title, url, fit_score, deadline_iso, first_seen "
            "FROM grant_opportunities "
            "WHERE fit_score >= 0.3 AND (dismissed IS NULL OR dismissed = 0) "
            "ORDER BY fit_score DESC LIMIT 30"
        ).fetchall()
        for r in rows:
            score = r["fit_score"] or 0
            urgency = 5 if score >= 0.7 else (4 if score >= 0.5 else (3 if score >= 0.4 else 2))
            # Boost urgency if deadline within 7 days
            try:
                if r["deadline_iso"]:
                    dl = datetime.fromisoformat(r["deadline_iso"].replace("Z", "+00:00"))
                    if dl.tzinfo is None:
                        dl = dl.replace(tzinfo=_tz.utc)
                    if (dl - now).days <= 7:
                        urgency = 5
            except Exception:
                pass
            items.append({
                "id": f"grant_{r['id']}",
                "kind": "grant",
                "title": (r["title"] or f"Grant #{r['id']}")[:120],
                "summary": f"Fit score: {score:.2f}. Deadline: {r['deadline_iso'] or 'unknown'}",
                "created_at": r["first_seen"] or "",
                "urgency": urgency,
                "link_text": "Open opportunity",
                "link_url": r["url"] or "",
                "action_hint": "Apply or dismiss",
            })
    except Exception:
        pass

    # ── Analyses ────────────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, project_id, run_iso, interpretation_md, human_review "
            "FROM analysis_runs "
            "WHERE run_iso >= datetime('now','-14 days') "
            "  AND human_review IS NULL "
            "ORDER BY run_iso DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            interp = r["interpretation_md"] or ""
            items.append({
                "id": f"analysis_{r['id']}",
                "kind": "analysis",
                "title": f"Analysis run — {(r['project_id'] or 'unknown project')[:60]}",
                "summary": interp[:200],
                "created_at": r["run_iso"] or "",
                "urgency": 3,
                "link_text": "View",
                "link_url": f"/dashboard#analyses?id={r['id']}",
                "action_hint": "Review interpretation",
            })
    except Exception:
        pass

    # ── Ledger entries (high critic score, unreviewed) ──────────────────────
    try:
        rows = conn.execute(
            "SELECT id, kind, content_md, critic_score, created_at "
            "FROM output_ledger WHERE critic_score >= 4 AND user_action IS NULL "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            content = r["content_md"] or ""
            items.append({
                "id": f"ledger_{r['id']}",
                "kind": "ledger",
                "title": f"High-quality {r['kind'] or 'output'} (critic score {r['critic_score']})",
                "summary": content[:200],
                "created_at": r["created_at"] or "",
                "urgency": 3,
                "link_text": "View",
                "link_url": f"/dashboard#ledger?id={r['id']}",
                "action_hint": "Review and approve",
            })
    except Exception:
        pass

    # ── Pending preregs ──────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, hypothesis_md, prereg_published_at, adjudicated_at "
            "FROM hypothesis_proposals "
            "WHERE prereg_published_at IS NOT NULL AND adjudicated_at IS NULL "
            "ORDER BY prereg_published_at DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            try:
                pub = datetime.fromisoformat((r["prereg_published_at"] or "").replace("Z", "+00:00"))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=_tz.utc)
                days_since = (now - pub).total_seconds() / 86400
            except Exception:
                days_since = 0
            urgency = 4 if days_since >= 7 else 2
            hyp_text = r["hypothesis_md"] or ""
            items.append({
                "id": f"prereg_{r['id']}",
                "kind": "prereg",
                "title": f"Prereg pending adjudication: {hyp_text[:60]}{'…' if len(hyp_text) > 60 else ''}",
                "summary": hyp_text[:200],
                "created_at": r["prereg_published_at"] or "",
                "urgency": urgency,
                "link_text": "View",
                "link_url": f"/dashboard#prereg?id={r['id']}",
                "action_hint": "Check T+7 adjudication status",
            })
    except Exception:
        pass

    # ── Reviewer invitations (draft — need to be sent) ──────────────────────
    try:
        rows = conn.execute(
            "SELECT id, reviewer_pseudonym, domain, sla_iso, created_at "
            "FROM reviewer_invitations WHERE status='draft' "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            items.append({
                "id": f"rev_inv_{r['id']}",
                "kind": "reviewer_invitation",
                "title": f"Reviewer invitation draft: {r['reviewer_pseudonym']} ({r['domain']})",
                "summary": f"Draft invitation for domain '{r['domain']}'. SLA: {r['sla_iso'] or 'TBD'}",
                "created_at": r["created_at"] or "",
                "urgency": 5,
                "link_text": "View",
                "link_url": "/dashboard#reviewer_circle",
                "action_hint": "Send invitation drafts",
            })
    except Exception:
        pass

    # ── Briefings (recent, unsurfaced) ──────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, kind, title, content_md, created_at "
            "FROM briefings "
            "WHERE created_at >= datetime('now','-2 days') "
            "  AND acknowledged_at IS NULL "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            content = r["content_md"] or ""
            items.append({
                "id": f"briefing_{r['id']}",
                "kind": "briefing",
                "title": r["title"] or f"Briefing ({r['kind']})",
                "summary": content[:200],
                "created_at": r["created_at"] or "",
                "urgency": 2,
                "link_text": "View",
                "link_url": f"/dashboard#briefings?id={r['id']}",
                "action_hint": "Read and acknowledge",
            })
    except Exception:
        pass

    # ── Open intentions (recent) ─────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, kind, description, priority, created_at "
            "FROM intentions "
            "WHERE status='pending' AND created_at >= datetime('now','-30 days') "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            items.append({
                "id": f"intention_{r['id']}",
                "kind": "intention",
                "title": f"Intention: {(r['description'] or '')[:80]}",
                "summary": (r["description"] or "")[:200],
                "created_at": r["created_at"] or "",
                "urgency": 1,
                "link_text": "View",
                "link_url": "/dashboard#intentions",
                "action_hint": "Set next action or close",
            })
    except Exception:
        pass

    # ── Sort by urgency DESC, then created_at DESC ───────────────────────────
    def _sort_key(item):
        try:
            t = datetime.fromisoformat((item["created_at"] or "").replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_tz.utc)
            ts = t.timestamp()
        except Exception:
            ts = 0
        return (-item.get("urgency", 0), -ts)

    items.sort(key=_sort_key)
    items = items[:50]

    # ── Summary stats ────────────────────────────────────────────────────────
    total = len(items)
    high_urgency = sum(1 for i in items if i.get("urgency", 0) >= 4)
    by_kind: dict = {}
    for i in items:
        k = i.get("kind", "other")
        by_kind[k] = by_kind.get(k, 0) + 1

    oldest_age = 0.0
    for i in items:
        try:
            t = datetime.fromisoformat((i["created_at"] or "").replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_tz.utc)
            age = (now - t).total_seconds() / 86400
            if age > oldest_age:
                oldest_age = age
        except Exception:
            pass

    actionable_today = sum(
        1 for i in items
        if i.get("urgency", 0) >= 4 and (lambda c: (
            (now - datetime.fromisoformat(c.replace("Z", "+00:00")).replace(
                tzinfo=_tz.utc if datetime.fromisoformat(c.replace("Z", "+00:00")).tzinfo is None else
                datetime.fromisoformat(c.replace("Z", "+00:00")).tzinfo
            )).total_seconds() / 86400 < 14
        ) if c else False)(i.get("created_at", ""))
    )

    return {
        "items": items,
        "inbox_summary": {
            "total_pending": total,
            "high_urgency_count": high_urgency,
            "by_kind": by_kind,
            "oldest_pending_age_days": round(oldest_age, 1),
            "actionable_today": actionable_today,
        },
    }


# ---------------------------------------------------------------------------
# Reviewer Circle
# ---------------------------------------------------------------------------

def _reviewer_circle(conn: sqlite3.Connection) -> dict:
    """Aggregated reviewer circle state for the dashboard."""
    # Invitations by status
    inv_by_status = {"draft": 0, "sent": 0, "replied": 0, "expired": 0}
    invitations = []
    try:
        rows = conn.execute(
            "SELECT id, reviewer_pseudonym, domain, status, sla_iso, sent_at "
            "FROM reviewer_invitations ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            s = r["status"] or "draft"
            if s in inv_by_status:
                inv_by_status[s] += 1
            else:
                inv_by_status[s] = inv_by_status.get(s, 0) + 1
            invitations.append({
                "id": r["id"],
                "pseudonym": r["reviewer_pseudonym"] or "",
                "domain": r["domain"] or "",
                "status": s,
                "sla_iso": r["sla_iso"] or "",
                "sent_at": r["sent_at"] or "",
            })
    except Exception:
        pass

    # Also count invitations not in the latest 20 for status counts
    try:
        all_status_rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM reviewer_invitations GROUP BY status"
        ).fetchall()
        inv_by_status = {"draft": 0, "sent": 0, "replied": 0, "expired": 0}
        for r in all_status_rows:
            s = r["status"] or "draft"
            if s in inv_by_status:
                inv_by_status[s] = r["n"]
    except Exception:
        pass

    # Latest correlation per (domain, dimension)
    correlations = []
    try:
        rows = conn.execute(
            "SELECT domain, dimension, n_pairs, spearman_r, "
            "       bootstrap_ci_lo, bootstrap_ci_hi, computed_at "
            "FROM reviewer_correlations "
            "WHERE (domain, dimension, computed_at) IN ("
            "  SELECT domain, dimension, MAX(computed_at) "
            "  FROM reviewer_correlations GROUP BY domain, dimension"
            ") ORDER BY domain, dimension"
        ).fetchall()
        for r in rows:
            correlations.append({
                "domain": r["domain"] or "",
                "dimension": r["dimension"] or "",
                "n_pairs": r["n_pairs"] or 0,
                "spearman_r": r["spearman_r"],
                "ci_lo": r["bootstrap_ci_lo"],
                "ci_hi": r["bootstrap_ci_hi"],
                "computed_at": r["computed_at"] or "",
            })
    except Exception:
        pass

    # Check if reviewers.json has emails filled in
    reviewers_configured = False
    manifest_path = "data/reviewer_circle/manifest.json"
    try:
        reviewers_json_path = os.path.join(_DATA, "reviewer_circle", "reviewers.json")
        if os.path.isfile(reviewers_json_path):
            import json as _json
            with open(reviewers_json_path) as f:
                rj = _json.load(f)
            reviewers_configured = any(
                r.get("email") and r["email"] != "TODO"
                for r in rj.get("reviewers", [])
            )
    except Exception:
        pass

    return {
        "invitations_by_status": inv_by_status,
        "invitations": invitations,
        "correlations_latest": correlations,
        "manifest_path": manifest_path,
        "reviewers_configured": reviewers_configured,
    }


def _last_24h_jobs(conn: sqlite3.Connection) -> list[dict]:
    _skip = (
        'heartbeat', 'refresh_context', 'email_burst', 'watch_deadlines',
        'publish_aquarium', 'publish_dashboard',
    )
    placeholders = ",".join("?" * len(_skip))
    try:
        rows = conn.execute(
            f"SELECT job_name, COUNT(*) AS runs, "
            f"       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok, "
            f"       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS fail, "
            f"       MAX(output_summary) AS latest_summary "
            f"FROM job_runs "
            f"WHERE started_at > datetime('now','-24 hours') "
            f"  AND job_name NOT IN ({placeholders}) "
            f"GROUP BY job_name "
            f"ORDER BY runs DESC LIMIT 20",
            _skip,
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "job_name": r["job_name"],
            "runs": r["runs"],
            "ok": r["ok"] or 0,
            "fail": r["fail"] or 0,
            "latest_summary": r["latest_summary"] or "",
        }
        for r in rows
    ]


def _recent_ledger(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, kind, project_id, critic_score, created_at "
            "FROM output_ledger "
            "ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "id": r["id"],
            "kind": r["kind"] or "",
            "project_id": r["project_id"] or "",
            "critic_score": r["critic_score"],
            "created_at": r["created_at"] or "",
        }
        for r in rows
    ]


def _cost_24h(conn: sqlite3.Connection) -> float:
    try:
        (total,) = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) "
            "FROM cost_tracking WHERE ts >= datetime('now','-24 hours')"
        ).fetchone()
        return round(float(total or 0.0), 4)
    except Exception:
        return 0.0


def _retrieval_quality_7d(conn: sqlite3.Connection) -> float | None:
    try:
        rows = conn.execute(
            "SELECT relevance_score FROM retrieval_quality "
            "WHERE sampled_at >= datetime('now','-7 days') "
            "  AND relevance_score IS NOT NULL"
        ).fetchall()
    except Exception:
        return None
    scores = [r[0] for r in rows]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 2)


# ---------------------------------------------------------------------------
# Prereg ledger
# ---------------------------------------------------------------------------

def _prereg_ledger(conn):
    import json as _json
    try:
        rows = conn.execute(
            "SELECT id, hypothesis_md, prereg_published_at, prereg_test_json, "
            "       adjudication, adjudicated_at, adjudication_rationale_md, prereg_md "
            "FROM hypothesis_proposals WHERE prereg_published_at IS NOT NULL "
            "ORDER BY prereg_published_at DESC LIMIT 50"
        ).fetchall()
    except Exception:
        return []
    result = []
    for r in rows:
        tj = {}
        try:
            tj = _json.loads(r[3] or "{}")
        except Exception:
            pass
        result.append({
            "id": r[0],
            "hypothesis_md": (r[1] or "")[:500],
            "prereg_published_at": r[2] or "",
            "db_name": tj.get("db_name"),
            "test_name": tj.get("test_name"),
            "p_threshold": tj.get("p_threshold"),
            "adjudication": r[4],
            "adjudicated_at": r[5] or "",
            "adjudication_rationale_md": (r[6] or "")[:1000],
            "prereg_md": (r[7] or "")[:2000],
        })
    return result


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

def _counts(conn: sqlite3.Connection, meeting_briefs, drafts, hypotheses, stalled, review_inv) -> dict:
    unsurfaced = 0
    try:
        (unsurfaced,) = conn.execute(
            "SELECT count(*) FROM briefings WHERE surfaced_at IS NULL AND acknowledged_at IS NULL"
        ).fetchone()
    except Exception:
        pass
    pending_reviewer_replies = 0
    try:
        (pending_reviewer_replies,) = conn.execute(
            "SELECT count(*) FROM reviewer_invitations WHERE status='sent'"
        ).fetchone()
    except Exception:
        pass
    return {
        "unsurfaced_briefings": int(unsurfaced or 0),
        "unreviewed_drafts": len(drafts),
        "pending_hypotheses": len(hypotheses),
        "pending_intentions": _pending_intentions(conn),
        "stalled_goals": len(stalled),
        "pending_review_invitations": len(review_inv),
        "pending_reviewer_replies": int(pending_reviewer_replies or 0),
    }


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("publish_dashboard")
def job() -> str:
    conn = _conn()
    try:
        meeting = _meeting_briefs(conn)
        drafts = _drafts_unreviewed(conn)
        hyps = _hypotheses_pending(conn)
        stalled = _stalled_goals(conn)
        review_inv = _review_invitations(conn)
        other = _other_briefings(conn)
        jobs_24h = _last_24h_jobs(conn)
        ledger = _recent_ledger(conn)
        cost = _cost_24h(conn)
        rq = _retrieval_quality_7d(conn)
        counts = _counts(conn, meeting, drafts, hyps, stalled, review_inv)
        prereg_ledger = _prereg_ledger(conn)
        inbox = _inbox(conn)
        reviewer_circle = _reviewer_circle(conn)
    finally:
        conn.close()

    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scheduler_status": _scheduler_status(),
        "counts": counts,
        "tasks": {
            "meeting_briefs": meeting,
            "drafts_unreviewed": drafts,
            "hypotheses_pending": hyps,
            "stalled_goals": stalled,
            "review_invitations": review_inv,
            "other_briefings": other,
        },
        "recent_activity": {
            "last_24h_jobs": jobs_24h,
            "recent_ledger": ledger,
            "cost_24h_usd": cost,
            "retrieval_quality_7d_mean": rq,
        },
        "prereg_ledger": prereg_ledger,
        "inbox": inbox,
        "reviewer_circle": reviewer_circle,
    }

    with open(DASHBOARD_PATH, "w") as f:
        json.dump(state, f, indent=2)

    inbox_total = inbox.get("inbox_summary", {}).get("total_pending", 0)
    summary = (
        f"dashboard: {counts['unsurfaced_briefings']} briefings, "
        f"{counts['unreviewed_drafts']} drafts, "
        f"{counts['pending_hypotheses']} hyps, "
        f"{counts['stalled_goals']} stalled goals, "
        f"{inbox_total} inbox items"
    )
    return summary


if __name__ == "__main__":
    print(job())
