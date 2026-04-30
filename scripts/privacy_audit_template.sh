#!/bin/bash
# Privacy audit for the Tealc public-template directory.
#
# Usage:
#     bash scripts/privacy_audit_template.sh [TEMPLATE_DIR]
#
# Exit 0  — no forbidden matches found (safe to publish)
# Exit 1  — one or more forbidden patterns found (BLOCK push)
#
# Greps for:
#   1. Hard-coded personal identifiers (blackmon, tamu.edu, etc.)
#   2. Names of anyone currently in lab_people.json
#   3. Grant-code-shaped patterns (R01, MIRA, dollar amounts near grant words)
#   4. DOI patterns (flagged for review — not a hard block, but reported)
#
# Binary files and image files are skipped automatically via grep's -I flag.

set -euo pipefail

TEMPLATE_DIR="${1:-$HOME/Desktop/tealc-template}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAB_PEOPLE_JSON="$PROJECT_ROOT/data/lab_people.json"

# ---------------------------------------------------------------------------
# Colour helpers (no-op if not a terminal)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; RESET=''
fi

echo "==> Privacy audit of: $TEMPLATE_DIR"
echo ""

if [ ! -d "$TEMPLATE_DIR" ]; then
    echo "${RED}ERROR: Template directory not found: $TEMPLATE_DIR${RESET}"
    echo "       Run scripts/build_template.sh first."
    exit 1
fi

# ---------------------------------------------------------------------------
# Build grep exclusion flags (skip .git, binary, images)
# ---------------------------------------------------------------------------
GREP_EXCLUDES=(
    "--exclude-dir=.git"
    "--exclude=*.png" "--exclude=*.jpg" "--exclude=*.jpeg"
    "--exclude=*.gif" "--exclude=*.svg" "--exclude=*.ico"
    "--exclude=*.pdf" "--exclude=*.pkl" "--exclude=*.npz"
    "--exclude=*.db"  "--exclude=*.db-shm" "--exclude=*.db-wal"
)

FOUND=0  # will be set to 1 if any HARD-BLOCK pattern matches
WARNED=0 # will be set to 1 if any WARN-ONLY pattern matches

# ---------------------------------------------------------------------------
# Helper — run one grep pass, print matches, return match count
# ---------------------------------------------------------------------------
grep_pass() {
    local label="$1"
    local pattern="$2"
    local mode="$3"   # "hard" or "warn"

    local matches
    matches=$(grep -rIn --binary-files=without-match \
        "${GREP_EXCLUDES[@]}" \
        -E "$pattern" \
        "$TEMPLATE_DIR" 2>/dev/null || true)

    if [ -n "$matches" ]; then
        if [ "$mode" = "hard" ]; then
            echo -e "${RED}[FAIL] $label${RESET}"
            echo "$matches" | while IFS= read -r line; do
                echo "       $line"
            done
            FOUND=1
        else
            echo -e "${YELLOW}[WARN] $label (review before pushing)${RESET}"
            echo "$matches" | while IFS= read -r line; do
                echo "       $line"
            done
            WARNED=1
        fi
        echo ""
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 1. Hard-block: known personal identifiers
# ---------------------------------------------------------------------------
# Files that LEGITIMATELY mention these patterns by design (privacy
# infrastructure: blind.py defines what to redact; this script defines what
# to detect). They are excluded from this pass but subject to all others.
PERSON_ID_FILE_ALLOWLIST=(
    "--exclude=privacy_audit_template.sh"
    "--exclude=blind.py"
    "--exclude=privacy.py"
    # User-managed public-repo attribution files (rsync preserves them across syncs)
    "--exclude=LICENSE"
    "--exclude=README.md"
    "--exclude=CONTRIBUTING.md"
    "--exclude=CHANGELOG.md"
)

grep_pass_person_id() {
    local label="$1"
    local pattern="$2"
    local matches
    matches=$(grep -rIn --binary-files=without-match \
        "${GREP_EXCLUDES[@]}" "${PERSON_ID_FILE_ALLOWLIST[@]}" \
        -E "$pattern" \
        "$TEMPLATE_DIR" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo -e "${RED}[FAIL] $label${RESET}"
        echo "$matches" | while IFS= read -r line; do
            echo "       $line"
        done
        echo ""
        FOUND=1
    fi
}

FORBIDDEN_REGEX='blackmon|Blackmon|tamu\.edu|Heath Blackmon|coleoguy|Heath B\.'
grep_pass_person_id "Personal identifiers (blackmon / tamu.edu / coleoguy / Heath B.)" \
    "$FORBIDDEN_REGEX"

# ---------------------------------------------------------------------------
# 2. Hard-block: lab member names from lab_people.json
# ---------------------------------------------------------------------------
if [ -f "$LAB_PEOPLE_JSON" ] && command -v jq &>/dev/null; then
    LAB_PEOPLE_PATTERN=$(jq -r '.names | join("|")' "$LAB_PEOPLE_JSON" 2>/dev/null || echo "")
    if [ -n "$LAB_PEOPLE_PATTERN" ]; then
        grep_pass "Lab member names (from data/lab_people.json)" \
            "$LAB_PEOPLE_PATTERN" "hard" || true
    else
        echo "${YELLOW}[SKIP] lab_people.json found but produced empty pattern — check jq parse.${RESET}"
        echo ""
    fi
elif ! command -v jq &>/dev/null; then
    echo "${YELLOW}[SKIP] jq not installed — lab_people.json audit skipped. Install jq to enable.${RESET}"
    echo ""
else
    echo "${YELLOW}[SKIP] data/lab_people.json not found in project root — skipping lab-names audit.${RESET}"
    echo ""
fi

# ---------------------------------------------------------------------------
# 3. Hard-block: grant codes and dollar amounts near grant context
# ---------------------------------------------------------------------------
# Files that LEGITIMATELY reference grant-mechanism codes (privacy infrastructure
# itself, agency-classifier logic, audit script). Excluded from the grant-code
# pass; still subject to the personal-identifiers + lab-names passes.
GRANT_CODE_FILE_ALLOWLIST=(
    "--exclude=privacy.py"
    "--exclude=privacy_audit_template.sh"
    "--exclude=build_template.sh"
    "--exclude=template_sync_config.json"
    "--exclude=grant_radar.py"
    "--exclude=web_grant_radar.py"
    "--exclude=dashboard_server.py"
    "--exclude=dashboard.js"
    "--exclude=submission_review.py"
    "--exclude=blind.py"           # evaluations/blind.py — same DENY_PATTERNS regex
    "--exclude=grants.py"          # agent/apis/grants.py — docstring example
    "--exclude=tools.py"            # docstring references to MIRA as a venue category
)

# A specialized grep_pass variant that adds the allowlist to the excludes.
grep_pass_grant_code() {
    local label="$1"
    local pattern="$2"
    local matches
    matches=$(grep -rIn --binary-files=without-match \
        "${GREP_EXCLUDES[@]}" "${GRANT_CODE_FILE_ALLOWLIST[@]}" \
        -E "$pattern" \
        "$TEMPLATE_DIR" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo -e "${RED}[FAIL] $label${RESET}"
        echo "$matches" | while IFS= read -r line; do
            echo "       $line"
        done
        echo ""
        FOUND=1
    fi
}

# Grant mechanism codes — fail only outside the privacy-infrastructure allowlist
GRANT_CODE_REGEX='R01[A-Z]{2}[0-9]+|R35[A-Z]{2}[0-9]+|R21[A-Z]{2}[0-9]+|R[0-9]{2}HG[0-9]{6,}|MIRA|CPRIT'
grep_pass_grant_code "Grant mechanism codes (R01/R35/R21/MIRA/CPRIT + specific IDs)" \
    "$GRANT_CODE_REGEX"

# Dollar amounts (any $NNN,NNN — broad; we don't want budget figures leaking)
DOLLAR_REGEX='\$[0-9]{1,3}(,[0-9]{3})+'
grep_pass "Dollar amounts (potential budget figures)" \
    "$DOLLAR_REGEX" "hard" || true

# Generic funding-agency mentions with amounts
FUNDING_COMBO_REGEX='(NIH|NSF|NIGMS|NHGRI|CPRIT).{0,60}\$[0-9]'
grep_pass "Funding-agency + dollar amount combinations" \
    "$FUNDING_COMBO_REGEX" "hard" || true

# ---------------------------------------------------------------------------
# 4. Warn-only: DOI patterns (OK to publish but flag for human review)
# ---------------------------------------------------------------------------
DOI_REGEX='10\.[0-9]{4,}/[^\s"'"'"'>,]+'
grep_pass "DOI patterns (flag for review — not auto-blocked)" \
    "$DOI_REGEX" "warn" || true

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if [ "$FOUND" -eq 0 ] && [ "$WARNED" -eq 0 ]; then
    echo -e "${GREEN}==> Audit PASSED — no forbidden patterns found.${RESET}"
    exit 0
elif [ "$FOUND" -eq 0 ]; then
    echo -e "${YELLOW}==> Audit PASSED WITH WARNINGS — review items above before pushing.${RESET}"
    exit 0
else
    echo -e "${RED}==> Audit FAILED — forbidden patterns found. Fix before pushing.${RESET}"
    exit 1
fi
