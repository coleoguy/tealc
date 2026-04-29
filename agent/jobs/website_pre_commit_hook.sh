#!/usr/bin/env bash
# Pre-commit hook installed by Tealc's website_git.py.
# Rejects commits whose staged diff contains any privacy-sentinel string —
# strings placed deliberately into tier-4 (email) or private-repo content so
# they can be detected if they ever leak into the public website.
#
# Sentinel list lives at __TEALC_SENTINELS_PATH__ (baked in at install time by
# agent.jobs.website_git::install_privacy_hook).
#
# To bypass (not recommended — only for known-safe automated tests):
#     TEALC_SKIP_PRIVACY_CHECK=1 git commit ...

set -euo pipefail

SENTINELS_FILE="__TEALC_SENTINELS_PATH__"

if [[ "${TEALC_SKIP_PRIVACY_CHECK:-0}" == "1" ]]; then
    echo "[tealc pre-commit] TEALC_SKIP_PRIVACY_CHECK=1 — skipping privacy scan." >&2
    exit 0
fi

if [[ ! -f "$SENTINELS_FILE" ]]; then
    echo "[tealc pre-commit] WARNING: sentinel file not found at $SENTINELS_FILE" >&2
    echo "[tealc pre-commit] Privacy scan cannot run. Commit allowed; create the file to enable scanning." >&2
    exit 0
fi

STAGED_DIFF=$(git diff --cached || true)

if [[ -z "$STAGED_DIFF" ]]; then
    exit 0
fi

# Iterate over sentinels. Skip blank lines and comment lines.
while IFS= read -r sentinel || [[ -n "$sentinel" ]]; do
    sentinel="${sentinel#"${sentinel%%[![:space:]]*}"}"   # ltrim
    sentinel="${sentinel%"${sentinel##*[![:space:]]}"}"   # rtrim
    [[ -z "$sentinel" ]] && continue
    [[ "$sentinel" =~ ^# ]] && continue
    if grep -qF -- "$sentinel" <<<"$STAGED_DIFF"; then
        echo "[tealc pre-commit] REJECTED: staged content contains a privacy sentinel." >&2
        echo "[tealc pre-commit]           Sentinel list: $SENTINELS_FILE" >&2
        echo "[tealc pre-commit]           Remove the offending content before committing." >&2
        exit 1
    fi
done <"$SENTINELS_FILE"

exit 0
