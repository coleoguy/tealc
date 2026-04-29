"""Monthly NAS case packet — generates a shareable Google Doc snapshot of
Heath's NAS case for chairs, letter writers, and program officers.

Recommended schedule (Wave 3 agent registers):
    CronTrigger(day="1-7", day_of_week="sun", hour=10, minute=0,
                timezone="America/Chicago")
    — First Sunday of each month at 10am Central.

Run manually to test:
    python -m agent.jobs.nas_case_packet
"""
import json
import os
import sqlite3
from datetime import date, datetime, timezone

import requests
from dotenv import load_dotenv

# Load .env from project root (two levels up from agent/jobs/)
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402
from agent.ledger import record_output  # noqa: E402

_MODEL = "claude-sonnet-4-6"
_MAILTO = "blackmon@tamu.edu"

# ---------------------------------------------------------------------------
# Plot directory
# ---------------------------------------------------------------------------
_DATA_DIR = os.path.normpath(os.path.join(_PROJECT_ROOT, "data", "nas_case_plots"))

# ---------------------------------------------------------------------------
# System prompt for the narrative
# ---------------------------------------------------------------------------
_NARRATIVE_SYSTEM = """\
You draft a one-page NAS case summary in Heath Blackmon's voice: direct, \
quantitative, no hedging. Structure in markdown:

# NAS Case Packet — {name} — {YYYY-MM}

## Current state
Bulleted key metrics (citations, h-index, works, recent impact).

## Top publications (cited-by count, field-normalized percentile when available)
Numbered list of top-10 papers with short 1-line significance each.

## Recent high-visibility activity
What happened in the past 30-90 days worth noting — new papers, invited talks \
(if inferable), citations by notable papers.

## Trajectory narrative
2-3 paragraphs: where Heath is, where he's headed, what distinguishes his case. \
Use Nature/Science/Cell-tier publications as visible markers. Do not invent \
facts not in the data.

## What the next 90 days should deliver
3-5 bullets: the specific outputs that would strengthen the case before next packet.

Keep total under 1000 words."""


# ---------------------------------------------------------------------------
# OpenAlex helper
# ---------------------------------------------------------------------------
def _openalex_get(url: str, params: dict) -> dict:
    """GET an OpenAlex endpoint; raises on HTTP error; returns parsed JSON."""
    params.setdefault("mailto", _MAILTO)
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_latest_nas_metrics(conn: sqlite3.Connection) -> dict | None:
    """Return the most-recent nas_metrics row as a dict, or None."""
    try:
        row = conn.execute(
            "SELECT snapshot_iso, total_citations, citations_since_2021, "
            "h_index, i10_index, works_count, raw_author_json "
            "FROM nas_metrics ORDER BY snapshot_iso DESC LIMIT 1"
        ).fetchone()
        if row:
            return {
                "snapshot_iso": row[0],
                "total_citations": row[1],
                "citations_since_2021": row[2],
                "h_index": row[3],
                "i10_index": row[4],
                "works_count": row[5],
                "raw_author_json": row[6],
            }
        return None
    except Exception as e:
        print(f"[nas_case_packet] load_latest_nas_metrics error: {e}")
        return None


def _load_12mo_snapshots(conn: sqlite3.Connection) -> list[dict]:
    """Return up to 12 months of nas_metrics snapshots, newest first."""
    try:
        rows = conn.execute(
            "SELECT snapshot_iso, total_citations "
            "FROM nas_metrics ORDER BY snapshot_iso DESC LIMIT 365"
        ).fetchall()
        # Down-sample to roughly monthly (take every ~30th row or all if <13)
        all_rows = [{"snapshot_iso": r[0], "total_citations": r[1]} for r in rows]
        if len(all_rows) <= 13:
            return all_rows
        # Sample: always include newest; take every nth to cover 12 months
        step = max(1, len(all_rows) // 12)
        sampled = [all_rows[i] for i in range(0, len(all_rows), step)][:13]
        return sampled
    except Exception as e:
        print(f"[nas_case_packet] load_12mo_snapshots error: {e}")
        return []


def _load_recent_impact(conn: sqlite3.Connection) -> list[dict]:
    """Return last 13 weeks (~3 months) of nas_impact_weekly rows."""
    try:
        rows = conn.execute(
            "SELECT week_start_iso, nas_trajectory_pct, service_drag_pct, "
            "total_activity_count "
            "FROM nas_impact_weekly ORDER BY week_start_iso DESC LIMIT 13"
        ).fetchall()
        return [
            {
                "week_start_iso": r[0],
                "nas_trajectory_pct": r[1],
                "service_drag_pct": r[2],
                "total_activity_count": r[3],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[nas_case_packet] load_recent_impact error: {e}")
        return []


def _load_last_retro(conn: sqlite3.Connection) -> str | None:
    """Return the content_md of the last quarterly_retrospective briefing, if any."""
    try:
        row = conn.execute(
            "SELECT content_md FROM briefings "
            "WHERE kind='quarterly_retrospective' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _count_high_nas_goals(conn: sqlite3.Connection) -> int:
    """Count active goals with nas_relevance='high'."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM goals "
            "WHERE nas_relevance='high' AND (status IS NULL OR status != 'done')"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _fetch_top10_papers(author_openalex_id: str) -> list[dict]:
    """Pull Heath's top-10 most-cited papers via OpenAlex."""
    try:
        raw_id = author_openalex_id.replace("https://openalex.org/", "")
        data = _openalex_get(
            "https://api.openalex.org/works",
            {
                "filter": f"author.id:{raw_id}",
                "sort": "cited_by_count:desc",
                "per_page": 10,
                "select": (
                    "id,title,publication_year,cited_by_count,"
                    "primary_location,fwci"
                ),
            },
        )
        papers = []
        for work in data.get("results", []):
            loc = work.get("primary_location") or {}
            source = loc.get("source") or {}
            journal = source.get("display_name") or "Unknown journal"
            fwci = work.get("fwci")
            papers.append({
                "title": (work.get("title") or "Untitled")[:200],
                "year": work.get("publication_year"),
                "citations": work.get("cited_by_count") or 0,
                "journal": journal,
                "fwci": fwci,
                "url": work.get("id") or "",
            })
        return papers
    except Exception as e:
        print(f"[nas_case_packet] fetch_top10_papers error (non-fatal): {e}")
        return []


# ---------------------------------------------------------------------------
# Plot generation (best-effort)
# ---------------------------------------------------------------------------

def _generate_plot(snapshots: list[dict], month_str: str, author_name: str) -> str | None:
    """Generate a citation-trajectory PNG. Returns path on success, None on failure."""
    try:
        from agent.python_runtime.executor import run_python  # type: ignore
    except ImportError:
        try:
            # Fallback: try direct matplotlib
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            dates = [s["snapshot_iso"] for s in reversed(snapshots)]
            citations = [s["total_citations"] or 0 for s in reversed(snapshots)]

            os.makedirs(_DATA_DIR, exist_ok=True)
            plot_path = os.path.join(_DATA_DIR, f"{month_str}.png")

            fig, ax = plt.subplots(figsize=(9, 4))
            ax.plot(dates, citations, marker="o", linewidth=2, color="#1a73e8")
            ax.set_title(f"Citation trajectory — {author_name}", fontsize=13)
            ax.set_xlabel("Snapshot date")
            ax.set_ylabel("Cumulative citations")
            ax.tick_params(axis="x", rotation=45)
            fig.tight_layout()
            fig.savefig(plot_path, dpi=120)
            plt.close(fig)
            return plot_path
        except Exception as e:
            print(f"[nas_case_packet] direct matplotlib plot failed (non-fatal): {e}")
            return None

    # agent.python_runtime path
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        plot_path = os.path.join(_DATA_DIR, f"{month_str}.png")

        dates = [s["snapshot_iso"] for s in reversed(snapshots)]
        citations = [s["total_citations"] or 0 for s in reversed(snapshots)]

        script = f"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

dates = {json.dumps(dates)}
citations = {json.dumps(citations)}
plot_path = {json.dumps(plot_path)}

fig, ax = plt.subplots(figsize=(9, 4))
ax.plot(dates, citations, marker="o", linewidth=2, color="#1a73e8")
ax.set_title("Citation trajectory — {author_name}", fontsize=13)
ax.set_xlabel("Snapshot date")
ax.set_ylabel("Cumulative citations")
ax.tick_params(axis="x", rotation=45)
fig.tight_layout()
fig.savefig(plot_path, dpi=120)
plt.close(fig)
print("saved:", plot_path)
"""
        result = run_python(script)
        if "saved:" in str(result):
            return plot_path
        return None
    except Exception as e:
        print(f"[nas_case_packet] run_python plot failed (non-fatal): {e}")
        return None


# ---------------------------------------------------------------------------
# Build the user message for Sonnet
# ---------------------------------------------------------------------------

def _build_user_message(
    latest: dict,
    snapshots: list[dict],
    top10: list[dict],
    impact_rows: list[dict],
    last_retro: str | None,
    high_nas_goals: int,
    month_str: str,
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Trend summary (first vs last in snapshot window)
    trend_note = ""
    if len(snapshots) >= 2:
        oldest = snapshots[-1]
        newest = snapshots[0]
        delta = (newest["total_citations"] or 0) - (oldest["total_citations"] or 0)
        trend_note = (
            f"Citations gained over tracked window "
            f"({oldest['snapshot_iso']} → {newest['snapshot_iso']}): +{delta}"
        )

    # Impact summary
    if impact_rows:
        avg_traj = sum(r["nas_trajectory_pct"] or 0 for r in impact_rows) / len(impact_rows)
        avg_drag = sum(r["service_drag_pct"] or 0 for r in impact_rows) / len(impact_rows)
        impact_note = (
            f"Past {len(impact_rows)} weeks avg: "
            f"{avg_traj:.0f}% NAS-trajectory activity, "
            f"{avg_drag:.0f}% service drag."
        )
    else:
        impact_note = "No weekly impact data available."

    # Top papers block
    papers_block_lines = []
    for i, p in enumerate(top10, 1):
        fwci_str = f", FWCI={p['fwci']:.2f}" if p.get("fwci") is not None else ""
        papers_block_lines.append(
            f"{i}. {p['title']} ({p['year']}) — "
            f"{p['citations']:,} citations, {p['journal']}{fwci_str}"
        )
    papers_block = "\n".join(papers_block_lines) if papers_block_lines else "(unavailable)"

    retro_section = (
        f"\nLAST QUARTERLY RETROSPECTIVE EXCERPT:\n{last_retro[:800]}"
        if last_retro
        else ""
    )

    return f"""NAS CASE PACKET DATA — {month_str} (generated {now_str})

AUTHOR: Heath Blackmon

CURRENT METRICS (snapshot: {latest['snapshot_iso']}):
- Total citations: {latest['total_citations']:,}
- H-index: {latest['h_index']}
- i10-index: {latest['i10_index']}
- Works count: {latest['works_count']}
- Citations since 2021: {latest['citations_since_2021']:,}
{trend_note}

WEEKLY ACTIVITY QUALITY:
{impact_note}

ACTIVE HIGH-NAS-RELEVANCE GOALS: {high_nas_goals}

TOP 10 PAPERS BY CITATION COUNT:
{papers_block}
{retro_section}

Please produce the NAS Case Packet document as described. Do not invent statistics \
not present in the data above. Fill in "What the next 90 days should deliver" based \
on the current gaps visible in the metrics.
"""


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@tracked("nas_case_packet")
def job() -> str:
    """Generate a monthly NAS case packet Google Doc for Heath Blackmon."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("nas_case_packet"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    now_utc = datetime.now(timezone.utc)
    today = date.today()
    month_str = today.strftime("%Y-%m")
    month_start_iso = today.replace(day=1).isoformat()

    # ------------------------------------------------------------------
    # 1. Idempotency — one packet per month
    # ------------------------------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = conn.execute(
            "SELECT id FROM briefings "
            "WHERE kind='nas_case_packet' AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (month_start_iso,),
        ).fetchone()
        conn.close()
        if existing:
            return "already_generated_this_month"
    except Exception as e:
        print(f"[nas_case_packet] idempotency check error: {e}")
        # Proceed anyway — better to generate a duplicate than to silently skip.

    # ------------------------------------------------------------------
    # 2. Pull data
    # ------------------------------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        latest = _load_latest_nas_metrics(conn)
        snapshots = _load_12mo_snapshots(conn)
        impact_rows = _load_recent_impact(conn)
        last_retro = _load_last_retro(conn)
        high_nas_goals = _count_high_nas_goals(conn)

        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return f"nas_case_packet: data-gather failed: {e}"

    if latest is None:
        return "nas_case_packet: no nas_metrics data available yet"

    # Extract OpenAlex ID from raw_author_json for top-10 fetch
    author_openalex_id = ""
    author_name = "Heath Blackmon"
    try:
        raw_author = json.loads(latest.get("raw_author_json") or "{}")
        author_openalex_id = raw_author.get("id", "")
        author_name = raw_author.get("display_name", "Heath Blackmon")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 2c. Top-10 papers via OpenAlex
    # ------------------------------------------------------------------
    top10 = _fetch_top10_papers(author_openalex_id) if author_openalex_id else []

    # ------------------------------------------------------------------
    # 3. Generate plot (best-effort; packet ships regardless)
    # ------------------------------------------------------------------
    plot_path: str | None = None
    if len(snapshots) >= 2:
        plot_path = _generate_plot(snapshots, month_str, author_name)

    # ------------------------------------------------------------------
    # 4. Generate narrative via Sonnet
    # ------------------------------------------------------------------
    user_msg = _build_user_message(
        latest=latest,
        snapshots=snapshots,
        top10=top10,
        impact_rows=impact_rows,
        last_retro=last_retro,
        high_nas_goals=high_nas_goals,
        month_str=month_str,
    )

    client = Anthropic()
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=1200,
            system=_NARRATIVE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        narrative: str = response.content[0].text.strip()
    except Exception as e:
        return f"nas_case_packet: Sonnet call failed: {e}"

    # Append plot note to narrative if plot was generated
    if plot_path:
        narrative += (
            f"\n\n---\n_Citation trajectory plot saved to: `{plot_path}`_"
        )

    # 4a. Record Anthropic cost (best-effort)
    try:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        record_call("nas_case_packet", _MODEL, usage)
    except Exception as cost_err:
        print(f"[nas_case_packet] cost_tracking error (non-fatal): {cost_err}")

    # ------------------------------------------------------------------
    # 5. Create Google Doc
    # ------------------------------------------------------------------
    doc_title = f"Tealc — Heath Blackmon NAS case — {month_str}"
    try:
        from agent.tools import create_google_doc  # noqa: PLC0415
        doc_result = create_google_doc.invoke({"title": doc_title, "body_markdown": narrative})
    except Exception as e:
        return f"nas_case_packet: Google Doc creation failed: {e}"

    doc_id: str = ""
    doc_url: str = ""
    if isinstance(doc_result, str) and "|" in doc_result:
        doc_id, doc_url = doc_result.split("|", 1)
    else:
        # Drive not connected or unexpected return — still record locally
        doc_id = ""
        doc_url = str(doc_result)

    # ------------------------------------------------------------------
    # 6. Plot is already saved locally (step 3); no embedding needed in v1
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 7. Record to output_ledger
    # ------------------------------------------------------------------
    provenance = {
        "doc_id": doc_id,
        "doc_url": doc_url,
        "plot_path": plot_path or "",
        "month": month_str,
        "citation_count": latest.get("total_citations"),
        "h_index": latest.get("h_index"),
        "works_count": latest.get("works_count"),
    }
    try:
        record_output(
            kind="nas_case_packet",
            job_name="nas_case_packet",
            model=_MODEL,
            project_id=None,
            content_md=narrative,
            tokens_in=getattr(response.usage, "input_tokens", 0) or 0,
            tokens_out=getattr(response.usage, "output_tokens", 0) or 0,
            provenance=provenance,
        )
    except Exception as e:
        print(f"[nas_case_packet] output_ledger error (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 8. Briefing
    # ------------------------------------------------------------------
    metrics_line = (
        f"Citations: {latest['total_citations']:,} | "
        f"H-index: {latest['h_index']} | "
        f"Works: {latest['works_count']}"
    )
    doc_link = f"[Open Google Doc]({doc_url})" if doc_url else "(Drive not connected)"
    briefing_content = (
        f"## NAS case packet ready — {month_str}\n\n"
        f"{doc_link}\n\n"
        f"**Key metrics:** {metrics_line}\n\n"
        f"_This packet is ready to share with chair, letter writers, or program officers._"
    )
    if plot_path:
        briefing_content += f"\n\n_Plot: `{plot_path}`_"

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
            "VALUES ('nas_case_packet', 'info', ?, ?, ?)",
            (
                f"NAS case packet ready — {month_str}",
                briefing_content,
                now_utc.isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[nas_case_packet] briefing insert error (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 9. Return
    # ------------------------------------------------------------------
    if doc_url and not doc_url.startswith("Drive not connected"):
        return f"packet generated: {doc_url}"
    return f"packet generated (Drive not connected — narrative recorded locally): month={month_str}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
