"""Python script executor for Tealc.

Mirrors the R runtime (agent/r_runtime/) pattern:
- Creates a timestamped run directory under data/py_runs/
- Writes user code to script.py
- Executes with the lab venv Python
- Returns a structured dict with stdout, stderr, created files, plots, etc.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
))

_PY_RUNS_DIR = _PROJECT_ROOT / "data" / "py_runs"

_VENV_PYTHON = Path(os.path.expanduser("~/.lab-agent-venv/bin/python"))

_PLOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".svg"}

_MAX_OUTPUT = 8000
_TRUNCATION_NOTICE = "\n[<truncated>]"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_python() -> str:
    """Return path to the lab venv Python if present, else sys.executable."""
    if _VENV_PYTHON.exists() and os.access(_VENV_PYTHON, os.X_OK):
        return str(_VENV_PYTHON)
    return sys.executable


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_NOTICE


def _is_unsafe(code: str) -> bool:
    """Minimal sanity check: reject code that imports subprocess AND
    contains 'rm -rf' on the same line."""
    for line in code.splitlines():
        if "subprocess" in line and "rm -rf" in line:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_python(
    code: str,
    working_dir: Optional[str] = None,
    timeout_seconds: int = 300,
    extra_files: Optional[dict] = None,
) -> dict:
    """Execute Python code in an isolated run directory.

    Parameters
    ----------
    code:
        Python source to execute.
    working_dir:
        Override the auto-generated run directory. If None, a fresh
        timestamped directory under data/py_runs/ is created.
    timeout_seconds:
        Hard wall-clock limit. On expiry, exit_code is set to -9.
    extra_files:
        Additional files to place in the working dir before execution,
        keyed by filename, values are raw bytes.

    Returns
    -------
    dict with keys:
        run_id, working_dir, script_path, stdout, stderr, exit_code,
        plot_paths, created_files, duration_seconds
    """
    # ------------------------------------------------------------------
    # 1. Validate code
    # ------------------------------------------------------------------
    if not code or not code.strip():
        return {
            "run_id": "",
            "working_dir": "",
            "script_path": "",
            "stdout": "",
            "stderr": "No code provided.",
            "exit_code": 1,
            "plot_paths": [],
            "created_files": [],
            "duration_seconds": 0.0,
        }

    if _is_unsafe(code):
        return {
            "run_id": "",
            "working_dir": "",
            "script_path": "",
            "stdout": "",
            "stderr": "Code rejected: subprocess import combined with rm -rf on same line.",
            "exit_code": 1,
            "plot_paths": [],
            "created_files": [],
            "duration_seconds": 0.0,
        }

    # ------------------------------------------------------------------
    # 2. Resolve working directory
    # ------------------------------------------------------------------
    if working_dir:
        wd = Path(working_dir)
        run_id = wd.name
    else:
        _PY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        short_id = uuid.uuid4().hex[:6]
        run_id = f"{timestamp}_{short_id}"
        wd = _PY_RUNS_DIR / run_id

    wd.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 3. Write code and extra files
    # ------------------------------------------------------------------
    script_path = wd / "script.py"
    script_path.write_text(code, encoding="utf-8")

    if extra_files:
        for fname, data in extra_files.items():
            (wd / fname).write_bytes(data)

    # ------------------------------------------------------------------
    # 4. Snapshot files before run (excluding script.py)
    # ------------------------------------------------------------------
    files_before = {p.name for p in wd.iterdir()}

    # ------------------------------------------------------------------
    # 5. Execute
    # ------------------------------------------------------------------
    python_bin = _resolve_python()
    t_start = datetime.now()

    try:
        proc = subprocess.run(
            [python_bin, str(script_path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(wd),
        )
        stdout_raw = proc.stdout
        stderr_raw = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        stdout_raw = ""
        stderr_raw = f"TIMEOUT after {timeout_seconds} seconds"
        exit_code = -9

    duration = (datetime.now() - t_start).total_seconds()

    # ------------------------------------------------------------------
    # 6. Classify created files
    # ------------------------------------------------------------------
    files_after = [p for p in wd.iterdir() if p.name not in files_before]

    plot_paths = [
        str(p) for p in files_after if p.suffix.lower() in _PLOT_EXTENSIONS
    ]
    created_files = [
        p.name for p in files_after if p.suffix.lower() not in _PLOT_EXTENSIONS
    ]

    # ------------------------------------------------------------------
    # 7. Return result dict
    # ------------------------------------------------------------------
    return {
        "run_id": run_id,
        "working_dir": str(wd),
        "script_path": str(script_path),
        "stdout": _truncate(stdout_raw),
        "stderr": _truncate(stderr_raw),
        "exit_code": exit_code,
        "plot_paths": plot_paths,
        "created_files": created_files,
        "duration_seconds": round(duration, 3),
    }
