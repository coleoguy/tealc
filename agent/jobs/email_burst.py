"""Email burst trigger consumer — runs every 60 seconds.

Checks for data/email_burst_pending.flag written by email_triage when it
encounters a "notify" (urgent) email.  If the flag exists: delete it and
run email_triage immediately to handle the urgent email sooner than the
normal 10-minute cron interval.  If the flag is absent: return immediately
at O(1) cost (no API calls).

Returns:
  "no_burst_pending"             — flag absent, nothing to do
  "burst_fired: <triage_result>" — flag found, triage ran

Run manually:
    cd "$HOME/Google Drive/My Drive/00-Lab-Agent"
    python -m agent.jobs.email_burst
"""
import os

from agent.jobs import tracked

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.normpath(os.path.join(_HERE, "..", "..", "data"))
FLAG_PATH = os.path.join(_DATA, "email_burst_pending.flag")


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------
@tracked("email_burst")
def job() -> str:
    """Consume burst flag; fire email_triage immediately if flag present."""

    if not os.path.exists(FLAG_PATH):
        return "no_burst_pending"

    # Delete the flag before running triage so a crash doesn't loop forever
    try:
        os.remove(FLAG_PATH)
    except OSError:
        # Already gone (race with another process) — still fine
        pass

    from agent.jobs.email_triage import job as email_triage_job  # noqa: PLC0415
    result = email_triage_job()
    return f"burst_fired: {result}"


# ---------------------------------------------------------------------------
# Manual run entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result = job()
    print(result)
