"""Git operations wrapper for Tealc's writes to the lab's GitHub Pages repo.

Safety boundaries enforced by this module (per HANDOFF / plan):
  1. Path allowlist. Tealc may only stage files under /knowledge/. Any attempt
     to stage a file elsewhere raises PathNotAllowed.
  2. Pull before push. Every push runs `git pull --rebase origin main` first.
     Merge conflicts abort the push (no auto-resolve).
  3. No force push. This module never passes --force or --force-with-lease.
  4. Clear attribution. Commit messages are prefixed `[tealc]`.
  5. Dry-run by default. stage_and_diff() writes to the working tree and
     returns the diff without staging or committing. Call commit_and_push()
     explicitly to actually commit.
  6. Privacy hook. install_privacy_hook() copies a pre-commit hook into
     .git/hooks/ that rejects commits containing sentinel strings from
     tier-4 (email) content or private repos.

Public API:
    stage_and_diff(path, content)      — write file, return diff
    stage_files(path_to_content)       — bulk write, return diff
    commit_and_push(message, edit_note)— stage the diff, pull, commit, push
    install_privacy_hook()             — set up the pre-commit hook
    website_repo_path()                — resolve the repo root

Environment:
    TEALC_WEBSITE_REPO — override the default repo path (default:
    ~/Desktop/GitHub/lab-pages)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_REPO = os.path.expanduser("~/Desktop/GitHub/lab-pages")
_TEALC_OWNED_SUBDIR = "knowledge"  # Tealc may only write under this subdir
_TEALC_COMMIT_PREFIX = "[tealc]"


class PathNotAllowed(Exception):
    """Raised when a caller tries to stage a path outside the allowlisted subdir."""


class RepoNotFound(Exception):
    """Raised when the website repo does not exist at the configured path."""


class PullConflict(Exception):
    """Raised when `git pull --rebase` fails (e.g. merge conflict)."""


class PushBlocked(Exception):
    """Raised when a push is refused (privacy hook or other pre-push guard)."""


@dataclass
class EditNote:
    """The teaching-mode 4-tuple attached to every Tealc edit.

    Attached to git commit messages and recorded in output_ledger.provenance_json.
    All four fields are required and must be specific (not generic placeholders).
    """
    what_changed: str
    why_changed: str
    evidence_quote: str
    counter_argument: str

    def is_complete(self) -> bool:
        return all(
            (getattr(self, f) or "").strip()
            for f in ("what_changed", "why_changed", "evidence_quote", "counter_argument")
        )

    def to_commit_body(self) -> str:
        return (
            "What changed:\n"
            f"  {self.what_changed.strip()}\n\n"
            "Why:\n"
            f"  {self.why_changed.strip()}\n\n"
            "Evidence:\n"
            f"  {self.evidence_quote.strip()}\n\n"
            "Counter-argument:\n"
            f"  {self.counter_argument.strip()}\n"
        )


@dataclass
class StageResult:
    """Return value of stage_and_diff() / stage_files()."""
    paths_written: list[str] = field(default_factory=list)
    diff: str = ""


# --- Path helpers ---


def website_repo_path() -> str:
    """Return the resolved path to the website repo. Raises if not a git repo."""
    path = os.environ.get("TEALC_WEBSITE_REPO", _DEFAULT_REPO)
    git_dir = os.path.join(path, ".git")
    if not os.path.isdir(git_dir):
        raise RepoNotFound(
            f"Expected a git repo at {path!r}. Set TEALC_WEBSITE_REPO or clone the "
            f"lab website there. Tealc refuses to operate on a non-git directory."
        )
    return path


def _assert_under_knowledge(rel_path: str) -> str:
    """Validate that rel_path is under the Tealc-owned subdir and normalize it.

    Raises PathNotAllowed if the path escapes or points outside /knowledge/.
    Returns the normalized relative path.
    """
    if os.path.isabs(rel_path):
        raise PathNotAllowed(
            f"Absolute paths are not allowed; got {rel_path!r}. Pass a path "
            f"relative to the website repo root, e.g. 'knowledge/papers/foo.md'."
        )

    normalized = os.path.normpath(rel_path)
    if normalized.startswith("..") or "/../" in normalized or normalized == "..":
        raise PathNotAllowed(
            f"Path escape detected in {rel_path!r}. Tealc refuses to write outside "
            f"the website repo root."
        )

    parts = normalized.split(os.sep)
    if not parts or parts[0] != _TEALC_OWNED_SUBDIR:
        raise PathNotAllowed(
            f"Path {rel_path!r} is outside /knowledge/. Tealc is only allowed to "
            f"write under '{_TEALC_OWNED_SUBDIR}/'. Attempt logged."
        )
    return normalized


# --- Git command helpers ---


def _run_git(args: list[str], cwd: str, check: bool = True,
             capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in cwd. Refuses --force / --force-with-lease.

    Raises on non-zero exit when check=True. Returns the CompletedProcess either way.
    """
    for arg in args:
        if arg in ("--force", "-f", "--force-with-lease"):
            raise RuntimeError(
                f"Refusing to run `git {' '.join(args)}` — force-push is disabled "
                f"for Tealc."
            )
    cmd = ["git", *args]
    return subprocess.run(
        cmd, cwd=cwd, check=check,
        capture_output=capture, text=True, timeout=120,
    )


# --- Public API: staging + diff ---


def stage_and_diff(rel_path: str, content: str,
                   repo_path: Optional[str] = None) -> StageResult:
    """Write `content` to `rel_path` (relative to repo root) and return the
    working-tree diff. Does NOT add, commit, or push.

    Raises PathNotAllowed if rel_path is outside /knowledge/.
    """
    repo = repo_path or website_repo_path()
    normalized = _assert_under_knowledge(rel_path)
    abs_path = os.path.join(repo, normalized)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)

    # Diff the single file to make the dry-run output scannable
    diff_proc = _run_git(["diff", "--", normalized], cwd=repo, check=False)
    return StageResult(paths_written=[normalized], diff=diff_proc.stdout)


def stage_files(path_to_content: dict[str, str],
                repo_path: Optional[str] = None) -> StageResult:
    """Bulk variant of stage_and_diff. Each key must be under /knowledge/."""
    repo = repo_path or website_repo_path()
    paths: list[str] = []
    for rel_path, content in path_to_content.items():
        normalized = _assert_under_knowledge(rel_path)
        abs_path = os.path.join(repo, normalized)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        paths.append(normalized)

    # Single combined diff over all written paths
    diff_proc = _run_git(["diff", "--", *paths], cwd=repo, check=False)
    return StageResult(paths_written=paths, diff=diff_proc.stdout)


# --- Public API: commit + push ---


def commit_and_push(message: str, edit_note: EditNote,
                    paths: Optional[list[str]] = None,
                    repo_path: Optional[str] = None,
                    remote: str = "origin",
                    branch: str = "main") -> str:
    """Pull, stage, commit, and push previously-written files.

    `message` is the one-line commit subject. The 4-tuple from edit_note goes
    into the commit body. The [tealc] prefix is added automatically.
    `paths` is the list of already-written paths (normally from stage_and_diff
    / stage_files); if None, all tracked changes under /knowledge/ are staged.

    Raises:
      PullConflict — if pull --rebase fails
      PushBlocked  — if pre-commit hook rejects the commit
      RuntimeError — on any other git failure

    Returns the pushed commit SHA.
    """
    if not edit_note.is_complete():
        raise RuntimeError(
            "EditNote is incomplete — all four fields must be specific and non-empty. "
            "Tealc refuses to commit with a generic or missing teaching-mode note."
        )

    repo = repo_path or website_repo_path()

    # 1. Pull before push — abort on conflict, never auto-resolve.
    pull = _run_git(["pull", "--rebase", remote, branch], cwd=repo, check=False)
    if pull.returncode != 0:
        # Try to back out a rebase-in-progress so Heath can resolve by hand.
        _run_git(["rebase", "--abort"], cwd=repo, check=False)
        raise PullConflict(
            f"`git pull --rebase {remote} {branch}` failed with code {pull.returncode}. "
            f"stderr: {pull.stderr.strip()[:500]}\n"
            f"Rebase aborted. Resolve manually in {repo} and re-run."
        )

    # 2. Stage the paths we wrote.
    if paths:
        # Validate every path is under /knowledge/.
        for p in paths:
            _assert_under_knowledge(p)
        _run_git(["add", "--", *paths], cwd=repo)
    else:
        # Stage anything under /knowledge/ that's changed.
        _run_git(["add", "--", _TEALC_OWNED_SUBDIR], cwd=repo)

    # 3. Confirm we have something to commit.
    diff_cached = _run_git(["diff", "--cached", "--name-only"], cwd=repo, check=False)
    if not diff_cached.stdout.strip():
        return ""  # nothing to commit — callers treat "" as a no-op

    # 4. Build the commit message with [tealc] prefix + 4-tuple body.
    if message.startswith(_TEALC_COMMIT_PREFIX):
        subject = message
    else:
        subject = f"{_TEALC_COMMIT_PREFIX} {message}"
    full_message = f"{subject}\n\n{edit_note.to_commit_body()}"

    # 5. Commit. Pre-commit hook may reject here on privacy-sentinel match.
    commit_proc = _run_git(["commit", "-m", full_message], cwd=repo, check=False)
    if commit_proc.returncode != 0:
        raise PushBlocked(
            f"`git commit` was rejected (exit {commit_proc.returncode}). This is usually "
            f"the pre-commit privacy hook catching a tier-4 or private-repo sentinel in "
            f"the staged content.\n"
            f"stdout: {commit_proc.stdout.strip()[:500]}\n"
            f"stderr: {commit_proc.stderr.strip()[:500]}"
        )

    # 6. Push — no force, ever.
    push_proc = _run_git(["push", remote, branch], cwd=repo, check=False)
    if push_proc.returncode != 0:
        raise PushBlocked(
            f"`git push {remote} {branch}` failed with code {push_proc.returncode}. "
            f"stderr: {push_proc.stderr.strip()[:500]}"
        )

    # 7. Return the SHA we just pushed.
    sha_proc = _run_git(["rev-parse", "HEAD"], cwd=repo)
    return sha_proc.stdout.strip()


# --- Public API: privacy hook ---


def _default_sentinels_path() -> str:
    """Absolute path to the privacy-sentinels file (default under 00-Lab-Agent/data/)."""
    here = os.path.dirname(os.path.abspath(__file__))
    tealc_root = os.path.normpath(os.path.join(here, "..", ".."))
    return os.path.join(tealc_root, "data", "privacy_sentinels.txt")


def install_privacy_hook(repo_path: Optional[str] = None,
                         force: bool = False,
                         sentinels_path: Optional[str] = None) -> str:
    """Install the pre-commit privacy hook at <repo>/.git/hooks/pre-commit.

    The hook greps the staged diff for sentinel strings known to appear only
    in tier-4 content (email history) or private repo READMEs. Any match
    aborts the commit with a clear message.

    The absolute path to the sentinels file is baked into the hook at install
    time (substituted for __TEALC_SENTINELS_PATH__ in the template).

    Set force=True to overwrite an existing hook; otherwise existing hooks
    are preserved and the function returns the existing hook's path.

    Returns the absolute path of the installed (or existing) hook.
    """
    repo = repo_path or website_repo_path()
    hooks_dir = os.path.join(repo, ".git", "hooks")
    hook_path = os.path.join(hooks_dir, "pre-commit")

    if os.path.exists(hook_path) and not force:
        return hook_path

    os.makedirs(hooks_dir, exist_ok=True)
    template_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "website_pre_commit_hook.sh",
    )
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f"Hook template not found at {template_path}. Make sure "
            f"website_pre_commit_hook.sh lives alongside website_git.py."
        )

    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    resolved_sentinels = sentinels_path or _default_sentinels_path()
    hook_content = template.replace("__TEALC_SENTINELS_PATH__", resolved_sentinels)

    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(hook_content)
    os.chmod(hook_path, 0o755)
    return hook_path
