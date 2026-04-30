"""Zenodo deposition client for Tealc.

Env vars:
  ZENODO_ACCESS_TOKEN      — production token (always required for production writes)
  ZENODO_SANDBOX_TOKEN     — sandbox token; falls back to ZENODO_ACCESS_TOKEN if not set
  ZENODO_USE_SANDBOX="1"   — (legacy) treat module-level calls as sandbox
  TEALC_ENV                — if set to anything other than "production", triggers extra
                             WARN before publish_deposit on the production endpoint

DOI minting is TWO-STEP: create_deposit() reserves a DOI; publish_deposit()
mints it (IRREVERSIBLE). Callers must call publish explicitly.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TOKEN: str | None = os.environ.get("ZENODO_ACCESS_TOKEN")
_SANDBOX: bool = os.environ.get("ZENODO_USE_SANDBOX", "0").strip() == "1"
_BASE_URL: str = (
    "https://sandbox.zenodo.org/api" if _SANDBOX else "https://zenodo.org/api"
)
_RESEARCHER_EMAIL: str = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
_HEADERS: dict[str, str] = {
    "User-Agent": f"Tealc/1.0 ({_RESEARCHER_EMAIL})",
}
_TIMEOUT: int = 30
_RETRY_DELAY: float = 2.0
_LARGE_FILE_THRESHOLD: int = 10 * 1024 * 1024  # 10 MB

_ERR_NO_TOKEN: dict[str, str] = {"error": "ZENODO_ACCESS_TOKEN not set"}

_VALID_UPLOAD_TYPES = {
    "dataset", "software", "image", "presentation", "poster",
    "journal-article", "article", "publication", "video", "lesson", "other",
}
_VALID_ACCESS_RIGHTS = {"open", "embargoed", "restricted", "closed"}


def is_configured(sandbox: bool = False) -> bool:
    """Return True iff the appropriate token is set for the chosen environment.

    For sandbox=True checks ZENODO_SANDBOX_TOKEN first, then falls back to
    ZENODO_ACCESS_TOKEN.  For sandbox=False checks ZENODO_ACCESS_TOKEN.
    """
    return bool(_get_token(sandbox))


# ---------------------------------------------------------------------------
# Per-call environment helpers (new, sandbox-aware)
# ---------------------------------------------------------------------------

def _get_token(sandbox: bool) -> str | None:
    """Return the right token for sandbox vs production."""
    if sandbox:
        return os.environ.get("ZENODO_SANDBOX_TOKEN") or os.environ.get("ZENODO_ACCESS_TOKEN")
    return os.environ.get("ZENODO_ACCESS_TOKEN")


def _get_base(sandbox: bool) -> str:
    if sandbox:
        return "https://sandbox.zenodo.org/api"
    return "https://zenodo.org/api"


def _auth_headers_for(sandbox: bool, extra: dict | None = None) -> dict[str, str]:
    h = dict(_HEADERS)
    tok = _get_token(sandbox)
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if extra:
        h.update(extra)
    return h


def _request_env(
    method: str,
    url: str,
    sandbox: bool,
    *,
    params: dict | None = None,
    json: dict | None = None,
    data=None,
    extra_headers: dict | None = None,
) -> requests.Response:
    """Single-retry wrapper (2 s backoff) on 5xx responses, sandbox-aware."""
    headers = _auth_headers_for(sandbox, extra_headers)
    kwargs: dict = {"headers": headers, "timeout": _TIMEOUT}
    if params:
        kwargs["params"] = params
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data

    resp = requests.request(method, url, **kwargs)
    if resp.status_code >= 500:
        time.sleep(_RETRY_DELAY)
        resp = requests.request(method, url, **kwargs)
    return resp


# ---------------------------------------------------------------------------
# Internal helpers (module-level, kept for backward compat)
# ---------------------------------------------------------------------------

def _auth_headers(extra: dict | None = None) -> dict[str, str]:
    """Merge base headers with Bearer auth and any extras."""
    h = dict(_HEADERS)
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    if extra:
        h.update(extra)
    return h


def _request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    data: bytes | None = None,
    extra_headers: dict | None = None,
) -> requests.Response:
    """Single-retry wrapper (2 s backoff) on 5xx responses."""
    headers = _auth_headers(extra_headers)
    kwargs: dict = {"headers": headers, "timeout": _TIMEOUT}
    if params:
        kwargs["params"] = params
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data

    resp = requests.request(method, url, **kwargs)
    if resp.status_code >= 500:
        time.sleep(_RETRY_DELAY)
        resp = requests.request(method, url, **kwargs)
    return resp


def _check_token() -> bool:
    """Return True if token present, False otherwise."""
    return bool(_TOKEN)


def _shape_deposit(raw: dict) -> dict:
    """Extract a consistent summary dict from a raw deposit record."""
    meta = raw.get("metadata", {})
    return {
        "deposit_id": raw.get("id"),
        "title": meta.get("title", ""),
        "state": raw.get("state", ""),
        "doi": raw.get("doi") or meta.get("prereserve_doi", {}).get("doi"),
        "created": raw.get("created"),
        "modified": raw.get("modified"),
        "bucket_url": raw.get("links", {}).get("bucket"),
        "html_url": raw.get("links", {}).get("html"),
    }


# ---------------------------------------------------------------------------
# Public write API — new sandbox-aware functions
# ---------------------------------------------------------------------------

def create_deposit(metadata: dict, sandbox: bool = False) -> dict:
    """Create a new draft deposit (or return an existing unsubmitted one).

    Idempotency: before creating, searches for an existing unsubmitted deposit
    with the exact same title and returns it if found.

    metadata keys:
      title (str, required)
      description (str, required)
      creators (list[{name, affiliation, orcid?}], required)
      upload_type (str, default "dataset")
      access_right (str, default "open")
      license (str, default "cc-by-4.0")
      keywords (list[str], optional)
      communities (list[str], optional — list of community identifiers)
      related_identifiers (list[dict], optional)
      publication_date (str YYYY-MM-DD, optional)

    Returns the full deposit dict including id, links, state.
    Raises ValueError on missing required fields.
    """
    # --- Validate required fields first (before token check so errors are clear) ---
    title = metadata.get("title", "").strip()
    description = metadata.get("description", "").strip()
    creators = metadata.get("creators", [])
    if not title:
        raise ValueError("metadata['title'] is required")
    if not description:
        raise ValueError("metadata['description'] is required")
    if not creators or not isinstance(creators, list):
        raise ValueError("metadata['creators'] must be a non-empty list of {name, affiliation, orcid?}")

    tok = _get_token(sandbox)
    if not tok:
        return {"error": "No token available. Set ZENODO_SANDBOX_TOKEN (sandbox) or ZENODO_ACCESS_TOKEN."}

    base = _get_base(sandbox)

    upload_type = metadata.get("upload_type", "dataset")
    access_right = metadata.get("access_right", "open")
    license_id = metadata.get("license", "cc-by-4.0")
    keywords = metadata.get("keywords") or []
    communities = metadata.get("communities") or []
    related_identifiers = metadata.get("related_identifiers") or []
    publication_date = metadata.get("publication_date")

    # --- Idempotency check ---
    search_resp = _request_env(
        "GET",
        f"{base}/deposit/depositions",
        sandbox,
        params={"q": f'title:"{title}"', "size": 10, "page": 1},
    )
    if search_resp.status_code == 200:
        for existing in search_resp.json():
            existing_title = existing.get("metadata", {}).get("title", "")
            existing_state = existing.get("state", "")
            if existing_title == title and existing_state == "unsubmitted":
                logger.info(
                    "create_deposit: idempotency hit — returning existing unsubmitted "
                    "deposit %s for title '%s'", existing.get("id"), title
                )
                return existing

    # --- Build metadata payload ---
    zen_meta: dict = {
        "title": title,
        "description": description,
        "upload_type": upload_type,
        "access_right": access_right,
        "license": license_id,
        "creators": [
            {k: v for k, v in c.items() if k in ("name", "affiliation", "orcid")}
            for c in creators
        ],
    }
    if keywords:
        zen_meta["keywords"] = keywords
    if communities:
        zen_meta["communities"] = [{"identifier": cid} for cid in communities]
    if related_identifiers:
        zen_meta["related_identifiers"] = related_identifiers
    if publication_date:
        zen_meta["publication_date"] = publication_date

    # --- Step 1: POST empty body to reserve deposit ---
    create_resp = _request_env(
        "POST",
        f"{base}/deposit/depositions",
        sandbox,
        json={},
        extra_headers={"Content-Type": "application/json"},
    )
    if create_resp.status_code not in (200, 201):
        return {"error": f"HTTP {create_resp.status_code}", "detail": create_resp.text[:400]}

    raw = create_resp.json()
    deposit_id = raw["id"]

    # --- Step 2: PUT metadata to update the deposit ---
    put_resp = _request_env(
        "PUT",
        f"{base}/deposit/depositions/{deposit_id}",
        sandbox,
        json={"metadata": zen_meta},
        extra_headers={"Content-Type": "application/json"},
    )
    if put_resp.status_code not in (200, 201):
        return {
            "error": f"Metadata PUT failed HTTP {put_resp.status_code}",
            "detail": put_resp.text[:400],
            "deposit_id": deposit_id,
        }

    return put_resp.json()


def upload_zenodo_file(deposit_id: int, file_path: str, sandbox: bool = False) -> dict:
    """Upload a file to a deposit via the bucket API (streaming for large files).

    Files >10 MB are streamed in chunks rather than loaded fully into memory.
    Uses the bucket PUT API (not the deprecated /files POST endpoint).

    Returns file metadata dict.
    """
    tok = _get_token(sandbox)
    if not tok:
        return {"error": "No token available. Set ZENODO_SANDBOX_TOKEN (sandbox) or ZENODO_ACCESS_TOKEN."}

    base = _get_base(sandbox)
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    target_name = path.name
    file_size = path.stat().st_size

    # Fetch deposit to get bucket URL
    dep_resp = _request_env("GET", f"{base}/deposit/depositions/{deposit_id}", sandbox)
    if dep_resp.status_code == 404:
        return {"error": f"Deposit {deposit_id} not found"}
    if dep_resp.status_code != 200:
        return {"error": f"HTTP {dep_resp.status_code} fetching deposit", "detail": dep_resp.text[:400]}

    dep_raw = dep_resp.json()
    bucket_url = dep_raw.get("links", {}).get("bucket")
    if not bucket_url:
        return {"error": "No bucket URL found for deposit — is it still in draft state?"}

    upload_url = f"{bucket_url}/{target_name}"
    headers = _auth_headers_for(sandbox, {"Content-Type": "application/octet-stream"})

    if file_size > _LARGE_FILE_THRESHOLD:
        logger.info("upload_zenodo_file: streaming %s (%d bytes)", target_name, file_size)
        with open(path, "rb") as fh:
            resp = requests.put(upload_url, data=fh, headers=headers, timeout=300)
        if resp.status_code >= 500:
            time.sleep(_RETRY_DELAY)
            with open(path, "rb") as fh:
                resp = requests.put(upload_url, data=fh, headers=headers, timeout=300)
    else:
        with open(path, "rb") as fh:
            data = fh.read()
        resp = _request_env(
            "PUT",
            upload_url,
            sandbox,
            data=data,
            extra_headers={"Content-Type": "application/octet-stream"},
        )

    if resp.status_code not in (200, 201):
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}

    raw = resp.json()
    return {
        "filename": raw.get("filename") or raw.get("key", target_name),
        "size_bytes": raw.get("size"),
        "checksum": raw.get("checksum"),
        "file_id": raw.get("id"),
    }


def publish_deposit(deposit_id: int, confirmed: bool = False, sandbox: bool = False) -> dict:
    """Publish a deposit, minting its DOI.

    IRREVERSIBLE — a published deposit cannot be deleted, only versioned.
    Requires confirmed=True to proceed (safety gate).

    If TEALC_ENV is set to anything other than "production" and sandbox=False,
    an additional WARN is logged.

    Returns the published record including the minted DOI.
    """
    if not confirmed:
        raise ValueError(
            "publish_deposit is irreversible — pass confirmed=True to proceed"
        )

    tok = _get_token(sandbox)
    if not tok:
        return {"error": "No token available. Set ZENODO_SANDBOX_TOKEN (sandbox) or ZENODO_ACCESS_TOKEN."}

    base = _get_base(sandbox)

    # Warn if running in a non-production env but targeting production
    if not sandbox:
        tealc_env = os.environ.get("TEALC_ENV", "").strip().lower()
        config_env = _read_config_env()
        if tealc_env and tealc_env != "production":
            logger.warning(
                "publish_deposit: TEALC_ENV=%s but targeting PRODUCTION Zenodo "
                "(deposit_id=%s). This will mint a real, irreversible DOI.",
                tealc_env, deposit_id,
            )
        elif config_env and config_env != "production":
            logger.warning(
                "publish_deposit: config.json env=%s but targeting PRODUCTION Zenodo "
                "(deposit_id=%s). This will mint a real, irreversible DOI.",
                config_env, deposit_id,
            )

    resp = _request_env(
        "POST",
        f"{base}/deposit/depositions/{deposit_id}/actions/publish",
        sandbox,
        extra_headers={"Content-Type": "application/json"},
    )

    if resp.status_code not in (200, 201, 202):
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}

    raw = resp.json()
    meta = raw.get("metadata", {})
    files = [
        {"filename": f.get("filename"), "size_bytes": f.get("filesize")}
        for f in raw.get("files", [])
    ]
    return {
        "deposit_id": raw.get("id"),
        "doi": raw.get("doi"),
        "html_url": raw.get("links", {}).get("html"),
        "files": files,
        "published_date": meta.get("publication_date"),
        "state": raw.get("state"),
    }


def delete_deposit(deposit_id: int, sandbox: bool = False) -> dict:
    """Delete an unsubmitted (draft) deposit. Cannot delete published records.

    Returns {"deleted": True} on success.
    """
    tok = _get_token(sandbox)
    if not tok:
        return {"error": "No token available."}

    base = _get_base(sandbox)
    resp = _request_env("DELETE", f"{base}/deposit/depositions/{deposit_id}", sandbox)
    if resp.status_code == 204:
        return {"deleted": True, "deposit_id": deposit_id}
    if resp.status_code == 404:
        return {"error": f"Deposit {deposit_id} not found"}
    return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}


def _read_config_env() -> str | None:
    """Read the 'env' field from data/config.json if present."""
    try:
        import json
        config_path = Path(__file__).parent.parent.parent / "data" / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                return json.load(f).get("env", "").strip().lower() or None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public API — original kwargs-based create_deposit (kept for back-compat)
# ---------------------------------------------------------------------------

def create_deposit_kwargs(
    title: str,
    description: str,
    creators: list[dict],
    upload_type: str = "dataset",
    access_right: str = "open",
    license: str = "CC-BY-4.0",
    keywords: list[str] | None = None,
    communities: list[str] | None = None,
    related_identifiers: list[dict] | None = None,
    publication_date: str | None = None,
) -> dict:
    """Create a new deposit (draft) via named kwargs (backward-compat wrapper).

    Returns {deposit_id, bucket_url, doi_reserved, html_url}.
    The DOI is reserved but NOT minted — call publish_deposit() to mint it.
    """
    if not _check_token():
        return _ERR_NO_TOKEN

    metadata: dict = {
        "title": title,
        "description": description,
        "upload_type": upload_type,
        "access_right": access_right,
        "license": license,
        "creators": [
            {k: v for k, v in c.items() if k in ("name", "affiliation", "orcid")}
            for c in creators
        ],
    }
    if keywords:
        metadata["keywords"] = keywords
    if communities:
        metadata["communities"] = [{"identifier": cid} for cid in communities]
    if related_identifiers:
        metadata["related_identifiers"] = related_identifiers
    if publication_date:
        metadata["publication_date"] = publication_date

    resp = _request(
        "POST",
        f"{_BASE_URL}/deposit/depositions",
        json={"metadata": metadata},
        extra_headers={"Content-Type": "application/json"},
    )

    if resp.status_code not in (200, 201):
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}

    raw = resp.json()
    pre_doi = raw.get("metadata", {}).get("prereserve_doi", {}).get("doi")
    return {
        "deposit_id": raw["id"],
        "bucket_url": raw["links"]["bucket"],
        "doi_reserved": pre_doi,
        "html_url": raw["links"]["html"],
    }


def upload_file(
    deposit_id: int | str,
    file_path: str,
    filename: str | None = None,
) -> dict:
    """Upload a file via bucket API (PUT raw bytes). Returns {filename, size_bytes, checksum, file_id}."""
    if not _check_token():
        return _ERR_NO_TOKEN

    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    target_name = filename or path.name

    # Fetch deposit to get bucket URL
    dep = get_deposit(deposit_id)
    if dep is None:
        return {"error": f"Deposit {deposit_id} not found"}
    bucket_url = dep.get("bucket_url") or dep.get("links", {}).get("bucket")
    if not bucket_url:
        return {"error": "Could not determine bucket URL for deposit"}

    with open(path, "rb") as fh:
        data = fh.read()

    resp = _request(
        "PUT",
        f"{bucket_url}/{target_name}",
        data=data,
        extra_headers={"Content-Type": "application/octet-stream"},
    )

    if resp.status_code not in (200, 201):
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}

    raw = resp.json()
    return {
        "filename": raw.get("filename") or raw.get("key", target_name),
        "size_bytes": raw.get("size"),
        "checksum": raw.get("checksum"),
        "file_id": raw.get("id"),
    }


def upload_multiple_files(
    deposit_id: int | str,
    file_paths: list[str],
) -> list[dict]:
    """Upload many files in sequence. Returns list of upload result dicts."""
    results = []
    for fp in file_paths:
        results.append(upload_file(deposit_id, fp))
    return results


def publish_deposit_legacy(deposit_id: int | str) -> dict:
    """Publish a deposit (legacy, no confirmation gate).

    IRREVERSIBLE — a published deposit cannot be deleted, only versioned.
    Returns {doi, html_url, files, published_date}.

    Prefer publish_deposit(deposit_id, confirmed=True) for new code.
    """
    if not _check_token():
        return _ERR_NO_TOKEN

    resp = _request(
        "POST",
        f"{_BASE_URL}/deposit/depositions/{deposit_id}/actions/publish",
        extra_headers={"Content-Type": "application/json"},
    )

    if resp.status_code not in (200, 201, 202):
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}

    raw = resp.json()
    meta = raw.get("metadata", {})
    files = [
        {"filename": f.get("filename"), "size_bytes": f.get("filesize")}
        for f in raw.get("files", [])
    ]
    return {
        "doi": raw.get("doi"),
        "html_url": raw.get("links", {}).get("html"),
        "files": files,
        "published_date": meta.get("publication_date"),
    }


def list_deposits(
    published: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """List deposits. published=True/False/None filters by state.
    Returns [{deposit_id, title, state, doi, created, modified}]."""
    params: dict = {"size": limit, "sort": "mostrecent", "page": 1}
    if published is True:
        params["status"] = "published"
    elif published is False:
        params["status"] = "draft"

    resp = _request("GET", f"{_BASE_URL}/deposit/depositions", params=params)

    if resp.status_code != 200:
        return [{"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}]

    return [_shape_deposit(r) for r in resp.json()]


def get_deposit(deposit_id: int | str) -> dict | None:
    """Fetch a deposit's current state. Returns None if not found."""
    resp = _request("GET", f"{_BASE_URL}/deposit/depositions/{deposit_id}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}
    raw = resp.json()
    shaped = _shape_deposit(raw)
    # Carry through raw links so upload_file can find bucket_url reliably
    shaped["links"] = raw.get("links", {})
    return shaped


def new_version(parent_deposit_id: int | str) -> dict:
    """Create a new version of a published deposit. Returns new draft deposit dict.
    Upload files then call publish_deposit() to mint the new DOI."""
    if not _check_token():
        return _ERR_NO_TOKEN

    resp = _request(
        "POST",
        f"{_BASE_URL}/deposit/depositions/{parent_deposit_id}/actions/newversion",
        extra_headers={"Content-Type": "application/json"},
    )

    if resp.status_code not in (200, 201):
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}

    # Zenodo returns the *parent* record; the new draft URL is in links.latest_draft
    raw = resp.json()
    draft_url = raw.get("links", {}).get("latest_draft")
    if not draft_url:
        return {"error": "No latest_draft link in response", "raw": raw}

    # Fetch the new draft deposit
    draft_resp = _request("GET", draft_url)
    if draft_resp.status_code != 200:
        return {
            "error": f"HTTP {draft_resp.status_code} fetching new draft",
            "detail": draft_resp.text[:400],
        }

    draft = draft_resp.json()
    pre_doi = draft.get("metadata", {}).get("prereserve_doi", {}).get("doi")
    return {
        "deposit_id": draft["id"],
        "bucket_url": draft["links"].get("bucket"),
        "doi_reserved": pre_doi,
        "html_url": draft["links"].get("html"),
    }


def upload_reproducibility_bundle(
    bundle_path: str,
    project_name: str,
    analysis_summary: str,
    related_paper_doi: str | None = None,
) -> dict:
    """Upload an agent/bundle.py tarball with sensible defaults, then publish.

    - title: 'Reproducibility bundle: {project_name}'
    - creators: read from RESEARCHER_CREATOR_NAME / RESEARCHER_ORCID /
                RESEARCHER_AFFILIATION env vars
                (defaults: 'Researcher, A.' / '' / 'University')
    - upload_type: 'software', license: 'MIT'
    - keywords: reproducibility, phylogenetics, comparative genomics, project_name
    - If related_paper_doi given, adds an isSupplementTo relation.
    - Publishes immediately after upload.

    Returns {deposit_id, doi, html_url}.
    """
    if not _check_token():
        return _ERR_NO_TOKEN

    creators = [
        {
            "name": os.environ.get("RESEARCHER_CREATOR_NAME", "Researcher, A."),
            "orcid": os.environ.get("RESEARCHER_ORCID", "0000-0002-5433-4036"),
            "affiliation": os.environ.get("RESEARCHER_AFFILIATION", "Texas A&M University"),
        }
    ]
    keywords = ["reproducibility", "phylogenetics", "comparative genomics", project_name]
    related_identifiers: list[dict] | None = None
    if related_paper_doi:
        related_identifiers = [
            {
                "identifier": related_paper_doi,
                "relation": "isSupplementTo",
                "scheme": "doi",
            }
        ]

    dep = create_deposit_kwargs(
        title=f"Reproducibility bundle: {project_name}",
        description=analysis_summary,
        creators=creators,
        upload_type="software",
        license="MIT",
        keywords=keywords,
        related_identifiers=related_identifiers,
    )
    if "error" in dep:
        return dep

    deposit_id = dep["deposit_id"]

    up = upload_file(deposit_id, bundle_path)
    if "error" in up:
        return {"error": f"Upload failed: {up['error']}", "deposit_id": deposit_id}

    pub = publish_deposit_legacy(deposit_id)
    if "error" in pub:
        return {
            "error": f"Publish failed: {pub['error']}",
            "deposit_id": deposit_id,
            "detail": pub.get("detail", ""),
        }

    return {
        "deposit_id": deposit_id,
        "doi": pub.get("doi"),
        "html_url": pub.get("html_url"),
    }
