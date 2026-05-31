#!/bin/bash
# Build a sanitized public-template version of this lab-agent.
#
# Usage:
#     bash scripts/build_template.sh [DEST]
#
# DEST defaults to ~/Desktop/tealc-template/
#
# Reproducible: re-run anytime you want to refresh the template from the
# current working tree. Safe — only copies into DEST, never modifies the
# source tree.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$HOME/Desktop/tealc-template}"

# ---------------------------------------------------------------------------
# Load exclude_extras from template_sync_config.json (optional)
# Lets Heath add extra lab-specific files to strip without editing this script.
# ---------------------------------------------------------------------------
EXTRA_EXCLUDES=()
CONFIG_FILE="$SRC/data/template_sync_config.json"
if [ -f "$CONFIG_FILE" ] && command -v jq &>/dev/null; then
    while IFS= read -r extra; do
        [ -n "$extra" ] && EXTRA_EXCLUDES+=("--exclude=$extra")
    done < <(jq -r '.exclude_extras[]? // empty' "$CONFIG_FILE" 2>/dev/null || true)
fi

echo "==> Building Tealc public template"
echo "    SRC:  $SRC"
echo "    DEST: $DEST"
echo ""

if [ -e "$DEST" ] && [ -d "$DEST/.git" ]; then
    echo "Destination is an existing git repo — preserving .git/ and syncing incrementally."
    echo "  (rsync --delete will remove tracked files no longer in source, but .git stays.)"
    INCREMENTAL=1
elif [ -e "$DEST" ]; then
    echo "Destination already exists (no .git) — removing for fresh build..."
    rm -rf "$DEST"
    INCREMENTAL=0
else
    INCREMENTAL=0
fi

mkdir -p "$DEST"

# ---------------------------------------------------------------------------
# Step 1 — rsync the codebase, excluding everything researcher-specific
# ---------------------------------------------------------------------------
# When syncing into an existing git repo, --delete removes tracked files
# whose source counterpart was deleted. .git/ is excluded so commit history
# is never touched.
RSYNC_DELETE_FLAG=""
if [ "$INCREMENTAL" = "1" ]; then
    RSYNC_DELETE_FLAG="--delete"
fi
rsync -a $RSYNC_DELETE_FLAG \
    --exclude='.git/' \
    --exclude='.gitignore' \
    --exclude='LICENSE' \
    --exclude='README.md' \
    --exclude='*.png' \
    --exclude='*.jpg' \
    --exclude='*.svg' \
    --exclude='CONTRIBUTING.md' \
    --exclude='CHANGELOG.md' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='.env.backup' \
    --exclude='.claude/' \
    --exclude='cloudflare/.wrangler/' \
    --exclude='cloudflare/README.md' \
    --exclude='evaluations/README.md' \
    --exclude='google_credentials.json' \
    --exclude='google_token.json' \
    --exclude='data/agent.db*' \
    --exclude='data/scheduler.pid' \
    --exclude='data/scheduler.log' \
    --exclude='data/scheduler.stdout.log' \
    --exclude='data/scheduler_heartbeat.json' \
    --exclude='data/dashboard_server.pid' \
    --exclude='data/dashboard_server.log' \
    --exclude='data/dashboard_state.json' \
    --exclude='data/chainlit.stdout.log' \
    --exclude='data/launchd.stdout.log' \
    --exclude='data/launchd.stderr.log' \
    --exclude='data/ntfy_log.jsonl' \
    --exclude='data/voice_index.pkl' \
    --exclude='data/voice_passages.json' \
    --exclude='data/voice_index_st.npz' \
    --exclude='data/voice_sentences.jsonl' \
    --exclude='data/heath_corpus.jsonl' \
    --exclude='data/post_recovery_repopulate.log' \
    --exclude='data/tier2_full_build.log' \
    --exclude='data/tier2_claims_rerun.log' \
    --exclude='public/notebook/' \
    --exclude='agent/jobs/seed_students.py' \
    --exclude='agent/jobs/nas_case_packet.py' \
    --exclude='agent/jobs/nas_impact_score.py' \
    --exclude='agent/jobs/nas_pipeline_health.py' \
    --exclude='agent/jobs/track_nas_metrics.py' \
    --exclude='agent/jobs/sync_goals_sheet.py' \
    --exclude='agent/jobs/executive.py' \
    --exclude='agent/jobs/mine_project_leads.py' \
    --exclude='agent/jobs/refresh_context.py' \
    --exclude='agent/graph.py' \
    --exclude='public/jobs.html' \
    --exclude='data/*MIRA*' \
    --exclude='data/*aims*' \
    --exclude='data/*renewal*' \
    --exclude='data/*draft*.md' \
    --exclude='data/CURE_student_*' \
    --exclude='data/README_DB_LOCATION.md' \
    --exclude='data/research_themes.md' \
    --exclude='data/agent.db.moved-out-*' \
    --exclude='data/agent.db.corrupt-*' \
    --exclude='data/heath_preferences.md' \
    --exclude='data/personality_addendum.md' \
    --exclude='data/lab_people.json' \
    --exclude='data/vip_senders.json' \
    --exclude='data/collaborators.json' \
    --exclude='data/grant_sources.json' \
    --exclude='data/known_sheets.json' \
    --exclude='data/known_sheets.json.bak-*' \
    --exclude='data/known_sheets_proposed_NOTES.md' \
    --exclude='data/known_methods.json' \
    --exclude='data/deadlines.json' \
    --exclude='data/research_topics.json' \
    --exclude='data/research_themes.md' \
    --exclude='data/last_*.json' \
    --exclude='data/oa_ingest_report.md' \
    --exclude='data/pdf_doi_map.json' \
    --exclude='data/privacy_sentinels.txt' \
    --exclude='data/ntfy_topic.txt' \
    --exclude='data/project_data_index_proposed*' \
    --exclude='data/wiki_batch_ingest_report.md' \
    --exclude='data/wiki_jargon_seed.json' \
    --exclude='data/wiki_slug_rename_report.md' \
    --exclude='data/abilities.json' \
    --exclude='data/public_abilities.json' \
    --exclude='data/tealc_config.json' \
    --exclude='data/config.json' \
    --exclude='data/template_sync_config.json' \
    --exclude='data/wiki_pdfs/' \
    --exclude='data/r_runs/' \
    --exclude='data/py_runs/' \
    --exclude='data/nas_case_plots/' \
    --exclude='data/reviewer_circle/manifest.json' \
    --exclude='HANDOFF_SCIENTIST_REFOCUS.md' \
    --exclude='BACKLOG.md' \
    --exclude='HYPOTHESIS_GATE_BRIEFING.md' \
    --exclude='IMPLEMENTATION_PLAN.md' \
    --exclude='REPLICATION.md' \
    --exclude='TEALC_SYSTEM.md' \
    --exclude='WIKI_BUILDER_HANDOFF.md' \
    --exclude='WIKI_V5_PLAN.md' \
    --exclude='Tealc_OnePager.docx' \
    --exclude='google_grant_*.md' \
    --exclude='tealc_personality_review.md' \
    --exclude='CREDENTIALS.md' \
    --exclude='anthropic_ai_for_science_application.Rmd' \
    --exclude='memory/' \
    "${EXTRA_EXCLUDES[@]+"${EXTRA_EXCLUDES[@]}"}" \
    "$SRC/" "$DEST/"

# ---------------------------------------------------------------------------
# Step 2 — ensure required directories exist (data subdirs)
# ---------------------------------------------------------------------------
mkdir -p "$DEST/data"
mkdir -p "$DEST/data/reviewer_circle"

# ---------------------------------------------------------------------------
# Step 3 — clear the local copy of memory + secrets folder (defense in depth)
# ---------------------------------------------------------------------------
find "$DEST" -name "*.pid" -delete
find "$DEST" -name "*.log" -delete
find "$DEST" -name ".DS_Store" -delete
find "$DEST" -name "*.pyc" -delete
find "$DEST" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 4 — purge any historically-tracked files that are now in the exclude
# list. Without this, rsync's --exclude only SKIPS files in source — old
# copies of researcher-specific files committed before they were excluded
# would survive in dest. This pass deletes them so `git status` will register
# the deletion in the next commit.
# ---------------------------------------------------------------------------
PURGE_PATHS=(
    "agent/jobs/seed_students.py"
    "agent/jobs/nas_case_packet.py"
    "agent/jobs/nas_impact_score.py"
    "agent/jobs/nas_pipeline_health.py"
    "agent/jobs/track_nas_metrics.py"
    "agent/jobs/sync_goals_sheet.py"
    "agent/jobs/executive.py"
    "agent/jobs/mine_project_leads.py"
    "agent/jobs/refresh_context.py"
    "agent/graph.py"
    "public/jobs.html"
    "data/voice_index.pkl"
    "data/voice_passages.json"
    "data/voice_index_st.npz"
    "data/voice_sentences.jsonl"
    "data/heath_corpus.jsonl"
    "data/research_themes.md"
    "data/heath_preferences.md"
    "data/personality_addendum.md"
    "data/lab_people.json"
    "data/vip_senders.json"
    "data/collaborators.json"
    "data/grant_sources.json"
    "data/known_sheets.json"
    "data/known_methods.json"
    "data/deadlines.json"
    "data/research_topics.json"
    "data/pdf_doi_map.json"
    "data/privacy_sentinels.txt"
    "data/ntfy_topic.txt"
    "data/abilities.json"
    "data/public_abilities.json"
    "data/tealc_config.json"
    "data/config.json"
    "data/template_sync_config.json"
    "data/README_DB_LOCATION.md"
    "evaluations/README.md"
    "cloudflare/README.md"
    "HANDOFF_SCIENTIST_REFOCUS.md"
    "BACKLOG.md"
    "HYPOTHESIS_GATE_BRIEFING.md"
    "IMPLEMENTATION_PLAN.md"
    "REPLICATION.md"
    "TEALC_SYSTEM.md"
    "WIKI_BUILDER_HANDOFF.md"
    "WIKI_V5_PLAN.md"
    "Tealc_OnePager.docx"
    "tealc_personality_review.md"
    "CREDENTIALS.md"
    "anthropic_ai_for_science_application.Rmd"
    ".env"
    ".env.backup"
    "google_credentials.json"
    "google_token.json"
)
for p in "${PURGE_PATHS[@]}"; do
    if [ -e "$DEST/$p" ]; then
        rm -rf "$DEST/$p"
    fi
done
# Glob-based purges for patterns
for pat in "data/*MIRA*" "data/*aims*" "data/*renewal*" "data/*draft*.md" \
           "data/CURE_student_*" "data/agent.db*" "data/*.log" \
           "data/last_*.json" "data/oa_ingest_report.md" \
           "data/known_sheets*.bak-*" "data/known_sheets_proposed_NOTES.md" \
           "data/project_data_index_proposed*" \
           "data/wiki_batch_ingest_report.md" "data/wiki_jargon_seed.json" \
           "data/wiki_slug_rename_report.md" \
           "data/post_recovery_repopulate.log" \
           "data/tier2_full_build.log" "data/tier2_claims_rerun.log" \
           "google_grant_*.md" \
           ".env.*" \
           "data/agent.db.moved-out-*" \
           "data/agent.db.corrupt-*"; do
    rm -rf $DEST/$pat 2>/dev/null || true
done
# Directory purges
for d in "data/wiki_pdfs" "data/r_runs" "data/py_runs" "data/nas_case_plots" \
         "data/reviewer_circle/manifest.json" "public/notebook" \
         "memory" ".claude" "cloudflare/.wrangler"; do
    rm -rf "$DEST/$d" 2>/dev/null || true
done

echo ""
echo "==> Initial copy complete."
echo ""
echo "Files copied: $(find "$DEST" -type f | wc -l | tr -d ' ')"
echo "Total size:   $(du -sh "$DEST" | awk '{print $1}')"
echo ""
echo "Next steps:"
echo "  1. Run privacy audit:   bash scripts/privacy_audit_template.sh $DEST"
echo "  2. Or run full sync:    bash scripts/sync_template_repo.sh [--push]"
echo "  3. Fill in repo info:   data/template_sync_config.json"
