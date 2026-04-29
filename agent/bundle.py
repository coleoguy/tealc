"""Reproducibility bundles — package analysis runs as self-contained tar.gz archives."""
import hashlib
import json
import os
import sqlite3
import tarfile
from datetime import datetime, timezone
from io import BytesIO

from agent.scheduler import DB_PATH

_BUNDLE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "r_runs", "bundles")
)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _add_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = (text or "").encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, BytesIO(data))


def package_analysis_run(run_id: int) -> str:
    """Bundle an analysis_runs row into a reproducible tar.gz archive.

    Returns the absolute path to the created bundle.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()

    if row is None:
        raise ValueError(f"No analysis_runs row with id={run_id}")

    row = dict(row)
    os.makedirs(_BUNDLE_DIR, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    bundle_name = f"analysis_{run_id}_{ts}.tar.gz"
    bundle_path = os.path.join(_BUNDLE_DIR, bundle_name)

    # Parse created_files JSON
    created_files: list[str] = []
    try:
        created_files = json.loads(row.get("created_files") or "[]") or []
    except Exception:
        pass

    # Extract session info from stderr if available
    stderr = row.get("stderr_truncated") or ""
    if "R version" in stderr or "sessionInfo" in stderr.lower():
        session_info_text = stderr
    else:
        session_info_text = (
            "Session info not captured in stderr. Re-run analysis.R and call "
            "sessionInfo() at the end to record environment details."
        )

    results_obj = {
        "outcome": row.get("outcome"),
        "exit_code": row.get("exit_code"),
        "stdout_truncated": row.get("stdout_truncated"),
        "stderr_truncated": row.get("stderr_truncated"),
        "plot_paths": row.get("plot_paths"),
    }

    readme_text = (
        f"# Reproducibility Bundle — Analysis Run {run_id}\n\n"
        "## Prerequisites\n\n"
        "- R >= 4.0\n"
        "- Install required CRAN packages:\n\n"
        "```r\n"
        'install.packages(c("ape", "phytools", "geiger", "diversitree"))\n'
        "```\n\n"
        "## How to Re-run\n\n"
        "```bash\n"
        "Rscript analysis.R\n"
        "```\n\n"
        "## Expected Outputs\n\n"
        f"- Exit code: {row.get('exit_code')}\n"
        f"- Outcome: {row.get('outcome')}\n"
        f"- Created files: {created_files}\n\n"
        "## Files in This Bundle\n\n"
        "- `analysis.R` — the R script\n"
        "- `sessionInfo.txt` — R session environment (if captured)\n"
        "- `results.json` — stdout, stderr, exit code, plot paths\n"
        "- `interpretation.md` — Tealc's interpretation of results\n"
        "- Any created data files with `.sha256` sidecars for integrity verification\n"
    )

    with tarfile.open(bundle_path, "w:gz") as tar:
        _add_text(tar, "analysis.R", row.get("r_code") or "")
        _add_text(tar, "sessionInfo.txt", session_info_text)
        _add_text(tar, "results.json", json.dumps(results_obj, indent=2))
        _add_text(tar, "interpretation.md", row.get("interpretation_md") or "")
        _add_text(tar, "README.md", readme_text)

        for fpath in created_files:
            if not os.path.isfile(fpath):
                continue
            arc_name = os.path.basename(fpath)
            tar.add(fpath, arcname=arc_name)
            digest = _sha256_file(fpath)
            _add_text(tar, f"{arc_name}.sha256", digest + "\n")

    return bundle_path


def list_bundles(limit: int = 50) -> list[dict]:
    """Return newest-first metadata for bundles in the bundles directory."""
    os.makedirs(_BUNDLE_DIR, exist_ok=True)
    entries = []
    for fname in os.listdir(_BUNDLE_DIR):
        if not fname.endswith(".tar.gz"):
            continue
        fpath = os.path.join(_BUNDLE_DIR, fname)
        stat = os.stat(fpath)
        # Parse run_id from filename: analysis_{run_id}_{ts}.tar.gz
        run_id = None
        parts = fname.replace(".tar.gz", "").split("_")
        if len(parts) >= 2:
            try:
                run_id = int(parts[1])
            except ValueError:
                pass
        created_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        entries.append({
            "path": fpath,
            "bytes": stat.st_size,
            "created_iso": created_iso,
            "run_id": run_id,
        })

    entries.sort(key=lambda e: e["created_iso"], reverse=True)
    return entries[:limit]
