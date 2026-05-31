#!/bin/bash
# restore_tealc.sh — bring up TEALC on a fresh Mac in ~5 minutes (after Drive sync).
#
# Prerequisites (manual; macOS won't let us script these):
#   1. Google Drive Desktop installed and signed in to coleoguy@gmail.com
#   2. /Users/$USER/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent
#      visible and synced (~150 MB)
#   3. Homebrew + Python 3.12 installed (brew install python@3.12)
#   4. macOS System Settings → Privacy & Security → Full Disk Access:
#      add /bin/bash and toggle it ON (required for launchd to read scripts in
#      AppSupport that exec into Drive paths)
#   5. ~/.lab-agent-venv created with python3 -m venv ~/.lab-agent-venv
#      (or run: python3 -m venv ~/.lab-agent-venv && source ~/.lab-agent-venv/bin/activate)
#
# What this script does:
#   • mkdir -p the AppSupport tree
#   • Copies bin/ scripts from deployment/ → AppSupport
#   • Copies plists from deployment/ → ~/Library/LaunchAgents/
#   • Restores agent.db from the latest deployment/snapshots/backups/agent_*.db
#   • Restores memories from deployment/snapshots/memories/ (if present)
#   • Copies Start Tealc.app + Stop Tealc.app to ~/Desktop/
#   • pip-installs requirements (assumes ~/.lab-agent-venv exists)
#   • Loads scheduler + backup + memories-sync LaunchAgents
#   • Reminds the user about the manual steps that can't be scripted
#
# Run from anywhere:
#   bash "/path/to/My Drive/00-Lab-Agent/deployment/restore_tealc.sh"

set -eu

# Detect Drive deployment directory (assumes coleoguy@gmail.com Drive account)
LAB_DIR="$HOME/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent"
DEPLOY_DIR="$LAB_DIR/deployment"
APPSUPPORT="$HOME/Library/Application Support/tealc"
LAUNCHAGENTS="$HOME/Library/LaunchAgents"
VENV="$HOME/.lab-agent-venv"
UID_NUM="$(id -u)"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*"; exit 1; }
step() { printf "\n\033[1m▸ %s\033[0m\n" "$*"; }

# --- Preflight ------------------------------------------------------------
step "Preflight"
[ -d "$LAB_DIR" ]    || err "Drive not mounted at expected path: $LAB_DIR"
[ -d "$DEPLOY_DIR" ] || err "deployment dir missing: $DEPLOY_DIR (Drive may still be syncing)"
[ -d "$VENV" ]       || err "venv missing: $VENV. Create with: python3 -m venv ~/.lab-agent-venv"
ok "Drive mounted, deployment present, venv exists"

# --- Install Python deps --------------------------------------------------
step "Installing Python dependencies"
"$VENV/bin/pip" install --upgrade pip > /dev/null
"$VENV/bin/pip" install -r "$LAB_DIR/requirements.txt" \
    && ok "requirements.txt installed" \
    || err "pip install failed; check $LAB_DIR/requirements.txt"

# --- AppSupport tree ------------------------------------------------------
step "Creating AppSupport tree"
mkdir -p "$APPSUPPORT/bin" "$APPSUPPORT/backups" "$APPSUPPORT/memories"
ok "directories created"

# --- Copy bin scripts -----------------------------------------------------
step "Installing bin/ scripts"
cp "$DEPLOY_DIR/bin/scheduler-wrapper.sh" "$APPSUPPORT/bin/scheduler-wrapper.sh"
cp "$DEPLOY_DIR/bin/backup-db.sh"         "$APPSUPPORT/bin/backup-db.sh"
cp "$DEPLOY_DIR/bin/memories-sync.sh"     "$APPSUPPORT/bin/memories-sync.sh"
chmod 0755 "$APPSUPPORT/bin/scheduler-wrapper.sh" "$APPSUPPORT/bin/backup-db.sh" "$APPSUPPORT/bin/memories-sync.sh"
ok "bin scripts installed"

# --- Restore latest DB snapshot from Drive --------------------------------
step "Restoring agent.db from latest Drive snapshot"
LATEST_BACKUP="$(/usr/bin/find "$DEPLOY_DIR/snapshots/backups" -name "agent_*.db" -type f 2>/dev/null | sort | tail -1)"
if [ -n "$LATEST_BACKUP" ]; then
    cp "$LATEST_BACKUP" "$APPSUPPORT/agent.db"
    SIZE=$(stat -f "%z" "$APPSUPPORT/agent.db")
    ok "restored from $LATEST_BACKUP (${SIZE} bytes)"
else
    warn "no backup found in $DEPLOY_DIR/snapshots/backups/ — TEALC will start with an empty DB."
    warn "  If this is wrong, copy a backup manually before launching the scheduler."
fi

# --- Restore memories from Drive snapshot --------------------------------
step "Restoring memories"
if [ -d "$DEPLOY_DIR/snapshots/memories" ] && [ -n "$(/bin/ls -A "$DEPLOY_DIR/snapshots/memories" 2>/dev/null)" ]; then
    /usr/bin/rsync -a "$DEPLOY_DIR/snapshots/memories/" "$APPSUPPORT/memories/" \
        && ok "memories restored from Drive snapshot" \
        || warn "rsync of memories failed; continuing"
else
    warn "no memories snapshot in Drive (this is OK on a brand-new install)"
fi

# --- Copy plists ---------------------------------------------------------
step "Installing LaunchAgent plists"
mkdir -p "$LAUNCHAGENTS"
cp "$DEPLOY_DIR/launchd/com.blackmon.tealc-scheduler.plist"     "$LAUNCHAGENTS/"
cp "$DEPLOY_DIR/launchd/com.blackmon.tealc-backup.plist"        "$LAUNCHAGENTS/"
cp "$DEPLOY_DIR/launchd/com.blackmon.tealc-memories-sync.plist" "$LAUNCHAGENTS/"
ok "plists installed"

# --- Copy desktop apps ---------------------------------------------------
step "Installing Start/Stop desktop apps"
if [ -d "$DEPLOY_DIR/apps/Start Tealc.app" ]; then
    cp -R "$DEPLOY_DIR/apps/Start Tealc.app" "$HOME/Desktop/"
    cp -R "$DEPLOY_DIR/apps/Stop Tealc.app"  "$HOME/Desktop/"
    chmod +x "$HOME/Desktop/Start Tealc.app/Contents/MacOS/"* 2>/dev/null || true
    chmod +x "$HOME/Desktop/Stop Tealc.app/Contents/MacOS/"*  2>/dev/null || true
    ok "desktop apps installed"
else
    warn "deployment/apps/ missing; skip Start/Stop apps"
fi

# --- Load LaunchAgents ---------------------------------------------------
step "Loading LaunchAgents (errors here usually mean Full Disk Access not granted to /bin/bash)"
launchctl bootstrap "gui/$UID_NUM" "$LAUNCHAGENTS/com.blackmon.tealc-scheduler.plist"     && ok "scheduler loaded"     || warn "scheduler failed to load"
launchctl bootstrap "gui/$UID_NUM" "$LAUNCHAGENTS/com.blackmon.tealc-backup.plist"        && ok "backup loaded"        || warn "backup failed to load"
launchctl bootstrap "gui/$UID_NUM" "$LAUNCHAGENTS/com.blackmon.tealc-memories-sync.plist" && ok "memories-sync loaded" || warn "memories-sync failed to load"

# --- Manual reminders ----------------------------------------------------
step "Manual steps remaining (cannot be scripted)"
cat <<'EOF'
  1. System Settings → Privacy & Security → Full Disk Access:
     • Add /bin/bash and toggle it ON
     • Restart scheduler:
         launchctl bootout gui/$(id -u)/com.blackmon.tealc-scheduler
         launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blackmon.tealc-scheduler.plist

  2. If Google API tokens are stale, re-auth:
         cd "$HOME/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent"
         "$HOME/.lab-agent-venv/bin/python" authenticate_google.py

  3. Verify TEALC is running:
         tail -n 20 "$HOME/Library/Application Support/tealc/launchd.stderr.log"
         curl -sI http://localhost:8001 | head -1

  4. Open the chat: double-click "Start Tealc.app" on the Desktop
     and visit http://localhost:8000
EOF

echo
ok "restore_tealc.sh complete"
