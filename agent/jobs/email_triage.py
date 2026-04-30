"""Email triage subagent — runs every 10 min via APScheduler.

Fetches unread emails since last check, classifies each with Haiku 4.5,
and for emails that warrant a reply, drafts one into Gmail Drafts with Sonnet.
Drafts are created live (non-destructive). Notifications are advisor-mode only
(would_notify=1 logged, but no actual notify_heath call).

Off-hours guard: returns early with "skipped: off-hours" outside 7am–10pm Central
so cost stays low when researcher isn't working.

Run manually:
    cd "$HOME/Google Drive/My Drive/00-Lab-Agent"
    python -m agent.jobs.email_triage
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# Load .env from project root — same pattern as executive.py / morning_briefing.py
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from anthropic import Anthropic  # noqa: E402 (after load_dotenv)

from agent.jobs import tracked  # noqa: E402
from agent.scheduler import DB_PATH  # noqa: E402

log = logging.getLogger("tealc.email_triage")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
CHECKPOINT_PATH = os.path.join(_DATA, "last_email_triage.json")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Allowed classifications
# ---------------------------------------------------------------------------
VALID_CLASSIFICATIONS = {"ignore", "file", "drafts_reply", "notify", "requires_human", "service_request", "review_invitation"}

# ---------------------------------------------------------------------------
# Haiku system prompt: researcher's email triage preferences
# ---------------------------------------------------------------------------
HAIKU_SYSTEM = """You are Tealc's email triage engine for the researcher (PI).
Classify each email into EXACTLY ONE of these categories:

- ignore: promotional, automated digests, newsletter, Slack/GitHub notifications, mailing list traffic
- file: informational emails that are good to archive but need no reply or action
- drafts_reply: emails from humans expecting a reply that Tealc can draft (collaborators, students, colleagues asking questions)
- notify: time-sensitive items the researcher should see today — calendar invites, urgent institutional notices
- requires_human: emails requiring the researcher's personal judgment — personal/sensitive emails, anything ambiguous that is NOT a service request
- service_request: any ask for the researcher's time as a service commitment — committee invitation, talk invitation, board membership, editorial board ask, award nomination task, organizing roles, advisory panels, or any similar ask that would consume the researcher's time as institutional or professional service (NOT manuscript/grant peer-review invitations — those are review_invitation)
- review_invitation: a specific invitation to peer-review a manuscript or grant proposal — phrases like "Invitation to review", "We invite you to review", "review this manuscript", "referee this submission", "peer review request", "would you be willing to review"

RULES (in order):
1. Peer-review or referee invitations (manuscript review, grant review, ad hoc reviewer requests) → review_invitation
2. Other service requests (committee invitations, speaking invitations, board memberships, editorial roles, award nominations requiring the researcher's effort) → service_request
3. Student crisis signals (urgent, distress, emergency, failing, withdrawing) → notify
4. Calendar invites or scheduling requests from humans → notify
5. Routine collaborator emails asking a question or requesting a response → drafts_reply
6. Newsletter, Slack digest, GitHub notification, automated alert, mailing list → ignore
7. Informational updates with no action needed → file
8. Anything else requiring the researcher's personal judgment but NOT a service ask → requires_human

Output ONLY valid JSON with no other text:
{"classification": "<one of the seven>", "reasoning": "<one sentence>", "confidence": <0.0 to 1.0>}"""

# ---------------------------------------------------------------------------
# Sonnet system prompt: draft replies in The researcher's voice
# ---------------------------------------------------------------------------
SONNET_DRAFT_SYSTEM = """You are Tealc drafting an email reply for the researcher (PI).
The researcher's writing voice: direct, precise, no hedging, no filler phrases, no "I hope this email finds you well."
Write 3–6 sentences. Be warm but concise. This is a draft for the researcher to review — frame it as a starting point.
Do not include a subject line or salutation header — just the body text."""


# ---------------------------------------------------------------------------
# Sonnet system prompt: NAS test for service requests
# ---------------------------------------------------------------------------
NAS_TEST_SYSTEM = """The researcher is being asked to take on a service commitment. His career goal is NAS membership; \
his protection rule is "service requests must directly advance NAS trajectory or protect students — otherwise decline." \
Score the request 0-1 against this rule, given the researcher's current active goals (provided below).

Output ONLY valid JSON with no other text:
{"recommendation": "accept" | "decline", "reasoning": "<2 sentences citing specific goals or the NAS rule>", \
"draft_body": "<a 3-5 sentence draft email the researcher can send — polite, in his direct voice, no hedging, no 'I hope this finds you well'>"}

For declines: thank but firmly decline without a long explanation. Do NOT say you're too busy — just say it's not the right fit for where your work is focused.
For accepts: frame as "yes, because this directly advances X (a specific named goal)".
The researcher's voice: direct, precise, no filler phrases."""


# ---------------------------------------------------------------------------
# Sonnet system prompt: review invitation fit + draft
# ---------------------------------------------------------------------------
REVIEW_INVITATION_SYSTEM = """The researcher (PI) received a peer-review invitation.
Their service-protection rule: DECLINE unless (a) topical fit is VERY high AND (b) it is a top-tier journal (Nature, Science, Cell, PNAS, Current Biology, eLife, Nature Genetics, Nature Ecology & Evolution, PLOS Biology, or equivalent).
Researcher's topics: (configured via RESEARCHER_TOPICS in .env)

Score bibliographic fit 0.0–1.0 based on how closely the manuscript topic matches the researcher's active research.
Default recommendation: DECLINE (PI has significant admin duties — default to protecting time).

Output ONLY valid JSON with no other text:
{"fit_score": <0.0 to 1.0>, "fit_reasoning": "<one sentence on topic overlap>", "journal_tier": "top-tier" | "mid-tier" | "low-tier" | "unknown", "recommendation": "accept" | "decline", "decline_draft": "<3-4 sentence polite decline in the researcher's direct voice, no hedging, no 'I hope this finds you well'>", "accept_draft": "<3-4 sentence acceptance noting the specific topical fit, in the researcher's direct voice>"}"""


# ---------------------------------------------------------------------------
# Review-invitation Sonnet pass
# ---------------------------------------------------------------------------
def _run_review_invitation_analysis(
    client, from_email: str, subject: str, snippet: str
) -> dict | None:
    """Run Sonnet fit-scoring + draft for a review invitation. Returns parsed JSON dict or None."""
    user_content = (
        f"Review invitation details:\n"
        f"From: {from_email}\n"
        f"Subject: {subject}\n"
        f"Body preview: {snippet}"
    )
    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=500,
            system=REVIEW_INVITATION_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as exc:
        log.error(f"email_triage: review_invitation Sonnet call failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Read active goals from SQLite
# ---------------------------------------------------------------------------
def _load_active_goals(db_path: str) -> list[dict]:
    """Return active goals with importance >= 3 from the goals table."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT id, name, importance, nas_relevance, why, success_metric "
            "FROM goals WHERE status='active' AND importance >= 3 "
            "ORDER BY importance DESC"
        ).fetchall()
        conn.close()
        goals = []
        for row in rows:
            gid, name, importance, nas_rel, why, success = row
            goals.append({
                "id": gid,
                "name": name,
                "importance": importance,
                "nas_relevance": nas_rel or "",
                "why": why or "",
                "success_metric": success or "",
            })
        return goals
    except Exception as exc:
        log.warning(f"email_triage: could not load goals: {exc}")
        return []


# ---------------------------------------------------------------------------
# Service-request NAS test (Sonnet pass)
# ---------------------------------------------------------------------------
def _run_nas_test(
    client, from_email: str, subject: str, snippet: str, goals: list[dict]
) -> dict | None:
    """Run Sonnet NAS test. Returns parsed JSON dict or None on failure."""
    if goals:
        goals_text = "\n".join(
            f"- [{g['id']}] {g['name']} (importance={g['importance']}, "
            f"NAS-relevance={g['nas_relevance']}): {g['why']}"
            for g in goals
        )
    else:
        goals_text = (
            "No active goals found in database. "
            "Apply the default NAS rule: decline unless this directly advances NAS trajectory."
        )

    user_content = (
        f"Service request details:\n"
        f"From: {from_email}\n"
        f"Subject: {subject}\n"
        f"Body preview: {snippet}\n\n"
        f"the researcher's active goals (importance >= 3):\n{goals_text}"
    )

    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=512,
            system=NAS_TEST_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as exc:
        log.error(f"email_triage: NAS test Sonnet call failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Off-hours guard
# ---------------------------------------------------------------------------
def _is_working_hours() -> bool:
    """Return True if current Central time is between 7am and 10pm."""
    try:
        import zoneinfo
        central = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        try:
            from datetime import timezone as _tz
            import pytz
            central = pytz.timezone("America/Chicago")
        except Exception:
            # If we can't determine timezone, allow run
            return True
    now_central = datetime.now(central)
    return 7 <= now_central.hour < 22


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------
def _load_checkpoint() -> str | None:
    """Return last_check_iso or None."""
    try:
        with open(CHECKPOINT_PATH) as f:
            return json.load(f).get("last_check_iso")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_checkpoint(iso: str):
    with open(CHECKPOINT_PATH, "w") as f:
        json.dump({"last_check_iso": iso}, f)


# ---------------------------------------------------------------------------
# Gmail newer_than query helper
# ---------------------------------------------------------------------------
def _newer_than_query(iso: str | None) -> str:
    """Convert an ISO timestamp to a Gmail newer_than:Xh query string."""
    if iso is None:
        return "is:unread newer_than:1h"
    try:
        then = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        # Ensure both are timezone-aware
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        hours_back = max(1, int((now - then).total_seconds() / 3600) + 1)
        # Gmail newer_than supports up to a few days; cap at 24h for safety
        hours_back = min(hours_back, 24)
        return f"is:unread newer_than:{hours_back}h"
    except Exception:
        return "is:unread newer_than:1h"


# ---------------------------------------------------------------------------
# Parse email list output from list_recent_emails tool
# ---------------------------------------------------------------------------
def _parse_email_blocks(raw: str) -> list[dict]:
    """Parse the text output of list_recent_emails into structured dicts."""
    if not raw or raw.startswith("No messages") or raw.startswith("Gmail not connected"):
        return []
    blocks = raw.split("\n\n---\n\n")
    emails = []
    for block in blocks:
        lines = block.strip().splitlines()
        item: dict = {}
        for line in lines:
            if line.startswith("ID:"):
                item["message_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("From:"):
                item["from_email"] = line.split(":", 1)[1].strip()
            elif line.startswith("Subject:"):
                item["subject"] = line.split(":", 1)[1].strip()
            elif line.startswith("Preview:"):
                item["snippet"] = line.split(":", 1)[1].strip()
        # Also grab thread_id if present
        for line in lines:
            if line.startswith("Thread:"):
                item["thread_id"] = line.split(":", 1)[1].strip()
        if item.get("message_id"):
            emails.append(item)
    return emails


# ---------------------------------------------------------------------------
# Extract draft_id from draft_email_reply output
# ---------------------------------------------------------------------------
def _extract_draft_id(reply: str) -> str | None:
    """Pull the draft ID from draft_email_reply return string."""
    if not reply:
        return None
    import re
    m = re.search(r"ID:\s*([A-Za-z0-9_\-]+)", reply)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Check if message_id already processed
# ---------------------------------------------------------------------------
def _already_triaged(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_triage_decisions WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("email_triage")
def job() -> str:
    """Fetch unread emails, classify with Haiku, draft replies with Sonnet."""

    # Off-hours guard — cheap exit
    if not _is_working_hours():
        return "skipped: off-hours"

    from agent.tools import list_recent_emails, draft_email_reply  # noqa: PLC0415

    # 1. Load checkpoint
    last_check = _load_checkpoint()
    query = _newer_than_query(last_check)
    log.info(f"email_triage: fetching with query='{query}'")

    # 2. Fetch emails
    try:
        raw_emails = list_recent_emails.invoke({"max_results": 25, "query": query})
    except Exception as exc:
        log.error(f"email_triage: list_recent_emails failed: {exc}")
        return f"error: list_recent_emails failed — {exc}"

    emails = _parse_email_blocks(raw_emails)
    log.info(f"email_triage: fetched {len(emails)} candidate emails")

    if not emails:
        _save_checkpoint(datetime.now(timezone.utc).isoformat())
        return "triaged=0 drafts=0 notify=0 ignore=0"

    # 3. Open DB connection (one per job run, as per Tealc pattern)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    client = Anthropic()
    now_iso = datetime.now(timezone.utc).isoformat()

    counts = {"total": 0, "drafts": 0, "notify": 0, "ignore": 0, "file": 0, "requires_human": 0, "service_request": 0, "review_invitation": 0}

    for email in emails:
        message_id = email["message_id"]

        # Skip already-processed
        if _already_triaged(conn, message_id):
            log.info(f"email_triage: skipping already-triaged {message_id}")
            continue

        counts["total"] += 1

        try:
            # 3a. Compose compact Haiku input
            from_email = email.get("from_email", "unknown")
            subject = email.get("subject", "(no subject)")
            snippet = (email.get("snippet") or "")[:300]
            thread_id = email.get("thread_id")

            haiku_user = (
                f"From: {from_email}\n"
                f"Subject: {subject}\n"
                f"Preview: {snippet}"
            )

            # 3b. Haiku classification
            haiku_resp = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=256,
                system=HAIKU_SYSTEM,
                messages=[{"role": "user", "content": haiku_user}],
            )
            raw_json = haiku_resp.content[0].text.strip()

            # Parse JSON — strip markdown fences if present
            if raw_json.startswith("```"):
                raw_json = raw_json.split("```")[1]
                if raw_json.startswith("json"):
                    raw_json = raw_json[4:]

            classification_data = json.loads(raw_json)
            classification = classification_data.get("classification", "requires_human")
            reasoning = classification_data.get("reasoning", "")
            confidence = float(classification_data.get("confidence", 0.5))

            # Validate classification
            if classification not in VALID_CLASSIFICATIONS:
                classification = "requires_human"
                reasoning = f"Invalid classification from Haiku ({classification}); defaulting to requires_human"

            # Service requests always notify the researcher regardless of hours
            would_notify = 1 if classification in ("notify", "service_request") else 0
            draft_id = None
            service_recommendation = None

            # 3c. Draft reply if warranted
            if classification == "drafts_reply":
                sonnet_user = (
                    f"Email to reply to:\n"
                    f"From: {from_email}\n"
                    f"Subject: {subject}\n"
                    f"Content preview: {snippet}"
                )
                try:
                    sonnet_resp = client.messages.create(
                        model=SONNET_MODEL,
                        max_tokens=512,
                        system=SONNET_DRAFT_SYSTEM,
                        messages=[{"role": "user", "content": sonnet_user}],
                    )
                    draft_body = sonnet_resp.content[0].text.strip()

                    # Save to Gmail Drafts
                    draft_result = draft_email_reply.invoke({
                        "message_id": message_id,
                        "draft_body": draft_body,
                    })
                    draft_id = _extract_draft_id(draft_result)
                    if draft_id:
                        counts["drafts"] += 1
                        log.info(f"email_triage: draft created {draft_id} for {message_id}")
                    else:
                        log.warning(f"email_triage: draft_email_reply returned unexpected: {draft_result}")
                except Exception as draft_exc:
                    log.error(f"email_triage: draft failed for {message_id}: {draft_exc}")
                    # Downgrade to requires_human so researcher sees it
                    classification = "requires_human"
                    reasoning = f"Draft attempt failed: {draft_exc}"
                    draft_id = None

            # 3d-2. Review invitation: fit-score + service-protection draft (Sonnet pass)
            elif classification == "review_invitation":
                try:
                    ri_result = _run_review_invitation_analysis(client, from_email, subject, snippet)
                    if ri_result:
                        fit_score = float(ri_result.get("fit_score", 0.0))
                        fit_reasoning = ri_result.get("fit_reasoning", "")
                        journal_tier = ri_result.get("journal_tier", "unknown")
                        ri_recommendation = ri_result.get("recommendation", "decline")
                        decline_draft = ri_result.get("decline_draft", "")
                        accept_draft = ri_result.get("accept_draft", "")
                        service_recommendation = ri_recommendation
                        reasoning = (
                            f"{reasoning} | Fit score: {fit_score:.2f} ({journal_tier}) — "
                            f"{fit_reasoning} | Recommendation: {ri_recommendation}"
                        )

                        # Save the recommended draft into Gmail Drafts
                        chosen_draft = accept_draft if ri_recommendation == "accept" else decline_draft
                        if chosen_draft:
                            try:
                                draft_result = draft_email_reply.invoke({
                                    "message_id": message_id,
                                    "draft_body": chosen_draft,
                                })
                                draft_id = _extract_draft_id(draft_result)
                                if draft_id:
                                    counts["drafts"] += 1
                                    log.info(
                                        f"email_triage: review_invitation draft ({ri_recommendation}) "
                                        f"created {draft_id} for {message_id}"
                                    )
                            except Exception as draft_exc:
                                log.error(f"email_triage: review_invitation draft failed for {message_id}: {draft_exc}")

                        # Build briefing content
                        draft_link = f"Draft ID: {draft_id}" if draft_id else "No draft created"
                        content_md = (
                            f"**From:** {from_email}\n"
                            f"**Subject:** {subject}\n\n"
                            f"**Topic preview:** {snippet[:300]}\n\n"
                            f"**Bibliographic fit score:** {fit_score:.2f} / 1.0\n"
                            f"**Journal tier:** {journal_tier}\n"
                            f"**Fit reasoning:** {fit_reasoning}\n\n"
                            f"**Recommendation:** {ri_recommendation.upper()}\n"
                            f"**{draft_link}**\n\n"
                            f"**Decline draft:**\n{decline_draft}\n\n"
                            f"**Accept draft:**\n{accept_draft}"
                        )
                        briefing_title = f"Review invitation: {subject[:60]}"
                        try:
                            conn.execute(
                                "INSERT INTO briefings(kind, urgency, title, content_md, created_at) "
                                "VALUES ('review_invitation', 'warn', ?, ?, ?)",
                                (briefing_title, content_md, now_iso),
                            )
                            conn.commit()
                            log.info(f"email_triage: review_invitation briefing inserted for {message_id}")
                        except Exception as bri_exc:
                            log.error(f"email_triage: briefing insert failed for {message_id}: {bri_exc}")
                    else:
                        log.warning(f"email_triage: review_invitation analysis returned None for {message_id}; defaulting to decline posture")
                        service_recommendation = "decline"
                except Exception as ri_exc:
                    log.error(f"email_triage: review_invitation handling error for {message_id}: {ri_exc}")
                    service_recommendation = "decline"

            # 3d. Service-request NAS test (Sonnet pass — per-request, not per-email)
            elif classification == "service_request":
                try:
                    active_goals = _load_active_goals(DB_PATH)
                    nas_result = _run_nas_test(client, from_email, subject, snippet, active_goals)
                    if nas_result:
                        service_recommendation = nas_result.get("recommendation", "decline")
                        nas_reasoning = nas_result.get("reasoning", "")
                        draft_body = nas_result.get("draft_body", "")
                        # Augment the Haiku reasoning with NAS test outcome
                        reasoning = (
                            f"{reasoning} | NAS test: {service_recommendation} — {nas_reasoning}"
                        )
                        # Draft the accept/decline into Gmail Drafts
                        if draft_body:
                            try:
                                draft_result = draft_email_reply.invoke({
                                    "message_id": message_id,
                                    "draft_body": draft_body,
                                })
                                draft_id = _extract_draft_id(draft_result)
                                if draft_id:
                                    counts["drafts"] += 1
                                    log.info(
                                        f"email_triage: service_request draft ({service_recommendation}) "
                                        f"created {draft_id} for {message_id}"
                                    )
                            except Exception as draft_exc:
                                log.error(f"email_triage: service_request draft failed for {message_id}: {draft_exc}")
                    else:
                        log.warning(f"email_triage: NAS test returned None for {message_id}; will surface without draft")
                        service_recommendation = "decline"  # safe default
                except Exception as svc_exc:
                    log.error(f"email_triage: service_request handling error for {message_id}: {svc_exc}")
                    service_recommendation = None

            # Count
            if classification in counts:
                counts[classification] += 1
            if classification == "notify":
                counts["notify"] += 1  # already counted above via would_notify

            # 3e. Insert row (service_recommendation column may not exist yet — handle gracefully)
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO email_triage_decisions
                       (decided_at, message_id, thread_id, from_email, subject,
                        classification, reasoning, confidence, draft_id, would_notify,
                        service_recommendation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now_iso, message_id, thread_id, from_email, subject,
                        classification, reasoning, confidence, draft_id, would_notify,
                        service_recommendation,
                    ),
                )
            except Exception:
                # Fallback: insert without service_recommendation (column not yet migrated)
                conn.execute(
                    """INSERT OR IGNORE INTO email_triage_decisions
                       (decided_at, message_id, thread_id, from_email, subject,
                        classification, reasoning, confidence, draft_id, would_notify)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now_iso, message_id, thread_id, from_email, subject,
                        classification, reasoning, confidence, draft_id, would_notify,
                    ),
                )
            conn.commit()
            log.info(f"email_triage: {message_id} → {classification} (conf={confidence:.2f})")

        except Exception as exc:
            log.error(f"email_triage: error on {message_id}: {exc}")
            # Insert a safe fallback row so the email gets a record
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO email_triage_decisions
                       (decided_at, message_id, thread_id, from_email, subject,
                        classification, reasoning, confidence, would_notify)
                       VALUES (?, ?, ?, ?, ?, 'requires_human', ?, 0.0, 0)""",
                    (
                        now_iso, message_id,
                        email.get("thread_id"), email.get("from_email", ""),
                        email.get("subject", ""),
                        f"triage error: {str(exc)[:200]}",
                    ),
                )
                conn.commit()
                counts["requires_human"] += 1
            except Exception as db_exc:
                log.error(f"email_triage: also failed to insert fallback row: {db_exc}")

    conn.close()

    # 4. Update checkpoint
    _save_checkpoint(datetime.now(timezone.utc).isoformat())

    # 5. If any email was classified as notify (urgent), touch the burst flag
    #    so email_burst.py fires another triage cycle within ~60 seconds.
    if counts["notify"] > 0:
        _burst_flag = os.path.join(_DATA, "email_burst_pending.flag")
        try:
            open(_burst_flag, "w").close()
            log.info(f"email_triage: wrote burst flag ({counts['notify']} notify email(s))")
        except OSError as _fe:
            log.warning(f"email_triage: could not write burst flag: {_fe}")

    # 6. Return summary
    n = counts["total"]
    d = counts["drafts"]
    notify = counts["notify"]
    ignore_count = counts["ignore"]
    svc = counts["service_request"]
    rev = counts["review_invitation"]
    return f"triaged={n} drafts={d} notify={notify} ignore={ignore_count} service_request={svc} review_invitation={rev}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
