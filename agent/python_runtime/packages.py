"""Package availability checker for Tealc's Python runtime.

Diagnostic only — does NOT install anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Package list
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = [
    "pandas",
    "numpy",
    "matplotlib",
    "scipy",
    "statsmodels",
    "seaborn",
    "scikit-learn",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VENV_PYTHON = Path(os.path.expanduser("~/.lab-agent-venv/bin/python"))


def _resolve_python() -> str:
    if _VENV_PYTHON.exists() and os.access(_VENV_PYTHON, os.X_OK):
        return str(_VENV_PYTHON)
    return sys.executable


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_packages() -> dict[str, bool]:
    """Try to import each required package and report availability.

    Uses a subprocess so the check targets the same Python interpreter
    that executor.py will use (the lab venv when present).

    Returns a dict mapping package name -> bool (True = importable).
    """
    python_bin = _resolve_python()
    results: dict[str, bool] = {}

    for pkg in REQUIRED_PACKAGES:
        # scikit-learn is imported as sklearn
        import_name = "sklearn" if pkg == "scikit-learn" else pkg
        try:
            proc = subprocess.run(
                [python_bin, "-c", f"import {import_name}"],
                capture_output=True,
                timeout=10,
            )
            results[pkg] = proc.returncode == 0
        except Exception:
            results[pkg] = False

    return results
