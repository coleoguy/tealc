"""Rolling context-state snapshot job for Tealc.

Refreshes current_context table every 10 minutes with a single-row summary
of "what's happening right now" — unsurfaced briefings, pending intentions,
next deadline, students needing attention, hours since last chat, etc.

The Haiku executive loop reads this single cheap row instead of running
6+ separate queries on every wake.
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

from agent.jobs import tracked

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
_DEADLINES_PATH = os.path.join(_DATA, "deadlines.json")


def _compute_snapshot(conn: sqlite3.Connection) -> dict:
    """Compute all snapshot fields from the DB and data files."""
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # ------------------------------------------------------------------ #
    # 1. Unsurfaced briefings
    # ------------------------------------------------------------------ #
    unsurfaced_rows = conn.execute(
        "SELECT id, kind, urgency, title FROM briefings "
        "WHERE surfaced_at IS NULL ORDER BY created_at DESC"
    ).fetchall()
    unsurfaced_count = len(unsurfaced_rows)
    unsurfaced_top = json.dumps([
        {"id": r[0], "kind": r[1], "urgency": r[2], "title": r[3][:120]}
        for r in unsurfaced_rows[:5]
    ])

    # ------------------------------------------------------------------ #
    # 2. Pending intentions (high + critical only for top list)
    # ------------------------------------------------------------------ #
    pending_all = conn.execute(
        "SELECT COUNT(*) FROM intentions WHERE status IN ('pending', 'in_progress')"
    ).fetchone()[0]
    pending_top_rows = conn.execute(
        "SELECT id, kind, description, target_iso, priority FROM intentions "
        "WHERE status IN ('pending', 'in_progress') "
        "  AND priority IN ('high', 'critical') "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
        "  CASE WHEN target_iso IS NULL THEN 1 ELSE 0 END, target_iso "
        "LIMIT 5"
    ).fetchall()
    pending_top = json.dumps([
        {
            "id": r[0], "kind": r[1],
            "description": r[2][:120],
            "target_iso": r[3],
            "priority": r[4],
        }
        for r in pending_top_rows
    ])

    # ------------------------------------------------------------------ #
    # 3. Next deadline from data/deadlines.json
    # ------------------------------------------------------------------ #
    next_deadline_name = None
    next_deadline_iso = None
    next_deadline_days = None
    try:
        with open(_DEADLINES_PATH) as f:
            deadlines_data = json.load(f).get("deadlines", [])
        future = []
        for d in deadlines_data:
            due_str = d.get("due_iso")
            if not due_str:
                continue
            try:
                due_dt = datetime.fromisoformat(due_str)
                # Make timezone-aware if naive
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                if due_dt > now_utc:
                    future.append((due_dt, d.get("name", ""), due_str))
            except Exception:
                pass
        if future:
            future.sort(key=lambda x: x[0])
            soonest_dt, soonest_name, soonest_iso = future[0]
            next_deadline_name = soonest_name
            next_deadline_iso = soonest_iso
            next_deadline_days = (soonest_dt.date() - now_utc.date()).days
    except Exception:
        pass

    # ------------------------------------------------------------------ #
    # 4. Students needing attention
    # ------------------------------------------------------------------ #
    attention_names = []
    try:
        today = now_utc.date()
        active = conn.execute(
            "SELECT id, full_name FROM students WHERE status='active'"
        ).fetchall()
        for sid, full_name in active:
            flag = False
            # Check last interaction
            last_row = conn.execute(
                "SELECT occurred_iso FROM interactions WHERE student_id=? "
                "ORDER BY occurred_iso DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if last_row:
                try:
                    last_date = datetime.fromisoformat(
                        last_row[0].replace("Z", "+00:00")
                    ).date()
                    if (today - last_date).days >= 14:
                        flag = True
                except Exception:
                    pass
            else:
                flag = True  # no interactions at all

            if not flag:
                # Check milestones
                ms_rows = conn.execute(
                    "SELECT target_iso, completed_iso FROM milestones WHERE student_id=?",
                    (sid,),
                ).fetchall()
                for target_iso, completed_iso in ms_rows:
                    if completed_iso or not target_iso:
                        continue
                    try:
                        target_date = datetime.fromisoformat(target_iso).date()
                        days_until = (target_date - today).days
                        if days_until < 30:  # upcoming (<30 days) or overdue (<0)
                            flag = True
                            break
                    except Exception:
                        pass

            if flag:
                attention_names.append(full_name)
            if len(attention_names) >= 10:
                break
    except Exception:
        pass

    attention_count = len(attention_names)
    attention_names_json = json.dumps(attention_names)

    # ------------------------------------------------------------------ #
    # 5. Hours since last chat (proxy: UUID v6 timestamp from checkpoints)
    # ------------------------------------------------------------------ #
    # The LangGraph checkpointer uses UUID v6 for checkpoint_id.
    # UUID v6 encodes a 60-bit Gregorian timestamp (100ns since 1582-10-15).
    # We decode the most recent checkpoint_id to get the last chat time.
    # Fallback: rowid ordering is monotone, so MAX(rowid) gives the latest row.
    hours_since_last_chat = None
    GREGORIAN_EPOCH_DIFF = 122192928000000000  # 100ns intervals 1582-10-15 → 1970-01-01
    try:
        cid_row = conn.execute(
            "SELECT checkpoint_id FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if cid_row:
            cid = cid_row[0]
            u = uuid.UUID(cid)
            if u.version == 6:
                # Reconstruct time field from v6 layout:
                # Top 48 bits of int = time_high (gregorian ts >> 12)
                # Bits 49-52 = version nibble (skip)
                # Bits 53-64 = time_low (lower 12 bits)
                i = u.int
                time_high = i >> 80           # top 48 bits
                time_low = (i >> 64) & 0x0FFF  # 12 bits after version nibble
                time_100ns = (time_high << 12) | time_low
                unix_us = (time_100ns - GREGORIAN_EPOCH_DIFF) / 10  # → microseconds
                last_chat_utc = datetime.fromtimestamp(unix_us / 1e6, tz=timezone.utc)
                delta_h = (now_utc - last_chat_utc).total_seconds() / 3600.0
                hours_since_last_chat = round(delta_h, 2)
            elif u.version == 1:
                # Older UUID v1 support
                time_100ns = u.time
                unix_us = (time_100ns - GREGORIAN_EPOCH_DIFF) / 10
                last_chat_utc = datetime.fromtimestamp(unix_us / 1e6, tz=timezone.utc)
                delta_h = (now_utc - last_chat_utc).total_seconds() / 3600.0
                hours_since_last_chat = round(delta_h, 2)
    except Exception:
        hours_since_last_chat = None

    # ------------------------------------------------------------------ #
    # 6. Open grant opportunities
    # ------------------------------------------------------------------ #
    open_grants = conn.execute(
        "SELECT COUNT(*) FROM grant_opportunities "
        "WHERE dismissed=0 AND (deadline_iso IS NULL OR deadline_iso > ?)",
        (now_iso[:10],),
    ).fetchone()[0]

    # ------------------------------------------------------------------ #
    # 7. Local time / working hours
    # ------------------------------------------------------------------ #
    import zoneinfo
    central = zoneinfo.ZoneInfo("America/Chicago")
    local_now = now_utc.astimezone(central)
    local_hour = local_now.hour
    local_day = local_now.strftime("%a").lower()[:3]  # 'mon', 'tue', ...
    is_working = (
        1
        if local_now.weekday() < 5 and 9 <= local_hour < 18
        else 0
    )

    # ------------------------------------------------------------------ #
    # 8. Derived idle classification
    # ------------------------------------------------------------------ #
    hours = hours_since_last_chat or 0
    if hours < 0.5:
        idle_class = "active"       # Heath is in chat now or just was
    elif hours < 4:
        idle_class = "engaged"      # working day, away briefly
    elif hours < 24:
        idle_class = "idle"         # overnight or away most of day
    else:
        idle_class = "deep_idle"    # weekend, vacation, multi-day silence

    # ------------------------------------------------------------------ #
    # 9. Human-readable notes line
    # ------------------------------------------------------------------ #
    notes_parts = []
    if unsurfaced_count:
        notes_parts.append(f"{unsurfaced_count} unsurfaced briefing(s)")
    if pending_all:
        notes_parts.append(f"{pending_all} pending intention(s)")
    if next_deadline_name:
        notes_parts.append(f"next deadline: {next_deadline_name} in {next_deadline_days}d")
    if attention_count:
        notes_parts.append(f"{attention_count} student(s) need attention")
    if hours_since_last_chat is not None:
        notes_parts.append(f"idle {hours_since_last_chat:.1f}h")
    notes = "; ".join(notes_parts) if notes_parts else "all clear"

    return {
        "refreshed_at": now_iso,
        "unsurfaced_briefings_count": unsurfaced_count,
        "unsurfaced_briefings_top": unsurfaced_top,
        "pending_intentions_count": pending_all,
        "pending_intentions_top": pending_top,
        "next_deadline_name": next_deadline_name,
        "next_deadline_iso": next_deadline_iso,
        "next_deadline_days_remaining": next_deadline_days,
        "students_needing_attention_count": attention_count,
        "students_needing_attention_names": attention_names_json,
        "hours_since_last_chat": hours_since_last_chat,
        "open_grant_opportunities_count": open_grants,
        "current_local_hour": local_hour,
        "current_local_day": local_day,
        "is_working_hours": is_working,
        "idle_class": idle_class,
        "notes": notes,
    }


@tracked("refresh_context")
def job():
    """Compute and upsert the rolling context snapshot into current_context."""
    from agent.scheduler import DB_PATH  # noqa: PLC0415

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    snapshot = _compute_snapshot(conn)

    conn.execute(
        """
        INSERT OR REPLACE INTO current_context (
            id,
            refreshed_at,
            unsurfaced_briefings_count,
            unsurfaced_briefings_top,
            pending_intentions_count,
            pending_intentions_top,
            next_deadline_name,
            next_deadline_iso,
            next_deadline_days_remaining,
            students_needing_attention_count,
            students_needing_attention_names,
            hours_since_last_chat,
            open_grant_opportunities_count,
            current_local_hour,
            current_local_day,
            is_working_hours,
            idle_class,
            notes
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot["refreshed_at"],
            snapshot["unsurfaced_briefings_count"],
            snapshot["unsurfaced_briefings_top"],
            snapshot["pending_intentions_count"],
            snapshot["pending_intentions_top"],
            snapshot["next_deadline_name"],
            snapshot["next_deadline_iso"],
            snapshot["next_deadline_days_remaining"],
            snapshot["students_needing_attention_count"],
            snapshot["students_needing_attention_names"],
            snapshot["hours_since_last_chat"],
            snapshot["open_grant_opportunities_count"],
            snapshot["current_local_hour"],
            snapshot["current_local_day"],
            snapshot["is_working_hours"],
            snapshot["idle_class"],
            snapshot["notes"],
        ),
    )
    conn.commit()
    conn.close()

    b = snapshot["unsurfaced_briefings_count"]
    i = snapshot["pending_intentions_count"]
    h = snapshot["hours_since_last_chat"]
    h_str = f"{h:.1f}" if h is not None else "?"
    return f"refreshed: briefings={b} intentions={i} hours_idle={h_str}"


if __name__ == "__main__":
    job()
    print("refresh_context job completed.")
