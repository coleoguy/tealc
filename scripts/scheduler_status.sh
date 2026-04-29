#!/bin/bash
# Check Tealc scheduler liveness via the heartbeat file.
# Usage: bash scripts/scheduler_status.sh
# Exit codes: 0=ALIVE, 1=STOPPED, 2=STALE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HB="$SCRIPT_DIR/data/scheduler_heartbeat.json"

if [ ! -f "$HB" ]; then
    echo "STOPPED (no heartbeat file)"
    exit 1
fi

AGE=$(python3 -c "
import json, datetime
d = json.load(open('$HB'))
hb_str = d['alive_at']
# Handle both 'Z' suffix and '+00:00' offset
if hb_str.endswith('Z'):
    hb_str = hb_str[:-1] + '+00:00'
hb = datetime.datetime.fromisoformat(hb_str)
now = datetime.datetime.now(datetime.timezone.utc)
print(int((now - hb).total_seconds()))
")

if [ $? -ne 0 ]; then
    echo "STOPPED (heartbeat file unreadable)"
    exit 1
fi

if [ "$AGE" -gt 300 ]; then
    echo "STALE (last heartbeat ${AGE}s ago)"
    exit 2
fi

echo "ALIVE (last heartbeat ${AGE}s ago)"
exit 0
