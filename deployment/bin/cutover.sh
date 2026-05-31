#!/bin/bash
# cutover.sh — migrate the LIVE TEALC ops layer to use deployment/ as the source of truth.
#
# What this does (on the CURRENT Mac, run AFTER you stop TEALC chat):
#   1. Stops the running scheduler + backup LaunchAgents
#   2. Copies deployment/bin/{scheduler-wrapper,backup-db,memories-sync}.sh
#      into ~/Library/Application Support/tealc/bin/  (overwriting the live versions)
#   3. Copies deployment/launchd/*.plist into ~/Library/LaunchAgents/
#      (overwriting the live versions)
#   4. Reloads scheduler + backup agents
#   5. (Optionally) loads the new memories-sync agent at 30-min intervals
#   6. Runs a one-shot test of backup-db.sh and memories-sync.sh
#
# The live LIVE DB at ~/Library/Application Support/tealc/agent.db is NEVER
# touched — only scripts/plists are replaced. The live DB stays at AppSupport
# (canonical for writes); the new backup-db.sh additionally mirrors snapshots
# to deployment/snapshots/backups/ in Drive.
#
# Run this with the chat ALREADY stopped to avoid any race with active SQL.
# Run from the Drive deployment/bin/ directory:
#   bash "/path/to/My Drive/00-Lab-Agent/deployment/bin/cutover.sh"

set -eu

DEPLOY_DIR="/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/deployment"
APPSUPPORT="$HOME/Library/Application Support/tealc"
LAUNCHAGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*"; exit 1; }
step() { printf "\n\033[1m▸ %s\033[0m\n" "$*"; }

# --- Preflight checks -----------------------------------------------------
step "Preflight checks"
[ -d "$DEPLOY_DIR" ] || err "deployment dir not found: $DEPLOY_DIR (Drive mounted?)"
[ -f "$DEPLOY_DIR/bin/scheduler-wrapper.sh" ] || err "missing $DEPLOY_DIR/bin/scheduler-wrapper.sh"
[ -f "$DEPLOY_DIR/bin/backup-db.sh" ] || err "missing $DEPLOY_DIR/bin/backup-db.sh"
[ -f "$DEPLOY_DIR/bin/memories-sync.sh" ] || err "missing $DEPLOY_DIR/bin/memories-sync.sh"
[ -f "$DEPLOY_DIR/launchd/com.blackmon.tealc-scheduler.plist" ] || err "missing scheduler plist"
[ -f "$DEPLOY_DIR/launchd/com.blackmon.tealc-backup.plist" ] || err "missing backup plist"
[ -f "$DEPLOY_DIR/launchd/com.blackmon.tealc-memories-sync.plist" ] || err "missing memories-sync plist"
mkdir -p "$APPSUPPORT/bin"
ok "deployment artifacts present"

# --- Stop running agents --------------------------------------------------
step "Stopping running LaunchAgents (safe even if not running)"
launchctl bootout "gui/$UID_NUM/com.blackmon.tealc-scheduler" 2>/dev/null && ok "scheduler stopped" || warn "scheduler was not running"
launchctl bootout "gui/$UID_NUM/com.blackmon.tealc-backup" 2>/dev/null && ok "backup stopped" || warn "backup was not running"
launchctl bootout "gui/$UID_NUM/com.blackmon.tealc-memories-sync" 2>/dev/null && ok "memories-sync stopped" || warn "memories-sync was not loaded"

# --- Mirror bin scripts ---------------------------------------------------
step "Mirroring bin/ scripts into AppSupport"
cp "$DEPLOY_DIR/bin/scheduler-wrapper.sh" "$APPSUPPORT/bin/scheduler-wrapper.sh"
cp "$DEPLOY_DIR/bin/backup-db.sh"         "$APPSUPPORT/bin/backup-db.sh"
cp "$DEPLOY_DIR/bin/memories-sync.sh"     "$APPSUPPORT/bin/memories-sync.sh"
chmod 0755 "$APPSUPPORT/bin/scheduler-wrapper.sh" "$APPSUPPORT/bin/backup-db.sh" "$APPSUPPORT/bin/memories-sync.sh"
ok "bin scripts copied + chmod 0755"

# --- Mirror plists --------------------------------------------------------
step "Mirroring plists into ~/Library/LaunchAgents"
cp "$DEPLOY_DIR/launchd/com.blackmon.tealc-scheduler.plist"     "$LAUNCHAGENTS/com.blackmon.tealc-scheduler.plist"
cp "$DEPLOY_DIR/launchd/com.blackmon.tealc-backup.plist"        "$LAUNCHAGENTS/com.blackmon.tealc-backup.plist"
cp "$DEPLOY_DIR/launchd/com.blackmon.tealc-memories-sync.plist" "$LAUNCHAGENTS/com.blackmon.tealc-memories-sync.plist"
ok "plists copied"

# --- Reload agents --------------------------------------------------------
step "Reloading LaunchAgents"
launchctl bootstrap "gui/$UID_NUM" "$LAUNCHAGENTS/com.blackmon.tealc-scheduler.plist" && ok "scheduler loaded" || err "scheduler failed to load"
launchctl bootstrap "gui/$UID_NUM" "$LAUNCHAGENTS/com.blackmon.tealc-backup.plist"    && ok "backup loaded"    || err "backup failed to load"
launchctl bootstrap "gui/$UID_NUM" "$LAUNCHAGENTS/com.blackmon.tealc-memories-sync.plist" && ok "memories-sync loaded" || err "memories-sync failed to load"

# --- Smoke tests ----------------------------------------------------------
step "Running one-shot tests"
bash "$APPSUPPORT/bin/backup-db.sh" && ok "backup-db.sh succeeded" || warn "backup-db.sh returned non-zero (check $APPSUPPORT/backup.log)"
bash "$APPSUPPORT/bin/memories-sync.sh" && ok "memories-sync.sh succeeded" || warn "memories-sync.sh returned non-zero (check $APPSUPPORT/memories-sync.log)"

echo
echo "Cutover complete. Next:"
echo "  • Check status: launchctl print gui/$UID_NUM/com.blackmon.tealc-scheduler | head -20"
echo "  • Watch logs:   tail -f \"$APPSUPPORT/launchd.stderr.log\" \"$APPSUPPORT/backup.log\""
echo "  • Drive check:  ls -lt \"$DEPLOY_DIR/snapshots/backups\" | head -3"
echo "  • Start TEALC:  open \"$HOME/Desktop/Start Tealc.app\""
