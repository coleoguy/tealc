"""Haiku executive loop — wakes every 15 min, reads current context, decides what Tealc
should do RIGHT NOW, and logs the decision to executive_decisions.

AUTONOMY MODE (controlled by data/tealc_config.json key "executive_autonomy_mode"):
  "advisor"          — all decisions logged only, nothing executes (original v1 behaviour)
  "restricted_actor" — 3 safe actions execute; all others remain advisor-only (DEFAULT)
  "actor"            — reserved; currently behaves the same as restricted_actor

Safe actions that execute in restricted_actor / actor mode:
  1. surface_stale_briefing  — bumps urgency of oldest stale unsurfaced briefing
  2. check_deadline_approach — runs deadline_countdown job inline (idempotent-by-day)
  3. propose_next_action     — runs next_action_filler job inline

Everything else (draft_reply_for_vip, flag_overdue_milestone, followup_unreviewed_draft,
nudge_intention, idle_* actions, escalate_*, paper_of_the_day, nothing …) remains
advisor-only regardless of mode.
"""
import json
import logging
import os
import sqlite3

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Load .env from project root (two levels up from agent/jobs/)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", ".env"))
load_dotenv(_ENV_PATH)
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import anthropic

from agent.jobs import tracked

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALLOWED_ACTIONS = [
    "nothing",                    # genuinely nothing actionable — use sparingly
    "surface_briefing",           # unsurfaced briefing Heath should see
    "nudge_intention",            # high-priority pending intention near target_iso
    "escalate_email_triage",      # emails piling up; would spawn email-triage subagent
    "paper_of_the_day",           # calm time + new papers; spawn paper-summary subagent
    "escalate_to_sonnet",         # situation has nuance Haiku doesn't trust itself on
    "escalate_to_human_critical", # genuine emergency (deadline tomorrow + grant draft empty, student crisis, etc.)
    # Idle-time batch work (ADVISOR ONLY — logged, not executed)
    "idle_paper_deep_dive",       # deep paper read when idle; Sonnet reads full text, connects to Heath's program
    "idle_intention_refinement",  # take a vague pending intention and flesh it into concrete sub-tasks
    "idle_grant_section_draft",   # when idle + grant deadline <14 days, draft an empty section
    # NEW actions (v2)
    "flag_overdue_milestone",     # one or more milestones past target_iso and not completed
    "propose_next_action",        # active research_project(s) with empty next_action field
    "surface_stale_briefing",     # unsurfaced briefing that is >48h old — re-surface with urgency
    "draft_reply_for_vip",        # recent VIP email detected but no draft reply yet
    "followup_unreviewed_draft",  # overnight_draft is >3 days old with no reviewed_at
    "check_deadline_approach",    # deadline ≤5 days out AND no deadline_countdown briefing created today
]

# ---------------------------------------------------------------------------
# Haiku system prompt — action-biased, every action has a 1-line trigger
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are the executive loop for Tealc, an AI postdoc agent for Heath Blackmon (Texas A&M,
evolutionary genomics). Your job: look at a snapshot of his current situation and pick the
single most useful action Tealc should log RIGHT NOW.

YOU ARE AN ADVISOR. Default to ACTION, not inaction.
Only choose `nothing` if every other action is genuinely inapplicable given the signals below.

HEATH'S URGENT PRIORITIES (April 2026):
- Google.org grant due ~2026-04-25 — THIS WEEK. Top priority.
- NIH MIRA (R35) renewal — NEXT MONTH. Second critical grant.
- Chromosomal stasis preprint — targeting Nature/Science/Cell, needs journal placement.
- NAS membership is the north-star career goal.

ACTION TRIGGER CONDITIONS (pick the first that matches; escalate down):
1. `escalate_to_human_critical` — deadline_days_remaining ≤ 1 AND grant draft is empty, OR student crisis email.
2. `check_deadline_approach`    — deadlines_within_5_days > 0 AND no deadline_countdown briefing today.
3. `flag_overdue_milestone`     — overdue_milestones_count > 0 (milestone past target date, still open).
4. `draft_reply_for_vip`        — vip_emails_pending > 0 (recent VIP email, no draft reply).
5. `followup_unreviewed_draft`  — unreviewed_drafts_gt3d > 0 (overnight draft > 3 days without review).
6. `surface_stale_briefing`     — stale_unsurfaced_briefings_count > 0 (unsurfaced briefing > 48h old).
7. `nudge_intention`            — pending high/critical intention with target_iso within 7 days.
8. `escalate_email_triage`      — explicit evidence of email pile-up in context.
9. `idle_grant_section_draft`   — idle_class in (idle, deep_idle) AND grant deadline within 14 days.
10. `surface_briefing`          — unsurfaced_briefings_count > 0 AND hours_since_last_chat > 6.
11. `propose_next_action`       — projects_missing_next_action > 0 AND idle_class in (idle, deep_idle).
12. `idle_intention_refinement` — idle_class in (idle, deep_idle) AND vague pending intentions exist.
13. `idle_paper_deep_dive`      — idle_class = deep_idle AND no critical deadlines within 3 days.
14. `paper_of_the_day`          — is_working_hours = 0 AND no critical deadlines tomorrow.
15. `escalate_to_sonnet`        — you see nuance that requires deeper reasoning.
16. `nothing`                   — ONLY when none of the above apply.

IDLE-AWARE BEHAVIOR:
- 'active' (<30 min since chat): only escalate if truly critical; otherwise prefer `nothing`.
- 'engaged' (30 min – 4 hr): light interventions — nudges, briefings if overdue.
- 'idle' (4 – 24 hr): heavier work — drafts, refinements, deadline checks.
- 'deep_idle' (>24 hr): full runway — grant drafts, paper dives, project work.

GOAL RULES:
G1. Prefer actions that advance the highest-importance active goal not recently touched.
G2. Milestone target_iso within 7 days → lean toward nudge or escalate, not nothing.
G3. Set linked_goal_id to the most relevant goal_id, or "" if none applies.

OUTPUT FORMAT — strict JSON, no prose, no preamble, no trailing text:
{"action": "<one of ALLOWED_ACTIONS>", "reasoning": "<2-3 sentences>", "confidence": <0.0-1.0>, "linked_goal_id": "<goal id like g_001, or empty string>"}
"""


# ---------------------------------------------------------------------------
# Internal helpers — context reads
# ---------------------------------------------------------------------------
def _get_context(conn: sqlite3.Connection) -> Optional[Dict]:
    """Fetch the current_context row. Returns None if table is empty."""
    try:
        row = conn.execute(
            "SELECT refreshed_at, unsurfaced_briefings_count, pending_intentions_count, "
            "next_deadline_name, next_deadline_iso, next_deadline_days_remaining, "
            "students_needing_attention_count, students_needing_attention_names, "
            "hours_since_last_chat, open_grant_opportunities_count, "
            "current_local_hour, current_local_day, is_working_hours, notes, idle_class "
            "FROM current_context WHERE id=1"
        ).fetchone()
        if not row:
            return None
        return {
            "refreshed_at": row[0],
            "unsurfaced_briefings_count": row[1],
            "pending_intentions_count": row[2],
            "next_deadline_name": row[3],
            "next_deadline_iso": row[4],
            "next_deadline_days_remaining": row[5],
            "students_needing_attention_count": row[6],
            "students_needing_attention_names": row[7],
            "hours_since_last_chat": row[8],
            "open_grant_opportunities_count": row[9],
            "current_local_hour": row[10],
            "current_local_day": row[11],
            "is_working_hours": row[12],
            "notes": row[13],
            "idle_class": row[14] if len(row) > 14 else "unknown",
        }
    except Exception:
        return None


def _get_top_briefings(conn: sqlite3.Connection) -> List[Dict]:
    """Fetch top 3 unsurfaced briefings (id, kind, urgency, title)."""
    try:
        rows = conn.execute(
            "SELECT id, kind, urgency, title FROM briefings "
            "WHERE surfaced_at IS NULL ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        return [{"id": r[0], "kind": r[1], "urgency": r[2], "title": r[3][:100]} for r in rows]
    except Exception:
        return []


def _get_top_intentions(conn: sqlite3.Connection) -> List[Dict]:
    """Fetch top 3 high-priority pending intentions."""
    try:
        rows = conn.execute(
            "SELECT id, kind, description, target_iso, priority FROM intentions "
            "WHERE status IN ('pending', 'in_progress') "
            "  AND priority IN ('high', 'critical') "
            "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
            "  CASE WHEN target_iso IS NULL THEN 1 ELSE 0 END, target_iso "
            "LIMIT 3"
        ).fetchall()
        return [
            {"id": r[0], "kind": r[1], "description": r[2][:100], "target_iso": r[3], "priority": r[4]}
            for r in rows
        ]
    except Exception:
        return []


def _get_active_goals(conn: sqlite3.Connection) -> List[Dict]:
    """Fetch top 5 active goals (importance >= 4), sorted by importance DESC then nearest milestone."""
    try:
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.importance, g.time_horizon, g.nas_relevance,
                   g.last_touched_iso, g.success_metric,
                   MIN(m.target_iso) AS nearest_milestone_iso
            FROM goals g
            LEFT JOIN milestones_v2 m
              ON m.goal_id = g.id
             AND m.status IN ('pending', 'in_progress')
             AND m.target_iso IS NOT NULL
            WHERE g.status = 'active'
              AND g.importance >= 4
            GROUP BY g.id
            ORDER BY g.importance DESC,
                     CASE WHEN MIN(m.target_iso) IS NULL THEN 1 ELSE 0 END,
                     MIN(m.target_iso) ASC
            LIMIT 5
            """
        ).fetchall()
        return [
            {
                "goal_id": r[0],
                "name": r[1],
                "importance": r[2],
                "time_horizon": r[3],
                "nas_relevance": r[4],
                "last_touched_iso": r[5],
                "success_metric": r[6],
                "nearest_milestone_iso": r[7],
            }
            for r in rows
        ]
    except Exception:
        return []


def _get_nearest_milestones(conn: sqlite3.Connection) -> List[Dict]:
    """Fetch 5 nearest unfinished milestones across all active goals."""
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.goal_id, g.name AS goal_name, m.milestone,
                   m.target_iso, m.status
            FROM milestones_v2 m
            JOIN goals g ON g.id = m.goal_id AND g.status = 'active'
            WHERE m.status IN ('pending', 'in_progress')
              AND m.target_iso IS NOT NULL
            ORDER BY m.target_iso ASC
            LIMIT 5
            """
        ).fetchall()
        return [
            {
                "milestone_id": r[0],
                "goal_id": r[1],
                "goal_name": r[2],
                "milestone": r[3],
                "target_iso": r[4],
                "status": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# NEW: state signal helpers for action-biased decision-making
# ---------------------------------------------------------------------------
def _count_overdue_milestones(conn: sqlite3.Connection) -> int:
    """Count milestones past their target_iso that are still pending/in_progress."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            """
            SELECT COUNT(*) FROM milestones_v2 m
            JOIN goals g ON g.id = m.goal_id AND g.status = 'active'
            WHERE m.status IN ('pending', 'in_progress')
              AND m.target_iso IS NOT NULL
              AND m.target_iso < ?
            """,
            (now_iso,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _count_projects_missing_next_action(conn: sqlite3.Connection) -> int:
    """Count active research_projects with a null/empty next_action."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM research_projects "
            "WHERE status = 'active' AND (next_action IS NULL OR TRIM(next_action) = '')"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _count_stale_unsurfaced_briefings(conn: sqlite3.Connection) -> int:
    """Count briefings that are unsurfaced and older than 48 hours."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM briefings "
            "WHERE surfaced_at IS NULL AND created_at < ?",
            (cutoff,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _count_unreviewed_drafts_gt3d(conn: sqlite3.Connection) -> int:
    """Count overnight_drafts older than 3 days with no reviewed_at."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM overnight_drafts "
            "WHERE reviewed_at IS NULL AND created_at < ?",
            (cutoff,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _count_vip_emails_pending(conn: sqlite3.Connection) -> int:
    """Count briefings of kind 'vip_email' that are unsurfaced."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM briefings "
            "WHERE kind = 'vip_email' AND surfaced_at IS NULL"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _count_deadlines_within_5_days(data_dir: str) -> int:
    """Count deadlines from data/deadlines.json that are <=5 days away."""
    try:
        path = os.path.join(data_dir, "deadlines.json")
        with open(path) as f:
            data = json.load(f)
        now = datetime.now(timezone.utc)
        count = 0
        for d in data.get("deadlines", []):
            due_str = d.get("due_iso", "")
            if not due_str:
                continue
            try:
                # Parse ISO with possible timezone offset
                due_dt = datetime.fromisoformat(due_str)
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                days_out = (due_dt - now).total_seconds() / 86400
                if 0 <= days_out <= 5:
                    count += 1
            except ValueError:
                pass
        return count
    except Exception:
        return 0


def _deadline_countdown_created_today(conn: sqlite3.Connection) -> bool:
    """Return True if a deadline_countdown briefing was already created today (UTC)."""
    try:
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COUNT(*) FROM briefings "
            "WHERE kind = 'deadline_countdown' AND created_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()
        return (row[0] if row else 0) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Compose situation JSON with enriched state signals
# ---------------------------------------------------------------------------
def _compose_situation(
    ctx: Dict,
    briefings: List[Dict],
    intentions: List[Dict],
    active_goals: List[Dict],
    nearest_milestones: List[Dict],
    state_signals: Dict,
) -> str:
    """Build the compact JSON situation string to send to Haiku."""
    situation = {
        "context": ctx,
        "top_unsurfaced_briefings": briefings,
        "top_high_priority_intentions": intentions,
        "active_goals": active_goals,
        "nearest_milestones": nearest_milestones,
        "state_signals": state_signals,
    }
    return json.dumps(situation, default=str)


def _call_haiku(situation_json: str) -> Tuple[str, Optional[str], Optional[Dict]]:
    """Call Haiku 4.5 and return (raw_text, error_or_none, usage_or_none)."""
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": situation_json}],
        )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return response.content[0].text.strip(), None, usage
    except Exception as e:
        return "", str(e), None


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from a string."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner_lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner_lines).strip()
    return text


def _parse_haiku_output(raw: str) -> Tuple[str, str, Optional[float], Optional[str], str]:
    """Parse Haiku's JSON output. Returns (action, reasoning, confidence, parse_error, linked_goal_id).

    Strips markdown code fences if present (Haiku sometimes adds them despite instructions).
    """
    text = _strip_fences(raw)
    try:
        data = json.loads(text)
        action = data.get("action", "nothing")
        reasoning = data.get("reasoning", "(no reasoning provided)")
        confidence = data.get("confidence")
        linked_goal_id = data.get("linked_goal_id", "") or ""
        if action not in ALLOWED_ACTIONS:
            return "nothing", reasoning, confidence, f"action '{action}' not in ALLOWED_ACTIONS; defaulted to nothing", linked_goal_id
        return action, reasoning, confidence, None, linked_goal_id
    except Exception as e:
        return "nothing", "(parse failed)", None, f"JSON parse failed: {e}", ""


def _insert_decision(
    conn: sqlite3.Connection,
    decided_at: str,
    action: str,
    reasoning: str,
    confidence: Optional[float],
    context_snapshot_json: str,
    raw_haiku_output: str,
    parse_error: Optional[str],
    linked_goal_id: str = "",
    executed: int = 0,
    execution_result: Optional[str] = None,
) -> None:
    """Write one row to executive_decisions."""
    conn.execute(
        """
        INSERT INTO executive_decisions (
            decided_at, action, reasoning, confidence,
            context_snapshot_json, raw_haiku_output, parse_error,
            executed, linked_goal_id, execution_result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decided_at,
            action,
            reasoning,
            confidence,
            context_snapshot_json,
            raw_haiku_output,
            parse_error,
            executed,
            linked_goal_id,
            execution_result,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Config gate — read executive_autonomy_mode from tealc_config.json
# ---------------------------------------------------------------------------
def _load_autonomy_mode(data_dir: str) -> str:
    """Return the executive_autonomy_mode from tealc_config.json.

    Defaults to "restricted_actor" if the key is absent (Heath explicitly
    requested this promotion).  Valid values:
      "advisor"          — log only, nothing executes
      "restricted_actor" — run the 3 safe actions; everything else is advisory
      "actor"            — reserved; behaves like restricted_actor for now
    """
    try:
        config_path = os.path.join(data_dir, "tealc_config.json")
        with open(config_path) as fh:
            cfg = json.load(fh)
        mode = cfg.get("executive_autonomy_mode", "restricted_actor")
        if mode not in ("advisor", "restricted_actor", "actor"):
            log.warning("Unknown executive_autonomy_mode %r — defaulting to restricted_actor", mode)
            return "restricted_actor"
        return mode
    except Exception as exc:
        log.warning("Could not read tealc_config.json for autonomy mode (%s); defaulting to restricted_actor", exc)
        return "restricted_actor"


# ---------------------------------------------------------------------------
# Safe-action executor (restricted_actor / actor modes only)
# ---------------------------------------------------------------------------
# Actions promoted to ACTOR mode:
#   1. surface_stale_briefing  — bumps oldest stale briefing urgency info→warn
#   2. check_deadline_approach — runs deadline_countdown job inline (idempotent)
#   3. propose_next_action     — runs next_action_filler job inline (idempotent)
#
# All other actions remain advisor-only regardless of mode.
_ACTOR_ACTIONS = frozenset({"surface_stale_briefing", "check_deadline_approach", "propose_next_action"})


def _execute_action(
    action: str,
    reasoning: str,
    confidence: Optional[float],
    ctx: dict,
    conn: sqlite3.Connection,
) -> Tuple[int, str]:
    """Attempt to execute one of the 3 safe promoted actions.

    Returns (executed_flag, execution_result_str).
    executed_flag = 1 on success, 0 on error or no-op / not-promoted.
    """
    if action not in _ACTOR_ACTIONS:
        return 0, "advisor-only: action not promoted to actor mode"

    try:
        if action == "surface_stale_briefing":
            row = conn.execute(
                "SELECT id, urgency FROM briefings "
                "WHERE surfaced_at IS NULL AND created_at < datetime('now', '-48 hours') "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return 1, "surface_stale_briefing: no stale unsurfaced briefings found (no-op)"
            bid, urgency = row
            new_urgency = "warn" if urgency == "info" else urgency
            conn.execute("UPDATE briefings SET urgency=? WHERE id=?", (new_urgency, bid))
            conn.commit()
            return 1, f"surface_stale_briefing: briefing id={bid} urgency {urgency}→{new_urgency}"

        elif action == "check_deadline_approach":
            from agent.jobs.deadline_countdown import job as _deadline_job  # noqa: PLC0415
            result = _deadline_job()
            return 1, f"check_deadline_approach: deadline_countdown job ran → {result}"

        elif action == "propose_next_action":
            from agent.jobs.next_action_filler import job as _naf_job  # noqa: PLC0415
            result = _naf_job()
            return 1, f"propose_next_action: next_action_filler job ran → {result}"

    except Exception as exc:
        return 0, f"error: {exc}"

    return 0, "advisor-only: unhandled branch"


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("executive")
def job() -> str:
    """Executive loop: read context, call Haiku, log decision, optionally execute."""
    from agent.scheduler import DB_PATH  # noqa: PLC0415

    decided_at = datetime.now(timezone.utc).isoformat()
    data_dir = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        # 0. Load autonomy mode from config (default: restricted_actor)
        mode = _load_autonomy_mode(data_dir)

        # 1. Read current context
        ctx = _get_context(conn)
        if ctx is None:
            # Context not yet populated — safe fallback
            conn.close()
            return "action=nothing confidence=1.00 (no context snapshot available yet)"

        # 2. Read top briefings and intentions
        briefings = _get_top_briefings(conn)
        intentions = _get_top_intentions(conn)

        # 3. Read active goals and nearest milestones (gracefully empty if sync hasn't run)
        active_goals = _get_active_goals(conn)
        nearest_milestones = _get_nearest_milestones(conn)

        # 4. Compute enriched state signals so Haiku can reason about new actions
        deadlines_within_5_days = _count_deadlines_within_5_days(data_dir)
        state_signals = {
            "overdue_milestones_count": _count_overdue_milestones(conn),
            "projects_missing_next_action": _count_projects_missing_next_action(conn),
            "stale_unsurfaced_briefings_count": _count_stale_unsurfaced_briefings(conn),
            "unreviewed_drafts_gt3d": _count_unreviewed_drafts_gt3d(conn),
            "vip_emails_pending": _count_vip_emails_pending(conn),
            "deadlines_within_5_days": deadlines_within_5_days,
            "deadline_countdown_created_today": _deadline_countdown_created_today(conn),
        }

        # 5. Compose situation JSON for Haiku
        situation_json = _compose_situation(ctx, briefings, intentions, active_goals,
                                            nearest_milestones, state_signals)

        # 6. Call Haiku
        raw_output, api_error, usage = _call_haiku(situation_json)

        # 7. Record cost (best-effort)
        if usage is not None:
            try:
                from agent.cost_tracking import record_call  # noqa: PLC0415
                record_call("executive", "claude-haiku-4-5-20251001", usage)
            except Exception:
                pass

        # 8. Parse output
        if api_error:
            action = "nothing"
            reasoning = f"Haiku API error: {api_error}"
            confidence = None
            parse_error = api_error
            raw_output = ""
            linked_goal_id = ""
        else:
            action, reasoning, confidence, parse_error, linked_goal_id = _parse_haiku_output(raw_output)

        log.info("Executive mode: %s, picked action: %s", mode, action)

        # 9. Attempt execution if mode allows it
        executed = 0
        execution_result: Optional[str] = None
        if mode in ("restricted_actor", "actor") and action in _ACTOR_ACTIONS:
            executed, execution_result = _execute_action(action, reasoning, confidence, ctx, conn)
        else:
            execution_result = f"advisor-only (mode={mode})"

        # 10. Insert decision row with final executed / execution_result
        _insert_decision(
            conn=conn,
            decided_at=decided_at,
            action=action,
            reasoning=reasoning,
            confidence=confidence,
            context_snapshot_json=situation_json,
            raw_haiku_output=raw_output,
            parse_error=parse_error,
            linked_goal_id=linked_goal_id,
            executed=executed,
            execution_result=execution_result,
        )

        conn.close()

    except Exception as e:
        # The executive must NEVER crash the scheduler
        try:
            conn.close()
        except Exception:
            pass
        return f"executive job error (logged, no crash): {e}"

    confidence_str = f"{confidence:.2f}" if confidence is not None else "n/a"
    exec_note = f" executed={executed}" if executed else ""
    return f"mode={mode} action={action} confidence={confidence_str}{exec_note}"


# ---------------------------------------------------------------------------
# Manual run helper
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print("Executive loop result:", result)
