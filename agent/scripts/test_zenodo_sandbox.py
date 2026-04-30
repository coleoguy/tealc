"""Sandbox smoke test for Zenodo write API.

Usage:
  ZENODO_SANDBOX_TOKEN=<token> python agent/scripts/test_zenodo_sandbox.py

Skips silently if ZENODO_SANDBOX_TOKEN is not set.
NEVER publishes — even sandbox publishes mint real (test) DOIs.
Cleans up by deleting the draft deposit after the file upload test.
"""
import os
import sys
import tempfile
import pathlib

# Allow running from the repo root without installing the package
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agent.apis.zenodo import create_deposit, upload_zenodo_file, delete_deposit, is_configured


SANDBOX_TOKEN = os.environ.get("ZENODO_SANDBOX_TOKEN") or os.environ.get("ZENODO_ACCESS_TOKEN")

_PASS = "ok"
_SKIP = "skipped"
_FAIL = "FAILED"


def run():
    results = {
        "create_deposit":       _SKIP,
        "upload_zenodo_file":   _SKIP,
        "delete_deposit":       _SKIP,
    }

    if not SANDBOX_TOKEN:
        print("ZENODO_SANDBOX_TOKEN not set — skipping all sandbox tests.")
        _print_results(results)
        return

    if not is_configured(sandbox=True):
        print("is_configured(sandbox=True) returned False — token lookup failed.")
        _print_results(results)
        return

    deposit_id = None

    # ------------------------------------------------------------------
    # 1. create_deposit
    # ------------------------------------------------------------------
    try:
        meta = {
            "title": "Tealc smoke test deposit — delete me",
            "description": "Automated sandbox smoke test. Safe to delete.",
            "creators": [{"name": os.environ.get("RESEARCHER_CREATOR_NAME", "Researcher, A."), "affiliation": os.environ.get("RESEARCHER_AFFILIATION", "University")}],
            "upload_type": "dataset",
            "access_right": "open",
            "license": "cc-by-4.0",
            "keywords": ["test", "smoke-test", "tealc"],
        }
        dep = create_deposit(meta, sandbox=True)
        if "error" in dep:
            print(f"create_deposit error: {dep}")
            results["create_deposit"] = _FAIL
        else:
            deposit_id = dep.get("id") or dep.get("deposit_id")
            state = dep.get("state", "?")
            print(f"create_deposit: id={deposit_id}  state={state}")
            results["create_deposit"] = _PASS
    except Exception as exc:
        print(f"create_deposit exception: {exc}")
        results["create_deposit"] = _FAIL

    # ------------------------------------------------------------------
    # 2. upload_zenodo_file — write a 1 KB temp file and upload it
    # ------------------------------------------------------------------
    if deposit_id is not None:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="tealc_smoke_", delete=False, mode="w"
            ) as tmp:
                tmp.write("x" * 1024)  # 1 KB
                tmp_path = tmp.name

            up = upload_zenodo_file(deposit_id, tmp_path, sandbox=True)
            if "error" in up:
                print(f"upload_zenodo_file error: {up}")
                results["upload_zenodo_file"] = _FAIL
            else:
                print(
                    f"upload_zenodo_file: filename={up.get('filename')}  "
                    f"size={up.get('size_bytes')}  checksum={up.get('checksum')}"
                )
                results["upload_zenodo_file"] = _PASS
        except Exception as exc:
            print(f"upload_zenodo_file exception: {exc}")
            results["upload_zenodo_file"] = _FAIL
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ------------------------------------------------------------------
    # 3. delete_deposit — clean up; do NOT publish
    # ------------------------------------------------------------------
    if deposit_id is not None:
        try:
            del_result = delete_deposit(deposit_id, sandbox=True)
            if del_result.get("deleted"):
                print(f"delete_deposit: deposit {deposit_id} cleaned up")
                results["delete_deposit"] = _PASS
            else:
                print(f"delete_deposit unexpected result: {del_result}")
                results["delete_deposit"] = _FAIL
        except Exception as exc:
            print(f"delete_deposit exception: {exc}")
            results["delete_deposit"] = _FAIL

    _print_results(results)

    failed = [k for k, v in results.items() if v == _FAIL]
    if failed:
        sys.exit(1)


def _print_results(results: dict):
    print("\n--- Zenodo sandbox smoke test ---")
    for name, status in results.items():
        print(f"  {name}: {status}")
    print("---------------------------------")


if __name__ == "__main__":
    run()
