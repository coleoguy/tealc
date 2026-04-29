#!/bin/bash
# Stop the Tealc background scheduler.
# Usage: bash scripts/stop_scheduler.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$SCRIPT_DIR/data/scheduler.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found — scheduler may not be running"
    exit 1
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm "$PID_FILE"
    echo "Tealc scheduler stopped (PID $PID)"
else
    echo "No process found for PID $PID — cleaning up stale PID file"
    rm "$PID_FILE"
    exit 1
fi

# v5 HQ: also stop the dashboard server
DASH_PID_FILE="$SCRIPT_DIR/data/dashboard_server.pid"
if [ -f "$DASH_PID_FILE" ]; then
    DASH_PID=$(cat "$DASH_PID_FILE")
    if kill -0 "$DASH_PID" 2>/dev/null; then
        kill "$DASH_PID"
        rm "$DASH_PID_FILE"
        echo "Dashboard server stopped (PID $DASH_PID)"
    else
        echo "No process found for dashboard PID $DASH_PID — cleaning up stale PID file"
        rm "$DASH_PID_FILE"
    fi
else
    echo "No dashboard server PID file found — skipping"
fi
