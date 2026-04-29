#!/bin/bash
# Install the Tealc scheduler as a macOS LaunchAgent.
# After running this, the scheduler starts automatically at login and restarts
# if it crashes. Run once; does not need to be repeated after reboot.
# Usage: bash scripts/install_launchd.sh

set -euo pipefail

PLIST_SRC="/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/scripts/com.blackmon.tealc-scheduler.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.blackmon.tealc-scheduler.plist"
LABEL="com.blackmon.tealc-scheduler"
UID_VAL="$(id -u)"

echo "==> Installing Tealc LaunchAgent..."

# Idempotent: bootout first if already loaded (suppresses error if not loaded)
launchctl bootout "gui/${UID_VAL}/${LABEL}" 2>/dev/null || true

# Copy plist into place
cp "$PLIST_SRC" "$PLIST_DST"
echo "    Copied plist to $PLIST_DST"

# Bootstrap with the modern launchctl API
launchctl bootstrap "gui/${UID_VAL}" "$PLIST_DST"
echo "    Bootstrapped gui/${UID_VAL}/${LABEL}"

echo ""
echo "Done. The Tealc scheduler will now start automatically at login"
echo "and restart itself if it ever crashes."
echo ""
echo "Check status:"
echo "    launchctl print gui/${UID_VAL}/${LABEL}"
echo ""
echo "Or use the project's own health check:"
echo "    bash \"/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/scripts/scheduler_status.sh\""
