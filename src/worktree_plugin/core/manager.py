"""Thin wrapper around ``git worktree`` plus canonical id allocation.

W2 keeps this module strictly mechanical: ``subprocess`` calls to ``git`` and
the in-memory state store from ``state.py``. Setup-script execution (W5),
port allocation (W4), process lifecycle (W6) and full teardown semantics (W8)
will hook in around ``WorktreeManager`` later — the seams are documented at
``_teardown`` and ``create`` so future phases know where to inject.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .state import InMemoryStateStore, StateStore, WorktreeRecord

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_STORE_ROOT_ENV = "WORKTREE_STORE_ROOT"
_DEFAULT_STORE_DIR_NAME = "agent-worktree-store"
_GIT_TIMEOUT_ENV = "WORKTREE_GIT_TIMEOUT_SEC"
_GIT_TIMEOUT_DEFAULT = 30.0


class WorktreeError(RuntimeError):
    """Base class for ``WorktreeManager`` errors surfaced to MCP clients."""


class BranchNotFoundError(WorktreeError):
    pass


class DuplicateWorktreeError(WorktreeError):
    pass


class WorktreeNotFoundError(WorktreeError):
    pass


class GitCommandError(WorktreeError):
    def __init__(self, command: List[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git command failed (exit {returncode}): {' '.join(command)}\n{stderr.strip()}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class GitTimeoutError(WorktreeError):
    """Raised when a ``git`` subprocess exceeds the configured timeout.

    Ticket #19: the Windows PyInstaller binary was hanging because the spawned
    ``git`` inherited the MCP client's stdin pipe and waited forever for input.
    ``_run_git`` now closes stdin, runs via ``Popen.communicate(timeout=...)``,
    and raises this on overrun so the MCP tool can surface a real error rather
    than blocking the client forever.
    """

    def __init__(self, command: List[str], elapsed: float) -> None:
        super().__init__(
            f"git command timed out after {elapsed:.1f}s: {' '.join(command)}"
        )
        self.command = command
        self.elapsed = elapsed


@dataclass
class ManagerConfig:
    """Runtime configuration for ``WorktreeManager``.

    ``store_root`` is the directory under which per-repo worktree checkouts
    live (decision D2, Option B). Resolved from ``WORKTREE_STORE_ROOT`` if
    unset on construction, falling back to ``~/agent-worktree-store``.
    """

    store_root: Path

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "ManagerConfig":
        environ = env if env is not None else os.environ
        raw = environ.get(_DEFAULT_STORE_ROOT_ENV)
        if raw:
            root = Path(raw).expanduser().resolve()
        else:
            root = (Path.home() / _DEFAULT_STORE_DIR_NAME).resolve()
        return cls(store_root=root)


def _slug(value: str, *, max_len: int = 40) -> str:
    """Lower-case ASCII slug suitable for filesystem use and IDs."""

    s = _SLUG_RE.sub("-", value.lower()).strip("-")
    if not s:
        s = "x"
    return s[:max_len]


def _short_uuid() -> str:
    return uuid.uuid4().hex[:8]


def _resolve_git_timeout(explicit: Optional[float]) -> Optional[float]:
    """Resolve the timeout for a single ``_run_git`` call.

    Precedence: explicit kwarg > ``WORKTREE_GIT_TIMEOUT_SEC`` env > built-in
    default of 30.0 s. ``None`` (either as kwarg or env value ``""``) disables
    the timeout entirely; that path exists for diagnostics, not normal use.

    Env is read on every call so that test fixtures and operators can change
    the value without re-importing the module.
    """

    if explicit is not None:
        return explicit
    raw = os.environ.get(_GIT_TIMEOUT_ENV)
    if raw is None:
        return _GIT_TIMEOUT_DEFAULT
    raw = raw.strip()
    if not raw:
        # Empty string is "no timeout", matching the explicit-None semantics.
        return None
    try:
        value = float(raw)
    except ValueError:
        return _GIT_TIMEOUT_DEFAULT
    return value if value > 0 else None


def _run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    *,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` and return a ``CompletedProcess``.

    Ticket #19 hardenings:
    * ``stdin=DEVNULL`` so ``git`` can never inherit the MCP client's stdin
      pipe and wedge waiting on input -- this was the Windows-exe hang root
      cause.
    * Explicit ``stdout=PIPE, stderr=PIPE`` (rather than ``capture_output``)
      because we now drive a ``Popen`` directly to keep a clean kill path.
    * On Windows: ``creationflags=CREATE_NO_WINDOW`` so packaged-exe runs
      don't briefly flash a console window per git call.
    * ``timeout`` defaults from ``WORKTREE_GIT_TIMEOUT_SEC`` (30 s if unset);
      on overrun the process is killed and ``GitTimeoutError`` is raised.
    """

    effective_timeout = _resolve_git_timeout(timeout)

    popen_kwargs: dict = {
        "cwd": str(cwd) if cwd else None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if sys.platform == "win32":
        # Suppress the brief console-window flash when the packaged worktree.exe
        # spawns git from a GUI MCP host.
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    cmd = ["git", *args]
    start = time.monotonic()
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        # Drain the pipes after kill so the child fully reaps; ignore output.
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        elapsed = time.monotonic() - start
        raise GitTimeoutError(cmd, elapsed) from None

    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


class WorktreeManager:
    """High-level facade used by the FastMCP tools.

    Decision D1 (Option C): id = ``<repo-slug>-<branch-slug>-<short-uuid8>``.
    Decision D2 (Option B): worktree checkouts live under
    ``<store_root>/<repo-slug>/<id>/``.
    """

    def __init__(
        self,
        config: Optional[ManagerConfig] = None,
        state: Optional[StateStore] = None,
    ) -> None:
        self.config = config or ManagerConfig.from_env()
        self.state: StateStore = state or InMemoryStateStore()

    # ---- public API used by the FastMCP tools ----

    def create(
        self,
        repo_root: str,
        branch: str,
        base: Optional[str] = None,
    ) -> WorktreeRecord:
        repo_path = self._validate_repo(repo_root)

        branch = branch.strip()
        if not branch:
            raise WorktreeError("branch must be a non-empty string")

        repo_slug = _slug(repo_path.name)

        if self.state.find_by_branch(str(repo_path), branch) is not None:
            raise DuplicateWorktreeError(
                f"A worktree for branch '{branch}' already exists in {repo_path}"
            )

        branch_exists = self._branch_exists(repo_path, branch)
        if not branch_exists and base is None:
            raise BranchNotFoundError(
                f"Branch '{branch}' does not exist in {repo_path}. "
                "Pass `base` to create it."
            )
        if not branch_exists and base is not None and not self._branch_exists(
            repo_path, base
        ):
            raise BranchNotFoundError(
                f"Base branch '{base}' does not exist in {repo_path}."
            )

        worktree_id = f"{repo_slug}-{_slug(branch)}-{_short_uuid()}"
        target_path = self.config.store_root / repo_slug / worktree_id
        target_path.parent.mkdir(parents=True, exist_ok=True)

        git_args = ["worktree", "add"]
        if not branch_exists:
            git_args += ["-b", branch, str(target_path), base]  # type: ignore[list-item]
        else:
            git_args += [str(target_path), branch]

        proc = _run_git(git_args, cwd=repo_path)
        if proc.returncode != 0:
            raise GitCommandError(["git", *git_args], proc.returncode, proc.stderr)

        record = WorktreeRecord(
            id=worktree_id,
            repo_root=str(repo_path),
            branch=branch,
            path=str(target_path),
        )
        self.state.add(record)
        return record

    def list(self) -> List[WorktreeRecord]:
        return self.state.list()

    def remove(self, worktree_id: str, force: bool = False) -> WorktreeRecord:
        record = self.state.get(worktree_id)
        if record is None:
            raise WorktreeNotFoundError(
                f"No worktree tracked with id '{worktree_id}'"
            )
        self._teardown(record, force=force)
        removed = self.state.remove(worktree_id)
        assert removed is not None  # state.get returned record above
        return removed

    # ---- seams for later phases ----

    def _teardown(self, record: WorktreeRecord, *, force: bool) -> None:
        """Tear down a worktree.

        W8 will hook in here (process termination, contract ``teardown:``
        steps, port release). W2 only calls ``git worktree remove``.
        """

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(record.path)
        proc = _run_git(args, cwd=Path(record.repo_root))
        if proc.returncode != 0:
            raise GitCommandError(
                ["git", *args], proc.returncode, proc.stderr
            )

    # ---- helpers ----

    def _validate_repo(self, repo_root: str) -> Path:
        if not repo_root:
            raise WorktreeError("repo_root must be a non-empty path")
        path = Path(repo_root).expanduser().resolve()
        if not path.exists():
            raise WorktreeError(f"repo_root does not exist: {path}")
        proc = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
        if proc.returncode != 0:
            raise WorktreeError(f"Not a git repository: {path}")
        return Path(proc.stdout.strip()).resolve()

    def _branch_exists(self, repo_path: Path, branch: str) -> bool:
        proc = _run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_path,
        )
        return proc.returncode == 0


__all__ = (
    "BranchNotFoundError",
    "DuplicateWorktreeError",
    "GitCommandError",
    "GitTimeoutError",
    "ManagerConfig",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeNotFoundError",
)
