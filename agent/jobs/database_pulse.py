"""Database pulse — twice a day (10 am, 3 pm), runs a small QC scan on ONE
karyotype database (rotating). Logs flags to database_health_runs.

Reuses the existing weekly_database_health logic but does only one sheet per
fire to keep the cost low and the aquarium signal frequent. The full weekly
sweep is unchanged.

Run manually:
    python -m agent.jobs.database_pulse
"""
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

from agent.jobs import tracked  # noqa: E402
from agent.config import is_job_enabled, should_run_this_cycle  # noqa: E402

# Resources we will rotate through. These keys must match agent/tools.py's
# require_data_resource registry. We don't import the registry directly because
# it lives inside tools.py and importing tools.py from a job would drag in the
# whole LangGraph stack.
_ROTATION = [
    "coleoptera_karyotypes",
    "diptera_karyotypes",
    "amphibia_karyotypes",
    "drosophila_karyotypes",
    "mammalia_karyotypes",
    "polyneoptera_karyotypes",
    "cures_karyotype_database",
]

_STATE_PATH = os.path.normpath(
    os.path.join(_PROJECT_ROOT, "data", "database_pulse.state.json")
)


def _is_pulse_window() -> bool:
    try:
        import zoneinfo
        central = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        return True
    h = datetime.now(central).hour
    # Tolerate a 2-hour window around each pulse so APScheduler interval
    # mis-fires don't accidentally skip everything.
    return h in (9, 10, 11, 14, 15, 16)


def _load_state() -> dict:
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"last_index": -1}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _STATE_PATH)


@tracked("database_pulse")
def job() -> str:
    if not is_job_enabled("database_pulse"):
        return "skipped: disabled in config"
    if not should_run_this_cycle("database_pulse"):
        return "skipped: reduced-mode sample miss"
    if not _is_pulse_window() and os.environ.get("FORCE_RUN") != "1":
        return "skipped: outside pulse window"

    state = _load_state()
    next_idx = (int(state.get("last_index", -1)) + 1) % len(_ROTATION)
    resource_key = _ROTATION[next_idx]

    # Defer to the weekly checker — its job() already accepts restrict_to_sheet,
    # which gives us a single-sheet QC scan with all the right book-keeping
    # (writes to database_health_runs, dedups today's prior runs, etc).
    try:
        from agent.jobs.weekly_database_health import job as weekly_job  # noqa: PLC0415
        result = weekly_job(restrict_to_sheet=resource_key)
        msg = f"ok: scanned {resource_key} ({result})"
    except Exception as e:
        msg = f"error scanning {resource_key}: {e}"

    state["last_index"] = next_idx
    _save_state(state)
    return msg


if __name__ == "__main__":
    os.environ["FORCE_RUN"] = "1"
    print(job())
