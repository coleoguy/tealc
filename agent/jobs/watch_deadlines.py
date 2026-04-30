"""Filesystem mtime watcher for data/deadlines.json.

Runs every 60 seconds (cheap — no API call).  Compares the current mtime of
data/deadlines.json against the stored last-mtime in data/last_deadlines_mtime.json.
If the file is newer, triggers an immediate refresh_context job so the executive
loop picks up the updated deadlines without waiting for the 10-minute cron.

Returns "deadlines_changed" or "no_change".

Run manually:
    cd "$HOME/Google Drive/My Drive/00-Lab-Agent"
    python -m agent.jobs.watch_deadlines
"""
import json
import os

from agent.jobs import tracked

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
DEADLINES_PATH = os.path.join(_DATA, "deadlines.json")
MTIME_STATE_PATH = os.path.join(_DATA, "last_deadlines_mtime.json")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def _load_last_mtime() -> float | None:
    """Return the stored last mtime float, or None if not yet recorded."""
    try:
        with open(MTIME_STATE_PATH) as f:
            data = json.load(f)
            val = data.get("mtime")
            return float(val) if val is not None else None
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None


def _save_mtime(mtime: float):
    os.makedirs(_DATA, exist_ok=True)
    with open(MTIME_STATE_PATH, "w") as f:
        json.dump({"mtime": mtime}, f)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("watch_deadlines")
def job() -> str:
    """Check if data/deadlines.json has changed; if so, trigger refresh_context."""

    # 1. Get current mtime — return no_change if file missing
    try:
        current_mtime = os.path.getmtime(DEADLINES_PATH)
    except OSError:
        return "no_change (deadlines.json not found)"

    # 2. Compare against stored mtime
    last_mtime = _load_last_mtime()

    if last_mtime is not None and current_mtime <= last_mtime:
        return "no_change"

    # 3. File is new or newer — save mtime first, then trigger context refresh
    _save_mtime(current_mtime)

    from agent.jobs.refresh_context import job as refresh_context_job  # noqa: PLC0415
    refresh_context_job()

    return "deadlines_changed"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
