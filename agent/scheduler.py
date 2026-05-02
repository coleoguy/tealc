"""Tealc background scheduler — runs as a standalone process alongside Chainlit.

Start:  bash scripts/start_scheduler.sh
Stop:   bash scripts/stop_scheduler.sh
Status: bash scripts/scheduler_status.sh
"""
import asyncio
import logging
import os
import sqlite3

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))

# DB lives OUTSIDE Google Drive to avoid Drive sync zeroing the file mid-write
# (the corruption issue that hit on Apr 24 and Apr 29). Override via env var.
# Default: ~/Library/Application Support/tealc/agent.db (macOS standard location).
_DEFAULT_DB_DIR = os.path.expanduser("~/Library/Application Support/tealc")
DB_PATH = os.environ.get(
    "TEALC_DB_PATH",
    os.path.join(_DEFAULT_DB_DIR, "agent.db"),
)
# Ensure the parent dir exists for whichever path is in effect.
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

LOG_PATH = os.path.normpath(os.path.join(_HERE, "..", "data", "scheduler.log"))

# ---------------------------------------------------------------------------
# Logging — goes to data/scheduler.log (NOT the aquarium)
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tealc.scheduler")

# Also echo to stdout so start_scheduler.sh can confirm boot
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_console)


# ---------------------------------------------------------------------------
# Schema migration — idempotent, safe to call on every startup
# ---------------------------------------------------------------------------
def _migrate():
    """Apply schema additions to data/agent.db.  Safe to call multiple times."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS briefings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT NOT NULL,
            urgency         TEXT NOT NULL,
            title           TEXT NOT NULL,
            content_md      TEXT NOT NULL,
            metadata_json   TEXT,
            created_at      TEXT NOT NULL,
            surfaced_at     TEXT,
            acknowledged_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_briefings_unsurfaced
            ON briefings(surfaced_at) WHERE surfaced_at IS NULL;

        CREATE TABLE IF NOT EXISTS job_runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name       TEXT NOT NULL,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            status         TEXT,
            error          TEXT,
            output_summary TEXT
        );

        -- Task 9: Grant opportunity radar
        CREATE TABLE IF NOT EXISTS grant_opportunities (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,
            program        TEXT NOT NULL,
            title          TEXT NOT NULL,
            deadline_iso   TEXT,
            url            TEXT NOT NULL,
            description    TEXT,
            fit_score      REAL,
            fit_reasoning  TEXT,
            surfaced_at    TEXT,
            dismissed      BOOLEAN DEFAULT 0,
            first_seen     TEXT NOT NULL,
            UNIQUE(source, title, deadline_iso)
        );

        -- Task 10: Student milestone tracker
        CREATE TABLE IF NOT EXISTS students (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name       TEXT NOT NULL UNIQUE,
            short_name      TEXT,
            role            TEXT NOT NULL,
            joined_iso      TEXT,
            status          TEXT,
            primary_project TEXT,
            email           TEXT,
            notes_md        TEXT
        );

        CREATE TABLE IF NOT EXISTS milestones (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id    INTEGER NOT NULL,
            kind          TEXT NOT NULL,
            target_iso    TEXT,
            completed_iso TEXT,
            notes         TEXT,
            FOREIGN KEY(student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id   INTEGER NOT NULL,
            occurred_iso TEXT NOT NULL,
            channel      TEXT,
            topic        TEXT,
            action_items TEXT,
            FOREIGN KEY(student_id) REFERENCES students(id)
        );

        -- Pending intentions queue: foundation for the always-on executive loop
        CREATE TABLE IF NOT EXISTS intentions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            description  TEXT NOT NULL,
            target_iso   TEXT,
            priority     TEXT NOT NULL DEFAULT 'normal',
            status       TEXT NOT NULL DEFAULT 'pending',
            created_by   TEXT NOT NULL,
            context_json TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            completed_at TEXT,
            notes        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_intentions_pending
            ON intentions(status, priority, target_iso)
            WHERE status IN ('pending', 'in_progress');

        -- Executive loop decisions (advisor mode; executed=0 always in v1)
        CREATE TABLE IF NOT EXISTS executive_decisions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_at            TEXT NOT NULL,
            action                TEXT NOT NULL,
            reasoning             TEXT NOT NULL,
            confidence            REAL,
            context_snapshot_json TEXT,
            raw_haiku_output      TEXT,
            parse_error           TEXT,
            executed              INTEGER NOT NULL DEFAULT 0,
            execution_result      TEXT,
            human_review          TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_exec_decided
            ON executive_decisions(decided_at DESC);

        -- Email triage decisions (advisor mode for notify; live for drafts_reply)
        CREATE TABLE IF NOT EXISTS email_triage_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_at TEXT NOT NULL,
            message_id TEXT NOT NULL,
            thread_id TEXT,
            from_email TEXT,
            subject TEXT,
            classification TEXT NOT NULL,
            reasoning TEXT,
            confidence REAL,
            draft_id TEXT,
            would_notify INTEGER NOT NULL DEFAULT 0,
            human_review TEXT,
            UNIQUE(message_id)
        );

        CREATE INDEX IF NOT EXISTS idx_triage_decided
            ON email_triage_decisions(decided_at DESC);

        -- Paper of the day (one row per date)
        CREATE TABLE IF NOT EXISTS papers_of_the_day (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            date_iso          TEXT NOT NULL UNIQUE,
            doi               TEXT,
            pubmed_id         TEXT,
            title             TEXT NOT NULL,
            authors           TEXT,
            journal           TEXT,
            publication_year  INTEGER,
            open_access_url   TEXT,
            citations_count   INTEGER,
            raw_abstract      TEXT,
            why_it_matters_md TEXT NOT NULL,
            topic_matched     TEXT,
            created_at        TEXT NOT NULL
        );

        -- Long-term conversation memory (session summaries + FTS)
        CREATE TABLE IF NOT EXISTS session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL UNIQUE,
            started_at TEXT,
            ended_at TEXT,
            message_count INTEGER,
            summary_md TEXT NOT NULL,
            topics TEXT,
            people_mentioned TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_summary_topics ON session_summaries(topics);

        CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
            thread_id UNINDEXED,
            summary_md,
            topics,
            people_mentioned,
            content='session_summaries',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS session_summaries_ai AFTER INSERT ON session_summaries
        BEGIN
            INSERT INTO session_summaries_fts(rowid, thread_id, summary_md, topics, people_mentioned)
            VALUES (new.id, new.thread_id, new.summary_md, new.topics, new.people_mentioned);
        END;

        -- NAS-metric tracker (weekly Monday snapshots)
        CREATE TABLE IF NOT EXISTS nas_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_iso TEXT NOT NULL UNIQUE,
            total_citations INTEGER,
            citations_since_2021 INTEGER,
            h_index INTEGER,
            i10_index INTEGER,
            works_count INTEGER,
            top_3_recent_papers_json TEXT,
            raw_author_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_nas_metrics_iso ON nas_metrics(snapshot_iso DESC);

        -- Task 11 — Goals Sheet SQLite mirror
        -- Note: milestones_v2 avoids collision with the student tracker's milestones table
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            time_horizon TEXT,
            importance INTEGER,
            nas_relevance TEXT,
            status TEXT,
            success_metric TEXT,
            why TEXT,
            owner TEXT,
            last_touched_by TEXT,
            last_touched_iso TEXT,
            notes TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS milestones_v2 (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            milestone TEXT NOT NULL,
            target_iso TEXT,
            status TEXT,
            notes TEXT,
            last_touched_iso TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS today_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_iso TEXT NOT NULL,
            priority_rank INTEGER,
            description TEXT NOT NULL,
            linked_goal_id TEXT,
            status TEXT,
            notes TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date_iso, priority_rank)
        );

        CREATE TABLE IF NOT EXISTS decisions_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_iso TEXT NOT NULL,
            decision TEXT NOT NULL,
            reasoning TEXT,
            linked_goal_id TEXT,
            decided_by TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL,
            tealc_dirty INTEGER NOT NULL DEFAULT 0
        );

        -- Overnight draft table (nightly_grant_drafter job)
        CREATE TABLE IF NOT EXISTS overnight_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            source_artifact_id TEXT NOT NULL,
            source_artifact_title TEXT,
            drafted_section TEXT,
            draft_doc_id TEXT NOT NULL,
            draft_doc_url TEXT NOT NULL,
            reasoning TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            outcome TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_drafts_unreviewed
            ON overnight_drafts(reviewed_at) WHERE reviewed_at IS NULL;

        -- Goal-conflict surfacing table
        CREATE TABLE IF NOT EXISTS goal_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_iso TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            involved_goal_ids TEXT,
            description TEXT NOT NULL,
            recommendation TEXT,
            acknowledged_at TEXT,
            human_response TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_conflicts_unack
            ON goal_conflicts(acknowledged_at) WHERE acknowledged_at IS NULL;

        -- Research-project operational layer (foundation for nightly lit-synthesis + drafter)
        CREATE TABLE IF NOT EXISTS research_projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT,
            linked_goal_ids TEXT,
            data_dir TEXT,
            output_dir TEXT,
            current_hypothesis TEXT,
            next_action TEXT,
            keywords TEXT,
            linked_artifact_id TEXT,
            last_touched_by TEXT,
            last_touched_iso TEXT,
            notes TEXT,
            sheet_row_index INTEGER,
            synced_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_projects_active
            ON research_projects(status) WHERE status='active';

        -- NAS impact weekly scoring table
        CREATE TABLE IF NOT EXISTS nas_impact_weekly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start_iso TEXT NOT NULL UNIQUE,
            week_end_iso TEXT NOT NULL,
            nas_trajectory_pct REAL,
            service_drag_pct REAL,
            maintenance_pct REAL,
            unattributed_pct REAL,
            goal_breakdown_json TEXT,
            total_activity_count INTEGER,
            summary_md TEXT,
            created_at TEXT NOT NULL
        );

        -- Rolling context snapshot — single-row table read by the Haiku executive loop
        CREATE TABLE IF NOT EXISTS current_context (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            refreshed_at TEXT NOT NULL,
            unsurfaced_briefings_count INTEGER NOT NULL DEFAULT 0,
            unsurfaced_briefings_top TEXT,
            pending_intentions_count INTEGER NOT NULL DEFAULT 0,
            pending_intentions_top TEXT,
            next_deadline_name TEXT,
            next_deadline_iso TEXT,
            next_deadline_days_remaining INTEGER,
            students_needing_attention_count INTEGER NOT NULL DEFAULT 0,
            students_needing_attention_names TEXT,
            hours_since_last_chat REAL,
            open_grant_opportunities_count INTEGER NOT NULL DEFAULT 0,
            current_local_hour INTEGER,
            current_local_day TEXT,
            is_working_hours INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        );
    """)

    conn.commit()

    # ---------------------------------------------------------------------------
    # Column migrations (ALTER TABLE; SQLite doesn't support IF NOT EXISTS)
    # ---------------------------------------------------------------------------
    # email_triage_decisions: triggered_burst column (event-driven burst flag)
    try:
        conn.execute(
            "ALTER TABLE email_triage_decisions ADD COLUMN triggered_burst INTEGER DEFAULT 0"
        )
        conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore

    # current_context: idle_class derived field
    try:
        conn.execute(
            "ALTER TABLE current_context ADD COLUMN idle_class TEXT DEFAULT 'unknown'"
        )
        conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore

    # email_triage_decisions: service_recommendation for NAS-test outcomes
    try:
        conn.execute(
            "ALTER TABLE email_triage_decisions ADD COLUMN service_recommendation TEXT"
        )
        conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore

    # executive_decisions: linked_goal_id for goal-trajectory analysis
    try:
        conn.execute(
            "ALTER TABLE executive_decisions ADD COLUMN linked_goal_id TEXT"
        )
        conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore

    # v4: per-project retrieval keywords (comma-separated)
    try:
        conn.execute("ALTER TABLE research_projects ADD COLUMN keywords TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Projects tab — explicit lead assignment
    try:
        conn.execute("ALTER TABLE research_projects ADD COLUMN lead_student_id INTEGER")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE research_projects ADD COLUMN lead_name TEXT")
        conn.commit()
    except Exception:
        pass

    # Projects tab — project type classification + type-specific fields
    for col in [
        "project_type TEXT",
        "journal TEXT",
        "paper_status TEXT",
        "agency TEXT",
        "program TEXT",
        "grant_status TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE research_projects ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass

    # literature_notes: per-project annotated bibliography (nightly_literature_synthesis)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS literature_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            doi TEXT,
            pubmed_id TEXT,
            title TEXT NOT NULL,
            authors TEXT,
            journal TEXT,
            publication_year INTEGER,
            open_access_url TEXT,
            citations_count INTEGER,
            raw_abstract TEXT,
            extracted_findings_md TEXT NOT NULL,
            relevance_to_project TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, doi)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_litnotes_project
            ON literature_notes(project_id, created_at DESC)
    """)
    conn.commit()

    # database_health_runs: weekly consistency-check results per sheet
    conn.execute("""
        CREATE TABLE IF NOT EXISTS database_health_runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_iso             TEXT NOT NULL,
            sheet_name          TEXT NOT NULL,
            spreadsheet_id      TEXT NOT NULL,
            total_rows          INTEGER,
            flagged_count       INTEGER,
            flagged_summary_json TEXT,
            notes               TEXT,
            UNIQUE(run_iso, sheet_name)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dbhealth_recent
            ON database_health_runs(run_iso DESC)
    """)
    conn.commit()

    # quarterly_retrospectives: deep 90-day goal portfolio retrospective
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_retrospectives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quarter_label TEXT NOT NULL UNIQUE,
            period_start_iso TEXT NOT NULL,
            period_end_iso TEXT NOT NULL,
            summary_md TEXT NOT NULL,
            goals_to_drop_json TEXT,
            goals_to_add_json TEXT,
            citation_delta INTEGER,
            h_index_delta INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # hypothesis_proposals: weekly hypothesis generator output (Sunday 5am CT)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hypothesis_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            proposed_iso TEXT NOT NULL,
            hypothesis_md TEXT NOT NULL,
            rationale_md TEXT,
            proposed_test_md TEXT,
            cited_paper_dois TEXT,
            novelty_score REAL,
            feasibility_score REAL,
            status TEXT NOT NULL DEFAULT 'proposed',
            human_review TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hyp_status
            ON hypothesis_proposals(project_id, status)
    """)
    conn.commit()

    # analysis_runs: records of overnight comparative R analyses
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            run_iso TEXT NOT NULL,
            next_action_text TEXT,
            r_code TEXT,
            working_dir TEXT,
            exit_code INTEGER,
            stdout_truncated TEXT,
            stderr_truncated TEXT,
            plot_paths TEXT,
            created_files TEXT,
            interpretation_md TEXT,
            outcome TEXT,
            human_review TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_analysis_recent
            ON analysis_runs(run_iso DESC)
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # v2: output ledger (full provenance for every research artifact)
    # -------------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS output_ledger (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at         TEXT NOT NULL,
            kind               TEXT NOT NULL,
            job_name           TEXT NOT NULL,
            model              TEXT NOT NULL,
            project_id         TEXT,
            content_md         TEXT NOT NULL,
            tokens_in          INTEGER,
            tokens_out         INTEGER,
            cache_read_tokens  INTEGER,
            cache_write_tokens INTEGER,
            critic_score       INTEGER,
            critic_notes       TEXT,
            critic_model       TEXT,
            critic_ran_at      TEXT,
            user_action        TEXT,
            user_reason        TEXT,
            user_action_at     TEXT,
            provenance_json    TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ledger_recent ON output_ledger(created_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ledger_kind ON output_ledger(kind, created_at DESC)
    """)
    conn.commit()

    # v2: retrieval quality sampling (daily Haiku-scored relevance)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_quality (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at       TEXT NOT NULL,
            source_job       TEXT NOT NULL,
            project_id       TEXT,
            paper_doi        TEXT,
            paper_title      TEXT,
            relevance_score  INTEGER,
            critic_reasoning TEXT,
            critic_model     TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_retqual_recent ON retrieval_quality(sampled_at DESC)
    """)
    conn.commit()

    # v2: cost tracking (every Anthropic API call)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_tracking (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                 TEXT NOT NULL,
            job_name           TEXT NOT NULL,
            model              TEXT NOT NULL,
            tokens_in          INTEGER DEFAULT 0,
            tokens_out         INTEGER DEFAULT 0,
            cache_read_tokens  INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cost_recent ON cost_tracking(ts DESC)
    """)
    conn.commit()

    # subagent_runs: one row per run_subagent() call (telemetry)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subagent_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            task            TEXT,
            model           TEXT,
            n_steps         INTEGER,
            tokens_in       INTEGER,
            tokens_out      INTEGER,
            cache_read      INTEGER,
            cache_write     INTEGER,
            cost_usd        REAL,
            status          TEXT,
            error           TEXT,
            final_text_len  INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_recent
            ON subagent_runs(started_at DESC)
    """)
    conn.commit()

    # v2: aquarium audit (daily leak scan)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aquarium_audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at      TEXT NOT NULL,
            entries_scanned INTEGER NOT NULL,
            leaks_found     INTEGER NOT NULL,
            incidents_json  TEXT
        )
    """)
    conn.commit()

    # v2: preference signals
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preference_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            target_id   INTEGER,
            user_reason TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prefsig_recent ON preference_signals(captured_at DESC)
    """)
    conn.commit()

    # v2: model routing decisions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_routing_decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_at      TEXT NOT NULL,
            task_type       TEXT NOT NULL,
            complexity_hint TEXT,
            chosen_model    TEXT NOT NULL,
            reasoning       TEXT
        )
    """)
    conn.commit()

    # v2: replication snapshots
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replication_snapshots (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at        TEXT NOT NULL,
            tool_count         INTEGER,
            job_count          INTEGER,
            table_count        INTEGER,
            schema_version     TEXT,
            diff_from_previous TEXT
        )
    """)
    conn.commit()

    # Knowledge Map: resource catalog — where Heath's information lives
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resource_catalog (
            id                TEXT PRIMARY KEY,
            kind              TEXT NOT NULL,
            handle            TEXT NOT NULL,
            display_name      TEXT NOT NULL,
            purpose           TEXT,
            tags              TEXT,
            linked_project_ids  TEXT,
            linked_goal_ids   TEXT,
            linked_person_ids TEXT,
            owner             TEXT DEFAULT 'Heath',
            status            TEXT DEFAULT 'proposed',
            proposed_by       TEXT DEFAULT 'tealc',
            last_confirmed_iso TEXT,
            last_used_iso     TEXT,
            notes             TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_catalog_kind ON resource_catalog(kind, status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_catalog_status ON resource_catalog(status)
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # v8: Lab wiki infrastructure
    # Feeds the /knowledge/ section of the lab's GitHub Pages site. Paper findings are
    # structured verbatim-quote records; topics are the state-of-understanding
    # pages; github_repos is the watched-repo registry (Tier 5 of the corpus).
    # -------------------------------------------------------------------------

    # literature_notes: add SHA256 fingerprint column for ingested PDFs
    try:
        conn.execute("ALTER TABLE literature_notes ADD COLUMN pdf_fingerprint TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore

    # paper_findings: one row per citable finding extracted from a paper.
    # Each row carries the verbatim quote, page, and teaching-mode fields.
    # confidence / last_confirmed / superseded_by are populated starting in
    # Phase 3 (supersession lifecycle); left NULL in v1.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_findings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            doi             TEXT NOT NULL,
            finding_idx     INTEGER NOT NULL,
            finding_text    TEXT NOT NULL,
            quote           TEXT NOT NULL,
            page            TEXT,
            reasoning       TEXT,
            counter         TEXT,
            topic_tags      TEXT,
            confidence      REAL,
            last_confirmed  TEXT,
            superseded_by   INTEGER,
            created_at      TEXT NOT NULL,
            UNIQUE(doi, finding_idx)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_findings_doi ON paper_findings(doi)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_findings_topics ON paper_findings(topic_tags)
    """)
    conn.commit()

    # topics: topic registry. Mirrors /knowledge/topics/{slug}.md pages.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            slug            TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            confidence      REAL,
            last_updated    TEXT,
            created_at      TEXT NOT NULL
        )
    """)
    conn.commit()

    # github_repos: watched-repo registry for Tier 5 ingestion.
    # in_allowlist must be 1 before github_ingest.py will sync a row.
    # tier='public' → page renders to /knowledge/repos/; tier='private' →
    # page stays in the private notebook, never on the website.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_repos (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            owner              TEXT NOT NULL,
            name               TEXT NOT NULL,
            full_name          TEXT NOT NULL,
            description        TEXT,
            default_branch     TEXT,
            language           TEXT,
            is_private         INTEGER NOT NULL DEFAULT 0,
            in_allowlist       INTEGER NOT NULL DEFAULT 0,
            tier               TEXT NOT NULL DEFAULT 'public',
            synced_locally     INTEGER NOT NULL DEFAULT 0,
            last_synced_at     TEXT,
            last_commit_at     TEXT,
            papers_using_json  TEXT,
            notes              TEXT,
            created_at         TEXT NOT NULL,
            UNIQUE(owner, name)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_github_repos_allowlist
            ON github_repos(in_allowlist) WHERE in_allowlist=1
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_github_repos_tier ON github_repos(tier)
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # V5 wiki expansion: concept cards, method pages, course units.
    # Populated by gloss_harvester (Tue 3am), method_promoter (Wed 3am), and a
    # future course_weaver job. editor_frozen=1 opts a row out of automation.
    # -------------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            slug                TEXT PRIMARY KEY,
            title               TEXT NOT NULL,
            concept_type        TEXT,
            prerequisites       TEXT,
            aliases             TEXT,
            primary_finding_id  INTEGER,
            appears_in_topics   TEXT,
            last_rendered       TEXT,
            editor_frozen       INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS methods (
            slug                TEXT PRIMARY KEY,
            title               TEXT NOT NULL,
            language            TEXT,
            package             TEXT,
            depends_on_concepts TEXT,
            appears_in_papers   TEXT,
            difficulty          TEXT,
            example_code        TEXT,
            last_executed       TEXT,
            last_rendered       TEXT,
            editor_frozen       INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS course_units (
            course            TEXT NOT NULL,
            week              INTEGER NOT NULL,
            title             TEXT NOT NULL,
            topic_slugs       TEXT,
            paper_slugs       TEXT,
            concept_slugs     TEXT,
            method_slugs      TEXT,
            last_rendered     TEXT,
            editor_frozen     INTEGER DEFAULT 0,
            PRIMARY KEY (course, week)
        )
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # Grants — split off from research_projects on 2026-04-24.  `research_projects`
    # now holds STUDENT-LED PAPER PROJECTS only (source of truth: subfolders of
    # "Lab/Projects" in the shared Drive).  Active grant applications
    # live here; `grant_opportunities` is still the radar-scored lead table.
    # -------------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grants (
            id                  TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            agency              TEXT,
            program             TEXT,
            status              TEXT,
            deadline_iso        TEXT,
            amount_usd          REAL,
            pi_role             TEXT,
            drive_folder_path   TEXT,
            linked_artifact_id  TEXT,
            linked_goal_ids     TEXT,
            current_hypothesis  TEXT,
            next_action         TEXT,
            notes               TEXT,
            last_touched_by     TEXT,
            last_touched_iso    TEXT,
            created_at          TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_grants_status ON grants(status)
    """)
    conn.commit()

    # research_projects gets a `lab_drive_path` column — set by sync_lab_projects
    # when a matching subfolder exists in the shared Drive "Projects" tree.
    # NULL means "not yet discovered on Drive" which, after the first sync, is
    # an audit signal.
    try:
        conn.execute("ALTER TABLE research_projects ADD COLUMN lab_drive_path TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Action-handler column drift (discovered 2026-04-24).  Each of these
    # columns is referenced by an existing /api/action handler but was
    # never in the original DDL — dashboard clicks silently 500'd.
    for ddl in (
        "ALTER TABLE hypothesis_proposals ADD COLUMN reviewed_at TEXT",
        "ALTER TABLE grant_opportunities ADD COLUMN dismissed_at TEXT",
        "ALTER TABLE grant_opportunities ADD COLUMN dismiss_reason TEXT",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass  # Column already exists

    # -------------------------------------------------------------------------
    # Tier 1 #2 — Live Reviewer Circle tables (added 2026-04-27)
    # -------------------------------------------------------------------------

    # output_ledger.domain — which rubric domain this artifact belongs to
    try:
        conn.execute("ALTER TABLE output_ledger ADD COLUMN domain TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # reviewer_invitations: one row per (reviewer_pseudonym, domain) send
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviewer_invitations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer_pseudonym  TEXT NOT NULL,
            domain              TEXT NOT NULL,
            batch_id            TEXT NOT NULL,
            draft_id            TEXT,
            status              TEXT NOT NULL DEFAULT 'draft',
            sla_iso             TEXT NOT NULL,
            sent_at             TEXT,
            created_at          TEXT NOT NULL,
            UNIQUE(reviewer_pseudonym, domain)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rev_invitations_status
            ON reviewer_invitations(status)
    """)
    conn.commit()

    # reviewer_scores: one row per (blinded_id, reviewer_id)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviewer_scores (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            blinded_id          TEXT NOT NULL,
            reviewer_id         TEXT NOT NULL,
            domain              TEXT NOT NULL,
            rigor               INTEGER,
            novelty             INTEGER,
            grounding           INTEGER,
            clarity             INTEGER,
            feasibility         INTEGER,
            qualitative_notes   TEXT,
            flags               TEXT,
            submitted_iso       TEXT NOT NULL,
            gmail_message_id    TEXT,
            UNIQUE(blinded_id, reviewer_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rev_scores_domain
            ON reviewer_scores(domain)
    """)
    conn.commit()

    # reviewer_correlations: Spearman r + bootstrap CI per (domain, dimension)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviewer_correlations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at     TEXT NOT NULL,
            domain          TEXT NOT NULL,
            dimension       TEXT NOT NULL,
            n_pairs         INTEGER NOT NULL,
            spearman_r      REAL,
            bootstrap_ci_lo REAL,
            bootstrap_ci_hi REAL,
            n_bootstrap     INTEGER NOT NULL DEFAULT 200
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rev_corr_recent
            ON reviewer_correlations(computed_at DESC)
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # Tier 2 Foundation — sentence-level corpus + heath_claims knowledge graph
    # -------------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heath_sentences (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sentence_id  TEXT NOT NULL UNIQUE,
            paper_id     TEXT NOT NULL,
            year         INTEGER,
            section      TEXT,
            sentence     TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hs_paper_id ON heath_sentences(paper_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heath_claims (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id       TEXT NOT NULL,
            year           INTEGER,
            subject        TEXT NOT NULL,
            predicate      TEXT NOT NULL,
            object         TEXT NOT NULL,
            evidence_quote TEXT,
            sentence_id    TEXT,
            confidence     REAL,
            created_at     TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hc_paper_id ON heath_claims(paper_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hc_subject ON heath_claims(subject)")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS heath_claims_fts USING fts5(
            paper_id UNINDEXED,
            subject,
            predicate,
            object,
            evidence_quote,
            content='heath_claims',
            content_rowid='id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS heath_claims_ai AFTER INSERT ON heath_claims
        BEGIN
            INSERT INTO heath_claims_fts(rowid, paper_id, subject, predicate, object, evidence_quote)
            VALUES (new.id, new.paper_id, new.subject, new.predicate, new.object, new.evidence_quote);
        END
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # Tier 2 #5 — Undercited Paper Surface (citation residuals)
    # -------------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS undercited_residuals (
            snapshot_iso       TEXT,
            paper_id           TEXT,
            doi                TEXT,
            year               INT,
            observed_citations INT,
            expected_citations REAL,
            residual           REAL,
            novelty_class      TEXT,
            novelty_rationale  TEXT,
            title              TEXT,
            PRIMARY KEY(snapshot_iso, paper_id)
        )
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # Inbox dismissals — generic per-(kind, target_id) soft-dismiss table.
    # Inbox query LEFT JOINs against this so any item dismissed from the
    # dashboard never re-surfaces, regardless of source table.
    # -------------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inbox_dismissals (
            kind          TEXT NOT NULL,
            target_id     TEXT NOT NULL,
            dismissed_at  TEXT NOT NULL,
            reason        TEXT,
            PRIMARY KEY(kind, target_id)
        )
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # Tier 1 #1 — Prereg-to-Replication Loop columns (added 2026-04-28)
    # -------------------------------------------------------------------------
    for col_def in [
        "prereg_published_at TEXT",
        "prereg_md TEXT",
        "prereg_test_json TEXT",
        "prereg_aquarium_url TEXT",
        "replication_run_id INTEGER",
        "adjudicated_at TEXT",
        "adjudication TEXT",
        "adjudication_rationale_md TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE hypothesis_proposals ADD COLUMN {col_def}")
        except Exception:
            pass  # column already exists
    conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS prereg_artifacts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id   INTEGER NOT NULL REFERENCES hypothesis_proposals(id),
            created_at      TEXT NOT NULL,
            artifact_type   TEXT NOT NULL,
            content_md      TEXT,
            provenance_json TEXT
        )
    """)
    conn.commit()

    # -------------------------------------------------------------------------
    # Bet 3: Open Lab Notebook — publish state machine columns (2026-04-28)
    # Each ALTER is wrapped in try/except — idempotent on repeated startup.
    # -------------------------------------------------------------------------
    for _col_def in [
        "publish_state TEXT DEFAULT 'private'",
        "published_at TEXT",
        "public_url TEXT",
        "code_sha TEXT",
        "data_sha TEXT",
        "prompt_sha TEXT",
        "embargo_until TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE output_ledger ADD COLUMN {_col_def}")
            conn.commit()
        except Exception:
            pass  # Column already exists — safe to ignore

    # publish_decisions: audit trail of every publish/redact decision
    conn.execute("""
        CREATE TABLE IF NOT EXISTS publish_decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_id   INTEGER NOT NULL,
            decision    TEXT NOT NULL,
            reason      TEXT,
            decided_by  TEXT NOT NULL,
            decided_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pub_decisions_ledger
            ON publish_decisions(ledger_id)
    """)
    conn.commit()

    conn.close()


# ---------------------------------------------------------------------------
# Job registration
# ---------------------------------------------------------------------------
def register_jobs(scheduler: AsyncIOScheduler):
    """Register all scheduled jobs."""
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    from agent.jobs.heartbeat import job as heartbeat_job  # noqa: PLC0415
    from agent.jobs.morning_briefing import job as morning_briefing_job  # noqa: PLC0415
    from agent.jobs.grant_radar import job as grant_radar_job  # noqa: PLC0415
    from agent.jobs.web_grant_radar import job as web_grant_radar_job  # noqa: PLC0415
    from agent.jobs.undercited_papers import job as undercited_papers_job  # noqa: PLC0415
    from agent.jobs.idle_research_task import job as idle_research_task_job  # noqa: PLC0415
    from agent.jobs.student_pulse import job as student_pulse_job  # noqa: PLC0415
    from agent.jobs.refresh_context import job as refresh_context_job  # noqa: PLC0415
    from agent.jobs.executive import job as executive_job  # noqa: PLC0415
    from agent.jobs.email_triage import job as email_triage_job  # noqa: PLC0415
    from agent.jobs.paper_of_the_day import job as paper_of_the_day_job  # noqa: PLC0415
    from agent.jobs.summarize_sessions import job as summarize_sessions_job  # noqa: PLC0415
    from agent.jobs.weekly_review import job as weekly_review_job  # noqa: PLC0415
    from agent.jobs.watch_deadlines import job as watch_deadlines_job  # noqa: PLC0415
    from agent.jobs.email_burst import job as email_burst_job  # noqa: PLC0415
    from agent.jobs.track_nas_metrics import job as track_nas_metrics_job  # noqa: PLC0415
    from agent.jobs.publish_aquarium import job as publish_aquarium_job  # noqa: PLC0415
    # Retired 2026-04-20: bidirectional Sheets sync caused 429 quota hits under bulk-dirty
    # state. Model switched to "SQLite canonical; on-demand export via export_state_to_sheet
    # tool". Code still lives in agent/jobs/sync_goals_sheet.py for reference + one-time
    # bootstrap use. Re-enable by uncommenting both this import and the add_job below.
    # from agent.jobs.sync_goals_sheet import job as sync_goals_sheet_job  # noqa: PLC0415
    from agent.jobs.nas_impact_score import job as nas_impact_score_job  # noqa: PLC0415
    from agent.jobs.daily_plan import job as daily_plan_job  # noqa: PLC0415
    from agent.jobs.quarterly_retrospective import job as quarterly_retrospective_job  # noqa: PLC0415
    from agent.jobs.goal_conflict_check import job as goal_conflict_check_job  # noqa: PLC0415
    from agent.jobs.nightly_grant_drafter import job as nightly_grant_drafter_job  # noqa: PLC0415
    from agent.jobs.nightly_literature_synthesis import job as nightly_literature_synthesis_job  # noqa: PLC0415
    from agent.jobs.weekly_database_health import job as weekly_database_health_job  # noqa: PLC0415
    from agent.jobs.weekly_comparative_analysis import job as weekly_comparative_analysis_job  # noqa: PLC0415
    from agent.jobs.weekly_hypothesis_generator import job as weekly_hypothesis_generator_job
    from agent.jobs.retrieval_quality_monitor import job as retrieval_quality_monitor_job
    from agent.jobs.aquarium_audit import job as aquarium_audit_job
    from agent.jobs.replication_docs import job as replication_docs_job
    from agent.jobs.preference_consolidator import job as preference_consolidator_job  # noqa: PLC0415
    from agent.jobs.midday_check import job as midday_check_job  # noqa: PLC0415
    from agent.jobs.deadline_countdown import job as deadline_countdown_job  # noqa: PLC0415
    from agent.jobs.next_action_filler import job as next_action_filler_job  # noqa: PLC0415
    from agent.jobs.meeting_prep import job as meeting_prep_job  # noqa: PLC0415
    from agent.jobs.vip_email_watch import job as vip_email_watch_job  # noqa: PLC0415
    from agent.jobs.nas_pipeline_health import job as nas_pipeline_health_job  # noqa: PLC0415
    from agent.jobs.cross_project_synthesis import job as cross_project_synthesis_job  # noqa: PLC0415
    from agent.jobs.student_agenda_drafter import job as student_agenda_drafter_job  # noqa: PLC0415
    from agent.jobs.populate_project_keywords import job as populate_project_keywords_job  # noqa: PLC0415
    from agent.jobs.exploratory_analysis import job as exploratory_analysis_job  # noqa: PLC0415
    from agent.jobs.nas_case_packet import job as nas_case_packet_job  # noqa: PLC0415
    from agent.jobs.rebuild_voice_index import job as rebuild_voice_index_job  # noqa: PLC0415
    from agent.jobs.publish_dashboard import job as publish_dashboard_job  # noqa: PLC0415
    from agent.jobs.publish_abilities import job as publish_abilities_job  # noqa: PLC0415
    from agent.jobs.wiki_janitor import job as wiki_janitor_job  # noqa: PLC0415
    from agent.jobs.refresh_enrichment import job as refresh_enrichment_job  # noqa: PLC0415
    from agent.jobs.improve_wiki import job as improve_wiki_job  # noqa: PLC0415
    from agent.jobs.contradictions_index import job as contradictions_index_job  # noqa: PLC0415
    from agent.jobs.projects_mirror import job as projects_mirror_job  # noqa: PLC0415
    from agent.jobs.open_questions_index import job as open_questions_index_job  # noqa: PLC0415
    from agent.jobs.gloss_harvester import job as gloss_harvester_job  # noqa: PLC0415
    from agent.jobs.method_promoter import job as method_promoter_job  # noqa: PLC0415
    from agent.jobs.sync_lab_projects import job as sync_lab_projects_job  # noqa: PLC0415
    from agent.jobs.sync_lab_team import job as sync_lab_team_job  # noqa: PLC0415
    from agent.jobs.prereg_replication_loop import run_monday_prereg, run_daily_t7_sweep  # noqa: PLC0415
    from agent.jobs.notebook_publisher import job as notebook_publisher_job  # noqa: PLC0415
    from agent.jobs.notebook_index import job as notebook_index_job  # noqa: PLC0415
    # Daytime science micro-jobs (work-hours visibility for the public aquarium)
    from agent.jobs.midday_lit_pulse import job as midday_lit_pulse_job  # noqa: PLC0415
    from agent.jobs.citation_watch import job as citation_watch_job  # noqa: PLC0415
    from agent.jobs.paper_radar import job as paper_radar_job  # noqa: PLC0415
    from agent.jobs.database_pulse import job as database_pulse_job  # noqa: PLC0415

    scheduler.add_job(
        heartbeat_job,
        IntervalTrigger(seconds=60),
        id="heartbeat",
        replace_existing=True,
    )

    # Task 2 — Morning briefing (daily 7:45am Central)
    scheduler.add_job(
        morning_briefing_job,
        CronTrigger(hour=7, minute=45, timezone="America/Chicago"),
        id="morning_briefing",
        replace_existing=True,
    )

    # Task 9 — Grant radar (weekly, Monday 6am Central)
    scheduler.add_job(
        grant_radar_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone="America/Chicago"),
        id="grant_radar",
        replace_existing=True,
    )

    # Web-search grant scout — weekly, Monday 7am Central (after API grant_radar)
    # Discovers society awards, foundation small grants, TAMU internal funding,
    # AI-for-Science seed funding, named lectureships — long-tail not in NIH/NSF APIs.
    scheduler.add_job(
        web_grant_radar_job,
        CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="America/Chicago"),
        id="web_grant_radar",
        replace_existing=True,
    )

    # Tier 2 #5 — Undercited papers (monthly, 1st of month, 9am Central)
    # Computes citation residuals across Heath's 63 papers; top undercited
    # flagship feeds the NAS case packet narrative.
    scheduler.add_job(
        undercited_papers_job,
        CronTrigger(day=1, hour=9, minute=0, timezone="America/Chicago"),
        id="undercited_papers",
        replace_existing=True,
    )

    # idle_research_task — hourly during work hours (8am-7pm CT). Internal idle-gate
    # skips when Heath is active in chat. Picks one short task from a menu (preprint
    # scout, self-citation prospector, hypothesis stress-test, undercited memo,
    # method scout). Each run capped at $0.30.
    scheduler.add_job(
        idle_research_task_job,
        CronTrigger(hour="8-19", minute=0, timezone="America/Chicago"),
        id="idle_research_task",
        replace_existing=True,
    )

    # Task 10 — Student pulse (weekly, Sunday 6pm Central)
    scheduler.add_job(
        student_pulse_job,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone="America/Chicago"),
        id="student_pulse",
        replace_existing=True,
    )

    # Rolling context snapshot — every 10 minutes
    scheduler.add_job(
        refresh_context_job,
        IntervalTrigger(minutes=10),
        id="refresh_context",
        replace_existing=True,
    )

    # Executive loop is in ADVISOR mode — logs decisions only. To promote actions to
    # autonomous, add per-action handling in agent/jobs/executive.py route_decision()
    # (currently a no-op stub).
    scheduler.add_job(
        executive_job,
        IntervalTrigger(minutes=15),
        id="executive",
        replace_existing=True,
    )

    # Email triage — every 10 min; off-hours guard (7am–10pm Central) is inside the job,
    # not in the trigger, so the job can return "skipped: off-hours" cheaply.
    scheduler.add_job(
        email_triage_job,
        IntervalTrigger(minutes=10),
        id="email_triage",
        replace_existing=True,
    )

    # Paper of the day — daily 6:00am Central
    scheduler.add_job(
        paper_of_the_day_job,
        CronTrigger(hour=6, minute=0, timezone="America/Chicago"),
        id="paper_of_the_day",
        replace_existing=True,
    )

    # Long-term conversation memory — every 30 minutes
    scheduler.add_job(
        summarize_sessions_job,
        IntervalTrigger(minutes=30),
        id="summarize_sessions",
        replace_existing=True,
    )

    # Weekly self-review — Sunday 7pm Central (learning loop for Heath)
    scheduler.add_job(
        weekly_review_job,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone="America/Chicago"),
        id="weekly_review",
        replace_existing=True,
    )

    # Event-driven: watch data/deadlines.json mtime — every 60 s, no API cost
    scheduler.add_job(
        watch_deadlines_job,
        IntervalTrigger(seconds=60),
        id="watch_deadlines",
        replace_existing=True,
    )

    # Event-driven: email burst consumer — every 60 s, O(1) when no flag present
    scheduler.add_job(
        email_burst_job,
        IntervalTrigger(seconds=60),
        id="email_burst",
        replace_existing=True,
    )

    # NAS-metric tracker — daily 5:30am Central (before morning_briefing at 7:45am)
    scheduler.add_job(
        track_nas_metrics_job,
        CronTrigger(hour=5, minute=30, timezone="America/Chicago"),
        id="track_nas_metrics",
        replace_existing=True,
    )

    # Task 11 — Goals Sheet sync — RETIRED 2026-04-20 (see import block above).
    # scheduler.add_job(
    #     sync_goals_sheet_job,
    #     IntervalTrigger(minutes=5),
    #     id="sync_goals_sheet",
    #     replace_existing=True,
    # )

    # Daily plan — 6:30am Central (before morning_briefing at 7:45am)
    scheduler.add_job(
        daily_plan_job,
        CronTrigger(hour=6, minute=30, timezone="America/Chicago"),
        id="daily_plan",
        replace_existing=True,
    )

    # NAS impact score — weekly Sunday 8pm Central (after weekly_review at 7pm)
    scheduler.add_job(
        nas_impact_score_job,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone="America/Chicago"),
        id="nas_impact_score",
        replace_existing=True,
    )

    # Quarterly retrospective — first Sunday of Jan/Apr/Jul/Oct at 8pm Central
    # day="1-7" + day_of_week="sun" selects days 1-7 of the month that fall on Sunday
    # i.e., the first Sunday of the month. month="1,4,7,10" = quarter-start months.
    scheduler.add_job(
        quarterly_retrospective_job,
        CronTrigger(month="1,4,7,10", day="1-7", day_of_week="sun", hour=20, minute=0, timezone="America/Chicago"),
        id="quarterly_retrospective",
        replace_existing=True,
    )

    # Goal-conflict surfacing — daily 7:15am Central (before morning briefing at 7:45am)
    scheduler.add_job(
        goal_conflict_check_job,
        CronTrigger(hour=7, minute=15, timezone="America/Chicago"),
        id="goal_conflict_check",
        replace_existing=True,
    )

    # Nightly literature synthesis — midnight Central; idle guard inside the job
    scheduler.add_job(
        nightly_literature_synthesis_job,
        CronTrigger(hour=0, minute=0, timezone="America/Chicago"),
        id="nightly_literature_synthesis",
        replace_existing=True,
    )

    # Nightly grant drafter — 1am Central (staggered after literature synthesis at midnight)
    scheduler.add_job(
        nightly_grant_drafter_job,
        CronTrigger(hour=1, minute=0, timezone="America/Chicago"),
        id="nightly_grant_drafter",
        replace_existing=True,
    )

    # Weekly database health — Saturday 3am Central (before any morning briefing)
    scheduler.add_job(
        weekly_database_health_job,
        CronTrigger(day_of_week="sat", hour=3, minute=0, timezone="America/Chicago"),
        id="weekly_database_health",
        replace_existing=True,
    )

    # Weekly comparative analysis — Sunday 4am Central; idle guard inside the job
    scheduler.add_job(
        weekly_comparative_analysis_job,
        CronTrigger(day_of_week="sun", hour=4, minute=0, timezone="America/Chicago"),
        id="weekly_comparative_analysis",
        replace_existing=True,
    )

    # Weekly hypothesis generator — Sunday 5am Central (staggered after comparative analysis at 4am)
    scheduler.add_job(
        weekly_hypothesis_generator_job,
        CronTrigger(day_of_week="sun", hour=5, minute=0, timezone="America/Chicago"),
        id="weekly_hypothesis_generator",
        replace_existing=True,
    )

    # Tier 1 — Prereg-to-Replication Loop (Mon prereg + daily T+7 adjudication)
    scheduler.add_job(
        run_monday_prereg,
        CronTrigger(day_of_week="mon", hour=4, minute=0, timezone="America/Chicago"),
        id="prereg_monday", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_t7_sweep,
        CronTrigger(hour=3, minute=30, timezone="America/Chicago"),
        id="prereg_t7_sweep", replace_existing=True,
    )

    # v2: Retrieval quality monitor — daily 6:15am Central
    scheduler.add_job(
        retrieval_quality_monitor_job,
        CronTrigger(hour=6, minute=15, timezone="America/Chicago"),
        id="retrieval_quality_monitor",
        replace_existing=True,
    )

    # v2: Aquarium privacy audit — daily 1:30am Central
    scheduler.add_job(
        aquarium_audit_job,
        CronTrigger(hour=1, minute=30, timezone="America/Chicago"),
        id="aquarium_audit",
        replace_existing=True,
    )

    # v2: Replication docs snapshot — Sundays 3am Central
    scheduler.add_job(
        replication_docs_job,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone="America/Chicago"),
        id="replication_docs",
        replace_existing=True,
    )

    # v2: Preference consolidator — Sundays 7:30pm Central (after weekly_review at 7pm)
    scheduler.add_job(
        preference_consolidator_job,
        CronTrigger(day_of_week="sun", hour=19, minute=30, timezone="America/Chicago"),
        id="preference_consolidator",
        replace_existing=True,
    )

    # v3 — proactive jobs: keep Tealc helpful throughout the day
    scheduler.add_job(
        midday_check_job,
        CronTrigger(hour=13, minute=0, timezone="America/Chicago"),
        id="midday_check", replace_existing=True,
    )
    scheduler.add_job(
        deadline_countdown_job,
        CronTrigger(hour=7, minute=30, timezone="America/Chicago"),
        id="deadline_countdown", replace_existing=True,
    )
    scheduler.add_job(
        next_action_filler_job,
        CronTrigger(day_of_week="mon,thu", hour=6, minute=45, timezone="America/Chicago"),
        id="next_action_filler", replace_existing=True,
    )

    # v4: aquarium heartbeat — every 2 min. Keeps the public page's status dot green
    # and reflects recent scheduler activity (privacy-labeled) when chat is idle.
    scheduler.add_job(
        publish_aquarium_job,
        IntervalTrigger(minutes=2),
        id="publish_aquarium", replace_existing=True,
    )

    # ----------------------------------------------------------------------
    # Daytime science micro-jobs — make the public aquarium feed look like a
    # research postdoc rather than an admin assistant during work hours.
    # All four enforce their own working-hours / pulse-window guards inside
    # job() so they no-op cheaply at night.
    # ----------------------------------------------------------------------
    scheduler.add_job(
        midday_lit_pulse_job,
        IntervalTrigger(minutes=90),
        id="midday_lit_pulse", replace_existing=True,
    )
    scheduler.add_job(
        citation_watch_job,
        IntervalTrigger(hours=4),
        id="citation_watch", replace_existing=True,
    )
    scheduler.add_job(
        paper_radar_job,
        IntervalTrigger(hours=2),
        id="paper_radar", replace_existing=True,
    )
    scheduler.add_job(
        database_pulse_job,
        IntervalTrigger(hours=5),  # fires roughly twice in the work day
        id="database_pulse", replace_existing=True,
    )

    # v4: meeting prep — every 15 min during work hours (internal time-guard skips off-hours)
    scheduler.add_job(
        meeting_prep_job,
        IntervalTrigger(minutes=15),
        id="meeting_prep", replace_existing=True,
    )

    # v4: VIP email watch — every 5 min (internal time-guard skips off-hours)
    scheduler.add_job(
        vip_email_watch_job,
        IntervalTrigger(minutes=5),
        id="vip_email_watch", replace_existing=True,
    )

    # v4: NAS pipeline health — Sundays 6:30pm Central (before weekly_review at 7pm)
    scheduler.add_job(
        nas_pipeline_health_job,
        CronTrigger(day_of_week="sun", hour=18, minute=30, timezone="America/Chicago"),
        id="nas_pipeline_health", replace_existing=True,
    )

    # v4: Cross-project hypothesis synthesis — Saturdays 4am Central
    scheduler.add_job(
        cross_project_synthesis_job,
        CronTrigger(day_of_week="sat", hour=4, minute=0, timezone="America/Chicago"),
        id="cross_project_synthesis", replace_existing=True,
    )

    # v4: Student 1:1 agenda drafter — daily 6am Central (internal guard: 4-10am window)
    scheduler.add_job(
        student_agenda_drafter_job,
        CronTrigger(hour=6, minute=0, timezone="America/Chicago"),
        id="student_agenda_drafter", replace_existing=True,
    )

    # v4: Populate project keywords — Wednesdays 4:30am Central (retrieval-quality fix)
    scheduler.add_job(
        populate_project_keywords_job,
        CronTrigger(day_of_week="wed", hour=4, minute=30, timezone="America/Chicago"),
        id="populate_project_keywords", replace_existing=True,
    )

    # v5: Exploratory Python analysis on deep idle — Fridays 3am Central
    scheduler.add_job(
        exploratory_analysis_job,
        CronTrigger(day_of_week="fri", hour=3, minute=0, timezone="America/Chicago"),
        id="exploratory_analysis", replace_existing=True,
    )

    # v5: Monthly NAS case packet — first Sunday of month, 10am Central
    scheduler.add_job(
        nas_case_packet_job,
        CronTrigger(day="1-7", day_of_week="sun", hour=10, minute=0, timezone="America/Chicago"),
        id="nas_case_packet", replace_existing=True,
    )

    # v5: Rebuild voice index from Heath's papers — Tuesdays 4am Central
    scheduler.add_job(
        rebuild_voice_index_job,
        CronTrigger(day_of_week="tue", hour=4, minute=0, timezone="America/Chicago"),
        id="rebuild_voice_index", replace_existing=True,
    )

    # v5 HQ: publish dashboard state — every 1 min (drives localhost:8001)
    scheduler.add_job(
        publish_dashboard_job,
        IntervalTrigger(minutes=1),
        id="publish_dashboard", replace_existing=True,
    )

    # v5 HQ: publish abilities catalog — Wednesdays 5am Central
    scheduler.add_job(
        publish_abilities_job,
        CronTrigger(day_of_week="wed", hour=5, minute=0, timezone="America/Chicago"),
        id="publish_abilities", replace_existing=True,
    )

    # Wiki janitor — Mondays 8am Central (static audit, no API cost)
    scheduler.add_job(
        wiki_janitor_job,
        CronTrigger(day_of_week="mon", hour=8, minute=0, timezone="America/Chicago"),
        id="wiki_janitor", replace_existing=True,
    )

    # Refresh enrichment — Tuesdays 8am Central (after janitor's Monday audit).
    # Rewrites the tealc:related region on every paper + topic page from
    # current DB state. Pure string manipulation, zero API cost.
    scheduler.add_job(
        refresh_enrichment_job,
        CronTrigger(day_of_week="tue", hour=8, minute=0, timezone="America/Chicago"),
        id="refresh_enrichment", replace_existing=True,
    )

    # improve_wiki — Sundays 10am Central. Picks 2 oldest topic pages + 1 paper
    # page, runs Opus with voice exemplars to propose targeted prose improvements.
    # First two weeks default to dry-run mode (writes diffs to briefings, no file
    # touches). Flip `jobs.improve_wiki.dry_run` to false in tealc_config.json to
    # go live. 40%-change cap; editor_frozen:true frontmatter opt-out.
    scheduler.add_job(
        improve_wiki_job,
        CronTrigger(day_of_week="sun", hour=10, minute=0, timezone="America/Chicago"),
        id="improve_wiki", replace_existing=True,
    )

    # V1 Phase 4 — three deterministic no-LLM wiki surface jobs.
    # All three read existing DB/topic-page state and render markdown under
    # knowledge/<subdir>/. Zero API cost. Working-hours guard inside each job.

    # projects_mirror — daily 4am Central. Renders research_projects rows as
    # /knowledge/projects/<project_id>.md + index page.
    scheduler.add_job(
        projects_mirror_job,
        CronTrigger(hour=4, minute=0, timezone="America/Chicago"),
        id="projects_mirror", replace_existing=True,
    )

    # contradictions_index — daily 5am Central. Aggregates
    # `## Contradictions / open disagreements` sections across topic pages
    # into /knowledge/contradictions/index.md.
    scheduler.add_job(
        contradictions_index_job,
        CronTrigger(hour=5, minute=0, timezone="America/Chicago"),
        id="contradictions_index", replace_existing=True,
    )

    # open_questions_index — daily 6am Central. Renders
    # hypothesis_proposals (status in proposed/adopted) as
    # /knowledge/questions/index.md.
    scheduler.add_job(
        open_questions_index_job,
        CronTrigger(hour=6, minute=0, timezone="America/Chicago"),
        id="open_questions_index", replace_existing=True,
    )

    # V5 wiki expansion — long-tail fillers.  gloss_harvester mines new concept
    # cards from paper_findings; method_promoter writes method pages from
    # data/known_methods.json.  Both default dry_run=True until Heath flips
    # the flag in the Control tab.
    scheduler.add_job(
        gloss_harvester_job,
        CronTrigger(day_of_week="tue", hour=3, minute=0, timezone="America/Chicago"),
        id="gloss_harvester", replace_existing=True,
    )
    scheduler.add_job(
        method_promoter_job,
        CronTrigger(day_of_week="wed", hour=3, minute=0, timezone="America/Chicago"),
        id="method_promoter", replace_existing=True,
    )

    # Daily 3:30am CT — mirror the shared-Drive Projects folder to
    # research_projects.  Source of truth for paper projects; writes a
    # briefing with any audit signals (DB orphans, ambiguous matches).
    scheduler.add_job(
        sync_lab_projects_job,
        CronTrigger(hour=3, minute=30, timezone="America/Chicago"),
        id="sync_lab_projects", replace_existing=True,
    )

    # Daily 4:15am CT — reconcile the lab website's data/team.json into the
    # students table.  Inserts new members, updates roles, soft-marks
    # departures as status='alumni'.  Heath is skipped (hardcoded in the
    # dashboard lead picker).
    scheduler.add_job(
        sync_lab_team_job,
        CronTrigger(hour=4, minute=15, timezone="America/Chicago"),
        id="sync_lab_team", replace_existing=True,
    )

    # Bet 3: Open Lab Notebook — drain publish queue every 30 min
    scheduler.add_job(
        notebook_publisher_job,
        IntervalTrigger(minutes=30),
        id="notebook_publisher", replace_existing=True,
    )
    # Bet 3: Open Lab Notebook — regenerate index every 2 hours
    scheduler.add_job(
        notebook_index_job,
        IntervalTrigger(hours=2),
        id="notebook_index", replace_existing=True,
    )

    log.info("Jobs registered: heartbeat, morning_briefing, grant_radar, student_pulse, refresh_context, executive, email_triage, paper_of_the_day, summarize_sessions, weekly_review, watch_deadlines, email_burst, track_nas_metrics, daily_plan, nas_impact_score, quarterly_retrospective, goal_conflict_check, nightly_literature_synthesis, nightly_grant_drafter, weekly_database_health, weekly_comparative_analysis, weekly_hypothesis_generator, retrieval_quality_monitor, aquarium_audit, replication_docs, preference_consolidator, midday_check, deadline_countdown, next_action_filler, meeting_prep, vip_email_watch, nas_pipeline_health, cross_project_synthesis, student_agenda_drafter, populate_project_keywords, publish_aquarium, exploratory_analysis, nas_case_packet, rebuild_voice_index, publish_dashboard, publish_abilities, wiki_janitor, refresh_enrichment, improve_wiki, projects_mirror, contradictions_index, open_questions_index, gloss_harvester, method_promoter, prereg_monday, prereg_t7_sweep, midday_lit_pulse, citation_watch, paper_radar, database_pulse (sync_goals_sheet RETIRED — use export_state_to_sheet tool)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    _migrate()
    scheduler = AsyncIOScheduler()
    register_jobs(scheduler)
    scheduler.start()
    log.info("Tealc scheduler started.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Tealc scheduler stopped.")


if __name__ == "__main__":
    asyncio.run(main())
