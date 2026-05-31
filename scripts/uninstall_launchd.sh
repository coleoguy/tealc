#!/bin/bash
# Uninstall the Tealc scheduler LaunchAgent.
# This stops the scheduler and removes it from auto-start on login.
# Usage: bash scripts/uninstall_launchd.sh

set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.tealc-scheduler.plist"
LABEL="com.tealc-scheduler"
UID_VAL="$(id -u)"

echo "==> Uninstalling Tealc LaunchAgent..."

# Gracefully stop and unregister the service
if launchctl print "gui/${UID_VAL}/${LABEL}" &>/dev/null; then
    launchctl bootout "gui/${UID_VAL}/${LABEL}"
    echo "    Booted out gui/${UID_VAL}/${LABEL}"
else
    echo "    Service not currently loaded — skipping bootout"
fi

# Remove the plist
if [ -f "$PLIST_DST" ]; then
    rm "$PLIST_DST"
    echo "    Removed $PLIST_DST"
else
    echo "    Plist not found at $PLIST_DST — nothing to remove"
fi

echo ""
echo "Done. The Tealc scheduler will no longer start at login."
echo "To start it manually: bash scripts/start_scheduler.sh"
