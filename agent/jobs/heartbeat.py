"""Heartbeat job — writes a timestamp to data/scheduler_heartbeat.json once per minute.

Used by scripts/scheduler_status.sh to confirm the scheduler is alive.
"""
import json
import os
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "..", "data", "scheduler_heartbeat.json")
)


def job():
    """Write current UTC timestamp to the heartbeat file."""
    os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump({"alive_at": datetime.now(timezone.utc).isoformat()}, f)
