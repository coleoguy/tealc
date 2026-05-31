#!/bin/bash
# Daily Tealc agent.db backup.
#
# Triggered by launchd via ~/Library/LaunchAgents/com.blackmon.tealc-backup.plist
# at 04:30 local time. Can also be invoked by hand.
#
# Uses SQLite's online .backup API — safe to run while the scheduler / chat
# have the DB open (WAL mode). Does NOT need to read/write inside Google
# Drive's CloudStorage path for the primary write, so launchd-spawn TCC
# restrictions don't apply. The Drive mirror is a SECONDARY copy via plain
# `cp` — failure to mirror does NOT fail the backup; it just logs a WARN.
#
# Layout:
#   primary:   ~/Library/Application Support/tealc/backups/agent_YYYY-MM-DD.db
#   mirror:    /Users/blackmon/Library/CloudStorage/GoogleDrive-.../My Drive/
#              00-Lab-Agent/deployment/snapshots/backups/agent_YYYY-MM-DD.db
#
# The mirror gives a cloud-resident copy for disaster recovery / new-Mac
# restore without exposing the LIVE DB to Drive sync (which previously
# zeroed it twice mid-write).
#
# This file is the canonical source. Live copy at:
#   ~/Library/Application Support/tealc/bin/backup-db.sh
# Use deployment/bin/cutover.sh to mirror this file into AppSupport.

set -eu
SRC="$HOME/Library/Application Support/tealc/agent.db"
DEST_DIR="$HOME/Library/Application Support/tealc/backups"
LOG="$HOME/Library/Application Support/tealc/backup.log"
RETAIN_DAYS=30

# Drive mirror target (best-effort; safe to be missing)
DRIVE_MIRROR_DIR="/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/deployment/snapshots/backups"
DRIVE_RETAIN_DAYS=60   # keep slightly longer in Drive for off-machine recovery

mkdir -p "$DEST_DIR"
DATE=$(date '+%Y-%m-%d')
DEST="$DEST_DIR/agent_${DATE}.db"
TS=$(date '+%Y-%m-%dT%H:%M:%S%z')

log() { echo "$TS $*" >> "$LOG"; }

if [ ! -f "$SRC" ]; then
    log "[ERR] source DB not found: $SRC"
    exit 1
fi

/usr/bin/sqlite3 "$SRC" ".backup '$DEST'"

if [ ! -s "$DEST" ]; then
    log "[ERR] backup file empty or missing after .backup: $DEST"
    exit 1
fi

SIZE=$(stat -f "%z" "$DEST")
log "[OK] $DEST (${SIZE} bytes)"

# Quick integrity check on the freshly written backup
INTEG=$(/usr/bin/sqlite3 "$DEST" "PRAGMA integrity_check" 2>&1 || true)
if [ "$INTEG" != "ok" ]; then
    log "[WARN] integrity_check on backup returned: $INTEG"
fi

# Prune local backups older than $RETAIN_DAYS days.
PRUNED=$(/usr/bin/find "$DEST_DIR" -name "agent_*.db" -mtime +${RETAIN_DAYS} -print -delete 2>/dev/null || true)
if [ -n "$PRUNED" ]; then
    log "[PRUNE] removed: $(echo "$PRUNED" | tr '\n' ' ')"
fi

# --- Drive mirror (best-effort, never fails the backup) -------------------
if [ -d "$DRIVE_MIRROR_DIR" ] || /bin/mkdir -p "$DRIVE_MIRROR_DIR" 2>/dev/null; then
    DRIVE_DEST="$DRIVE_MIRROR_DIR/agent_${DATE}.db"
    if /bin/cp "$DEST" "$DRIVE_DEST" 2>>"$LOG"; then
        D_SIZE=$(stat -f "%z" "$DRIVE_DEST" 2>/dev/null || echo "?")
        log "[MIRROR-OK] $DRIVE_DEST (${D_SIZE} bytes)"
    else
        log "[MIRROR-WARN] cp to Drive mirror failed; check Drive sync state"
    fi
    # Prune Drive mirror older than $DRIVE_RETAIN_DAYS days
    D_PRUNED=$(/usr/bin/find "$DRIVE_MIRROR_DIR" -name "agent_*.db" -mtime +${DRIVE_RETAIN_DAYS} -print -delete 2>/dev/null || true)
    if [ -n "$D_PRUNED" ]; then
        log "[MIRROR-PRUNE] removed: $(echo "$D_PRUNED" | tr '\n' ' ')"
    fi
else
    log "[MIRROR-SKIP] Drive mirror dir unavailable: $DRIVE_MIRROR_DIR"
fi
