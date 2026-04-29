#!/bin/bash
# Start the Tealc background scheduler.
# Usage: bash scripts/start_scheduler.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.lab-agent-venv"
PID_FILE="$SCRIPT_DIR/data/scheduler.pid"

cd "$SCRIPT_DIR"
source "$VENV/bin/activate"

# --- Scheduler: start if not already running ---
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Tealc scheduler is already running, PID $(cat "$PID_FILE")"
else
    [ -f "$PID_FILE" ] && { echo "Stale scheduler PID file — cleaning up"; rm "$PID_FILE"; }
    PYTHONPATH="$SCRIPT_DIR" nohup python -m agent.scheduler \
        >> "$SCRIPT_DIR/data/scheduler.stdout.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Tealc scheduler started, PID $(cat "$PID_FILE")"
fi

# --- Dashboard: start if not already running ---
DASHBOARD_PID_FILE="$SCRIPT_DIR/data/dashboard_server.pid"
if [ -f "$DASHBOARD_PID_FILE" ] && kill -0 "$(cat "$DASHBOARD_PID_FILE")" 2>/dev/null; then
    echo "Dashboard server already running (PID $(cat "$DASHBOARD_PID_FILE"))"
else
    [ -f "$DASHBOARD_PID_FILE" ] && { echo "Stale dashboard PID file — cleaning up"; rm "$DASHBOARD_PID_FILE"; }
    PYTHONPATH="$SCRIPT_DIR" nohup "$VENV/bin/python" -m agent.dashboard_server \
        >> "$SCRIPT_DIR/data/dashboard_server.log" 2>&1 &
    echo $! > "$DASHBOARD_PID_FILE"
    echo "Dashboard server started on http://localhost:8001, PID $(cat "$DASHBOARD_PID_FILE")"
fi
