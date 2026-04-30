"""GitHub REST API client for Tealc's lab-wiki repo-watcher.

Read-only for v1. Does not push commits, open PRs, or modify any watched
repository — only reads README, recent commits, and open issues.

Exports: list_user_repos, list_org_repos, get_repo, get_readme,
         get_recent_commits, get_open_issues, get_commit, get_issue,
         RateLimitExceeded, NotAuthenticated

Authentication: reads GITHUB_PAT from the environment (via .env loaded at
module import time). If GITHUB_PAT is missing, calls raise NotAuthenticated
rather than silently falling back to unauthenticated requests (which have a
much lower rate limit and cannot see private repos).
"""
from __future__ import annotations

import base64
import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

_BASE = "https://api.github.com"
_RESEARCHER_EMAIL = os.environ.get("RESEARCHER_EMAIL", "researcher@example.org")
_HEADERS_BASE = {
    "User-Agent": f"Tealc/1.0 ({_RESEARCHER_EMAIL})",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_TIMEOUT = 10
_RETRY_DELAYS = [2, 4]


class NotAuthenticated(Exception):
    """Raised when GITHUB_PAT is not set in the environment."""


class RateLimitExceeded(Exception):
    """Raised when GitHub's 5000/hr authenticated rate limit is hit."""


def _auth_headers() -> dict:
    token = os.environ.get("GITHUB_PAT")
    if not token:
        raise NotAuthenticated(
            "GITHUB_PAT is not set. Add a fine-grained PAT to .env — see .env.example."
        )
    return {**_HEADERS_BASE, "Authorization": f"Bearer {token}"}


def _get_with_retry(path: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    """GET with one retry on transient 429/5xx. Returns None on permanent failure.

    Raises NotAuthenticated if no PAT; RateLimitExceeded if GitHub says we're out.
    """
    headers = _auth_headers()  # raises NotAuthenticated if token missing
    url = path if path.startswith("http") else f"{_BASE}{path}"

    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        except requests.exceptions.RequestException:
            if attempt >= len(_RETRY_DELAYS):
                return None
            continue

        if resp.status_code == 200:
            return resp
        if resp.status_code == 404:
            return None
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset", "?")
            raise RateLimitExceeded(f"GitHub rate limit hit; resets at {reset} (unix)")
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt < len(_RETRY_DELAYS):
                continue
        # Anything else — fail soft so one bad repo does not abort a batch.
        return None
    return None


def _paginate(path: str, params: Optional[dict] = None, max_pages: int = 10) -> list[dict]:
    """Follow Link: next headers until exhausted or max_pages reached."""
    out: list[dict] = []
    next_url: Optional[str] = None
    page_params = dict(params or {})
    page_params.setdefault("per_page", 100)

    for _ in range(max_pages):
        if next_url:
            resp = _get_with_retry(next_url)
        else:
            resp = _get_with_retry(path, params=page_params)
        if resp is None:
            break
        try:
            page = resp.json()
        except Exception:
            break
        if not isinstance(page, list):
            break
        out.extend(page)
        link = resp.headers.get("Link", "")
        next_url = _parse_next_link(link)
        if not next_url:
            break
    return out


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract the rel=next URL from a GitHub Link header, or None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segs = part.split(";")
        if len(segs) < 2:
            continue
        url_seg = segs[0].strip()
        rel_seg = segs[1].strip()
        if rel_seg == 'rel="next"' and url_seg.startswith("<") and url_seg.endswith(">"):
            return url_seg[1:-1]
    return None


# --- Public API ---


def list_user_repos(username: str, include_private: bool = False) -> list[dict]:
    """List repositories owned by a user. If username matches the PAT owner and
    include_private=True, private repos are included.

    Returns [{owner, name, full_name, description, default_branch, language,
              is_private, last_commit_at, url}, ...]
    """
    if username == "me" or include_private:
        # /user/repos returns repos the PAT owner has access to, including private
        path = "/user/repos"
        params = {"affiliation": "owner", "visibility": "all" if include_private else "public"}
    else:
        path = f"/users/{username}/repos"
        params = {"type": "public"}

    raw = _paginate(path, params=params)
    return [_parse_repo(r) for r in raw if isinstance(r, dict)]


def list_org_repos(org: str, include_private: bool = False) -> list[dict]:
    """List repositories owned by an organization."""
    path = f"/orgs/{org}/repos"
    params = {"type": "all" if include_private else "public"}
    raw = _paginate(path, params=params)
    return [_parse_repo(r) for r in raw if isinstance(r, dict)]


def get_repo(owner: str, name: str) -> Optional[dict]:
    """Get metadata for a single repository. Returns None if not found."""
    resp = _get_with_retry(f"/repos/{owner}/{name}")
    if resp is None:
        return None
    return _parse_repo(resp.json())


def get_readme(owner: str, name: str) -> Optional[str]:
    """Get the README content (decoded UTF-8 text). Returns None if no README."""
    resp = _get_with_retry(f"/repos/{owner}/{name}/readme")
    if resp is None:
        return None
    body = resp.json()
    content_b64 = body.get("content", "")
    encoding = body.get("encoding", "base64")
    if encoding != "base64":
        return None
    try:
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        return None


def get_recent_commits(owner: str, name: str, limit: int = 10,
                       since_iso: Optional[str] = None) -> list[dict]:
    """Get the most recent commits on the default branch.

    Returns [{sha, author, date, message, files_changed_count, url}, ...] newest first.
    since_iso filters to commits at or after the given ISO8601 timestamp.
    """
    params: dict = {"per_page": min(limit, 100)}
    if since_iso:
        params["since"] = since_iso
    resp = _get_with_retry(f"/repos/{owner}/{name}/commits", params=params)
    if resp is None:
        return []
    out: list[dict] = []
    for c in resp.json()[:limit]:
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        out.append({
            "sha": c.get("sha", ""),
            "author": author.get("name", ""),
            "date": author.get("date", ""),
            "message": commit.get("message", "").splitlines()[0] if commit.get("message") else "",
            "full_message": commit.get("message", ""),
            "url": c.get("html_url", ""),
        })
    return out


def get_commit(owner: str, name: str, sha: str) -> Optional[dict]:
    """Get full detail on a single commit, including files changed."""
    resp = _get_with_retry(f"/repos/{owner}/{name}/commits/{sha}")
    if resp is None:
        return None
    c = resp.json()
    files = c.get("files") or []
    commit = c.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "sha": c.get("sha", ""),
        "author": author.get("name", ""),
        "date": author.get("date", ""),
        "message": commit.get("message", ""),
        "url": c.get("html_url", ""),
        "files_changed": [
            {"path": f.get("filename", ""), "status": f.get("status", ""),
             "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
            for f in files
        ],
    }


def get_open_issues(owner: str, name: str, limit: int = 20,
                    labels: Optional[list[str]] = None) -> list[dict]:
    """Get open issues for a repository. Excludes pull requests.

    Returns [{number, title, body, labels, created_at, updated_at, url, user}, ...]
    """
    params: dict = {"state": "open", "per_page": min(limit, 100)}
    if labels:
        params["labels"] = ",".join(labels)
    resp = _get_with_retry(f"/repos/{owner}/{name}/issues", params=params)
    if resp is None:
        return []
    out: list[dict] = []
    for i in resp.json()[:limit]:
        # GitHub's issues endpoint returns PRs too; filter them out.
        if i.get("pull_request"):
            continue
        user = i.get("user") or {}
        out.append({
            "number": i.get("number", 0),
            "title": i.get("title", ""),
            "body": i.get("body", "") or "",
            "labels": [lbl.get("name", "") for lbl in (i.get("labels") or [])],
            "created_at": i.get("created_at", ""),
            "updated_at": i.get("updated_at", ""),
            "url": i.get("html_url", ""),
            "user": user.get("login", ""),
        })
    return out


def get_issue(owner: str, name: str, number: int) -> Optional[dict]:
    """Get full detail on a single issue by number."""
    resp = _get_with_retry(f"/repos/{owner}/{name}/issues/{number}")
    if resp is None:
        return None
    i = resp.json()
    if i.get("pull_request"):
        return None  # Caller asked for an issue, not a PR.
    user = i.get("user") or {}
    return {
        "number": i.get("number", 0),
        "title": i.get("title", ""),
        "body": i.get("body", "") or "",
        "labels": [lbl.get("name", "") for lbl in (i.get("labels") or [])],
        "state": i.get("state", ""),
        "created_at": i.get("created_at", ""),
        "updated_at": i.get("updated_at", ""),
        "url": i.get("html_url", ""),
        "user": user.get("login", ""),
    }


# --- Internal helpers ---


def _parse_repo(r: dict) -> dict:
    """Normalize a GitHub repo JSON blob into Tealc's canonical shape."""
    owner = (r.get("owner") or {}).get("login", "")
    return {
        "owner": owner,
        "name": r.get("name", ""),
        "full_name": r.get("full_name", ""),
        "description": r.get("description", "") or "",
        "default_branch": r.get("default_branch", ""),
        "language": r.get("language", "") or "",
        "is_private": bool(r.get("private", False)),
        "last_commit_at": r.get("pushed_at", ""),
        "stars": r.get("stargazers_count", 0),
        "forks": r.get("forks_count", 0),
        "url": r.get("html_url", ""),
    }
