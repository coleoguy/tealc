#!/bin/bash
# Start the Tealc HQ dashboard server (localhost:8001).
# Designed to run alongside the scheduler + Chainlit.
# Usage: bash scripts/start_dashboard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.lab-agent-venv"
PID_FILE="$SCRIPT_DIR/data/dashboard_server.pid"

# Prevent double-start
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Tealc HQ dashboard is already running, PID $PID"
        exit 0
    else
        echo "Stale PID file found — cleaning up"
        rm "$PID_FILE"
    fi
fi

cd "$SCRIPT_DIR"
source "$VENV/bin/activate"

PYTHONPATH="$SCRIPT_DIR" nohup python -m agent.dashboard_server \
    >> "$SCRIPT_DIR/data/dashboard_server.log" 2>&1 &

echo $! > "$PID_FILE"
echo "Tealc HQ dashboard started on http://localhost:8001, PID $(cat "$PID_FILE")"
