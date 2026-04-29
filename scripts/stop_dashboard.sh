#!/bin/bash
# Stop the Tealc HQ dashboard server.
# Usage: bash scripts/stop_dashboard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$SCRIPT_DIR/data/dashboard_server.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found — dashboard may not be running"
    exit 1
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm "$PID_FILE"
    echo "Tealc HQ dashboard stopped (PID $PID)"
else
    echo "No process found for PID $PID — cleaning up stale PID file"
    rm "$PID_FILE"
    exit 1
fi
