"""Tealc memory backend — file-backed storage for Anthropic's Memory tool.

Storage root: ~/Library/Application Support/tealc/memories/
(outside Google Drive to avoid Drive-sync corruption, same convention as
scheduler.py uses for agent.db).

Exposed API
-----------
TealcMemoryTool       — subclass of BetaAbstractMemoryTool, all six commands
build_memory_tool()   — factory that returns a configured TealcMemoryTool
MEMORY_TOOL_SPEC      — tool-spec dict for messages.create(..., tools=[...])
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from urllib.parse import unquote

from anthropic.lib.tools import BetaAbstractMemoryTool, ToolError
from anthropic.types.beta import (
    BetaMemoryTool20250818ViewCommand,
    BetaMemoryTool20250818CreateCommand,
    BetaMemoryTool20250818DeleteCommand,
    BetaMemoryTool20250818InsertCommand,
    BetaMemoryTool20250818RenameCommand,
    BetaMemoryTool20250818StrReplaceCommand,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Storage root — outside Google Drive (same rationale as scheduler.py).
# Override via TEALC_MEMORY_DIR env var if needed.
_DEFAULT_MEMORY_DIR = os.path.expanduser(
    "~/Library/Application Support/tealc/memories"
)
STORAGE_ROOT: Path = Path(
    os.environ.get("TEALC_MEMORY_DIR", _DEFAULT_MEMORY_DIR)
)

# Size / safety caps
MAX_FILE_BYTES: int = 200 * 1024        # 200 KB
MAX_FILENAME_CHARS: int = 200
MAX_FILE_LINES: int = 50_000

# File/dir permission modes (owner-only, avoids world-readable in Docker, etc.)
_FILE_MODE = 0o600
_DIR_MODE = 0o700

# Line-number display width (matches SDK upstream width for 6-digit line numbers)
_LINE_NUMBER_WIDTH = 6

# Tool spec dict to pass directly to messages.create tools list
MEMORY_TOOL_SPEC: dict = {"type": "memory_20250818", "name": "memory"}

# Git tracking — git ops log lives next to the memories dir, in AppSupport
_GIT_LOG_PATH: Path = STORAGE_ROOT.parent / "memory-git.log"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_storage_root() -> None:
    """Create the storage root directory if it does not yet exist."""
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)


# ---------------------------------------------------------------------------
# Git auto-commit layer (F11)
# ---------------------------------------------------------------------------
# After every successful memory mutation, the change is committed to a Git
# repo rooted at STORAGE_ROOT. Unlocks `git log memories/projects/p_002/
# progress.md` for temporal queries — "what did we know about this project
# in January?", "what was the last state before I cleared it?", and so on.
#
# Best-effort: a failed git operation never raises (the file write already
# succeeded; git is opportunistic provenance). Failures are appended to
# `~/Library/Application Support/tealc/memory-git.log` for later forensics.
#
# The repo is auto-initialized on first mutation (idempotent — fast no-op
# if `.git` already exists). Local-only `user.name`/`user.email` are set
# so commits don't fall back to the Mac user's global git identity.

def _git(*args: str) -> tuple[int, str, str]:
    """Run `git <args>` in STORAGE_ROOT. Returns (rc, stdout, stderr).

    Captures output, applies a 10s timeout. Returns rc=-1 on timeout / any
    non-CalledProcessError exception, with stderr containing the message.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(STORAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except Exception as exc:
        return -1, "", str(exc)


def _log_git(msg: str) -> None:
    """Append a timestamped line to memory-git.log. Best-effort; never raises."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with open(_GIT_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _ensure_git_repo() -> bool:
    """Initialize STORAGE_ROOT as a Git repo if not already.

    Returns True if the repo is usable (already-init or freshly init), False
    if init failed. Idempotent and fast: ~50us per call once initialized.
    """
    if not STORAGE_ROOT.exists():
        return False
    if (STORAGE_ROOT / ".git").exists():
        return True
    rc, _, err = _git("init", "-q", "-b", "main")
    if rc != 0:
        _log_git(f"git init failed: {err}")
        return False
    # Repo-local identity (does not pollute global git config).
    _git("config", "user.name", "TEALC")
    _git("config", "user.email", "tealc@local")
    # Capture any pre-existing files as a baseline commit so subsequent
    # `git blame` / `git log` against them works from day one.
    _git("add", "-A")
    rc, _, err = _git(
        "commit", "-q", "--allow-empty", "-m", "init: TEALC memory git tracking"
    )
    if rc != 0:
        _log_git(f"initial commit failed: {err}")
    _log_git("git repo initialized at STORAGE_ROOT")
    return True


def _commit_memory_change(operation: str, path: str = "", summary: str = "") -> None:
    """Stage all changes and commit with a structured message. Best-effort."""
    try:
        if not _ensure_git_repo():
            return
        rc, _, err = _git("add", "-A")
        if rc != 0:
            _log_git(f"add failed ({operation} {path}): {err}")
            return
        msg = operation
        if path:
            msg += f" {path}"
        if summary:
            msg += f"\n\n{summary}"
        # --allow-empty so a no-op write (e.g. rewriting identical content)
        # still records the intent; cheaper than detecting whether content
        # actually changed and clearer in the log.
        rc, _, err = _git("commit", "-q", "--allow-empty", "-m", msg)
        if rc != 0:
            _log_git(f"commit failed ({msg!r}): {err}")
    except Exception as exc:
        _log_git(f"unexpected error in commit hook: {exc}")


def _validate_path(path: str) -> Path:
    """Validate a memory path and return the resolved, canonical filesystem path.

    Rules enforced:
    - Must start with ``/memories``
    - No URL-encoded traversal sequences (e.g. ``%2F``, ``%2e%2e``)
    - Resolved path must remain inside STORAGE_ROOT (catches ``../``, symlinks)
    - File-name component may not exceed MAX_FILENAME_CHARS characters
    """
    if not path.startswith("/memories"):
        raise ToolError(f"Path must start with /memories, got: {path!r}")

    # Reject URL-encoded traversal sequences before any further processing.
    decoded = unquote(path)
    if decoded != path:
        raise ToolError(
            f"Path contains URL-encoded characters which are not permitted: {path!r}"
        )
    if ".." in Path(path).parts:
        raise ToolError(
            f"Path traversal sequences ('..') are not permitted: {path!r}"
        )

    # Strip leading "/memories" to get the part relative to STORAGE_ROOT.
    relative = path[len("/memories"):].lstrip("/")

    # Check filename length for the leaf component.
    leaf = Path(relative).name if relative else ""
    if leaf and len(leaf) > MAX_FILENAME_CHARS:
        raise ToolError(
            f"Filename {leaf!r} exceeds the {MAX_FILENAME_CHARS}-character limit."
        )

    full = STORAGE_ROOT / relative if relative else STORAGE_ROOT

    # Resolve to catch symlink escapes; resolve() works even for non-existent
    # paths on Python 3.6+ when strict=False.
    resolved = full.resolve()
    resolved_root = STORAGE_ROOT.resolve()

    if resolved != resolved_root and not str(resolved).startswith(
        str(resolved_root) + os.sep
    ):
        raise ToolError(
            f"Path {path!r} would escape the /memories directory (possible path-traversal attempt)."
        )

    return resolved


def _format_size(n: int) -> str:
    """Return a human-readable file size string (B / K / M / G)."""
    if n == 0:
        return "0B"
    sizes = ["B", "K", "M", "G"]
    i = min(int(n.bit_length() - 1) // 10, len(sizes) - 1)
    v = n / (1024 ** i)
    return f"{int(v)}{sizes[i]}" if v == int(v) else f"{v:.1f}{sizes[i]}"


def _read_text(full_path: Path, memory_path: str) -> str:
    """Read UTF-8 text from *full_path*, raising ToolError on missing file."""
    try:
        return full_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ToolError(
            f"The file {memory_path} no longer exists "
            f"(may have been deleted or renamed concurrently)."
        ) from exc


def _atomic_write(target: Path, content: str) -> None:
    """Write *content* to *target* atomically via a temp file + os.replace."""
    data = content.encode("utf-8")
    tmp = target.parent / f".tmp-{os.getpid()}-{uuid.uuid4()}"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written == 0:
                    raise OSError("os.write returned 0")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _check_size(content: str, path: str) -> None:
    """Raise ToolError if *content* exceeds size caps."""
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError(
            f"File {path} would exceed the {MAX_FILE_BYTES // 1024} KB size limit."
        )
    lines = content.split("\n")
    if len(lines) > MAX_FILE_LINES:
        raise ToolError(
            f"File {path} would exceed the {MAX_FILE_LINES:,} line limit."
        )


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class TealcMemoryTool(BetaAbstractMemoryTool):
    """File-backed memory tool for Tealc.

    Stores all memory files under STORAGE_ROOT (``~/Library/Application
    Support/tealc/memories/`` by default).  The directory is created
    automatically on first use.

    This is a synchronous implementation.  The graph uses it via the SDK's
    synchronous ``run_tools`` / tool-dispatch path.
    """

    def __init__(self) -> None:
        super().__init__()
        _ensure_storage_root()

    # ------------------------------------------------------------------
    # view
    # ------------------------------------------------------------------
    def view(self, command: BetaMemoryTool20250818ViewCommand) -> str:
        full = _validate_path(command.path)

        if not full.exists():
            raise ToolError(
                f"The path {command.path} does not exist. Please provide a valid path."
            )

        if full.is_dir():
            # Directory listing — up to 2 levels deep, no hidden entries.
            items: List[tuple[str, str]] = []

            def _collect(dir_path: Path, rel: str, depth: int) -> None:
                if depth > 2:
                    return
                try:
                    entries = sorted(dir_path.iterdir(), key=lambda x: x.name)
                except Exception:
                    return
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    entry_rel = f"{rel}/{entry.name}" if rel else entry.name
                    try:
                        st = entry.stat()
                    except Exception:
                        continue
                    if entry.is_dir():
                        items.append((_format_size(st.st_size), f"{entry_rel}/"))
                        if depth < 2:
                            _collect(entry, entry_rel, depth + 1)
                    elif entry.is_file():
                        items.append((_format_size(st.st_size), entry_rel))

            _collect(full, "", 1)

            header = (
                f"Here're the files and directories up to 2 levels deep in "
                f"{command.path}, excluding hidden items:"
            )
            dir_size = _format_size(full.stat().st_size)
            lines = [f"{dir_size}\t{command.path}"]
            lines += [f"{sz}\t{command.path}/{p}" for sz, p in items]
            return f"{header}\n" + "\n".join(lines)

        elif full.is_file():
            content = _read_text(full, command.path)
            all_lines = content.split("\n")

            if len(all_lines) > MAX_FILE_LINES:
                raise ToolError(
                    f"File {command.path} exceeds the {MAX_FILE_LINES:,} line limit."
                )

            display = all_lines
            start_num = 1

            if command.view_range and len(command.view_range) == 2:
                s = max(1, command.view_range[0]) - 1
                e = len(all_lines) if command.view_range[1] == -1 else command.view_range[1]
                display = all_lines[s:e]
                start_num = s + 1

            numbered = [
                f"{str(i + start_num).rjust(_LINE_NUMBER_WIDTH)}\t{ln}"
                for i, ln in enumerate(display)
            ]
            return (
                f"Here's the content of {command.path} with line numbers:\n"
                + "\n".join(numbered)
            )
        else:
            raise ToolError(f"Unsupported file type for {command.path}")

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------
    def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        full = _validate_path(command.path)

        # Must be a file path, not the /memories root itself.
        if full == STORAGE_ROOT.resolve():
            raise ToolError("Cannot create a file at /memories itself.")

        _check_size(command.file_text, command.path)

        full.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

        try:
            fd = os.open(full, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
            try:
                data = command.file_text.encode("utf-8")
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written == 0:
                        raise OSError("os.write returned 0")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
        except FileExistsError as exc:
            raise ToolError(f"File {command.path} already exists") from exc

        _commit_memory_change("create", command.path)
        return f"File created successfully at: {command.path}"

    # ------------------------------------------------------------------
    # str_replace
    # ------------------------------------------------------------------
    def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        full = _validate_path(command.path)

        if not full.exists():
            raise ToolError(
                f"The path {command.path} does not exist. Please provide a valid path."
            )
        if not full.is_file():
            raise ToolError(f"The path {command.path} is not a file.")

        content = _read_text(full, command.path)
        count = content.count(command.old_str)

        if count == 0:
            raise ToolError(
                f"No replacement was performed, old_str `{command.old_str}` "
                f"did not appear verbatim in {command.path}."
            )
        if count > 1:
            lines_found: List[int] = []
            start = 0
            while True:
                pos = content.find(command.old_str, start)
                if pos == -1:
                    break
                lines_found.append(content[:pos].count("\n") + 1)
                start = pos + 1
            raise ToolError(
                f"No replacement was performed. Multiple occurrences of old_str "
                f"`{command.old_str}` in lines: {', '.join(map(str, lines_found))}. "
                f"Please ensure it is unique"
            )

        pos = content.find(command.old_str)
        changed_line_idx = content[:pos].count("\n")
        new_content = content.replace(command.old_str, command.new_str)
        _check_size(new_content, command.path)
        _atomic_write(full, new_content)

        new_lines = new_content.split("\n")
        ctx_start = max(0, changed_line_idx - 2)
        ctx_end = min(len(new_lines), changed_line_idx + 3)
        snippet = [
            f"{str(ln).rjust(_LINE_NUMBER_WIDTH)}\t{new_lines[ln - 1]}"
            for ln in range(ctx_start + 1, ctx_end + 1)
        ]
        _commit_memory_change(
            "str_replace",
            command.path,
            summary=f"replaced 1 occurrence near line {changed_line_idx + 1}",
        )
        return (
            "The memory file has been edited. "
            "Here is the snippet showing the change (with line numbers):\n"
            + "\n".join(snippet)
        )

    # ------------------------------------------------------------------
    # insert
    # ------------------------------------------------------------------
    def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        full = _validate_path(command.path)

        if not full.exists():
            raise ToolError(
                f"The path {command.path} does not exist. Please provide a valid path."
            )
        if not full.is_file():
            raise ToolError(f"The path {command.path} is not a file.")

        content = _read_text(full, command.path)
        lines = content.splitlines()

        if command.insert_line < 0 or command.insert_line > len(lines):
            raise ToolError(
                f"Invalid `insert_line` parameter: {command.insert_line}. "
                f"It should be within the range [0, {len(lines)}]."
            )

        lines.insert(command.insert_line, command.insert_text.rstrip("\n"))
        new_content = "\n".join(lines)
        if not new_content.endswith("\n"):
            new_content += "\n"

        _check_size(new_content, command.path)
        _atomic_write(full, new_content)
        _commit_memory_change(
            "insert",
            command.path,
            summary=f"inserted at line {command.insert_line}",
        )
        return f"The file {command.path} has been edited."

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------
    def delete(self, command: BetaMemoryTool20250818DeleteCommand) -> str:
        if command.path == "/memories":
            raise ToolError("Cannot delete the /memories directory itself")

        full = _validate_path(command.path)

        try:
            if full.is_file():
                full.unlink()
            elif full.is_dir():
                shutil.rmtree(full)
            else:
                raise ToolError(f"The path {command.path} does not exist")
        except FileNotFoundError as exc:
            raise ToolError(f"The path {command.path} does not exist") from exc

        _commit_memory_change("delete", command.path)
        return f"Successfully deleted {command.path}"

    # ------------------------------------------------------------------
    # rename
    # ------------------------------------------------------------------
    def rename(self, command: BetaMemoryTool20250818RenameCommand) -> str:
        old_full = _validate_path(command.old_path)
        new_full = _validate_path(command.new_path)

        if new_full.exists():
            raise ToolError(f"The destination {command.new_path} already exists")

        new_full.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

        try:
            old_full.rename(new_full)
        except FileNotFoundError as exc:
            raise ToolError(f"The path {command.old_path} does not exist") from exc

        _commit_memory_change(
            "rename", f"{command.old_path} -> {command.new_path}"
        )
        return f"Successfully renamed {command.old_path} to {command.new_path}"

    # ------------------------------------------------------------------
    # clear_all_memory
    # ------------------------------------------------------------------
    def clear_all_memory(self) -> str:
        """Remove all memory files but preserve git history.

        Pre-F11 behavior wiped the entire STORAGE_ROOT (including any future
        .git dir). Now we delete every entry EXCEPT .git, then commit the
        cleared state. The previous memory state remains recoverable via
        ``git checkout HEAD~1 -- .`` until manually pruned.
        """
        if STORAGE_ROOT.exists():
            for entry in STORAGE_ROOT.iterdir():
                if entry.name == ".git":
                    continue
                try:
                    if entry.is_file() or entry.is_symlink():
                        entry.unlink()
                    elif entry.is_dir():
                        shutil.rmtree(entry)
                except Exception:
                    # Best-effort: keep going on individual failures.
                    pass
        else:
            STORAGE_ROOT.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        _commit_memory_change(
            "clear_all_memory",
            summary="all memory files removed; previous state preserved in git history",
        )
        return "All memory cleared (git history preserved for recovery)"


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_memory_tool() -> TealcMemoryTool:
    """Return a configured TealcMemoryTool instance (creates storage dir if needed)."""
    return TealcMemoryTool()
