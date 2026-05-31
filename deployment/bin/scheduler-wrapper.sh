#!/bin/bash
# launchd wrapper for the Tealc scheduler.
#
# Why this exists: macOS Privacy / TCC blocks launchd from reading scripts
# inside Google Drive's CloudStorage path. Pointing the LaunchAgent directly
# at scripts/start_scheduler.sh fails with "Operation not permitted" on every
# spawn attempt, so the auto-restart-on-login agent silently dies forever.
#
# This wrapper lives outside CloudStorage (in ~/Library/Application Support/tealc/bin/),
# so launchd can read and execute it. It then exec's the real start_scheduler.sh
# inside the Drive lab repo.
#
# If launchd's TCC restriction also blocks the inner bash from reading the
# Drive script (it inherits launchd's TCC context), `/bin/bash` itself needs
# Full Disk Access granted in System Settings → Privacy & Security → Full
# Disk Access. Add `/bin/bash` and toggle it on. Restart the LaunchAgent:
#   launchctl bootout gui/$(id -u)/com.blackmon.tealc-scheduler
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blackmon.tealc-scheduler.plist
#
# This file is the canonical source. Live copy at:
#   ~/Library/Application Support/tealc/bin/scheduler-wrapper.sh
# Use deployment/bin/cutover.sh to mirror this file into AppSupport.

set -e
LAB_DIR="/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent"
exec /bin/bash "$LAB_DIR/scripts/start_scheduler.sh"
