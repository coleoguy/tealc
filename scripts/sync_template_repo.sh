#!/bin/bash
# Sync the Tealc public-template repo from the working copy.
#
# Usage:
#     bash scripts/sync_template_repo.sh [--push] [--config PATH]
#
# Flags:
#   --push        After a clean audit, commit + push to the configured remote.
#                 Without this flag the script runs as a dry-run and only
#                 shows what would change.
#   --config PATH Path to config JSON (default: data/template_sync_config.json)
#
# Workflow:
#   1. Read config — fail if repo_path is empty.
#   2. Build the sanitized template copy (scripts/build_template.sh).
#   3. Run privacy audit (scripts/privacy_audit_template.sh) — exit 1 on failure.
#   4. Show git diff preview of the template repo.
#   5. If --push AND audit clean: git add -A, commit, push.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
DO_PUSH=0
CONFIG_PATH="$PROJECT_ROOT/data/template_sync_config.json"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --push)
            DO_PUSH=1
            shift
            ;;
        --config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: bash scripts/sync_template_repo.sh [--push] [--config PATH]"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; RESET=''
fi

echo -e "${CYAN}==> Tealc template-repo sync${RESET}"
echo "    Config: $CONFIG_PATH"
echo "    Push:   $( [ "$DO_PUSH" -eq 1 ] && echo 'YES' || echo 'no (dry run)' )"
echo ""

# ---------------------------------------------------------------------------
# Step 1 — Read and validate config
# ---------------------------------------------------------------------------
if [ ! -f "$CONFIG_PATH" ]; then
    echo -e "${RED}ERROR: Config not found: $CONFIG_PATH${RESET}"
    echo "       Create it by copying the template at data/template_sync_config.json"
    echo "       and filling in repo_path and git_remote."
    exit 1
fi

if ! command -v jq &>/dev/null; then
    echo -e "${RED}ERROR: jq is required. Install with: brew install jq${RESET}"
    exit 1
fi

REPO_PATH=$(jq -r '.repo_path // empty' "$CONFIG_PATH")
GIT_REMOTE=$(jq -r '.git_remote // empty' "$CONFIG_PATH")
BRANCH=$(jq -r '.branch // "main"' "$CONFIG_PATH")

if [ -z "$REPO_PATH" ]; then
    echo -e "${RED}ERROR: repo_path is empty in $CONFIG_PATH${RESET}"
    echo "       Set it to the absolute path of your local public-template git clone."
    exit 1
fi

if [ "$DO_PUSH" -eq 1 ] && [ -z "$GIT_REMOTE" ]; then
    echo -e "${RED}ERROR: git_remote is empty in $CONFIG_PATH but --push was requested.${RESET}"
    echo "       Set git_remote to your GitHub repo URL, e.g.:"
    echo "       \"git_remote\": \"git@github.com:YOUR_ORG/tealc.git\""
    exit 1
fi

echo "    repo_path:  $REPO_PATH"
echo "    git_remote: ${GIT_REMOTE:-(not set)}"
echo "    branch:     $BRANCH"
echo ""

# ---------------------------------------------------------------------------
# Step 2 — Build the sanitized template copy
# ---------------------------------------------------------------------------
echo -e "${CYAN}==> Step 1/4: Building template copy...${RESET}"
bash "$SCRIPT_DIR/build_template.sh" "$REPO_PATH"
echo ""

# ---------------------------------------------------------------------------
# Step 3 — Privacy audit
# ---------------------------------------------------------------------------
echo -e "${CYAN}==> Step 2/4: Running privacy audit...${RESET}"
if ! bash "$SCRIPT_DIR/privacy_audit_template.sh" "$REPO_PATH"; then
    echo ""
    echo -e "${RED}BLOCKED: Privacy audit failed. Commit aborted.${RESET}"
    echo "         Fix all flagged items in the template before re-running."
    exit 1
fi
echo ""

# ---------------------------------------------------------------------------
# Step 4 — Git diff preview
# ---------------------------------------------------------------------------
echo -e "${CYAN}==> Step 3/4: Git status in template repo...${RESET}"
if [ ! -d "$REPO_PATH/.git" ]; then
    echo -e "${YELLOW}WARNING: $REPO_PATH is not a git repo yet.${RESET}"
    echo "         To initialise it:"
    echo "           cd \"$REPO_PATH\""
    echo "           git init && git checkout -b $BRANCH"
    if [ -n "$GIT_REMOTE" ]; then
        echo "           git remote add origin $GIT_REMOTE"
    fi
    echo ""
    if [ "$DO_PUSH" -eq 1 ]; then
        echo -e "${RED}ERROR: Cannot push — not a git repo. Initialise it first.${RESET}"
        exit 1
    fi
else
    (cd "$REPO_PATH" && git status)
    echo ""
    DIFF_OUTPUT=$(cd "$REPO_PATH" && git diff 2>/dev/null || true)
    if [ -n "$DIFF_OUTPUT" ]; then
        echo "--- Diff of modified tracked files (first 200 lines) ---"
        echo "$DIFF_OUTPUT" | head -200
        echo ""
    fi
fi

# ---------------------------------------------------------------------------
# Step 5 — Commit + push (only if --push and audit clean)
# ---------------------------------------------------------------------------
if [ "$DO_PUSH" -eq 0 ]; then
    echo -e "${YELLOW}Dry run — pass --push to commit and push. Diff above shows what would change.${RESET}"
    exit 0
fi

echo -e "${CYAN}==> Step 4/4: Committing and pushing...${RESET}"
cd "$REPO_PATH"

# Ensure remote is set correctly
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [ -z "$CURRENT_REMOTE" ]; then
    echo "    Adding remote origin: $GIT_REMOTE"
    git remote add origin "$GIT_REMOTE"
elif [ "$CURRENT_REMOTE" != "$GIT_REMOTE" ]; then
    echo -e "${YELLOW}WARNING: Remote origin ($CURRENT_REMOTE) differs from config ($GIT_REMOTE).${RESET}"
    echo "         Using existing remote. Update git_remote in config if needed."
fi

git add -A

# Check if there's anything to commit
if git diff --cached --quiet; then
    echo -e "${GREEN}Nothing to commit — template repo is already up to date.${RESET}"
    exit 0
fi

COMMIT_MSG="Sync from working copy at $(date -Iseconds)"
git commit -m "$COMMIT_MSG"
echo "    Committed: $COMMIT_MSG"

git push -u origin "$BRANCH"
echo -e "${GREEN}==> Pushed to $GIT_REMOTE ($BRANCH)${RESET}"
