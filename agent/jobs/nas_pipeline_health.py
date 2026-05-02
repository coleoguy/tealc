"""NAS pipeline health briefing — runs every Sunday at 6:30pm Central via APScheduler.

Recommended schedule:
    CronTrigger(day_of_week="sun", hour=18, minute=30, timezone="America/Chicago")

Fires BEFORE weekly_review (7pm) so its single-highest-leverage action
recommendation can inform that summary.

Computes a citation-trajectory health score against a 3500-citation target by
end of 2027, calls Sonnet for a concise gap-analysis briefing, and writes it
to the briefings table with kind='nas_pipeline_health'.

Run manually to test:
    python -m agent.jobs.nas_pipeline_health
"""
import os
import sqlite3
from datetime import date, datetime, timezone

from dotenv import load_dotenv

# Load .env from project root (two levels up from agent/jobs/)
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402 (after load_dotenv)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

# ---------------------------------------------------------------------------
# Constants — trajectory target
# ---------------------------------------------------------------------------

# Target: 3500 total citations by 2028-01-01.
# Rationale: Heath currently has ~2001 citations (April 2026).  NAS members in
# evolutionary/genomics typically exceed 5000–8000, but a step-function target
# of 3500 by 2028 is aggressive-but-reachable and keeps the weekly nudge
# meaningful.  Adjust TARGET_CITATIONS or TARGET_DATE as Heath's profile grows.
TARGET_CITATIONS = 3500
TARGET_DATE = date(2028, 1, 1)

_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# NAS gap notes (from agent/graph.py SYSTEM_PROMPT) — baked in so the Sonnet
# prompt is self-contained even without a graph.py read at runtime.
# ---------------------------------------------------------------------------
_GAP_NOTES = """\
Heath's own NAS gap assessment (from profile):
- Need more high-impact papers (Current Biology, PNAS, Nature Comms minimum; CNS ideal)
- Need broader citation visibility — current work is underappreciated relative to scope
- Need stronger international profile: invited talks at Evolution, SMBE, Gordon Conferences,
  international collaborations, invited reviews in top journals
- NOT interested in field leadership / society officer roles — protect research time
- Current flagship: "Dismantling chromosomal stasis" preprint (April 2026) — needs CNS home
- Admin burden: Associate Dept Head + EEB Chair — service is already maxed out"""

# ---------------------------------------------------------------------------
# System prompt for Sonnet
# ---------------------------------------------------------------------------
from agent.jobs import SCIENTIST_MODE  # noqa: E402

_PIPELINE_SYSTEM = SCIENTIST_MODE + "\n\n" + """\
You are a career-strategy advisor looking at the researcher's national-recognition \
trajectory. Given the metrics and recent activity, produce one concise briefing \
in markdown with these sections:
- **Current state** (1-2 lines on citations, h-index, trajectory)
- **Gap to close** (name the specific gap — e.g. CNS-tier publications, \
international visibility, invited talks)
- **This week's single highest-leverage action** (one concrete suggestion — NOT a list)
- **Confidence** (low/med/high in your recommendation, and one observation \
that would change it)
- **Dismiss this week if** (one tripwire — "skip this suggestion if X is true", \
preventing advice that ignores Heath's current priorities)

Total under 300 words. No cheerleading. Be quantitative. If the data don't \
support a recommendation, say so rather than padding."""


# ---------------------------------------------------------------------------
# Trajectory helpers
# ---------------------------------------------------------------------------

def _weeks_remaining(today: date) -> float:
    """Weeks from today until TARGET_DATE (always positive; floor 1)."""
    delta = (TARGET_DATE - today).days
    return max(delta / 7.0, 1.0)


def _trajectory_score(snapshots: list[dict]) -> tuple[float, float, float, float]:
    """Return (trajectory_pct, actual_rate, target_rate, current_citations).

    Uses the last 4 weeks of citation-count deltas.  If fewer than 2 snapshots
    exist, returns (0, 0, 0, 0) to signal insufficient_data.

    Formula:
        target_rate = (TARGET_CITATIONS - current) / weeks_remaining
        actual_rate = mean weekly delta over last ≤4 snapshots
        trajectory_pct = actual_rate / target_rate * 100
    """
    if len(snapshots) < 2:
        return 0.0, 0.0, 0.0, 0.0

    today = date.today()
    current_citations = snapshots[0]["total_citations"] or 0

    # Compute week-over-week deltas (newest first, so delta = snap[i] - snap[i+1])
    window = snapshots[:5]  # up to 5 rows → up to 4 deltas
    deltas = []
    for i in range(len(window) - 1):
        newer = window[i]["total_citations"] or 0
        older = window[i + 1]["total_citations"] or 0
        delta = newer - older
        if delta >= 0:  # ignore negative deltas (data anomalies)
            deltas.append(float(delta))

    if not deltas:
        return 0.0, 0.0, 0.0, float(current_citations)

    actual_rate = sum(deltas) / len(deltas)
    target_rate = (TARGET_CITATIONS - current_citations) / _weeks_remaining(today)

    if target_rate <= 0:
        # Already past target — score it at 100%
        return 100.0, actual_rate, target_rate, float(current_citations)

    trajectory_pct = actual_rate / target_rate * 100.0
    return trajectory_pct, actual_rate, target_rate, float(current_citations)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_snapshots(conn: sqlite3.Connection) -> list[dict]:
    """Return up to 6 most-recent nas_metrics rows, newest first."""
    try:
        rows = conn.execute(
            "SELECT snapshot_iso, total_citations, citations_since_2021, "
            "h_index, i10_index, works_count "
            "FROM nas_metrics ORDER BY snapshot_iso DESC LIMIT 6"
        ).fetchall()
        return [
            {
                "snapshot_iso": r[0],
                "total_citations": r[1],
                "citations_since_2021": r[2],
                "h_index": r[3],
                "i10_index": r[4],
                "works_count": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_pipeline_health] load_snapshots error: {e}")
        return []


def _load_latest_impact(conn: sqlite3.Connection) -> dict | None:
    """Return the most-recent nas_impact_weekly row, or None."""
    try:
        row = conn.execute(
            "SELECT week_start_iso, nas_trajectory_pct, service_drag_pct, "
            "total_activity_count "
            "FROM nas_impact_weekly ORDER BY week_start_iso DESC LIMIT 1"
        ).fetchone()
        if row:
            return {
                "week_start_iso": row[0],
                "nas_trajectory_pct": row[1],
                "service_drag_pct": row[2],
                "total_activity_count": row[3],
            }
        return None
    except Exception as e:
        print(f"[nas_pipeline_health] load_impact error: {e}")
        return None


def _load_high_goals(conn: sqlite3.Connection) -> list[str]:
    """Return names of active goals with nas_relevance='high'."""
    try:
        rows = conn.execute(
            "SELECT name FROM goals "
            "WHERE nas_relevance='high' AND (status IS NULL OR status != 'done') "
            "ORDER BY importance DESC"
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        print(f"[nas_pipeline_health] load_goals error: {e}")
        return []


def _fetch_cns_paper_count(author_openalex_id: str) -> int:
    """Query OpenAlex for Heath's papers in CNS-tier journals in the past 2 years.

    Uses a concept/venue filter compatible with the OpenAlex works API.
    Returns count; returns -1 if OpenAlex is unreachable.
    """
    try:
        import requests
        two_years_ago = date(date.today().year - 2, date.today().month, date.today().day).isoformat()
        raw_id = author_openalex_id.replace("https://openalex.org/", "")

        # High-impact journal OpenAlex source IDs (stable identifiers avoid
        # free-text search issues).  Fallback: use journal display_name filter
        # with pipe-separated values which OpenAlex supports natively.
        high_impact_sources = (
            "S3880285|"     # Science
            "S137773608|"   # Nature
            "S178527|"      # Cell
            "S3401009|"     # PNAS
            "S111940168|"   # Current Biology
            "S7247153"      # Nature Communications
        )
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "filter": (
                    f"author.id:{raw_id},"
                    f"from_publication_date:{two_years_ago},"
                    f"primary_location.source.id:{high_impact_sources}"
                ),
                "per_page": 1,
                "select": "id",
                "mailto": os.environ.get("RESEARCHER_EMAIL", "researcher@example.org"),
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("meta", {}).get("count", 0)
    except Exception as e:
        print(f"[nas_pipeline_health] OpenAlex CNS count error (non-fatal): {e}")
        return -1


def _get_author_openalex_id(conn: sqlite3.Connection) -> str:
    """Extract OpenAlex author ID from the latest raw_author_json in nas_metrics."""
    try:
        import json
        row = conn.execute(
            "SELECT raw_author_json FROM nas_metrics ORDER BY snapshot_iso DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            author = json.loads(row[0])
            return author.get("id", "")
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("nas_pipeline_health")
def job() -> str:
    """Compute NAS pipeline health and write a single-action briefing."""
    now_utc = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # 1. Load data                                                         #
    # ------------------------------------------------------------------ #
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        snapshots = _load_snapshots(conn)
        latest_impact = _load_latest_impact(conn)
        high_goals = _load_high_goals(conn)
        author_oa_id = _get_author_openalex_id(conn)

        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"nas_pipeline_health: data-gather failed: {e}"

    # ------------------------------------------------------------------ #
    # 2. Gate on minimum data                                              #
    # ------------------------------------------------------------------ #
    if len(snapshots) < 2:
        return "insufficient_data"

    # ------------------------------------------------------------------ #
    # 3. Trajectory score                                                  #
    # ------------------------------------------------------------------ #
    trajectory_pct, actual_rate, target_rate, current_citations = _trajectory_score(snapshots)

    # ------------------------------------------------------------------ #
    # 4. CNS paper count (best-effort via OpenAlex)                        #
    # ------------------------------------------------------------------ #
    cns_count = _fetch_cns_paper_count(author_oa_id) if author_oa_id else -1
    cns_note = (
        f"{cns_count} papers in CNS-tier journals in the past 2 years"
        if cns_count >= 0
        else "CNS-tier paper count unavailable (OpenAlex unreachable)"
    )

    # ------------------------------------------------------------------ #
    # 5. Build Sonnet user message                                         #
    # ------------------------------------------------------------------ #
    latest = snapshots[0]
    impact_note = (
        f"Last NAS-impact week ({latest_impact['week_start_iso']}): "
        f"{latest_impact['nas_trajectory_pct']:.0f}% trajectory activity, "
        f"{latest_impact['service_drag_pct']:.0f}% service drag, "
        f"{latest_impact['total_activity_count']} items"
        if latest_impact
        else "No nas_impact_weekly data available."
    )

    weeks_left = _weeks_remaining(date.today())
    user_msg = f"""NAS PIPELINE HEALTH — {now_utc.strftime('%Y-%m-%d')}

CITATION METRICS (latest snapshot: {latest['snapshot_iso']}):
- Total citations: {int(current_citations):,}
- H-index: {latest['h_index']}  |  i10-index: {latest['i10_index']}
- Works count: {latest['works_count']}
- Citations since 2021: {latest['citations_since_2021']:,}

TRAJECTORY SCORE: {trajectory_pct:.0f}% on track
- Actual citation rate (mean, last ≤4 weeks): {actual_rate:.1f} citations/week
- Required rate to hit {TARGET_CITATIONS:,} citations by {TARGET_DATE}: {target_rate:.1f}/week
- Weeks remaining to target: {weeks_left:.0f}

HIGH-IMPACT PAPERS (past 2 years):
- {cns_note}

ACTIVE HIGH-NAS-RELEVANCE GOALS:
{chr(10).join(f'  - {g}' for g in high_goals) if high_goals else '  (none tagged)'}

WEEKLY ACTIVITY QUALITY:
{impact_note}

GAP NOTES:
{_GAP_NOTES}
"""

    # ------------------------------------------------------------------ #
    # 6. Call Sonnet                                                       #
    # ------------------------------------------------------------------ #
    try:
        client = Anthropic()
        response = client.messages.create(
            model=_MODEL,
            max_tokens=600,
            system=_PIPELINE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        content_md: str = response.content[0].text

        # 7. Record cost (best-effort)
        try:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            record_call("nas_pipeline_health", _MODEL, usage)
        except Exception as cost_err:
            print(f"[nas_pipeline_health] cost_tracking error (non-fatal): {cost_err}")

    except Exception as e:
        # Fall back to a plain-text summary so we still write a briefing
        content_md = (
            f"[nas_pipeline_health] Sonnet call failed: {e}\n\n"
            f"**Current state**: {int(current_citations):,} citations, "
            f"h-index {latest['h_index']}, trajectory {trajectory_pct:.0f}% on track.\n\n"
            f"**Target**: {TARGET_CITATIONS:,} citations by {TARGET_DATE} "
            f"({target_rate:.1f} citations/week needed; actual {actual_rate:.1f}/week).\n\n"
            f"**Gap**: See NAS gap notes in profile."
        )

    # ------------------------------------------------------------------ #
    # 8. Insert briefing                                                   #
    # ------------------------------------------------------------------ #
    title = f"NAS pipeline health: {trajectory_pct:.0f}% on track"
    try:
        conn2 = sqlite3.connect(DB_PATH)
        conn2.execute("PRAGMA journal_mode=WAL")
        conn2.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('nas_pipeline_health', 'info', ?, ?, ?)",
            (title, content_md, now_utc.isoformat()),
        )
        conn2.commit()
        conn2.close()
    except Exception as e:
        try:
            conn2.close()
        except Exception:
            pass
        return f"nas_pipeline_health: DB write failed: {e}"

    # ------------------------------------------------------------------ #
    # 9. Return summary string                                             #
    # ------------------------------------------------------------------ #
    return f"surfaced: trajectory={trajectory_pct:.0f}%"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
