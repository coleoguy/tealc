"""Nightly grant drafter — runs at 1am Central via APScheduler.

For each active research_project with a linked_artifact_id AND a goal deadline
within 30 days, Tealc reads the artifact, finds the next unfinished section,
and drafts a first pass into a NEW Google Doc tagged '[draft]'.
NEVER overwrites the source artifact.

Cost: 1 project/run × 2 Sonnet 4.6 calls = ~$0.30-0.50/night (~$15/month).

Run manually to test:
    python -m agent.jobs.nightly_grant_drafter
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402
from agent.ledger import record_output, update_critic  # noqa: E402
from agent.critic import critic_pass  # noqa: E402
from agent.cost_tracking import record_call  # noqa: E402

try:
    from agent.voice_index import voice_system_prompt_addendum as _voice_addendum  # noqa: E402
except ImportError:
    _voice_addendum = None  # type: ignore[assignment]

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
DEADLINES_PATH = os.path.join(_DATA, "deadlines.json")

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
_GAP_FINDER_SYSTEM = (
    "You read a grant draft or manuscript draft and identify the next section that needs work. "
    "Look for: empty sections under headings, sections with only an outline, "
    "[TODO] / [needs writing] / [draft] markers, very short sections, "
    "sections that end mid-sentence. "
    "Output JSON: "
    '{\"section_label\": \"<short name like \'Significance\' or \'Approach Aim 2\'>\", '
    '\"what_is_missing\": \"<1 sentence>\", '
    '\"context_already_present\": \"<1 paragraph summary of what surrounding sections say so the draft fits>\"}. '
    "If nothing obvious is missing, output {\"section_label\": null} and we will skip."
)

from agent.jobs import SCIENTIST_MODE  # noqa: E402

_DRAFTER_SYSTEM = SCIENTIST_MODE + "\n\n" + (
    "You're drafting one section of a grant or manuscript. It will be reviewed "
    "line-by-line by Heath in the morning, and may end up in front of NIH "
    "program officers or journal reviewers — the prose carries that weight.\n\n"
    "Voice exemplars are already in the user message. Match their density and "
    "quantitative specificity; avoid AI-assistant cadence ('In this proposal "
    "we will...', 'a comprehensive framework', 'leveraging cutting-edge methods').\n\n"
    "For every preliminary-data claim, name the n, the test, and the effect "
    "size — or write [Heath: confirm n / test / effect] if you don't have it. "
    "Do not assert results you can't ground in the literature notes or the "
    "project's data.\n\n"
    "Acknowledge limitations explicitly when you make a strong claim — what "
    "would falsify it, what scope is conditional. Reviewers reward intellectual "
    "honesty more than bravado.\n\n"
    "Match the section_label and the surrounding context. Output ONLY the "
    "markdown text of the section, no preamble."
)


# ---------------------------------------------------------------------------
# Deadline helper
# ---------------------------------------------------------------------------
def _days_until(due_iso: str) -> int | None:
    """Return days until the deadline. Negative = past due. None on parse error."""
    try:
        due = datetime.fromisoformat(due_iso)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (due - now).days
    except Exception:
        return None


def _load_deadlines() -> list[dict]:
    try:
        with open(DEADLINES_PATH) as f:
            return json.load(f).get("deadlines", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("nightly_grant_drafter")
def job() -> str:
    """Read artifact for the soonest-deadline project and draft the next missing section."""
    # Heath can toggle this job via the Control tab (data/tealc_config.json).
    try:
        from agent.config import should_run_this_cycle  # noqa: PLC0415
        if not should_run_this_cycle("nightly_grant_drafter"):
            return "disabled_or_reduced_skip"
    except Exception:
        pass

    # 1. Self-pause check: if 3 consecutive unreviewed drafts older than 24h exist, halt.
    try:
        _conn = sqlite3.connect(DB_PATH)
        _conn.execute("PRAGMA journal_mode=WAL")
        _tbl_od = _conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='overnight_drafts'"
        ).fetchone()
        if _tbl_od:
            _cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            try:
                from agent.config import get_threshold as _gt  # noqa: PLC0415
                _pause_count = int(_gt("drafter_pause_count", 3))
            except Exception:
                _pause_count = 3
            _recent = _conn.execute(
                "SELECT id, created_at, reviewed_at, outcome FROM overnight_drafts "
                f"ORDER BY created_at DESC LIMIT {_pause_count}"
            ).fetchall()
            if (
                len(_recent) == _pause_count
                and all(r[2] is None and r[3] is None for r in _recent)
                and all(r[1] < _cutoff_iso for r in _recent)
            ):
                # Check for an existing drafter_paused briefing in the past 48h
                _48h_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
                _existing_pause = _conn.execute(
                    "SELECT id FROM briefings WHERE kind='drafter_paused' AND created_at >= ?",
                    (_48h_ago,),
                ).fetchone()
                _conn.close()
                if not _existing_pause:
                    _pause_content = (
                        "The nightly grant drafter has produced **3 consecutive drafts** "
                        "that have not been reviewed. To keep costs in check and avoid "
                        "pile-up, the drafter has paused itself.\n\n"
                        "**To resume:** review any one of the pending drafts using "
                        "`review_overnight_draft(draft_id=<id>, outcome=\"accepted\"|\"edited\"|\"rejected\", "
                        "notes=\"<your feedback>\")`, or delete this paused briefing from the database."
                    )
                    _now_iso = datetime.now(timezone.utc).isoformat()
                    _conn2 = sqlite3.connect(DB_PATH)
                    _conn2.execute("PRAGMA journal_mode=WAL")
                    _conn2.execute(
                        "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
                        "VALUES ('drafter_paused', 'warn', "
                        "'Grant drafter paused — 3 unreviewed in a row', ?, ?)",
                        (_pause_content, _now_iso),
                    )
                    _conn2.commit()
                    _conn2.close()
                return "paused: 3 consecutive unreviewed drafts"
            else:
                _conn.close()
        else:
            _conn.close()
    except Exception as _pause_err:
        pass  # self-pause failure must not block the job

    # 2. Time guard only. This is a 1am Central scheduled job — idle-class check
    # was causing 100% skip rate (Tealc sees itself as "active" whenever its own
    # scheduler is running). If we're firing outside 8am–10pm local, proceed.
    # FORCE_RUN=1 bypasses for chat-driven manual triggers.
    from datetime import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hour = _dt.now(ZoneInfo("America/Chicago")).hour
    if 8 <= hour < 22 and os.environ.get("FORCE_RUN") != "1":
        return f"skipped: working-hours guard (hour={hour})"

    # 3. Find candidate research projects
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        # Check table exists
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='research_projects'"
        ).fetchone()
        if not tbl:
            conn.close()
            return "no_projects_yet"

        rows = conn.execute(
            "SELECT id, name, description, current_hypothesis, linked_artifact_id, linked_goal_ids "
            "FROM research_projects "
            "WHERE status='active' AND linked_artifact_id IS NOT NULL AND linked_artifact_id != ''"
        ).fetchall()
        conn.close()
    except Exception as e:
        return f"no_projects_yet: {e}"

    if not rows:
        return "no_imminent_drafts"

    # 4. Load deadlines and score each project
    deadlines = _load_deadlines()
    deadline_map: dict[str, int] = {}  # deadline name → days_until
    for dl in deadlines:
        days = _days_until(dl.get("due_iso", ""))
        if days is not None:
            deadline_map[dl.get("name", "")] = days

    # For each project, find soonest deadline from linked_goal_ids or deadlines.json
    # We match goal names to deadline names (substring match) as a heuristic
    candidates = []
    for project_id, name, description, hypothesis, artifact_id, linked_goal_ids in rows:
        # Find the soonest upcoming deadline associated with this project
        soonest = None
        # Check deadline.json for name matches against project name
        for dl_name, days in deadline_map.items():
            if days is not None and 0 <= days <= 30:
                # Match if project name appears in deadline name or vice versa (case-insensitive)
                if (
                    name.lower() in dl_name.lower()
                    or dl_name.lower() in name.lower()
                    or any(
                        word.lower() in dl_name.lower()
                        for word in name.split()
                        if len(word) > 4
                    )
                ):
                    if soonest is None or days < soonest:
                        soonest = days

        # Also check goals table if linked_goal_ids is set
        if linked_goal_ids:
            try:
                gids = [g.strip() for g in linked_goal_ids.split(",") if g.strip()]
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA journal_mode=WAL")
                for gid in gids:
                    # Check if any milestone for this goal has a target_iso within 30 days
                    mrows = conn.execute(
                        "SELECT target_iso FROM milestones_v2 "
                        "WHERE goal_id=? AND status!='done' AND target_iso IS NOT NULL",
                        (gid,),
                    ).fetchall()
                    for (target_iso,) in mrows:
                        days = _days_until(target_iso)
                        if days is not None and 0 <= days <= 30:
                            if soonest is None or days < soonest:
                                soonest = days
                conn.close()
            except Exception:
                pass

        # Fallback slot (intentionally a no-op): if no project-specific
        # deadline within 30 days was found, the per-project candidate is
        # skipped entirely.  The previous placeholder here (`any_soon = [...]`)
        # was a TypeError crash since 2026-04-24.  Fallback logic to pick a
        # best-name-match project across deadlines.json is future work; for
        # now the job just skips projects with no imminent deadline.

        if soonest is not None:
            candidates.append((soonest, project_id, name, description, hypothesis, artifact_id))

    if not candidates:
        return "no_imminent_drafts"

    # Pick the project with the soonest deadline
    candidates.sort(key=lambda x: x[0])
    soonest_days, project_id, project_name, description, hypothesis, artifact_id = candidates[0]

    # 4. Read the artifact
    from agent.tools import read_drive_file, create_google_doc  # noqa: PLC0415
    raw = read_drive_file.invoke({"file_id": artifact_id})
    if raw.startswith("Drive not connected") or raw.startswith("Error"):
        return f"error reading artifact {artifact_id}: {raw[:200]}"

    # Strip the leading "**title**\n\n" that read_drive_file prepends
    artifact_text = raw
    artifact_title = ""
    if raw.startswith("**") and "\n\n" in raw:
        first_line_end = raw.index("\n\n")
        artifact_title = raw[2:first_line_end].rstrip("*")
        artifact_text = raw[first_line_end + 2:]

    # 5. Have Sonnet find the next unfinished section
    client = Anthropic()
    gap_user = artifact_text[:12000] if len(artifact_text) > 12000 else artifact_text

    try:
        gap_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=_GAP_FINDER_SYSTEM,
            messages=[{"role": "user", "content": gap_user}],
        )
        gap_raw = gap_msg.content[0].text.strip()
        # Strip markdown fences if present
        if gap_raw.startswith("```"):
            gap_raw = gap_raw.split("```")[1]
            if gap_raw.startswith("json"):
                gap_raw = gap_raw[4:]
        gap_data = json.loads(gap_raw)
    except Exception as e:
        return f"error in gap-finder call: {e}"

    section_label = gap_data.get("section_label")
    if not section_label:
        return "no_gap_found"

    what_is_missing = gap_data.get("what_is_missing", "")
    context_already_present = gap_data.get("context_already_present", "")

    # 6. Have Sonnet draft the missing section
    draft_user = (
        f"Section to draft: {section_label}\n"
        f"What is missing: {what_is_missing}\n"
        f"Surrounding context: {context_already_present}\n\n"
        f"Project description: {description or '(not provided)'}\n"
        f"Current hypothesis: {hypothesis or '(not provided)'}"
    )

    # Build voice-exemplar addendum (falls back to "" if helper missing or index empty)
    _voice_query = f"{what_is_missing} {section_label} {project_name}"
    _drafter_system = _DRAFTER_SYSTEM
    if _voice_addendum is not None:
        try:
            _addendum = _voice_addendum(_voice_query, k=3)
            if _addendum:
                _drafter_system = _addendum + "\n\n" + _DRAFTER_SYSTEM
        except Exception:
            pass  # voice index errors must not block drafting

    try:
        draft_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=_drafter_system,
            messages=[{"role": "user", "content": draft_user}],
        )
        drafted_text = draft_msg.content[0].text.strip()
    except Exception as e:
        return f"error in drafter call: {e}"

    # 6a-pre. Citation suggester — append "Consider citing" footnote block
    try:
        from agent.citation_suggester import suggest_citations  # noqa: PLC0415
        _cite_result = suggest_citations(drafted_text)
        if _cite_result.get("suggestions"):
            drafted_text += "\n\n" + _cite_result["footnote_block_md"]
    except Exception as exc:
        log.warning("Citation suggester failed: %s", exc)

    # 6a. Record cost for both Anthropic calls
    try:
        for _msg, _label in ((gap_msg, "gap_finder"), (draft_msg, "drafter")):
            _usage = {
                "input_tokens": getattr(_msg.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(_msg.usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(_msg.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(_msg.usage, "cache_read_input_tokens", 0) or 0,
            }
            record_call(job_name="nightly_grant_drafter", model="claude-sonnet-4-6", usage=_usage)
    except Exception as _e:
        print(f"[nightly_grant_drafter] cost_tracking error: {_e}")

    # 6b. Adversarial critic pass
    critic_result: dict = {}
    try:
        critic_result = critic_pass(drafted_text, rubric_name="grant_draft")
    except Exception as _e:
        print(f"[nightly_grant_drafter] critic_pass error: {_e}")

    # 6c. Ledger: record output + critic
    ledger_row_id: int | None = None
    try:
        ledger_row_id = record_output(
            kind="grant_draft",
            job_name="nightly_grant_drafter",
            model="claude-sonnet-4-6",
            project_id=project_id,
            content_md=drafted_text,
            tokens_in=getattr(draft_msg.usage, "input_tokens", 0) or 0,
            tokens_out=getattr(draft_msg.usage, "output_tokens", 0) or 0,
            provenance={
                "section_label": section_label,
                "what_is_missing": what_is_missing,
                "context_already_present": context_already_present,
                "source_artifact_id": artifact_id,
                "source_artifact_title": artifact_title,
            },
        )
        if ledger_row_id and critic_result:
            update_critic(
                ledger_row_id,
                critic_result.get("score", 0),
                critic_result.get("overall_notes", ""),
                critic_result.get("model", ""),
            )
    except Exception as _e:
        print(f"[nightly_grant_drafter] ledger error: {_e}")

    # 7. Create a NEW Google Doc — never touches the source
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    doc_title = f"[draft] {project_name} — {section_label} — {today_str}"
    source_url = f"https://docs.google.com/document/d/{artifact_id}/edit"
    header_md = (
        f"# Tealc Overnight Draft — For Review Only\n\n"
        f"**Source artifact:** {artifact_title or artifact_id} — {source_url}\n\n"
        f"**Section:** {section_label}\n\n"
        f"**Reasoning:** {what_is_missing}\n\n"
        f"**Context summary:** {context_already_present}\n\n"
        f"---\n\n"
        f"## {section_label}\n\n"
        f"{drafted_text}"
    )

    try:
        doc_result = create_google_doc.invoke({"title": doc_title, "body_markdown": header_md})
    except Exception as e:
        return f"error creating doc: {e}"

    if "|" not in doc_result:
        return f"error creating doc: {doc_result[:200]}"

    draft_doc_id, draft_doc_url = doc_result.split("|", 1)

    # 8. Insert row into overnight_drafts; capture the new row id for the briefing.
    now_iso = datetime.now(timezone.utc).isoformat()
    overnight_draft_id: int | None = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute(
            """INSERT INTO overnight_drafts
               (project_id, source_artifact_id, source_artifact_title, drafted_section,
                draft_doc_id, draft_doc_url, reasoning, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                artifact_id,
                artifact_title or None,
                section_label,
                draft_doc_id,
                draft_doc_url,
                what_is_missing,
                now_iso,
            ),
        )
        overnight_draft_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        return f"error inserting overnight_drafts row: {e}"

    # Back-link the ledger row to the overnight_drafts row so the dashboard's
    # /api/action review_draft handler can propagate accept/edit/reject
    # into output_ledger.user_action via json_extract.
    if ledger_row_id is not None:
        try:
            _link_conn = sqlite3.connect(DB_PATH)
            _link_conn.execute("PRAGMA journal_mode=WAL")
            _link_conn.execute(
                "UPDATE output_ledger "
                "SET provenance_json=json_set("
                "    COALESCE(provenance_json, '{}'),"
                "    '$.draft_doc_id', ?,"
                "    '$.overnight_draft_id', ?"
                ") WHERE id=?",
                (draft_doc_id, overnight_draft_id, ledger_row_id),
            )
            _link_conn.commit()
            _link_conn.close()
        except Exception as _e:
            print(f"[nightly_grant_drafter] ledger back-link error: {_e}")

    # 9. Create a briefing so it surfaces in morning chat
    briefing_title = f"Overnight draft ready: {project_name} — {section_label}"
    briefing_urgency = "critical" if critic_result.get("score", 5) <= 2 else "warn"
    briefing_content = (
        f"**Section drafted:** {section_label}\n\n"
        f"**Project:** {project_name}\n\n"
        f"**Why this section:** {what_is_missing}\n\n"
        f"**Draft doc:** [{doc_title}]({draft_doc_url})\n\n"
        f"**Critic score:** {critic_result.get('score', 'n/a')}/5\n"
        f"**Unsupported claims:** {len(critic_result.get('unsupported_claims', []))}\n"
        f"**Missing citations:** {len(critic_result.get('missing_citations', []))}\n\n"
        f"_Review with `review_overnight_draft(draft_id={overnight_draft_id}, "
        f"outcome=\"accepted\"|\"edited\"|\"rejected\", notes=\"<your feedback>\")`._"
    )
    briefing_metadata = json.dumps({
        "draft_id": overnight_draft_id,
        "doc_id": draft_doc_id,
        "doc_url": draft_doc_url,
        "project_id": project_id,
        "section_label": section_label,
    })
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO briefings(kind, urgency, title, content_md, metadata_json, created_at) "
            "VALUES ('overnight_draft', ?, ?, ?, ?, ?)",
            (briefing_urgency, briefing_title, briefing_content, briefing_metadata, now_iso),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # briefing failure must not crash the job

    return f"drafted: project={project_id} section={section_label} doc={draft_doc_url}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
