"""Sync the public Tealc template repo — optional weekly scheduled job.

Reads data/template_sync_config.json.  Only runs the full build+audit+push
pipeline when ``auto_push`` is true in the config AND repo_path is set.
Otherwise logs a dry-run notice and exits cleanly.

Run manually to test:
    python -m agent.jobs.sync_template_repo

Register in scheduler (off by default — Heath decides when to enable):
    # scheduler.add_job(
    #     sync_template_repo_job,
    #     CronTrigger(day_of_week="sun", hour=22, minute=0, timezone="US/Central"),
    #     id="sync_template_repo",
    # )
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from agent.jobs import tracked

log = logging.getLogger("tealc.sync_template_repo")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "data", "template_sync_config.json")
_SYNC_SCRIPT = os.path.join(_PROJECT_ROOT, "scripts", "sync_template_repo.sh")


def _load_config() -> dict:
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(
            f"template_sync_config.json not found at {_CONFIG_PATH}. "
            "Create it from the template in data/ and fill in repo_path."
        )
    with open(_CONFIG_PATH) as f:
        return json.load(f)


@tracked("sync_template_repo")
def job(dry_run_override: bool = False) -> str:
    """Build + audit + optionally push the public template repo.

    Args:
        dry_run_override: If True, forces dry-run even if auto_push=true in config.
                          Used by manual runs / dashboard "test" invocations.
    """
    config = _load_config()

    repo_path = config.get("repo_path", "").strip()
    git_remote = config.get("git_remote", "").strip()
    auto_push = config.get("auto_push", False)

    if not repo_path:
        msg = (
            "template_sync_config.json has an empty repo_path. "
            "Fill it in before enabling auto-sync."
        )
        log.warning(msg)
        return msg

    if not auto_push or dry_run_override:
        reason = "dry_run_override=True" if dry_run_override else "auto_push=false in config"
        log.info("Dry-run mode (%s) — running build+audit without push.", reason)
        cmd = ["bash", _SYNC_SCRIPT, "--config", _CONFIG_PATH]
        # No --push flag → dry run
    else:
        if not git_remote:
            msg = (
                "auto_push=true but git_remote is empty in config. "
                "Set git_remote before enabling auto-push."
            )
            log.error(msg)
            return msg
        log.info("auto_push=true — running full sync+push to %s.", git_remote)
        cmd = ["bash", _SYNC_SCRIPT, "--push", "--config", _CONFIG_PATH]

    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        error_msg = f"sync_template_repo.sh exited {result.returncode}.\n{stderr or stdout}"
        log.error(error_msg)
        raise RuntimeError(error_msg)

    log.info(stdout)
    if stderr:
        log.warning("stderr: %s", stderr)

    # Return a compact summary for job_runs.output_summary
    lines = stdout.splitlines()
    summary_lines = [l for l in lines if l.strip() and not l.startswith("    ")]
    return " | ".join(summary_lines[-5:]) if summary_lines else "sync complete"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(job())
