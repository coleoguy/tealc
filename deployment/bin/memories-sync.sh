#!/bin/bash
# Periodic mirror of TEALC memory-tool storage from AppSupport into Drive.
#
# Memories live at ~/Library/Application Support/tealc/memories/ (the active
# location used by agent/memory_backend.py, overridable via TEALC_MEMORY_DIR).
# This script rsyncs that tree into Drive's deployment/snapshots/memories/
# so a new-Mac restore has the latest memory state.
#
# Memories are markdown files written via atomic temp-file + rename, NOT
# SQLite — Drive sync handles them safely. They're still kept primarily at
# AppSupport (per current architecture) and only mirrored periodically.
#
# rsync flags:
#   -a  archive (preserves perms / mtimes / symlinks)
#   --delete  remove destination files that no longer exist in source
#   -q  quiet (only show errors)
#
# This file is the canonical source. Live copy at:
#   ~/Library/Application Support/tealc/bin/memories-sync.sh

set -eu
SRC="$HOME/Library/Application Support/tealc/memories/"
DEST="/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/deployment/snapshots/memories/"
LOG="$HOME/Library/Application Support/tealc/memories-sync.log"
TS=$(date '+%Y-%m-%dT%H:%M:%S%z')

log() { echo "$TS $*" >> "$LOG"; }

if [ ! -d "$SRC" ]; then
    log "[ERR] source memories dir not found: $SRC"
    exit 1
fi

# Ensure destination exists (Drive mounted)
if ! /bin/mkdir -p "$DEST" 2>/dev/null; then
    log "[ERR] cannot create destination (Drive not mounted?): $DEST"
    exit 1
fi

# rsync content
if /usr/bin/rsync -a --delete -q "$SRC" "$DEST" 2>>"$LOG"; then
    COUNT=$(/usr/bin/find "$DEST" -type f 2>/dev/null | wc -l | tr -d ' ')
    log "[OK] mirrored $COUNT files"
else
    log "[ERR] rsync failed (rc=$?); see preceding lines"
    exit 1
fi
