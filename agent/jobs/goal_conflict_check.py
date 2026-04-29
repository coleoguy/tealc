"""Goal-conflict surfacing job — daily 7:15am Central.

Detects mismatches between Heath's recent activity and his goal portfolio.
Hard truths Tealc can surface that no human in his circle is positioned to.

Conflict types:
  stale_high_priority     — importance>=4 goal untouched for >7 days
  low_priority_overdriven — importance<=2 goal overworked while high-priority stales
  imminent_milestone_no_activity — milestone due within 14 days, parent goal cold ≥5 days
  service_drag_spike      — >30% of email triage decisions this week are service_request+accept
"""
import sqlite3
from datetime import datetime, timezone, timedelta

from agent.jobs import tracked
from agent.scheduler import DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_since(iso_str: str | None, reference: datetime) -> float:
    """Return fractional days between iso_str and reference. Returns very large if None."""
    if not iso_str:
        return 9999.0
    try:
        # Handle both naive and aware ISO strings
        ts = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (reference - dt).total_seconds() / 86400.0
    except Exception:
        return 9999.0


def _compute_days_since_touched(conn: sqlite3.Connection, goal_id: str, goal_created_iso: str, now: datetime) -> float:
    """Find the most recent activity touching this goal and return days since it."""
    candidates: list[str | None] = []

    # 1. Last executive_decision linked to this goal
    row = conn.execute(
        "SELECT MAX(decided_at) FROM executive_decisions WHERE linked_goal_id=?",
        (goal_id,),
    ).fetchone()
    candidates.append(row[0] if row else None)

    # 2. Last today_plan item linked to this goal
    row = conn.execute(
        "SELECT MAX(synced_at) FROM today_plan WHERE linked_goal_id=?",
        (goal_id,),
    ).fetchone()
    candidates.append(row[0] if row else None)

    # 3. Last milestone update for any milestone with this goal_id
    row = conn.execute(
        "SELECT MAX(last_touched_iso) FROM milestones_v2 WHERE goal_id=?",
        (goal_id,),
    ).fetchone()
    candidates.append(row[0] if row else None)

    # Also check synced_at on the goal itself (goal-level edits)
    row = conn.execute(
        "SELECT last_touched_iso, synced_at FROM goals WHERE id=?",
        (goal_id,),
    ).fetchone()
    if row:
        candidates.append(row[0])  # last_touched_iso
        candidates.append(row[1])  # synced_at

    # Filter valid candidates and return smallest days_since
    valid_days = [_days_since(ts, now) for ts in candidates if ts]
    if valid_days:
        return min(valid_days)

    # Never touched — fall back to goal creation date
    return _days_since(goal_created_iso, now)


def _count_touches_past_days(conn: sqlite3.Connection, goal_id: str, days: int, now: datetime) -> int:
    """Count activities touching this goal in the past N days."""
    cutoff = (now - timedelta(days=days)).isoformat()
    count = 0

    row = conn.execute(
        "SELECT COUNT(*) FROM executive_decisions WHERE linked_goal_id=? AND decided_at>=?",
        (goal_id, cutoff),
    ).fetchone()
    count += row[0] if row else 0

    row = conn.execute(
        "SELECT COUNT(*) FROM today_plan WHERE linked_goal_id=? AND synced_at>=?",
        (goal_id, cutoff),
    ).fetchone()
    count += row[0] if row else 0

    row = conn.execute(
        "SELECT COUNT(*) FROM milestones_v2 WHERE goal_id=? AND last_touched_iso>=?",
        (goal_id, cutoff),
    ).fetchone()
    count += row[0] if row else 0

    return count


def _already_detected_recently(conn: sqlite3.Connection, conflict_type: str, involved_ids: str, hours: int = 24) -> bool:
    """Return True if a conflict of the same type+goals was inserted within the past N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM goal_conflicts "
        "WHERE conflict_type=? AND involved_goal_ids=? AND detected_iso>=?",
        (conflict_type, involved_ids, cutoff),
    ).fetchone()
    return bool(row and row[0] > 0)


def _severity(conflict_type: str, days_stale: float = 0, days_until: float = 99) -> str:
    """Heuristic severity classification."""
    if conflict_type == "stale_high_priority":
        if days_stale > 14:
            return "high"
        return "med"
    if conflict_type == "low_priority_overdriven":
        return "med"
    if conflict_type == "imminent_milestone_no_activity":
        if days_until < 7:
            return "high"
        return "med"
    if conflict_type == "service_drag_spike":
        return "med"
    return "low"


@tracked("goal_conflict_check")
def job():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        now = datetime.now(timezone.utc)
        new_conflicts: list[dict] = []

        # ------------------------------------------------------------------ #
        # Pull active goals
        # ------------------------------------------------------------------ #
        goal_rows = conn.execute(
            "SELECT id, name, importance, synced_at FROM goals WHERE status='active'",
        ).fetchall()

        if not goal_rows:
            conn.close()
            return "conflicts: detected=0 new_briefing=no (no active goals)"

        # Pre-compute days_since_touched for each goal
        goal_info: list[dict] = []
        for gid, gname, gimportance, gsynced in goal_rows:
            importance = gimportance or 0
            days_stale = _compute_days_since_touched(conn, gid, gsynced or now.isoformat(), now)
            touches_7d = _count_touches_past_days(conn, gid, 7, now)
            goal_info.append({
                "id": gid,
                "name": gname,
                "importance": importance,
                "days_stale": days_stale,
                "touches_7d": touches_7d,
            })

        high_priority_goals = [g for g in goal_info if g["importance"] >= 4]
        low_priority_goals = [g for g in goal_info if g["importance"] <= 2]
        stale_high = [g for g in high_priority_goals if g["days_stale"] > 7]

        # ------------------------------------------------------------------ #
        # 1. stale_high_priority
        # ------------------------------------------------------------------ #
        for g in stale_high:
            gid = g["id"]
            days = g["days_stale"]
            if _already_detected_recently(conn, "stale_high_priority", gid):
                continue
            severity = "high" if days > 14 else "med"
            description = (
                f"High-priority goal '{g['name']}' (importance {g['importance']}/5) "
                f"has had no recorded activity for {days:.0f} days. "
                "No executive decisions, today-plan items, or milestone updates are linked to it."
            )
            recommendation = (
                "Explicitly schedule time for this goal this week or decide to defer it — "
                "but make that a conscious choice, not drift."
            )
            new_conflicts.append({
                "conflict_type": "stale_high_priority",
                "severity": severity,
                "involved_goal_ids": gid,
                "description": description,
                "recommendation": recommendation,
            })

        # ------------------------------------------------------------------ #
        # 2. low_priority_overdriven
        # ------------------------------------------------------------------ #
        if stale_high:
            stale_high_ids = ", ".join(g["id"] for g in stale_high)
            for g in low_priority_goals:
                if g["touches_7d"] <= 5:
                    continue
                gid = g["id"]
                combined_ids = f"{gid}|{stale_high_ids}"
                if _already_detected_recently(conn, "low_priority_overdriven", combined_ids):
                    continue
                description = (
                    f"Low-priority goal '{g['name']}' (importance {g['importance']}/5) "
                    f"received {g['touches_7d']} activity touches this past week — "
                    f"while {len(stale_high)} high-priority goal(s) ({stale_high_ids}) "
                    "sit untouched."
                )
                recommendation = (
                    "Review whether this time allocation reflects deliberate priorities "
                    "or unplanned drift toward easier/more comfortable work."
                )
                new_conflicts.append({
                    "conflict_type": "low_priority_overdriven",
                    "severity": "med",
                    "involved_goal_ids": combined_ids,
                    "description": description,
                    "recommendation": recommendation,
                })

        # ------------------------------------------------------------------ #
        # 3. imminent_milestone_no_activity
        # ------------------------------------------------------------------ #
        cutoff_14d = (now + timedelta(days=14)).isoformat()[:10]
        milestone_rows = conn.execute(
            """SELECT m.id, m.goal_id, m.milestone, m.target_iso, g.name, g.importance
               FROM milestones_v2 m
               JOIN goals g ON g.id = m.goal_id
               WHERE m.status IN ('pending', 'in_progress')
                 AND m.target_iso IS NOT NULL
                 AND m.target_iso <= ?
                 AND g.status = 'active'""",
            (cutoff_14d,),
        ).fetchall()

        for mid, goal_id, ms_name, target_iso, goal_name, importance in milestone_rows:
            # days until milestone
            try:
                target_dt = datetime.fromisoformat(target_iso + "T00:00:00+00:00")
                days_until = (target_dt - now).total_seconds() / 86400.0
            except Exception:
                continue

            # Check activity on parent goal in past 5 days
            goal_stale_5d = _compute_days_since_touched(conn, goal_id, now.isoformat(), now) > 5

            if not goal_stale_5d:
                continue

            conflict_id_key = f"{mid}:{goal_id}"
            if _already_detected_recently(conn, "imminent_milestone_no_activity", conflict_id_key):
                continue

            severity = "high" if days_until < 7 else "med"
            description = (
                f"Milestone '{ms_name}' on goal '{goal_name}' (importance {importance}/5) "
                f"is due in {days_until:.0f} days ({target_iso[:10]}) "
                "but the parent goal has had no activity in the past 5 days."
            )
            recommendation = (
                "Either act on this milestone this week or explicitly mark it "
                "as blocked/deferred so the deadline doesn't silently pass."
            )
            new_conflicts.append({
                "conflict_type": "imminent_milestone_no_activity",
                "severity": severity,
                "involved_goal_ids": conflict_id_key,
                "description": description,
                "recommendation": recommendation,
            })

        # ------------------------------------------------------------------ #
        # 4. service_drag_spike
        # ------------------------------------------------------------------ #
        cutoff_7d = (now - timedelta(days=7)).isoformat()
        triage_rows = conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN classification='service_request'
                   AND service_recommendation='accept' THEN 1 ELSE 0 END)
               FROM email_triage_decisions
               WHERE decided_at >= ?""",
            (cutoff_7d,),
        ).fetchone()

        total_triage = triage_rows[0] if triage_rows else 0
        service_accept = triage_rows[1] if (triage_rows and triage_rows[1]) else 0

        if total_triage >= 5:  # need enough data to be meaningful
            service_rate = service_accept / total_triage
            if service_rate > 0.30:
                spike_key = "service_drag_spike"
                if not _already_detected_recently(conn, "service_drag_spike", spike_key):
                    description = (
                        f"This week {service_accept} of {total_triage} email triage decisions "
                        f"({service_rate:.0%}) were classified as service requests with a "
                        "recommendation to accept — above the 30% alert threshold. "
                        "Cumulative service-request acceptance can quietly drain research time."
                    )
                    recommendation = (
                        "Review list_pending_service_requests and apply the NAS test: "
                        "does each acceptance directly advance research output or protect students? "
                        "If not, decline now rather than accumulate debt."
                    )
                    new_conflicts.append({
                        "conflict_type": "service_drag_spike",
                        "severity": "med",
                        "involved_goal_ids": spike_key,
                        "description": description,
                        "recommendation": recommendation,
                    })

        # ------------------------------------------------------------------ #
        # Insert new conflicts
        # ------------------------------------------------------------------ #
        detected_iso = now.isoformat()
        inserted = 0
        for c in new_conflicts:
            try:
                conn.execute(
                    """INSERT INTO goal_conflicts
                       (detected_iso, conflict_type, severity, involved_goal_ids,
                        description, recommendation)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        detected_iso,
                        c["conflict_type"],
                        c["severity"],
                        c["involved_goal_ids"],
                        c["description"],
                        c["recommendation"],
                    ),
                )
                inserted += 1
            except Exception:
                pass  # never crash

        conn.commit()

        # ------------------------------------------------------------------ #
        # Briefing for HIGH-severity conflicts
        # ------------------------------------------------------------------ #
        briefing_created = False
        high_conflicts = [c for c in new_conflicts if c["severity"] == "high"]

        if high_conflicts:
            lines = ["## Goal-portfolio conflicts detected\n"]
            for c in new_conflicts:  # include all in briefing, highlight high
                badge = " ⚠ HIGH" if c["severity"] == "high" else ""
                lines.append(f"**[{c['conflict_type']}]{badge}**")
                lines.append(c["description"])
                if c.get("recommendation"):
                    lines.append(f"_Recommendation: {c['recommendation']}_")
                lines.append("")
            lines.append("Use `list_goal_conflicts` to inspect all open conflicts.")
            content_md = "\n".join(lines)

            try:
                conn.execute(
                    "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
                    "VALUES ('goal_conflict', 'warn', 'Goal-portfolio conflict detected', ?, ?)",
                    (content_md, detected_iso),
                )
                conn.commit()
                briefing_created = True
            except Exception:
                pass

        conn.close()
        return (
            f"conflicts: detected={inserted} "
            f"new_briefing={'yes' if briefing_created else 'no'}"
        )

    except Exception as exc:
        # Catch-all: log but don't crash the scheduler
        try:
            import logging
            logging.getLogger("tealc.scheduler").error(
                "goal_conflict_check failed: %s", exc, exc_info=True
            )
        except Exception:
            pass
        return f"conflicts: error={exc}"
