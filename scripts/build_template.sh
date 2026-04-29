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

echo "==> Building Tealc public template"
echo "    SRC:  $SRC"
echo "    DEST: $DEST"
echo ""

if [ -e "$DEST" ]; then
    echo "Destination already exists. Removing for fresh build..."
    rm -rf "$DEST"
fi

mkdir -p "$DEST"

# ---------------------------------------------------------------------------
# Step 1 — rsync the codebase, excluding everything researcher-specific
# ---------------------------------------------------------------------------
rsync -a \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='.env' \
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

echo ""
echo "==> Initial copy complete."
echo ""
echo "Files copied: $(find "$DEST" -type f | wc -l | tr -d ' ')"
echo "Total size:   $(du -sh "$DEST" | awk '{print $1}')"
echo ""
echo "Next steps (run separately):"
echo "  1. Scrub source code for personal identifiers"
echo "  2. Add README.md, LICENSE, .env.example, .gitignore, setup.py"
echo "  3. Create template config files in data/"
echo "  4. Initialize git repo: cd $DEST && git init"
