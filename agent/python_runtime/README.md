# Python Runtime — Tealc's Python Analysis Arm

This package is the Python sibling of `agent/r_runtime/`. It gives Tealc the
ability to run pandas data wrangling, matplotlib figures, statsmodels regressions,
seaborn plots, and scikit-learn models — the same way `run_r_script` handles
phylogenetic R analyses.

## Required packages

`pandas`, `numpy`, `matplotlib`, `scipy`, `statsmodels`, `seaborn`, `scikit-learn`

All must be installed in `~/.lab-agent-venv/`. Run `packages.check_packages()` to
verify availability without installing anything.

## Path conventions

Each run gets an isolated directory:

```
data/py_runs/<YYYYMMDDTHHMMSS_<6-char-uuid>>/
    script.py        ← user code
    *.png / *.pdf    ← plots (returned in plot_paths)
    *                ← other created files (returned in created_files)
```

## Tool-layer integration

Wave 3 registers a `run_python_script` tool in `agent/tools.py` that wraps
`executor.run_python()` and returns a JSON string — exactly mirroring how
`run_r_script` wraps the R runtime.

## Safety model

Heath is the sole user and operator. The executor runs code with the lab venv
Python in the run directory; no sandbox is enforced beyond the minimal check
that rejects a `subprocess` import combined with `rm -rf` on the same line.
Network access and filesystem access outside the working directory are not
blocked — this is a trust-based setup, not a multi-tenant system.
