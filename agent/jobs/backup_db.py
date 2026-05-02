"""Force-runnable companion to the launchd-driven daily DB backup.

Lets Heath say "run backup_db now" / "trigger backup_db" in chat to take an
immediate snapshot. The launchd LaunchAgent (com.blackmon.tealc-backup) is the
primary scheduled trigger — it fires at 04:30 local. This APScheduler-side job
simply shells out to the same script the LaunchAgent runs, so the two paths
share one canonical implementation.

Logs land in the same backup.log alongside the launchd-driven runs.
"""
import os
import subprocess

from agent.jobs import tracked

_BACKUP_SCRIPT = os.path.expanduser(
    "~/Library/Application Support/tealc/bin/backup-db.sh"
)


@tracked("backup_db")
def job() -> str:
    if not os.path.exists(_BACKUP_SCRIPT):
        return f"skipped: backup script not found at {_BACKUP_SCRIPT}"

    try:
        result = subprocess.run(
            ["/bin/bash", _BACKUP_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "failed: backup script timed out after 120s"
    except Exception as e:  # noqa: BLE001
        return f"failed: {e}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:300]
        return f"failed: rc={result.returncode}, stderr={stderr!r}"

    # On success the script writes one line to ~/Library/Application Support/tealc/backup.log;
    # surface that line so chat sees the destination + size.
    log_path = os.path.expanduser("~/Library/Application Support/tealc/backup.log")
    try:
        with open(log_path) as f:
            last = f.readlines()[-1].rstrip()
        return f"ok — {last}"
    except Exception:
        return "ok"


if __name__ == "__main__":
    print(job())
