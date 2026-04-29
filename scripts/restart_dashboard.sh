#!/bin/bash
# Stop + start the Tealc HQ dashboard in one command.  Use this after
# touching agent/dashboard_server.py so new endpoints come online without a
# full reboot.  Runs the dashboard in the background via nohup (same pattern
# as start_dashboard.sh).
#
# Usage: bash scripts/restart_dashboard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$SCRIPT_DIR/scripts/stop_dashboard.sh" || true
sleep 1
bash "$SCRIPT_DIR/scripts/start_dashboard.sh"
