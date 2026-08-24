"""Integration tests for the W2 core tools.

These exercise real ``git worktree`` operations against a temporary repo, as
required by the planning comment's Verifikation section.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

import lib_python_worktree.core._git_utils as git_utils_module
from lib_python_worktree import (
    BranchAlreadyCheckedOutError,
    BranchNotFoundError,
    CheckoutTargetError,
    ContractError,
    DuplicateWorktreeError,
    GitCommandError,
    GitTimeoutError,
    InMemoryStateStore,
    KilledProcessInfo,
    ManagerConfig,
    ProcessAlreadyRunningError,
    ProcessLifecycleError,
    ProcessNotRunningError,
    SetupFailedError,
    WorktreeDirLockedError,
    WorktreeError,
    WorktreeManager,
    WorktreeNotFoundError,
    WorktreeRecord,
    WorktreeRemovalBlockedError,
)
from lib_python_worktree.core.manager import _run_git


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# ---- ticket #141 ambient-handle retry helper (MIRRORED) BEGIN ----
# Kept BYTE-IDENTICAL with the copy in the sibling test module. A shared
# conftest.py / helper module is deliberately not used (ticket #141 scope
# constraint); test_environment_tools.py carries the drift guard.

_AMBIENT_RETRY_ATTEMPTS = 5
_AMBIENT_RETRY_BASE_DELAY = 0.2

# Narrow text signatures for the generic `except WorktreeError` tail in
# tools/worktree.py, which can carry git's raw permission wording without a
# lock-typed __cause__. Deliberately excludes every deterministic refusal
# wording used by this suite ("not a git repository", "does not exist",
# "primary", "resolved to id", "not found").
_AMBIENT_TEXT_SIGNATURES = (
    "being used by another process",
    "access is denied",
    "permission denied",
    "unable to unlink",
    "failed to delete",
)


def _is_ambient_handle_error(exc: BaseException) -> bool:
    """True when `exc`, or anything in its cause/context chain, is the
    "something outside this test is holding the checkout" signal."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, WorktreeRemovalBlockedError):
            # Ticket #141 review fix: WorktreeRemovalBlockedError subclasses
            # WorktreeDirLockedError (a compound remove() failure: a
            # directory lock AND real uncommitted/untracked changes, both
            # at once). Retrying it as ambient would mask the
            # dirty-worktree half behind the AMBIENT-HANDLE banner, which
            # never mentions dirty_paths -- a compound block is a real,
            # non-transient condition, never ambient. This check must come
            # before the WorktreeDirLockedError check below.
            return False
        if isinstance(cur, WorktreeDirLockedError):
            return True
        if isinstance(cur, OSError) and getattr(cur, "winerror", None) in (5, 32):
            # ERROR_ACCESS_DENIED (5) / ERROR_SHARING_VIOLATION (32): the
            # actual foreign-handle signals. ERROR_DIR_NOT_EMPTY (145) is
            # deliberately excluded -- it can indicate a genuine
            # removal/cleanup regression (a leftover file the engine
            # failed to delete), not a lock. PermissionError is an OSError
            # subclass, so a winerror-carrying PermissionError is caught
            # here too; a bare PermissionError with no winerror (POSIX
            # EACCES, or a genuine ACL failure) is NOT ambient -- this
            # pre-flight is win32-only.
            return True
        if not isinstance(cur, OSError):
            # The text fallback deliberately does not apply to OSError
            # (PermissionError included): its winerror check above is
            # already authoritative for that family, so the text path must
            # not second-guess it by matching e.g. "permission denied" in
            # a POSIX PermissionError's own str(). This fallback exists
            # solely for the tool layer's generic `except WorktreeError`
            # tail, which can carry git's raw wording inside a
            # ValueError/WorktreeError with no lock-typed __cause__.
            text = str(cur).lower()
            if any(sig in text for sig in _AMBIENT_TEXT_SIGNATURES):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def _remove_with_ambient_retry(
    op,
    *,
    what: str,
    attempts: int = _AMBIENT_RETRY_ATTEMPTS,
    base_delay: float = _AMBIENT_RETRY_BASE_DELAY,
):
    """Run `op()` (a zero-arg callable performing a real-git removal),
    retrying only while the failure looks like a foreign handle holder.

    Contract:
      * returns `op()`'s value unchanged on the first success;
      * re-raises any NON-ambient exception immediately and unchanged, on
        the very first attempt (so ordinary product failures keep their
        original type, message and traceback);
      * on an ambient-looking failure, sleeps `base_delay * attempt`
        (linear backoff) and retries, up to `attempts` total calls;
      * when the budget is exhausted, calls `pytest.fail` with the
        `AMBIENT-HANDLE (ticket #141)` banner naming the diagnosis AND the
        original engine message. It never skips: a persistent holder may
        equally be a genuine engine-level lock regression, and that must
        stay visible (ticket #141, Q1).

    Never call this inside a `pytest.raises(...)` block, and never wrap a
    deterministic refusal/lock-mapping test with it.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return op()
        except Exception as exc:  # noqa: BLE001 -- re-raised unless ambient
            if not _is_ambient_handle_error(exc):
                raise
            last = exc
            if attempt < attempts:
                time.sleep(base_delay * attempt)
    total = base_delay * (attempts - 1) * attempts / 2
    pytest.fail(
        f"AMBIENT-HANDLE (ticket #141): {what} stayed blocked by a foreign "
        f"handle across {attempts} attempts (~{total:.1f}s of retries). "
        f"Diagnosis: a process outside this test (indexer / AV / editor / "
        f"agent) is holding a handle inside the checkout -- OR the engine "
        f"has a real lock regression, which is why this fails loudly "
        f"instead of skipping. Original engine error: "
        f"{type(last).__name__}: {last}"
    )
# ---- ticket #141 ambient-handle retry helper (MIRRORED) END ----


@pytest.fixture
def temp_repo(tmp_path: Path) -> Iterator[Path]:
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/alpha", cwd=repo)
    yield repo


@pytest.fixture
def manager(tmp_path: Path) -> WorktreeManager:
    store_root = tmp_path / "store"
    return WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=InMemoryStateStore(),
    )


def test_create_list_remove_roundtrip(manager: WorktreeManager, temp_repo: Path):
    rec = manager.create(str(temp_repo), "feature/alpha")
    assert rec.id.startswith("src-repo-feature-alpha-")
    assert rec.branch == "feature/alpha"
    assert Path(rec.path).exists()
    assert Path(rec.path).is_dir()

    listed = manager.list()
    assert len(listed) == 1
    assert listed[0].id == rec.id

    removed = _remove_with_ambient_retry(
        lambda: manager.remove(rec.id), what=f"manager.remove({rec.id!r})"
    )
    assert removed.id == rec.id
    assert not Path(rec.path).exists()
    assert manager.list() == []


# ---- Ticket #114: v0.3.3 default-base semantics for create() ----
#
# v0.3.2 always raised BranchNotFoundError when `branch` did not exist and
# `base` was omitted. v0.3.3 (see WorktreeManager.create()'s docstring and
# manager._current_branch()) changed that: an omitted `base` now defaults to
# the branch currently checked out at the main clone, and only still raises
# when that HEAD is detached or unborn (no commits yet) -- the two cases
# where no sensible default branch exists. The three tests below replace the
# old single "always raises" test with coverage of both the new success path
# and the two still-raising conditions.


def test_create_unknown_branch_without_base_defaults_to_checked_out_branch(
    manager: WorktreeManager, temp_repo: Path
):
    """v0.3.3: an unknown branch with `base` omitted no longer raises -- it
    defaults to the branch currently checked out at the main clone (`main`,
    in `temp_repo`) and the new worktree is created from that tip.

    Advances `main` past the commit `feature/alpha` was branched from so the
    two SHAs provably diverge, proving the new worktree is based on main's
    *current* tip rather than merely some commit shared by every branch in
    the fixture.
    """
    (temp_repo / "README.md").write_text("hello again\n", encoding="utf-8")
    _git("add", "-A", cwd=temp_repo)
    _git("commit", "-q", "-m", "second commit on main", cwd=temp_repo)

    main_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    alpha_sha = subprocess.run(
        ["git", "rev-parse", "feature/alpha"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert main_sha != alpha_sha  # sanity: the fixture's branches now diverge

    rec = manager.create(str(temp_repo), "feature/does-not-exist")

    assert rec.branch == "feature/does-not-exist"
    assert Path(rec.path).exists()
    wt_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=rec.path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert wt_sha == main_sha, (
        "New worktree's HEAD must match main's tip (the defaulted base), "
        "not feature/alpha's older commit"
    )


def test_create_unknown_branch_without_base_raises_when_head_detached(
    manager: WorktreeManager, temp_repo: Path
):
    """Still raises BranchNotFoundError when the main clone's HEAD is
    detached -- there is no "currently checked out branch" to default to."""
    _git("checkout", "--detach", "HEAD", cwd=temp_repo)

    with pytest.raises(BranchNotFoundError):
        manager.create(str(temp_repo), "feature/does-not-exist")


def test_create_unknown_branch_without_base_raises_when_head_unborn(
    manager: WorktreeManager, tmp_path: Path
):
    """Still raises BranchNotFoundError when the main clone's HEAD is
    unborn (freshly `git init`ed, no commits yet) -- there is no branch
    checked out to default to. Uses a bespoke repo rather than the
    `temp_repo` fixture, which commits immediately on setup."""
    repo = tmp_path / "unborn-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)

    with pytest.raises(BranchNotFoundError):
        manager.create(str(repo), "feature/does-not-exist")


def test_create_unknown_branch_with_base(
    manager: WorktreeManager, temp_repo: Path
):
    # Pass fetch=False because the temp repo has no origin remote.
    # The fetch behaviour (v0.1.7+) is exercised separately by the library's
    # own suite; here we only want to verify the tool-layer plumbing.
    rec = manager.create(str(temp_repo), "feature/new", base="main", fetch=False)
    assert rec.branch == "feature/new"
    assert Path(rec.path).exists()
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=rec.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "feature/new"


def test_duplicate_create_same_branch_fails(
    manager: WorktreeManager, temp_repo: Path
):
    manager.create(str(temp_repo), "feature/alpha")
    with pytest.raises(DuplicateWorktreeError):
        manager.create(str(temp_repo), "feature/alpha")


def test_remove_unknown_id_fails(manager: WorktreeManager):
    with pytest.raises(WorktreeNotFoundError):
        manager.remove("nope-nope-12345678")


def test_tool_remove_unknown_id_returns_soft_error(tmp_path: Path):
    """Tool layer: worktree_remove with an unknown id must return a soft-error
    dict ({"error": "..."}) rather than raising an exception."""
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    store_root = tmp_path / "store"
    mgr = WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)

    unknown_id = "definitely-unknown-id-99999"
    fn = mcp._tool_manager._tools["worktree_remove"].fn

    result = fn(environment_id=unknown_id)

    assert isinstance(result, dict), "Expected a dict, not an exception"
    assert "error" in result, f"Expected 'error' key in result, got: {result}"
    assert unknown_id in result["error"], (
        f"Expected unknown_id '{unknown_id}' in error message, got: {result['error']}"
    )


def test_store_root_from_env(tmp_path: Path, monkeypatch):
    target = tmp_path / "custom-store"
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(target))
    cfg = ManagerConfig.from_env()
    assert cfg.store_root == target.resolve()


def test_store_root_default(monkeypatch):
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    cfg = ManagerConfig.from_env()
    assert cfg.store_root.name == "agent-worktree-store"
    assert cfg.store_root.is_absolute()


def test_worktree_paths_under_store_root(
    manager: WorktreeManager, temp_repo: Path, tmp_path: Path
):
    rec = manager.create(str(temp_repo), "feature/alpha")
    # store_root / repo_slug / id
    assert Path(rec.path).parent.parent == (tmp_path / "store").resolve()
    assert Path(rec.path).parent.name == "src-repo"


# ---- Ticket #19: _run_git timeout + stdin handling ----


def test_run_git_smoke_version_completes_quickly():
    """Sanity check: ``git --version`` finishes well under 1 s with the new
    Popen-based plumbing. Catches pipe/handle plumbing regressions on every
    platform (Linux, Windows, packaged exe).
    """

    import time as _time

    start = _time.monotonic()
    proc = _run_git(["--version"])
    elapsed = _time.monotonic() - start
    assert proc.returncode == 0
    assert proc.stdout.startswith("git version")
    assert elapsed < 1.0, f"_run_git(['--version']) took {elapsed:.2f}s"


def test_run_git_raises_timeout_when_subprocess_hangs(monkeypatch):
    """Simulate a hanging git via a fake Popen, confirm GitTimeoutError fires
    and the process gets killed (rather than the call blocking forever).
    """

    killed = {"value": False}

    class _HangingPopen:
        def __init__(self, *args, **kwargs):
            self.returncode = None

        def communicate(self, timeout=None):
            # Always pretend the child is still running.
            raise subprocess.TimeoutExpired(cmd=["git", "hang"], timeout=timeout)

        def kill(self):
            killed["value"] = True
            self.returncode = -9

    # `_run_git` was extracted into `core/_git_utils.py` (lib-python-worktree
    # v0.3.10, ticket #159 bump); `subprocess.Popen` there resolves against
    # `_git_utils`'s own module-level `subprocess` import, not `manager`'s
    # (which no longer imports `subprocess` at all -- it only re-exports the
    # `_run_git` function object itself). Patch it where it is actually used.
    monkeypatch.setattr(git_utils_module.subprocess, "Popen", _HangingPopen)

    with pytest.raises(GitTimeoutError) as excinfo:
        _run_git(["status"], timeout=0.05)

    assert killed["value"] is True
    assert excinfo.value.command == ["git", "status"]
    assert excinfo.value.elapsed >= 0.0


def test_run_git_timeout_respects_env_override(monkeypatch):
    """``WORKTREE_GIT_TIMEOUT_SEC`` overrides the built-in 30 s default
    when no explicit timeout kwarg is passed.
    """

    captured = {"timeout": None}

    class _CapturingPopen:
        def __init__(self, *args, **kwargs):
            self.returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return ("", "")

        def kill(self):  # pragma: no cover - not reached in this test
            pass

    monkeypatch.setenv("WORKTREE_GIT_TIMEOUT_SEC", "7.5")
    # See test_run_git_raises_timeout_when_subprocess_hangs above for why
    # this patches `_git_utils.subprocess`, not `manager.subprocess`.
    monkeypatch.setattr(git_utils_module.subprocess, "Popen", _CapturingPopen)

    _run_git(["--version"])
    assert captured["timeout"] == 7.5


def test_run_git_closes_stdin(monkeypatch):
    """Regression guard: ``stdin=DEVNULL`` must always be passed so the spawned
    git can never inherit the MCP client's stdin pipe (the Windows hang root
    cause).
    """

    captured_kwargs: dict = {}

    class _RecordingPopen:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):  # pragma: no cover - not reached in this test
            pass

    # See test_run_git_raises_timeout_when_subprocess_hangs above for why
    # this patches `_git_utils.subprocess`, not `manager.subprocess`.
    monkeypatch.setattr(git_utils_module.subprocess, "Popen", _RecordingPopen)
    _run_git(["--version"])
    assert captured_kwargs.get("stdin") is subprocess.DEVNULL


# ---- Ticket #18: structured error for "branch already checked out elsewhere" ----


def test_create_branch_already_checked_out_elsewhere(
    manager: WorktreeManager, temp_repo: Path, tmp_path: Path
):
    """Creating a worktree for a branch that is already checked out in
    another worktree (tracked by a different state store, so the in-memory
    duplicate-check shortcut at manager.py:133 doesn't fire) must surface as
    a structured ``BranchAlreadyCheckedOutError`` with branch + path attrs.
    """

    # First state store creates worktree A for feature/alpha.
    first = manager.create(str(temp_repo), "feature/alpha")
    assert Path(first.path).exists()

    # Fresh manager + fresh state store simulates a second client session
    # that doesn't know about worktree A yet -- now the duplicate-check at
    # manager.py:133 falls through and we reach the actual `git worktree add`.
    other = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store2"),
        state=InMemoryStateStore(),
    )

    with pytest.raises(BranchAlreadyCheckedOutError) as excinfo:
        other.create(str(temp_repo), "feature/alpha")

    err = excinfo.value
    assert err.branch == "feature/alpha"
    assert Path(err.path).resolve() == Path(first.path).resolve()
    # Existing dir -> not prunable.
    assert err.prunable is False
    # Message contract matches the format used by tools/worktree.py callers.
    msg = str(err)
    assert "branch_already_checked_out" in msg
    assert "'feature/alpha'" in msg
    assert "git worktree prune" in msg


def test_already_checked_out_reports_prunable_after_dir_removed(
    manager: WorktreeManager, temp_repo: Path, tmp_path: Path
):
    """If the worktree directory is gone but git still has the registration,
    the structured error must report ``prunable is True`` so the caller can
    suggest ``git worktree prune``.
    """

    import shutil

    first = manager.create(str(temp_repo), "feature/alpha")
    # Wipe the worktree dir behind git's back so its registration goes stale.
    shutil.rmtree(first.path)

    other = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store2"),
        state=InMemoryStateStore(),
    )

    with pytest.raises(BranchAlreadyCheckedOutError) as excinfo:
        other.create(str(temp_repo), "feature/alpha")

    err = excinfo.value
    assert err.branch == "feature/alpha"
    assert err.prunable is True
    assert "prunable=True" in str(err)


# ---- Ticket #25: tool-surface clarity ----


def _make_tool_fixtures(tmp_path: Path):
    """Return (mgr, fn_map) for tool-layer tests."""
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    store_root = tmp_path / "store"
    mgr = WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fns = {name: t.fn for name, t in mcp._tool_manager._tools.items()}
    return mgr, fns


def test_create_reroot_emits_warning(tmp_path: Path):
    """worktree_create must emit a 'warning' key when repo_root is re-rooted
    (e.g. a subdirectory of the repo is passed instead of the repo root)."""
    # Create a real repo so git rev-parse works.
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # feature/wt is not the currently-checked-out branch, so git allows
    # creating a worktree for it.
    _git("branch", "feature/wt", cwd=repo)

    # Create a subdirectory inside the repo.
    subdir = repo / "subdir"
    subdir.mkdir()

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(subdir), branch="feature/wt")

    assert "warning" in result, f"Expected 'warning' key, got: {result}"
    assert str(subdir) in result["warning"] or "subdir" in result["warning"], (
        f"Expected original subdir path in warning, got: {result['warning']}"
    )
    assert str(repo.resolve()) in result["warning"] or result["warning"].endswith(
        str(repo.resolve())
    ), f"Expected resolved repo root in warning, got: {result['warning']}"


def test_create_no_reroot_warning_when_paths_match(tmp_path: Path):
    """worktree_create must NOT emit a 'warning' key when the passed repo_root
    is already the actual git repository root."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # feature/wt is not the currently-checked-out branch, so git allows
    # creating a worktree for it.
    _git("branch", "feature/wt", cwd=repo)

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")

    assert "warning" not in result, (
        f"Unexpected 'warning' key in result: {result.get('warning')}"
    )


# ---- Ticket #6: worktree_start and worktree_stop MCP tools ----


def _make_running_record(worktree_id: str = "wt-id") -> WorktreeRecord:
    """Return a minimal WorktreeRecord with status='running' and a pid."""
    return WorktreeRecord(
        id=worktree_id,
        repo_root="/r",
        branch="b",
        path="/p",
        status="running",
        pids={"main": 12345},
    )


def _make_stopped_record(worktree_id: str = "wt-id", killed_pids=None) -> WorktreeRecord:
    """Return a minimal WorktreeRecord with status='stopped' and no pids."""
    if killed_pids is None:
        killed_pids = []
    return WorktreeRecord(
        id=worktree_id,
        repo_root="/r",
        branch="b",
        path="/p",
        status="stopped",
        pids={},
        killed_pids=killed_pids,
    )


def test_environment_start_stop_tools_registered(tmp_path: Path):
    """Both environment_start and environment_stop must be registered as MCP tools."""
    mgr, fns = _make_tool_fixtures(tmp_path)
    assert "environment_start" in fns, "environment_start not registered"
    assert "environment_stop" in fns, "environment_stop not registered"


def test_tool_environment_start_returns_record(tmp_path: Path):
    """Happy path: environment_start returns a dict with status='running' and pids set."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    result = fns["environment_start"](environment_id="wt-id")

    assert isinstance(result, dict)
    assert "error" not in result
    assert result["status"] == "running"
    assert result["pids"] == {"main": 12345}
    mgr.start.assert_called_once_with(
        "wt-id", checkout_path=None, role="main", env=None, cwd=None, variant="default"
    )


def test_tool_environment_start_unknown_id_returns_soft_error(tmp_path: Path):
    """environment_start with an unknown id must return a soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))

    result = fns["environment_start"](environment_id="wt-missing")

    assert isinstance(result, dict)
    assert "error" in result
    assert "wt-missing" in result["error"]


def test_tool_environment_start_empty_string_id_not_absent(tmp_path: Path):
    """Regression test: environment_id="" is a present-but-empty identifier,
    not an absent one -- the not-found error must name it (empty string),
    not silently fall back to checkout_path (which is None here)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(side_effect=WorktreeNotFoundError(""))

    result = fns["environment_start"](environment_id="")

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "environment '' not found"


def test_tool_environment_start_already_running_returns_soft_error(tmp_path: Path):
    """environment_start when already running must return soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(
        side_effect=ProcessAlreadyRunningError("wt-id", "main", 99)
    )

    result = fns["environment_start"](environment_id="wt-id")

    assert isinstance(result, dict)
    assert "error" in result
    # Must not raise; soft error only.


def test_tool_environment_start_engine_error_raises_valueerror(tmp_path: Path):
    """environment_start on a generic ProcessLifecycleError must raise ValueError."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(side_effect=ProcessLifecycleError("engine failure"))

    with pytest.raises(ValueError):
        fns["environment_start"](environment_id="wt-id")


def test_tool_environment_stop_returns_record(tmp_path: Path):
    """Happy path: environment_stop returns a dict with status='stopped'."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    result = fns["environment_stop"](environment_id="wt-id")

    assert isinstance(result, dict)
    assert "error" not in result
    assert result["status"] == "stopped"
    assert result["pids"] == {}
    # role=None is forwarded unchanged (ticket #118's role=None sentinel) --
    # the engine itself defaults an omitted role to "main"; the wrapper no
    # longer hardcodes "main" here so it can distinguish "role omitted" from
    # "role explicitly main" when variant is also given.
    mgr.stop.assert_called_once_with(
        "wt-id",
        checkout_path=None,
        role=None,
        variant=None,
        timeout=10.0,
        kill_orphans=False,
    )


def test_tool_environment_stop_unknown_id_returns_soft_error(tmp_path: Path):
    """environment_stop with an unknown id must return a soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))

    result = fns["environment_stop"](environment_id="wt-missing")

    assert isinstance(result, dict)
    assert "error" in result
    assert "wt-missing" in result["error"]


def test_tool_environment_stop_empty_string_id_not_absent(tmp_path: Path):
    """Regression test: environment_id="" is a present-but-empty identifier,
    not an absent one -- the not-found error must name it (empty string),
    not silently fall back to checkout_path (which is None here)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(side_effect=WorktreeNotFoundError(""))

    result = fns["environment_stop"](environment_id="")

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "environment '' not found"


def test_tool_environment_stop_not_running_returns_soft_error(tmp_path: Path):
    """environment_stop when no process is running must return soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(
        side_effect=ProcessNotRunningError("wt-id", "main")
    )

    result = fns["environment_stop"](environment_id="wt-id")

    assert isinstance(result, dict)
    assert "error" in result
    # Must not raise; soft error only.


def test_tool_environment_stop_engine_error_raises_valueerror(tmp_path: Path):
    """environment_stop on a generic ProcessLifecycleError must raise ValueError."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(side_effect=ProcessLifecycleError("engine failure"))

    with pytest.raises(ValueError):
        fns["environment_stop"](environment_id="wt-id")


def test_tool_environment_start_custom_role_and_cwd_forwarded(tmp_path: Path):
    """environment_start must forward custom role and cwd to manager.start (no cmd)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = WorktreeRecord(
        id="wt-id", repo_root="/r", branch="b", path="/p",
        status="running", pids={"worker": 42},
    )
    mgr.start = MagicMock(return_value=record)

    fns["environment_start"](
        environment_id="wt-id",
        role="worker",
        cwd="/custom/cwd",
    )

    call_args = mgr.start.call_args
    # Only the id as positional; checkout_path/role/cwd/variant as kwargs; no cmd anywhere.
    assert call_args.args == ("wt-id",)
    assert call_args.kwargs == {
        "checkout_path": None,
        "role": "worker",
        "env": None,
        "cwd": "/custom/cwd",
        "variant": "default",
    }
    # Confirm no command list was passed.
    all_args = list(call_args.args) + list(call_args.kwargs.values())
    assert not any(isinstance(a, list) for a in all_args), (
        "No command list should be forwarded to manager.start"
    )


def test_tool_environment_start_no_start_configured_raises_valueerror(tmp_path: Path):
    """environment_start raises ValueError when the contract has no start: command.

    This is the regression test covering the config-error path that replaces
    the old caller-supplied-cmd path. The lib raises WorktreeError when the
    contract's start: field is missing or ambiguous; the tool must surface it
    as a ValueError so MCP reports a hard error.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(
        side_effect=WorktreeError(
            "no start: command configured in contract for worktree 'wt-id'"
        )
    )

    with pytest.raises(ValueError, match="no start: command configured"):
        fns["environment_start"](environment_id="wt-id")


def test_tool_environment_stop_custom_role_and_timeout_forwarded(tmp_path: Path):
    """environment_stop must forward custom role and timeout to manager.stop."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    fns["environment_stop"](environment_id="wt-id", role="worker", timeout=5.0)

    mgr.stop.assert_called_once_with(
        "wt-id",
        checkout_path=None,
        role="worker",
        variant=None,
        timeout=5.0,
        kill_orphans=False,
    )


# ---- Ticket #51: environment_start variant + env, environment_stop kill_orphans ----


def test_tool_environment_start_variant_forwarded(tmp_path: Path):
    """environment_start must forward variant='unity-gui' to manager.start."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    fns["environment_start"](environment_id="wt-id", variant="unity-gui")

    call_args = mgr.start.call_args
    assert call_args.kwargs["variant"] == "unity-gui"


def test_tool_environment_start_env_forwarded(tmp_path: Path):
    """environment_start must forward env dict to manager.start."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    fns["environment_start"](environment_id="wt-id", env={"K": "v"})

    call_args = mgr.start.call_args
    assert call_args.kwargs["env"] == {"K": "v"}


def test_tool_environment_start_default_forwards_variant_and_env_explicitly(tmp_path: Path):
    """Default environment_start call must pass variant='default' and env=None explicitly."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    fns["environment_start"](environment_id="wt-id")

    call_args = mgr.start.call_args
    assert call_args.kwargs["variant"] == "default"
    assert call_args.kwargs["env"] is None


def test_tool_environment_start_unknown_variant_raises_valueerror(tmp_path: Path):
    """environment_start raises ValueError when manager raises WorktreeError for unknown variant."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(
        side_effect=WorktreeError("no start: step named 'bogus' found ...")
    )

    with pytest.raises(ValueError, match="no start: step named 'bogus'"):
        fns["environment_start"](environment_id="wt-id", variant="bogus")


def test_tool_environment_stop_kill_orphans_forwarded(tmp_path: Path):
    """environment_stop with kill_orphans=True must forward that flag to manager.stop."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    fns["environment_stop"](environment_id="wt-id", kill_orphans=True)

    call_args = mgr.stop.call_args
    assert call_args.kwargs["kill_orphans"] is True


def test_tool_environment_stop_default_forwards_kill_orphans_false(tmp_path: Path):
    """Default environment_stop call must pass kill_orphans=False explicitly."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    fns["environment_stop"](environment_id="wt-id")

    call_args = mgr.stop.call_args
    assert call_args.kwargs["kill_orphans"] is False


# ---- Ticket #153: killed_pids[].cmdline must be agent-readable ----


def test_tool_environment_stop_killed_pids_cmdline_is_decoded_and_raw_preserved(
    tmp_path: Path,
):
    """Ticket #153: environment_stop's killed_pids[].cmdline must surface the
    decoded PowerShell script text (not the opaque base64 -EncodedCommand
    blob), with the original raw argv still recoverable via cmdline_raw.

    Uses the real engine KilledProcessInfo dataclass -- its __post_init__ (as
    of lib-python-worktree v0.3.9, ticket #132) performs the decode when
    constructed with a -EncodedCommand-shaped argv, exactly as the engine's
    own process-kill code paths construct it. No wrapper-side decoding
    exists; asdict() picks the resulting cmdline/cmdline_raw fields up
    automatically."""
    from unittest.mock import MagicMock

    script = "Write-Host 'hello #153'"
    encoded_payload = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    raw_argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded_payload,
    ]

    mgr, fns = _make_tool_fixtures(tmp_path)
    killed = [KilledProcessInfo(pid=4321, name="powershell.exe", cmdline=list(raw_argv))]
    record = _make_stopped_record(killed_pids=killed)
    mgr.stop = MagicMock(return_value=record)

    result = fns["environment_stop"](environment_id="wt-id")

    assert "error" not in result
    assert "killed_pids" in result
    entry = result["killed_pids"][0]
    assert "hello #153" in entry["cmdline"][-1], (
        f"expected decoded script text in cmdline, got {entry['cmdline']!r}"
    )
    assert encoded_payload not in entry["cmdline"], (
        "raw base64 -EncodedCommand payload must not survive in cmdline"
    )
    assert entry.get("cmdline_raw") == raw_argv, (
        "cmdline_raw must round-trip the original, undecoded argv"
    )


# ---- Ticket #44: worktree_remove kill_blocking_processes parameter ----


def _make_removed_record(worktree_id: str = "wt-id", killed_pids=None) -> WorktreeRecord:
    """Return a minimal WorktreeRecord as returned by manager.remove."""
    if killed_pids is None:
        killed_pids = []
    return WorktreeRecord(
        id=worktree_id,
        repo_root="/r",
        branch="b",
        path="/p",
        status="removed",
        pids={},
        killed_pids=killed_pids,
    )


def test_tool_worktree_remove_kill_blocking_processes_forwarded(tmp_path: Path):
    """worktree_remove with kill_blocking_processes=True must forward that flag
    to manager.remove."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_removed_record()
    mgr.remove = MagicMock(return_value=record)

    fns["worktree_remove"](environment_id="wt-id", kill_blocking_processes=True)

    mgr.remove.assert_called_once_with(
        "wt-id", force=False, kill_blocking_processes=True, checkout_path=None
    )


def test_tool_worktree_remove_default_kill_false_forwarded(tmp_path: Path):
    """worktree_remove without kill_blocking_processes must forward False to
    manager.remove (the default must not silently drop the kwarg)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_removed_record()
    mgr.remove = MagicMock(return_value=record)

    fns["worktree_remove"](environment_id="wt-id")

    mgr.remove.assert_called_once_with(
        "wt-id", force=False, kill_blocking_processes=False, checkout_path=None
    )


def test_tool_worktree_remove_killed_pids_in_response(tmp_path: Path):
    """When manager.remove returns a record with killed_pids, the response
    dict must include a non-empty killed_pids list with correct fields."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    killed = [KilledProcessInfo(pid=1234, name="devenv.exe", cmdline=["devenv.exe", "/x"])]
    record = _make_removed_record(killed_pids=killed)
    mgr.remove = MagicMock(return_value=record)

    result = fns["worktree_remove"](environment_id="wt-id", kill_blocking_processes=True)

    assert isinstance(result, dict)
    assert "error" not in result
    assert "killed_pids" in result
    assert len(result["killed_pids"]) == 1
    entry = result["killed_pids"][0]
    assert entry["pid"] == 1234
    assert entry["name"] == "devenv.exe"
    assert isinstance(entry["cmdline"], list)


def test_tool_worktree_remove_default_empty_killed_pids(tmp_path: Path):
    """When no processes were killed, killed_pids must be present and equal []
    (not absent, not None)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_removed_record(killed_pids=[])
    mgr.remove = MagicMock(return_value=record)

    result = fns["worktree_remove"](environment_id="wt-id")

    assert isinstance(result, dict)
    assert "error" not in result
    assert "killed_pids" in result
    assert result["killed_pids"] == []


def test_tool_worktree_remove_killed_pids_cmdline_is_decoded_and_raw_preserved(
    tmp_path: Path,
):
    """Ticket #153: worktree_remove's killed_pids[].cmdline must surface the
    decoded PowerShell script text (not the opaque base64 -EncodedCommand
    blob), with the original raw argv still recoverable via cmdline_raw.

    Uses the real engine KilledProcessInfo dataclass -- its __post_init__ (as
    of lib-python-worktree v0.3.9, ticket #132) performs the decode when
    constructed with a -EncodedCommand-shaped argv, exactly as the engine's
    own process-kill code paths construct it. No wrapper-side decoding
    exists; asdict() picks the resulting cmdline/cmdline_raw fields up
    automatically."""
    from unittest.mock import MagicMock

    script = "Write-Host 'hello #153'"
    encoded_payload = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    raw_argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded_payload,
    ]

    mgr, fns = _make_tool_fixtures(tmp_path)
    killed = [KilledProcessInfo(pid=4321, name="powershell.exe", cmdline=list(raw_argv))]
    record = _make_removed_record(killed_pids=killed)
    mgr.remove = MagicMock(return_value=record)

    result = fns["worktree_remove"](environment_id="wt-id", kill_blocking_processes=True)

    assert "error" not in result
    assert "killed_pids" in result
    entry = result["killed_pids"][0]
    assert "hello #153" in entry["cmdline"][-1], (
        f"expected decoded script text in cmdline, got {entry['cmdline']!r}"
    )
    assert encoded_payload not in entry["cmdline"], (
        "raw base64 -EncodedCommand payload must not survive in cmdline"
    )
    assert entry.get("cmdline_raw") == raw_argv, (
        "cmdline_raw must round-trip the original, undecoded argv"
    )


def test_tool_worktree_remove_blocked_by_both_conditions_names_both_flags(
    tmp_path: Path,
):
    """Ticket #120: when manager.remove raises WorktreeRemovalBlockedError
    (BOTH a directory lock AND uncommitted changes are blocking removal),
    the tool must surface both conditions and both required flags in a
    single ValueError -- not silently fall through the existing
    WorktreeDirLockedError clause (WorktreeRemovalBlockedError subclasses
    it), which would swallow the uncommitted-changes half of the picture.

    Filesystem paths must NOT leak into the message -- the engine
    deliberately keeps ``dirty_paths`` out of the human-readable text."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = WorktreeRemovalBlockedError(
        worktree_id="x", killed=[], kill_attempted=False, dirty_paths=["notes.txt"]
    )
    mgr.remove = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id="x")

    msg = str(excinfo.value)
    assert str(exc) in msg
    assert "blocked_by:" in msg
    assert "dir_locked" in msg
    assert "uncommitted_changes" in msg
    assert "required_flags:" in msg
    assert "kill_blocking_processes=True" in msg
    assert "force=True" in msg
    assert "notes.txt" not in msg


def test_tool_worktree_remove_blocked_after_kill_attempt_still_names_both_flags(
    tmp_path: Path,
):
    """Same compound-blocking condition, but reached via the
    kill_attempted=True message branch (kill_blocking_processes=True was
    passed, processes were killed, and the directory is STILL locked AND
    the worktree is still dirty). Both required flags must still be
    named."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = WorktreeRemovalBlockedError(
        worktree_id="x",
        killed=[KilledProcessInfo(pid=1234, name="devenv.exe", cmdline=[])],
        kill_attempted=True,
        dirty_paths=["notes.txt"],
    )
    mgr.remove = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id="x", kill_blocking_processes=True)

    msg = str(excinfo.value)
    assert str(exc) in msg
    assert "blocked_by:" in msg
    assert "dir_locked" in msg
    assert "uncommitted_changes" in msg
    assert "required_flags:" in msg
    assert "kill_blocking_processes=True" in msg
    assert "force=True" in msg
    assert "notes.txt" not in msg


def test_tool_worktree_remove_dir_locked_raises_valueerror(tmp_path: Path):
    """When manager.remove raises WorktreeDirLockedError (directory still
    locked after kill attempt), the tool must raise ValueError."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(
        side_effect=WorktreeDirLockedError("wt-id", killed=[])
    )

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id="wt-id", kill_blocking_processes=True)

    # Regression guard (ticket #120): the single-condition case must NOT be
    # captured by the new compound-blocking branch, so it must not carry
    # the compound-only tokens.
    msg = str(excinfo.value)
    assert "blocked_by:" not in msg
    assert "required_flags:" not in msg


def test_worktree_remove_docstring_documents_compound_blocking_contract(
    tmp_path: Path,
):
    """Ticket #120: the docstring must document the one-shot compound
    reporting contract, naming the blocked_by/required_flags tokens
    callers can branch on."""
    mgr, fns = _make_tool_fixtures(tmp_path)
    doc = fns["worktree_remove"].__doc__ or ""

    for token in ("blocked_by", "required_flags"):
        assert token in doc, f"worktree_remove docstring missing {token!r}"


def test_tool_worktree_remove_unknown_checkout_target_reason_defensive_text(
    tmp_path: Path,
):
    """Ticket #119: an unknown/future CheckoutTargetError.reason (not
    "missing" or "id_mismatch") must fall through to a generic,
    wrapper-native addressing message -- never `str(exc)`, which would leak
    the engine's internal `worktree_id` wording straight through."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(
        side_effect=CheckoutTargetError(
            worktree_id="x", checkout_path="y", reason="future_reason"
        )
    )

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id="x", checkout_path="y")

    msg = str(excinfo.value)
    assert "worktree_remove" in msg
    assert "worktree_id" not in msg


def test_tool_worktree_remove_empty_string_id_not_absent(tmp_path: Path):
    """Regression test: environment_id="" is a present-but-empty identifier,
    not an absent one -- the not-found error must name it (empty string),
    not silently fall back to checkout_path (which is None here)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(side_effect=WorktreeNotFoundError(""))

    result = fns["worktree_remove"](environment_id="")

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "environment '' not found"


def test_tool_worktree_remove_not_found_still_soft_error(tmp_path: Path):
    """Adding kill_blocking_processes must not break the existing soft-error
    path: WorktreeNotFoundError must still return {"error": ...} dict."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))

    result = fns["worktree_remove"](
        environment_id="wt-missing", kill_blocking_processes=True
    )

    assert isinstance(result, dict)
    assert "error" in result


# ---- Ticket #48: worktree_remove teardown-before-remove ----


def test_tool_worktree_remove_teardown_before_remove_wrapper_contract(tmp_path: Path):
    """Regression test for #48: worktree_remove must return the full record dict
    produced by _record_to_dict (all fields present, no 'error' key) after the
    v0.0.8 bump.

    This asserts the return-value shape — fields id, status, branch, repo_root,
    path, pids, killed_pids — which the existing
    test_tool_worktree_remove_default_kill_false_forwarded does NOT check.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_removed_record(worktree_id="wt-48")
    mgr.remove = MagicMock(return_value=record)

    result = fns["worktree_remove"](environment_id="wt-48")

    # Must return a plain dict without an 'error' key.
    assert isinstance(result, dict)
    assert "error" not in result
    # All fields from _record_to_dict(record) must be present with correct values.
    assert result["id"] == "wt-48"
    assert result["status"] == "removed"
    assert result["branch"] == "b"
    assert result["repo_root"] == "/r"
    assert result["path"] == "/p"
    assert result["pids"] == {}
    assert result["killed_pids"] == []


def test_tool_worktree_remove_teardown_before_remove_force_forwarded(tmp_path: Path):
    """Regression test for #48: force=True must be forwarded to manager.remove,
    ensuring root-owned file cleanup (teardown) runs before the forced git
    worktree removal in v0.0.8.

    This is the only test that exercises the force=True path end-to-end;
    it asserts both the call contract AND the return-value (id, status).
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_removed_record(worktree_id="wt-48-force")
    mgr.remove = MagicMock(return_value=record)

    result = fns["worktree_remove"](environment_id="wt-48-force", force=True)

    # Call contract: force=True forwarded correctly.
    mgr.remove.assert_called_once_with(
        "wt-48-force", force=True, kill_blocking_processes=False, checkout_path=None
    )
    # Return-value contract: must be the removed record, not a soft-error.
    assert isinstance(result, dict)
    assert "error" not in result
    assert result["id"] == "wt-48-force"
    assert result["status"] == "removed"


def test_tool_worktree_remove_teardown_before_remove_not_found_soft_error(tmp_path: Path):
    """Regression test for #48: the soft-error path must still return
    {"error": ...} after the v0.0.8 bump (no regression from teardown change)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(side_effect=WorktreeNotFoundError("wt-48-missing"))

    result = fns["worktree_remove"](environment_id="wt-48-missing")

    assert isinstance(result, dict)
    assert "error" in result
    assert "wt-48-missing" in result["error"]


# ---- Ticket #59: untracked contract provisioning ----


def test_create_copies_contract_dir_when_untracked(tmp_path: Path):
    """When .seretos/ exists in repo_root but is absent from the new worktree
    (e.g. excluded via .git/info/exclude), worktree_create must copy it
    into the worktree path so setup can find the contract."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    # Place .seretos/ in the repo root but do NOT git-add it (untracked).
    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")

    assert "error" not in result
    wt_contract = Path(result["path"]) / ".seretos" / "worktree-setup.yml"
    assert wt_contract.exists(), (
        f".seretos/worktree-setup.yml not found in worktree at {result['path']}"
    )


def test_create_does_not_overwrite_existing_contract_dir(tmp_path: Path):
    """When .seretos/ already exists in the worktree (tracked), worktree_create
    must not attempt a second copy (idempotency guard)."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    # Track .seretos/ so git copies it into the worktree automatically.
    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")

    assert "error" not in result
    wt_contract = Path(result["path"]) / ".seretos" / "worktree-setup.yml"
    assert wt_contract.exists(), "Tracked .seretos/ must still be present after create"


# ---- Ticket #110: freshly created worktrees remove without force ----


def test_create_then_remove_without_force_succeeds_with_untracked_contract_dir(
    tmp_path: Path,
):
    """A freshly created worktree whose .seretos/ was copied in (untracked)
    must be removable with worktree_remove's default force=False.

    Before the fix, the untracked .seretos/ copy made git consider the
    worktree dirty, so plain removal raised DirtyWorktreeError -> ValueError
    and force=True was practically mandatory (ticket #110, Befund 2)."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    # Place .seretos/ in the repo root but do NOT git-add it (untracked).
    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )

    mgr, fns = _make_tool_fixtures(tmp_path)
    create_result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in create_result

    remove_result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=create_result["id"]),
        what="worktree_remove(untracked .seretos worktree)",
    )

    assert "error" not in remove_result, (
        f"Expected plain removal (force=False) to succeed, got: {remove_result}"
    )
    assert remove_result["status"] == "removed"
    assert not Path(create_result["path"]).exists()


def test_create_leaves_worktree_git_clean(tmp_path: Path):
    """After copying an untracked .seretos/ into the new worktree, `git
    status --porcelain` run inside the worktree must report nothing -- the
    copy must be invisible to git, mechanism-agnostic of how that is
    achieved."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in result

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=result["path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == "", (
        f"Expected clean git status in new worktree, got: {status.stdout!r}"
    )
    assert ".seretos" not in status.stdout


def test_create_does_not_modify_repo_root_or_source_contract_dir(tmp_path: Path):
    """The fix must be fully contained inside the new worktree: it must not
    write a .gitignore into the source .seretos/ under repo_root, and it
    must not write to the shared common git dir's info/exclude."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in result

    assert not (repo / ".seretos" / ".gitignore").exists(), (
        "Fix must not write a .gitignore into repo_root's source .seretos/"
    )
    exclude_path = repo / ".git" / "info" / "exclude"
    if exclude_path.exists():
        assert ".seretos" not in exclude_path.read_text(encoding="utf-8")


def test_create_preserves_existing_gitignore_in_copied_contract_dir(tmp_path: Path):
    """If the source .seretos/ already ships its own .gitignore, the fix must
    append its self-ignoring rule rather than clobbering the existing
    content, and the resulting worktree must still be git-clean."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )
    (seretos / ".gitignore").write_text("foo\n", encoding="utf-8")

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in result

    wt_gitignore = Path(result["path"]) / ".seretos" / ".gitignore"
    content = wt_gitignore.read_text(encoding="utf-8")
    assert "foo" in content, "Existing .gitignore content must be preserved"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=result["path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_create_tracked_contract_dir_gets_no_injected_gitignore(tmp_path: Path):
    """When .seretos/ is git-tracked (git already copied it into the new
    worktree), worktree_create must skip the copy entirely and must not
    inject a .gitignore -- but the worktree is still git-clean and still
    removable without force, since git already tracks the directory."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    mgr, fns = _make_tool_fixtures(tmp_path)
    create_result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in create_result

    wt_gitignore = Path(create_result["path"]) / ".seretos" / ".gitignore"
    assert not wt_gitignore.exists(), (
        "Tracked .seretos/ must not receive an injected .gitignore"
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=create_result["path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""

    remove_result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=create_result["id"]),
        what="worktree_remove(tracked .seretos worktree)",
    )
    assert "error" not in remove_result
    assert remove_result["status"] == "removed"


def test_ensure_contract_copy_ignored_is_idempotent(tmp_path: Path):
    """Calling the helper twice on the same directory must not duplicate the
    self-ignoring rule.

    The idempotency signal is the helper's own marker comment (not "any bare
    '*' line" -- see test_create_preserves_gitignore_with_star_then_negation
    for why that detection would be wrong), so this asserts the marker
    appears exactly once after two calls."""
    from worktree_plugin.tools.worktree import _ensure_contract_copy_ignored

    target = tmp_path / "contract-dir"
    target.mkdir()

    _ensure_contract_copy_ignored(target)
    _ensure_contract_copy_ignored(target)

    content = (target / ".gitignore").read_text(encoding="utf-8")
    marker = "# Ticket #110: keep this create-time copy out of git status."
    marker_count = content.count(marker)
    assert marker_count == 1, (
        f"Expected the idempotency marker exactly once after two calls, got "
        f"{marker_count} occurrences in: {content!r}"
    )
    star_lines = [line for line in content.splitlines() if line.strip() == "*"]
    assert len(star_lines) == 1, (
        f"Expected exactly one '*' line after two calls, got: {content!r}"
    )


def test_create_preserves_gitignore_with_star_then_negation(tmp_path: Path):
    """Regression for the order-sensitivity bug in the marker-detection fix:
    gitignore semantics mean the *last* matching pattern wins, so a source
    .seretos/.gitignore containing a bare '*' followed by a later negation
    (e.g. '!worktree-setup.yml') does NOT actually ignore everything -- the
    negated file stays visible to git. Detecting "already self-ignoring"
    from the presence of any bare '*' line (the pre-fix behaviour) would
    wrongly skip appending an overriding '*' at the end, leaving the copied
    .seretos/ only partially ignored and the worktree dirty. The fix must
    always append its own trailing '*' (using its own marker comment, not a
    bare-'*' scan, to detect idempotency) so it wins regardless of what the
    pre-existing file contains."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )
    (seretos / ".gitignore").write_text("*\n!worktree-setup.yml\n", encoding="utf-8")

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in result

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=result["path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == "", (
        f"Expected clean git status even with a source .gitignore containing "
        f"'*' followed by a negation, got: {status.stdout!r}"
    )

    remove_result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=result["id"]),
        what="worktree_remove(star-then-negation gitignore worktree)",
    )
    assert "error" not in remove_result, (
        f"Expected plain removal (force=False) to succeed, got: {remove_result}"
    )
    assert remove_result["status"] == "removed"


def test_worktree_create_docstring_documents_base_default(tmp_path: Path):
    """worktree_create's docstring -- the MCP tool description an agent
    reads -- must document that omitting `base` for a not-yet-existing
    branch defaults to whatever branch is currently checked out at
    repo_root, and that a detached/unborn HEAD still raises (ticket #110,
    Befund 1; the default behaviour itself shipped in #114's v0.3.3 bump)."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    doc = fns["worktree_create"].__doc__ or ""
    doc_lower = doc.lower()
    assert "currently checked out" in doc_lower or "currently checked-out" in doc_lower, (
        "worktree_create docstring must document the default-to-checked-out-"
        "branch behaviour when base is omitted"
    )
    assert "detached" in doc_lower and "unborn" in doc_lower, (
        "worktree_create docstring must document that a detached or unborn "
        "HEAD still raises even with the base default"
    )


def test_create_preserves_non_utf8_existing_gitignore_bytes(tmp_path: Path):
    """If the source .seretos/.gitignore is not valid UTF-8 (e.g. cp1252),
    worktree_create must still succeed (no unwrapped UnicodeDecodeError),
    the new worktree must be git-clean, worktree_remove must succeed with
    force left at its default False, and -- crucially -- the copied
    .gitignore's original bytes must be preserved unchanged. A whole-file
    rewrite through a tolerant/lossy decode would silently corrupt those
    bytes; only a true append (never reading the pre-existing bytes back out
    through decode+encode) guarantees this."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )
    non_utf8_bytes = b"caf\xe9\n"  # cp1252 for "café\n"; invalid UTF-8
    (seretos / ".gitignore").write_bytes(non_utf8_bytes)

    mgr, fns = _make_tool_fixtures(tmp_path)
    create_result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in create_result

    wt_gitignore = Path(create_result["path"]) / ".seretos" / ".gitignore"
    copied_bytes = wt_gitignore.read_bytes()
    assert copied_bytes.startswith(non_utf8_bytes), (
        "Original non-UTF-8 bytes of the source .gitignore must be preserved "
        f"unchanged at the start of the copy, got: {copied_bytes!r}"
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=create_result["path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == "", (
        f"Expected clean git status with a non-UTF-8 source .gitignore, got: "
        f"{status.stdout!r}"
    )

    remove_result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=create_result["id"]),
        what="worktree_remove(non-utf8 gitignore worktree)",
    )
    assert "error" not in remove_result, (
        f"Expected plain removal (force=False) to succeed, got: {remove_result}"
    )
    assert remove_result["status"] == "removed"


def test_create_appends_marker_on_gitignore_without_trailing_newline(tmp_path: Path):
    """Reviewer nit: when the pre-existing .gitignore has no trailing
    newline, the appended self-ignoring marker block must still land on its
    own line rather than being glued onto the last existing line, and the
    resulting worktree must still be git-clean."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )
    (seretos / ".gitignore").write_text("foo", encoding="utf-8")  # no trailing newline

    mgr, fns = _make_tool_fixtures(tmp_path)
    result = fns["worktree_create"](repo_root=str(repo), branch="feature/wt")
    assert "error" not in result

    wt_gitignore = Path(result["path"]) / ".seretos" / ".gitignore"
    content = wt_gitignore.read_text(encoding="utf-8")
    marker = "# Ticket #110: keep this create-time copy out of git status."
    lines = content.splitlines()
    assert "foo" in lines, (
        f"Expected 'foo' to remain on its own line, got: {content!r}"
    )
    assert any(line == marker for line in lines), (
        f"Expected marker header on its own line, got: {content!r}"
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=result["path"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


# ---- Ticket #84: UX polish (path style, id-instability visibility) ----


def test_worktree_create_contract_copy_error_uses_forward_slashes(
    tmp_path: Path, monkeypatch
):
    """When copying an untracked .seretos/ into the new worktree fails, the
    raised error message must render the source contract-dir path with
    forward slashes -- consistent with the forward-slash-normalized paths in
    success responses -- instead of leaking native Windows backslashes.

    NOTE: on POSIX, pathlib already renders forward slashes for plain string
    interpolation, so this assertion holds even pre-fix there; its RED state
    is Windows-conditional. That is expected and acceptable.
    """
    import worktree_plugin.tools.worktree as worktree_tools_module

    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/wt", cwd=repo)

    # Place .seretos/ in the repo root but do NOT git-add it (untracked), so
    # worktree_create takes the copytree path.
    seretos = repo / ".seretos"
    seretos.mkdir()
    (seretos / "worktree-setup.yml").write_text(
        "version: 1\nisolation: none\n", encoding="utf-8"
    )

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(worktree_tools_module.shutil, "copytree", _boom)

    mgr, fns = _make_tool_fixtures(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        fns["worktree_create"](repo_root=str(repo), branch="feature/wt")

    message = str(excinfo.value)
    assert "contract directory" in message
    assert "\\" not in message, (
        f"Expected forward-slash-only error message, got: {message!r}"
    )


def test_id_instability_caution_prominent_in_docstrings(tmp_path: Path):
    """The 'id is not stable across remove/re-create cycles' caveat must be
    surfaced as a prominent standalone CAUTION callout in worktree_create's
    docstring, pointing callers at environment_list to re-fetch the current
    id -- not at the removed worktree_get tool."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    doc = fns["worktree_create"].__doc__ or ""
    assert "CAUTION:" in doc, "worktree_create docstring missing prominent CAUTION callout"
    assert "not stable" in doc.lower(), (
        "worktree_create docstring missing 'not stable' caveat text"
    )
    assert "environment_list" in doc, (
        "worktree_create docstring should direct callers to re-fetch the "
        "current id via environment_list"
    )
    assert "worktree_get" not in doc, (
        "worktree_create docstring must no longer reference the removed "
        "worktree_get tool"
    )


def test_worktree_remove_docstring_distinguishes_tracked_from_foreign_blockers(
    tmp_path: Path,
):
    """Claim under protection (ticket #130, re-slicing #126): the
    kill_blocking_processes flag is for *foreign* holders (an editor, a
    shell whose cwd is in the checkout, a build tool, a reparented orphan),
    not for a process this tool itself started via environment_start --
    removal stops every tracked role first, before any FS delete, so a
    tracked process is normally already gone. The tracked stop is
    best-effort, so a tracked process that refuses to die still blocks."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    doc = fns["worktree_remove"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    assert "foreign" in norm
    assert "tracked" in norm
    assert "environment_start" in norm
    assert re.search(r"(stops|terminates)[^.]{0,160}tracked[^.]{0,160}(first|before)", norm), (
        "worktree_remove docstring must state that removal stops tracked "
        "processes first/before the blocking-process scan"
    )
    assert "best-effort" in norm or "best effort" in norm

    # Guard: this note belongs on worktree_remove's kill_blocking_processes
    # parameter, not on environment_stop's docstring (a wording slip the
    # ticket flagged and the plan explicitly declined to fix there).
    stop_doc = fns["environment_stop"].__doc__ or ""
    assert "kill_blocking_processes" not in stop_doc


# ---- Ticket #60: env passthrough and variant selection verification ----


def _write_contract(path: Path, content: str) -> None:
    """Write content to .seretos/worktree-setup.yml under path."""
    seretos = path / ".seretos"
    seretos.mkdir(parents=True, exist_ok=True)
    (seretos / "worktree-setup.yml").write_text(content, encoding="utf-8")


def test_tool_environment_start_env_vars_reach_child(tmp_path: Path):
    """Verify that _lifecycle_start receives WORKTREE_* env vars built from
    the WorktreeRecord (id, path, and port slots) when worktree_start is called.

    Patch target: lib_python_worktree.core.manager._lifecycle_start
    (the function imported into manager.py as the actual process-spawn call).
    The patch intercepts the call after WorktreeManager.start has called
    _build_worktree_env(record, caller_env) so we can inspect the full env.
    """
    from unittest.mock import MagicMock, patch

    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    # Prepare a fake worktree path under tmp_path that has a contract file.
    wt_path = tmp_path / "store" / "repo" / "wt-env-test-12345678"
    wt_path.mkdir(parents=True)
    # Repo root dir with a minimal contract having a single unnamed start step.
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    _write_contract(
        repo_root,
        "version: 1\nisolation: partial\nstart:\n  - run: start.sh\n",
    )

    worktree_id = "wt-env-test-12345678"
    record = WorktreeRecord(
        id=worktree_id,
        repo_root=str(repo_root),
        branch="feature/env-test",
        path=str(wt_path),
        status="created",
        ports={"web": 8080, "db": 5432},
    )

    state = InMemoryStateStore()
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn

    captured: dict = {}

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd, variant=None):
        captured["env"] = env
        # Return the record with status updated to "running" so the tool succeeds.
        record.status = "running"
        record.pids = {role: 99999}
        return record

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        fn(environment_id=worktree_id)

    assert "env" in captured, "_lifecycle_start was not called"
    env = captured["env"]
    assert env.get("WORKTREE_ID") == worktree_id, (
        f"Expected WORKTREE_ID=={worktree_id!r}, got {env.get('WORKTREE_ID')!r}"
    )
    assert env.get("WORKTREE_PATH") == str(wt_path), (
        f"Expected WORKTREE_PATH=={str(wt_path)!r}, got {env.get('WORKTREE_PATH')!r}"
    )
    assert env.get("WORKTREE_PORT_WEB") == "8080", (
        f"Expected WORKTREE_PORT_WEB=='8080', got {env.get('WORKTREE_PORT_WEB')!r}"
    )
    assert env.get("WORKTREE_PORT_DB") == "5432", (
        f"Expected WORKTREE_PORT_DB=='5432', got {env.get('WORKTREE_PORT_DB')!r}"
    )


def test_tool_environment_start_variant_selects_correct_step(
    tmp_path: Path,
):
    """Verify that passing variant='worker' to worktree_start causes _lifecycle_start
    to receive a cmd that references start-worker.sh and not start-web.sh.

    Patch target: lib_python_worktree.core.manager._lifecycle_start
    """
    from unittest.mock import patch

    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    wt_path = tmp_path / "store" / "repo" / "wt-variant-test-12345678"
    wt_path.mkdir(parents=True)
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    _write_contract(
        repo_root,
        (
            "version: 1\n"
            "isolation: partial\n"
            "start:\n"
            "  - name: web\n"
            "    run: start-web.sh\n"
            "  - name: worker\n"
            "    run: start-worker.sh\n"
        ),
    )

    worktree_id = "wt-variant-test-12345678"
    record = WorktreeRecord(
        id=worktree_id,
        repo_root=str(repo_root),
        branch="feature/variant-test",
        path=str(wt_path),
        status="created",
    )

    state = InMemoryStateStore()
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn

    captured: dict = {}

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd, variant=None):
        captured["cmd"] = cmd
        record.status = "running"
        record.pids = {role: 99999}
        return record

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        fn(environment_id=worktree_id, variant="worker")

    assert "cmd" in captured, "_lifecycle_start was not called"
    # ticket #137: do NOT pin sys.platform here (as ticket #129 did). Doing
    # so forces core/manager.py's env=_build_worktree_env(record, env) --
    # an *argument expression*, evaluated before the patched
    # _lifecycle_start is ever entered -- down _get_user_profile_env's
    # win32 branch, which does `import winreg` at
    # lib_python_worktree/core/_env_utils.py:55. winreg is a Windows-only
    # stdlib module, so pinning sys.platform to "win32" on the
    # ubuntu-22.04 CI leg makes this test itself raise
    # ModuleNotFoundError -- the #129 fix for a Linux regression was
    # itself a Linux regression. Branch the assertion on the *ambient*
    # sys.platform instead so each leg exercises (and strictly checks)
    # its own real argv shape.
    cmd = captured["cmd"]
    if sys.platform == "win32":
        # ticket #109 (upstream lib-python-worktree): the default win32
        # shell (powershell.exe) transports the run line as a base64
        # -EncodedCommand blob rather than a raw -Command <text>
        # argument, so decode it before checking which script was
        # selected.
        assert len(cmd) == 5, f"Expected a 5-element argv, got: {cmd!r}"
        assert cmd[:4] == [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
        ], f"Expected powershell -EncodedCommand prefix, got: {cmd!r}"
        decoded_run_line = base64.b64decode(cmd[4]).decode("utf-16-le")
    else:
        assert cmd == ["bash", "-c", "start-worker.sh"], (
            f"Expected ['bash', '-c', 'start-worker.sh'], got: {cmd!r}"
        )
        decoded_run_line = cmd[2]
    assert "start-worker.sh" in decoded_run_line, (
        f"Expected 'start-worker.sh' in decoded cmd, got: {cmd!r}"
    )
    assert "start-web.sh" not in decoded_run_line, (
        f"Expected 'start-web.sh' NOT in decoded cmd when variant='worker', got: {cmd!r}"
    )


# ---- Ticket #93: default cwd falls back to the worktree path ----


def test_tool_environment_start_default_cwd_falls_back_to_worktree_path(tmp_path: Path):
    """Omitting ``cwd`` must not silently forward ``cwd=None`` to the OS spawn
    call (which makes the child inherit the host's directory instead of the
    worktree on Windows). The engine is responsible for defaulting ``cwd`` to
    the worktree path before it ever reaches ``_spawn_detached``.

    Patch target: lib_python_worktree.core.process_lifecycle._spawn_detached
    (the true spawn seam), so the test proves the *engine's* default applies
    rather than something the wrapper itself papers over.
    """
    from unittest.mock import patch

    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    wt_path = tmp_path / "store" / "repo" / "wt-cwd-test-12345678"
    wt_path.mkdir(parents=True)
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    _write_contract(
        repo_root,
        "version: 1\nisolation: partial\nstart:\n  - run: start.sh\n",
    )

    worktree_id = "wt-cwd-test-12345678"
    record = WorktreeRecord(
        id=worktree_id,
        repo_root=str(repo_root),
        branch="feature/cwd-test",
        path=str(wt_path),
        status="created",
    )

    state = InMemoryStateStore()
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn

    captured: dict = {}

    class _FakeProc:
        """Minimal Popen-alike: process_lifecycle.start() calls .wait() on
        the return value to detect an early exit, then reads .pid/.returncode.
        Raising TimeoutExpired simulates a still-running process.
        """

        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    def _fake_spawn(cmd, *, env=None, cwd=None, **_ignored):
        captured["cwd"] = cwd
        return _FakeProc(4242)

    with patch(
        "lib_python_worktree.core.process_lifecycle._spawn_detached",
        side_effect=_fake_spawn,
    ):
        fn(environment_id=worktree_id)

    assert captured["cwd"] == str(wt_path), (
        f"Expected omitted cwd to default to the worktree path {str(wt_path)!r}, "
        f"got {captured.get('cwd')!r}"
    )

    # Regression: an explicit cwd must still reach _spawn_detached unchanged.
    explicit_dir = tmp_path / "explicit-cwd"
    explicit_dir.mkdir()

    with patch(
        "lib_python_worktree.core.process_lifecycle._spawn_detached",
        side_effect=_fake_spawn,
    ):
        fn(environment_id=worktree_id, role="secondary", cwd=str(explicit_dir))

    assert captured["cwd"] == str(explicit_dir), (
        f"Expected explicit cwd {str(explicit_dir)!r} to pass through unchanged, "
        f"got {captured.get('cwd')!r}"
    )


def test_tool_environment_start_surfaces_start_log_path(tmp_path: Path):
    """The engine's per-role ``start_log_paths`` mapping (ticket #145,
    upstream lib-python-worktree#119) must flow through to the tool's
    response dict, both as the raw mapping and as a scalar ``start_log_path``
    derived for the role this call requested, so callers can inspect it when
    a process exits immediately.
    """
    from unittest.mock import patch

    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    wt_path = tmp_path / "store" / "repo" / "wt-log-test-12345678"
    wt_path.mkdir(parents=True)
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()

    worktree_id = "wt-log-test-12345678"
    record = WorktreeRecord(
        id=worktree_id,
        repo_root=str(repo_root),
        branch="feature/log-test",
        path=str(wt_path),
        status="running",
        pids={"main": 4242},
        start_log_paths={"main": "/logs/start-main.log"},
    )

    state = InMemoryStateStore()
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn

    with patch.object(mgr, "start", return_value=record):
        result = fn(environment_id=worktree_id)

    assert result.get("start_log_path") == "/logs/start-main.log", (
        f"Expected start_log_path to flow through to the response dict, "
        f"got {result.get('start_log_path')!r}"
    )
    assert result.get("start_log_paths") == {"main": "/logs/start-main.log"}, (
        f"Expected the raw start_log_paths mapping to flow through unchanged "
        f"via asdict(), got {result.get('start_log_paths')!r}"
    )


def test_tool_environment_start_start_log_path_is_scoped_to_requested_role(
    tmp_path: Path,
):
    """A multi-role record must not bleed another role's log path into the
    scalar ``start_log_path`` -- this is the exact cross-role bleed upstream
    lib-python-worktree#119 fixed by keying ``start_log_paths`` per role.
    ``environment_start(role="ui")`` must return ``ui``'s path, never
    ``main``'s.
    """
    from unittest.mock import patch

    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    wt_path = tmp_path / "store" / "repo" / "wt-log-test-multirole"
    wt_path.mkdir(parents=True)
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()

    worktree_id = "wt-log-test-multirole"
    record = WorktreeRecord(
        id=worktree_id,
        repo_root=str(repo_root),
        branch="feature/log-test",
        path=str(wt_path),
        status="running",
        pids={"main": 4242, "ui": 4243},
        start_log_paths={
            "main": "/logs/start-main.log",
            "ui": "/logs/start-ui.log",
        },
    )

    state = InMemoryStateStore()
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn

    with patch.object(mgr, "start", return_value=record):
        result = fn(environment_id=worktree_id, role="ui")

    assert result.get("start_log_path") == "/logs/start-ui.log", (
        f"Expected the scalar start_log_path to be scoped to the requested "
        f"role ('ui'), not another role's entry, got "
        f"{result.get('start_log_path')!r}"
    )


def test_tool_environment_start_start_log_path_edge_cases(tmp_path: Path):
    """Edge cases pinning the ``.get(role)`` derivation semantics against
    upstream's invariants: a no-op ``"ready"`` start records no key at all
    (never a ``None`` value) for a role, and ``stop()`` deliberately does not
    pop ``start_log_paths[role]`` so a role can outlive its pid entry.
    """
    from unittest.mock import patch

    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    mcp = FastMCP("test")

    def _make_record(worktree_id: str, wt_path: Path, repo_root: Path, **kwargs):
        wt_path.mkdir(parents=True)
        repo_root.mkdir()
        return WorktreeRecord(
            id=worktree_id,
            repo_root=str(repo_root),
            branch="feature/log-test",
            path=str(wt_path),
            **kwargs,
        )

    # Case 1: empty mapping (no-op "ready" start, nothing spawned) -> scalar
    # is None, no KeyError.
    worktree_id = "wt-log-test-empty"
    record = _make_record(
        worktree_id,
        tmp_path / "store" / "repo" / worktree_id,
        tmp_path / "repo-root-empty",
        status="ready",
        pids={},
        start_log_paths={},
    )
    state = InMemoryStateStore()
    state.add(record)
    mgr = WorktreeManager(config=ManagerConfig(store_root=tmp_path / "store"), state=state)
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn
    with patch.object(mgr, "start", return_value=record):
        result = fn(environment_id=worktree_id)
    assert result.get("start_log_path") is None
    assert result.get("start_log_paths") == {}

    # Case 2: role retained past stop() -- present in start_log_paths but
    # absent from pids -- scalar still surfaces, no exception.
    worktree_id2 = "wt-log-test-stopped"
    record2 = _make_record(
        worktree_id2,
        tmp_path / "store" / "repo" / worktree_id2,
        tmp_path / "repo-root-stopped",
        status="stopped",
        pids={},
        start_log_paths={"main": "/logs/start-main.log"},
    )
    state2 = InMemoryStateStore()
    state2.add(record2)
    mgr2 = WorktreeManager(config=ManagerConfig(store_root=tmp_path / "store"), state=state2)
    mcp2 = FastMCP("test2")
    register(mcp2, mgr2)
    fn2 = mcp2._tool_manager._tools["environment_start"].fn
    with patch.object(mgr2, "start", return_value=record2):
        result2 = fn2(environment_id=worktree_id2)
    assert result2.get("start_log_path") == "/logs/start-main.log"

    # Case 3: role present in pids but absent from start_log_paths -> scalar
    # is None, no exception.
    worktree_id3 = "wt-log-test-nolog"
    record3 = _make_record(
        worktree_id3,
        tmp_path / "store" / "repo" / worktree_id3,
        tmp_path / "repo-root-nolog",
        status="running",
        pids={"main": 4242},
        start_log_paths={},
    )
    state3 = InMemoryStateStore()
    state3.add(record3)
    mgr3 = WorktreeManager(config=ManagerConfig(store_root=tmp_path / "store"), state=state3)
    mcp3 = FastMCP("test3")
    register(mcp3, mgr3)
    fn3 = mcp3._tool_manager._tools["environment_start"].fn
    with patch.object(mgr3, "start", return_value=record3):
        result3 = fn3(environment_id=worktree_id3)
    assert result3.get("start_log_path") is None


# ---- Ticket #66: SetupFailedError from worktree_create is caught as ValueError ----


def test_worktree_create_setup_failed_raises_valueerror_not_runtimeerror(
    tmp_path: Path,
):
    """Regression: worktree_create must raise ValueError (not raw RuntimeError)
    when manager.create raises SetupFailedError.

    SetupFailedError inherits from RuntimeError (not WorktreeError), so an
    uncaught SetupFailedError would leak as RuntimeError. The explicit
    except-SetupFailedError clause must intercept it first and wrap it as
    ValueError so MCP callers receive a well-typed error.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)

    exc = SetupFailedError(
        worktree_id="wt-setup-fail-12345678",
        step_index=0,
        step_name="build",
        log_path=Path("/tmp/setup.log"),
        returncode=1,
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch="feature/x")

    # Must be ValueError, not RuntimeError
    assert not isinstance(exc_info.value, RuntimeError)
    # The error message must mention the failure context
    assert "Setup failed" in str(exc_info.value) or "setup" in str(exc_info.value).lower()


# ---- Ticket #77: v0.1.9 install_enabled_plugins() integration coverage ----
#
# The v0.1.8 -> v0.1.9 bump of lib-python-worktree (ticket #64 upstream)
# inverted the install strategy for a worktree's `.claude/settings.json`
# enabledPlugins: it is now **clone-first** -- for each enabled key,
# WorktreeManager.create() looks for any existing, structurally-valid
# registry entry (any scope/projectPath; validity means
# `<installPath>/.claude-plugin/plugin.json` exists and parses) and clones
# it under a lock into a new `scope: "project"` entry for the worktree. This
# never shells out, so it is the primary mechanism now. Only when no valid
# clone source exists does it fall back to
# `claude plugin install <key> --scope project` (with a second clone
# attempt if that CLI invocation itself fails, in case it partially
# populated the registry). The old `seed_plugin_registry()` registry-clone
# fallback (ticket #39) is no longer wired from `manager.py` as of #64 --
# clone-first supersedes it. This repo's own `.claude/settings.json` has 3
# enabledPlugins keys, so every real `worktree_create()` call against this
# exact repo now exercises that path. The tests below mirror that shape
# using WorktreeManager's dedicated test seams (`_plugin_install_which`,
# `_plugin_install_runner`, `_plugin_install_config_dir`) so nothing shells
# out to a real `claude` process or touches the developer's actual
# `~/.claude` registry. `_plugin_seed_config_dir` is still accepted by the
# constructor for backward compatibility but is no longer read by
# `create()`, so it is not used below.


def _write_claude_settings(repo: Path, enabled_plugins: dict) -> None:
    """Write .claude/settings.json with the given enabledPlugins map."""
    claude_dir = repo / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled_plugins}), encoding="utf-8"
    )


_REPO_ENABLED_PLUGINS = {
    "agent-project-issues@agent-marketplace": True,
    "agent-worktree@agent-marketplace": True,
    "agent-autonomous-developer@agent-marketplace": True,
}


def _make_plugin_repo(tmp_path: Path, name: str = "src-repo") -> Path:
    """Build a temp repo whose .claude/settings.json mirrors this repo's own
    enabledPlugins shape (3 truthy keys), matching the reviewer's finding."""
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _write_claude_settings(repo, _REPO_ENABLED_PLUGINS)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "feature/alpha", cwd=repo)
    return repo


def test_create_with_enabled_plugins_clone_first_uses_existing_registry_without_cli(
    tmp_path: Path, monkeypatch
):
    """Primary path introduced by the v0.1.8 -> v0.1.9 bump (ticket #64
    upstream, consumed here via ticket #77): when a structurally-valid
    registry entry already exists for an enabledPlugins key (any scope),
    WorktreeManager.create() clones it into a new project-scoped entry for
    the worktree instead of shelling out to `claude plugin install` -- even
    though the `claude` CLI is resolvable. The CLI is only a fallback (see
    the two tests below) when no valid clone source exists.

    Grounded directly in lib_python_worktree.core.plugin_install:
    `_find_clone_source()` accepts any structurally-valid entry (validity
    per `_is_structurally_valid()`: `<installPath>/.claude-plugin/plugin.json`
    exists and parses as JSON), and `_clone_entry_to_worktree()` writes the
    clone into `installed_plugins.json` under a portalocker lock.
    """
    monkeypatch.setenv("WORKTREE_LOG_ROOT", str(tmp_path / "logs"))
    repo = _make_plugin_repo(tmp_path, name="src-repo3")

    # Seed a fake ~/.claude registry with one pre-existing, structurally
    # valid install per enabledPlugins key, registered at a scope/path
    # unrelated to this worktree -- any scope is an acceptable clone source.
    config_dir = tmp_path / "claude-clone-cfg"
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True)

    registry = {"version": 2, "plugins": {}}
    for key in _REPO_ENABLED_PLUGINS:
        install_dir = plugins_dir / "cache" / key.replace("/", "_").replace("@", "_")
        (install_dir / ".claude-plugin").mkdir(parents=True)
        (install_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": key.split("@")[0]}), encoding="utf-8"
        )
        registry["plugins"][key] = [
            {
                "scope": "user",
                "projectPath": None,
                "installPath": str(install_dir),
                "installedAt": "2026-01-01T00:00:00Z",
                "resolvedVersion": "1.0.0",
            }
        ]
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )

    calls = []

    def _boom_runner(cmd, *, cwd, timeout):
        # If clone-first is working, this must never be called: a valid
        # clone source exists for every key.
        calls.append((tuple(cmd), cwd, timeout))
        return type("_FailProc", (), {"returncode": 1, "stdout": "", "stderr": "unused"})()

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=InMemoryStateStore(),
        _plugin_install_which=lambda name: "claude",  # resolvable, but must go unused
        _plugin_install_runner=_boom_runner,
        _plugin_install_config_dir=config_dir,
    )

    rec = mgr.create(str(repo), "feature/alpha")

    assert rec.branch == "feature/alpha"
    assert Path(rec.path).exists()
    # The CLI fallback must never be reached: a valid clone source existed
    # for every key up front.
    assert calls == []

    # Every key now has a NEW project-scoped registry entry cloned for this
    # worktree's path, alongside the original source entry.
    updated = json.loads(
        (plugins_dir / "installed_plugins.json").read_text(encoding="utf-8")
    )
    for key in _REPO_ENABLED_PLUGINS:
        project_entries = [
            e for e in updated["plugins"][key] if e.get("scope") == "project"
        ]
        assert len(project_entries) == 1
        assert os.path.normcase(
            str(Path(project_entries[0]["projectPath"]))
        ) == os.path.normcase(str(Path(rec.path)))


def test_create_with_enabled_plugins_claude_unavailable_falls_back_and_does_not_hang(
    tmp_path: Path, monkeypatch
):
    """When enabledPlugins is set (mirroring this repo's own
    .claude/settings.json), no valid clone source exists in the registry,
    and the `claude` CLI can't be resolved on PATH either,
    WorktreeManager.create() must record every key as failed (best-effort)
    and still return a normal record -- without hanging and without
    spawning any real subprocess.

    Regression guard originally added for the v0.1.8 rewrite (ticket #75
    review finding) and re-verified against v0.1.9's clone-first mechanism
    (ticket #77): prior to the original test, no fixture in this suite had
    a populated .claude/settings.json, so this code path was completely
    untested.
    """
    import time as _time

    monkeypatch.setenv("WORKTREE_LOG_ROOT", str(tmp_path / "logs"))
    repo = _make_plugin_repo(tmp_path)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=InMemoryStateStore(),
        _plugin_install_which=lambda name: None,  # simulate claude not on PATH
        _plugin_install_config_dir=tmp_path / "claude-install-cfg",
    )

    start = _time.monotonic()
    rec = mgr.create(str(repo), "feature/alpha")
    elapsed = _time.monotonic() - start

    assert rec.branch == "feature/alpha"
    assert Path(rec.path).exists()
    # Hermetic: the config dir has no registry file at all, so
    # _find_clone_source() has nothing to offer (no clone source) and, with
    # claude unavailable too, install_enabled_plugins() records every key as
    # failed and returns immediately -- well under the 60s default
    # subprocess-install timeout that would apply on the real (non-test)
    # path.
    assert elapsed < 5.0, (
        f"create() took {elapsed:.2f}s; expected a fast no-op fallback, not a hang"
    )


def test_create_with_enabled_plugins_install_subprocess_failure_does_not_raise(
    tmp_path: Path, monkeypatch
):
    """When no valid clone source exists (the fake config dir has no
    registry file, so `_find_clone_source()` always returns None) and
    `claude` IS resolvable but every `claude plugin install` invocation
    fails (nonzero exit) and the post-failure recovery clone also finds
    nothing to clone, create() must still succeed -- failures are
    best-effort and swallowed by the bare except in manager.py. This also
    verifies the real subprocess is never invoked (the fake `runner` seam
    intercepts every call) and that all 3 enabledPlugins keys from
    .claude/settings.json were actually attempted via the CLI fallback.
    """
    monkeypatch.setenv("WORKTREE_LOG_ROOT", str(tmp_path / "logs"))
    repo = _make_plugin_repo(tmp_path, name="src-repo2")

    calls = []

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "simulated failure"

    def _fake_runner(cmd, *, cwd, timeout):
        calls.append((tuple(cmd), cwd, timeout))
        return _FakeCompletedProcess()

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=InMemoryStateStore(),
        _plugin_install_which=lambda name: "claude",  # simulate claude resolvable
        _plugin_install_runner=_fake_runner,
        _plugin_install_config_dir=tmp_path / "claude-install-cfg",
    )

    rec = mgr.create(str(repo), "feature/alpha")

    assert rec.branch == "feature/alpha"
    assert Path(rec.path).exists()

    # All 3 enabledPlugins keys were attempted via the fake runner seam --
    # confirms the install path was actually exercised end-to-end, and
    # confirms no real subprocess was spawned (a real `claude` binary is not
    # installed in the test environment, so the genuine subprocess.Popen
    # path would fail or hang if it were reached).
    called_keys = {cmd[3] for cmd, _cwd, _timeout in calls}
    assert called_keys == set(_REPO_ENABLED_PLUGINS.keys())
    for cmd, cwd, _timeout in calls:
        assert cmd[:3] == ("claude", "plugin", "install")
        assert cmd[4:] == ("--scope", "project")
        assert cwd == rec.path


def test_create_without_claude_settings_skips_install_entirely(
    tmp_path: Path, monkeypatch, temp_repo: Path
):
    """Sanity check: repos without .claude/settings.json (the existing
    fixtures used throughout the rest of this suite) never reach
    install_enabled_plugins' clone-first or subprocess branches at all --
    `_read_enabled_plugins` returns [] and the function returns immediately.
    Confirms the install_enabled_plugins() code path (unchanged by the
    v0.1.8 -> v0.1.9 bump) is opt-in, gated on enabledPlugins being present,
    rather than a universal regression for every worktree_create call.
    """
    monkeypatch.setenv("WORKTREE_LOG_ROOT", str(tmp_path / "logs"))

    def _boom(*args, **kwargs):
        raise AssertionError("no plugin install runner should be invoked")

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"),
        state=InMemoryStateStore(),
        _plugin_install_which=lambda name: "claude",
        _plugin_install_runner=_boom,
    )

    rec = mgr.create(str(temp_repo), "feature/alpha")
    assert rec.branch == "feature/alpha"


# ---- Ticket #83: worktree_start/worktree_stop docstrings document the
# per-step `.seretos/worktree-setup.yml` schema (`run:` required, `name:`
# optional) ----


def test_worktree_start_docstring_documents_step_schema():
    """The registered worktree_start tool's docstring must explain the
    per-step schema of `start:` entries in `.seretos/worktree-setup.yml`:
    a required `run:` key (the shell command) and an optional `name:` key
    used by `variant` to select the step. It must also include a concrete
    example so callers can see the shape rather than infer it.

    Prior to the docs fix, the docstring described `variant` selecting a
    step by `name`, but never named the `run:` key that carries the actual
    command -- so this assertion fails against the unfixed docstring.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=Path("unused-store")),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn
    doc = fn.__doc__ or ""

    assert "run:" in doc, "worktree_start docstring must document the `run:` step key"
    assert "name:" in doc, "worktree_start docstring must document the optional `name:` step key"
    assert "start-web.sh" in doc, (
        "worktree_start docstring must include a concrete example step (e.g. start-web.sh)"
    )


def test_worktree_stop_docstring_documents_step_schema():
    """The registered worktree_stop tool's docstring must explain that
    `stop:` steps in `.seretos/worktree-setup.yml` share the same per-step
    shape as `start:` steps (`run:` required, `name:` optional), with a
    concrete example.

    Prior to the docs fix, the docstring mentioned `stop:` steps running
    best-effort before the shutdown signal, but never named the `run:` key
    or showed an example -- so this assertion fails against the unfixed
    docstring.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=Path("unused-store")),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_stop"].fn
    doc = fn.__doc__ or ""

    assert "run:" in doc, "worktree_stop docstring must document the `run:` step key"
    assert "stop-web.sh" in doc, (
        "worktree_stop docstring must include a concrete example step (e.g. stop-web.sh)"
    )


# ---- Ticket #87: worktree_start/worktree_stop docstrings correct the false
# claim that the contract is read "inside the worktree" -- the engine reads
# `.seretos/worktree-setup.yml` from `repo_root` (the original clone), not
# the worktree checkout. A contract placed only in the worktree yields the
# same silent {"status":"ready","pids":{}} no-op as "no contract configured"
# (ticket #41). The docstrings must also document the required `version`/
# `isolation` top-level keys and that `isolation: none` forbids
# `start:`/`stop:`/`ports:`. ----


def test_worktree_start_docstring_documents_repo_root_contract_location():
    """The registered worktree_start tool's docstring must not claim the
    contract is read "inside the worktree" -- it must instead state that
    the engine reads `.seretos/worktree-setup.yml` from `repo_root` (the
    original repository clone the worktree was created from), and must
    warn that placing the contract only in the worktree checkout produces
    a silent no-op indistinguishable from "no contract configured".

    Prior to the docs fix, the docstring said the contract is read
    "inside the worktree", which is false -- so this assertion fails
    against the unfixed docstring.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=Path("unused-store")),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn
    doc = fn.__doc__ or ""

    assert "inside the worktree" not in doc, (
        "worktree_start docstring must not claim the contract is read "
        "inside the worktree -- it is read from repo_root"
    )
    assert "repo_root" in doc, (
        "worktree_start docstring must name repo_root as the authoritative "
        "contract read location"
    )
    assert "no-op" in doc or "no op" in doc, (
        "worktree_start docstring must warn about the silent ready/no-op "
        "outcome when the contract is placed in the wrong location"
    )


def test_worktree_start_docstring_shows_version_and_isolation():
    """The registered worktree_start tool's docstring example must lead with
    the required `version:` and `isolation:` top-level keys, and must
    document that `isolation: none` forbids `start:`/`stop:`/`ports:`.

    Prior to the docs fix, the YAML example only showed `start:` steps
    with no `version`/`isolation` keys -- so this assertion fails against
    the unfixed docstring.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=Path("unused-store")),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn
    doc = fn.__doc__ or ""

    assert "version:" in doc, "worktree_start docstring must show the required version: key"
    assert "isolation:" in doc, "worktree_start docstring must show the required isolation: key"
    assert "isolation: none" in doc and (
        "forbids" in doc or "forbidden" in doc or "not allowed" in doc
    ), (
        "worktree_start docstring must document that isolation: none forbids "
        "start:/stop:/ports:"
    )


def test_worktree_stop_docstring_documents_repo_root_contract_location():
    """The registered worktree_stop tool's docstring must state that
    `.seretos/worktree-setup.yml` is read from `repo_root` (the original
    clone), not the worktree checkout, and must document that
    `isolation: none` forbids `start:`/`stop:`/`ports:`.

    Prior to the docs fix, the docstring named the contract file without
    any location, leaving the false "inside the worktree" impression from
    worktree_start uncorrected here -- so this assertion fails against the
    unfixed docstring.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=Path("unused-store")),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_stop"].fn
    doc = fn.__doc__ or ""

    assert "repo_root" in doc, (
        "worktree_stop docstring must name repo_root as the authoritative "
        "contract read location"
    )
    assert "isolation: none" in doc and (
        "forbids" in doc or "forbidden" in doc or "not allowed" in doc
    ), (
        "worktree_stop docstring must document that isolation: none forbids "
        "start:/stop:/ports:"
    )


# ---- Ticket #112: soft-error dicts carry a machine-readable `code` ----
#
# Additive to the pre-existing `{"error": "..."}` soft-error shape at all 5
# not-found/already-running/not-running call sites, so MCP callers can
# branch on `code` instead of parsing the `error` string. The `error` text
# itself must stay byte-identical -- see
# test_soft_error_message_text_unchanged_alongside_code below, and the
# untouched exact-string assertions elsewhere in this file (e.g.
# test_tool_environment_start_empty_string_id_not_absent,
# test_tool_environment_stop_empty_string_id_not_absent,
# test_tool_worktree_remove_empty_string_id_not_absent), which this ticket
# deliberately leaves unedited as must-stay-passing guards.

_SOFT_ERROR_CODE_SITES = [
    (
        "worktree_remove",
        "remove",
        WorktreeNotFoundError("wt-missing"),
        {"environment_id": "wt-missing"},
        "not_found",
        "environment 'wt-missing' not found",
    ),
    (
        "environment_start",
        "start",
        WorktreeNotFoundError("wt-missing"),
        {"environment_id": "wt-missing"},
        "not_found",
        "environment 'wt-missing' not found",
    ),
    (
        "environment_start",
        "start",
        ProcessAlreadyRunningError("wt-id", "main", 12345),
        {"environment_id": "wt-id"},
        "already_running",
        "process already running for worktree 'wt-id' role 'main' (pid=12345)",
    ),
    (
        "environment_stop",
        "stop",
        WorktreeNotFoundError("wt-missing"),
        {"environment_id": "wt-missing"},
        "not_found",
        "environment 'wt-missing' not found",
    ),
    (
        "environment_stop",
        "stop",
        ProcessNotRunningError("wt-id", "main"),
        {"environment_id": "wt-id"},
        "not_running",
        "no running process for worktree 'wt-id' role 'main'",
    ),
]

_SOFT_ERROR_CODE_SITE_IDS = [
    "worktree_remove-not_found",
    "environment_start-not_found",
    "environment_start-already_running",
    "environment_stop-not_found",
    "environment_stop-not_running",
]


@pytest.mark.parametrize(
    "tool_name,mock_attr,exception,call_kwargs,expected_code,expected_error",
    _SOFT_ERROR_CODE_SITES,
    ids=_SOFT_ERROR_CODE_SITE_IDS,
)
def test_soft_error_dicts_carry_machine_readable_code(
    tmp_path: Path,
    tool_name: str,
    mock_attr: str,
    exception: Exception,
    call_kwargs: dict,
    expected_code: str,
    expected_error: str,
):
    """Driving test (RED before this ticket: no ``"code"`` key existed at
    any of the 5 soft-error call sites). Every soft-error dict returned by
    ``worktree_remove``/``environment_start``/``environment_stop`` must now
    carry an additive machine-readable ``"code"`` key alongside the
    pre-existing ``"error"`` text."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    setattr(mgr, mock_attr, MagicMock(side_effect=exception))

    result = fns[tool_name](**call_kwargs)

    assert isinstance(result, dict)
    assert "error" in result
    assert "code" in result, f"expected a 'code' key in {result}"
    assert result["code"] == expected_code
    assert result["error"] == expected_error


def test_soft_error_message_text_unchanged_alongside_code(tmp_path: Path):
    """The 5 soft-error call sites' ``"error"`` text must be byte-identical
    to what it was before the ``"code"`` key was added -- adding ``code``
    must never reword, reorder, or repunctuate the existing message. Each
    literal string here is hardcoded independently of the implementation,
    so a future accidental reword of the error text (not just a missing
    code) would fail this test."""
    from unittest.mock import MagicMock

    for tool_name, mock_attr, exception, call_kwargs, _code, expected_error in _SOFT_ERROR_CODE_SITES:
        mgr, fns = _make_tool_fixtures(tmp_path)
        setattr(mgr, mock_attr, MagicMock(side_effect=exception))

        result = fns[tool_name](**call_kwargs)

        assert result["error"] == expected_error, (
            f"{tool_name} error text changed: got {result['error']!r}, "
            f"expected {expected_error!r}"
        )


def test_soft_error_code_absent_on_success(tmp_path: Path):
    """A successful (non-error) record dict from worktree_remove/
    environment_start/environment_stop must never carry a ``"code"`` key --
    guards against a blanket/implementation mistake that injects ``code``
    unconditionally rather than only on the 5 documented soft-error paths."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)

    remove_record = _make_removed_record()
    mgr.remove = MagicMock(return_value=remove_record)
    remove_result = fns["worktree_remove"](environment_id="wt-id")
    assert "error" not in remove_result
    assert "code" not in remove_result

    start_record = _make_running_record()
    mgr.start = MagicMock(return_value=start_record)
    start_result = fns["environment_start"](environment_id="wt-id")
    assert "error" not in start_result
    assert "code" not in start_result

    stop_record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=stop_record)
    stop_result = fns["environment_stop"](environment_id="wt-id")
    assert "error" not in stop_result
    assert "code" not in stop_result


# ---- Ticket #127 ----
#
# environment_start's `variant` defaults to "default", but the engine only
# resolves that to a contract `start:` step under specific conditions (an
# exact `name: default` match, a lone unnamed step, or -- as of upstream
# lib-python-worktree #112, shipped in the pinned v0.3.5 -- a lone step
# overall regardless of naming). Nothing in `worktree_create`'s returned
# record surfaced the contract's actual named `start:` steps up front, so a
# contract author naming their sole step something other than "default"
# only discovered the mismatch from an `UnknownVariantError` on the first
# `environment_start` call. `start_variants` closes that gap: it is the raw
# list of declared `start:` step names (unnamed steps excluded, exactly
# like the engine's own `UnknownVariantError.available`), always present in
# `worktree_create`'s result, `None` when there is no contract to read (or
# it could not be read), and `[]` when a contract was read successfully but
# declares no *named* `start:` steps.


def test_worktree_create_surfaces_start_step_names(tmp_path: Path, temp_repo: Path):
    """Driving test: a single named `start:` step must surface verbatim as
    `start_variants` on the record `worktree_create` returns -- the record
    an agent already has in hand at create time, before it ever calls
    `environment_start` and risks an `UnknownVariantError`."""
    _write_contract(
        temp_repo,
        "version: 1\nisolation: partial\nstart:\n  - name: main\n    run: echo hi\n",
    )
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert "error" not in result
    assert result["start_variants"] == ["main"]


def test_worktree_create_start_step_names_multi_step_preserves_order(
    tmp_path: Path, temp_repo: Path
):
    """Multiple named steps must surface in declaration order."""
    _write_contract(
        temp_repo,
        "version: 1\nisolation: partial\nstart:\n"
        "  - name: web\n    run: echo web\n"
        "  - name: worker\n    run: echo worker\n",
    )
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert result["start_variants"] == ["web", "worker"]


def test_worktree_create_start_step_names_excludes_unnamed(
    tmp_path: Path, temp_repo: Path
):
    """A step with no `name:` key must not appear in `start_variants` --
    mirrors the engine's own `UnknownVariantError.available` computation,
    which also omits unnamed steps."""
    _write_contract(
        temp_repo,
        "version: 1\nisolation: partial\nstart:\n"
        "  - name: web\n    run: echo web\n"
        "  - run: echo unnamed\n",
    )
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert result["start_variants"] == ["web"]


def test_worktree_create_start_step_names_all_unnamed_is_empty_list_not_none(
    tmp_path: Path, temp_repo: Path
):
    """A contract with a single, unnamed `start:` step is still a
    successfully-read contract with nothing named to offer -- `[]`, not
    `None`. Conflating the two would make it indistinguishable from "no
    contract file at all", which is a materially different situation for a
    contract-authoring agent to diagnose."""
    _write_contract(
        temp_repo,
        "version: 1\nisolation: partial\nstart:\n  - run: echo hi\n",
    )
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert result["start_variants"] == []
    assert result["start_variants"] is not None


def test_worktree_create_start_step_names_none_when_no_contract_file(
    tmp_path: Path, temp_repo: Path
):
    """No contract file at all must surface `start_variants is None`, and
    `worktree_create` must still succeed -- a missing contract is not an
    error condition for the checkout lifecycle."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert "error" not in result
    assert result["start_variants"] is None


def test_worktree_create_start_step_names_empty_for_isolation_none(
    tmp_path: Path, temp_repo: Path
):
    """`isolation: none` forbids a `start:` block entirely, but the
    contract itself was still read successfully -- `[]`, proving the
    missing-file `None` case above is never conflated with a validly-read
    contract that simply has nothing to offer."""
    _write_contract(temp_repo, "version: 1\nisolation: none\n")
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert result["start_variants"] == []


def test_worktree_create_start_step_names_none_when_contract_unreadable(
    tmp_path: Path, temp_repo: Path
):
    """A contract that exists and is perfectly valid on disk, but whose
    read fails specifically inside the new helper (the documented TOCTOU
    idiom -- see test_environment_tools.py's equivalent
    `load_contract`-patching tests) must degrade to `start_variants is
    None` without `worktree_create` raising. Uses a VALID contract on disk
    and patches `worktree_plugin.tools.worktree.load_contract` rather than
    writing malformed YAML: a genuinely malformed contract on disk would
    make `manager.create()`'s own internal load fail one frame earlier
    (inside its rollback `try`), so `worktree_create` itself would raise
    and the helper's except clause would never be reached at all."""
    from unittest.mock import patch

    _write_contract(
        temp_repo,
        "version: 1\nisolation: partial\nstart:\n  - name: main\n    run: echo hi\n",
    )
    mgr, fns = _make_tool_fixtures(tmp_path)

    with patch(
        "worktree_plugin.tools.worktree.load_contract",
        side_effect=ContractError("boom"),
    ):
        result = fns["worktree_create"](repo_root=str(temp_repo), branch="feature/wt")

    assert "error" not in result
    assert result["start_variants"] is None


def test_worktree_create_docstring_documents_start_variants(tmp_path: Path):
    """worktree_create's docstring must document the new `start_variants`
    field and both of its sentinel states (`None`/`null` for "no contract
    to read", `[]`/"empty list" for "contract read, nothing named")."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    doc = fns["worktree_create"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    idx = norm.find("start_variants")
    assert idx != -1, "worktree_create docstring must mention start_variants"
    window = norm[max(0, idx - 300) : idx + 900]
    assert re.search(r"null|none", window)
    assert re.search(r"empty list|\[\]", window)


# ---- Ticket #141: ambient-handle hardening ----


def test_ambient_retry_survives_transient_dir_lock():
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise WorktreeDirLockedError("wt-id", killed=[])
        return "sentinel"

    result = _remove_with_ambient_retry(
        _flaky, what="flaky probe", attempts=5, base_delay=0.0
    )

    assert result == "sentinel"
    assert calls["n"] == 3


def test_ambient_retry_passes_through_non_ambient_error():
    calls = {"n": 0}

    def _always_not_found():
        calls["n"] += 1
        raise ValueError("environment 'x' not found")

    with pytest.raises(ValueError, match="not found"):
        _remove_with_ambient_retry(
            _always_not_found, what="not-found probe", attempts=5, base_delay=0.0
        )

    assert calls["n"] == 1


def test_ambient_retry_detects_lock_through_valueerror_cause():
    try:
        raise WorktreeDirLockedError("wt-id", killed=[])
    except WorktreeDirLockedError as cause:
        wrapped = ValueError("wrapped")
        wrapped.__cause__ = cause
        assert _is_ambient_handle_error(wrapped) is True


def test_ambient_retry_detects_windows_sharing_violation():
    """Ticket #141 review fix: the ambient classification is win32-only and
    keyed on ERROR_SHARING_VIOLATION (32) / ERROR_ACCESS_DENIED (5) -- the
    actual foreign-handle signals. A `PermissionError` also counts, but
    only when it carries one of those two winerror codes."""
    sharing = OSError("sharing violation")
    sharing.winerror = 32
    assert _is_ambient_handle_error(sharing) is True

    access_denied = OSError("access denied")
    access_denied.winerror = 5
    assert _is_ambient_handle_error(access_denied) is True

    permission_with_winerror = PermissionError("denied")
    permission_with_winerror.winerror = 5
    assert _is_ambient_handle_error(permission_with_winerror) is True


def test_ambient_retry_permission_error_without_winerror_is_not_ambient():
    """Ticket #141 review fix: a bare `isinstance(cur, PermissionError)`
    branch unconditionally counted ANY PermissionError as ambient, which is
    broader than "foreign handle holder" and would retry/mask genuine
    permission or ACL failures. The ambient-handle pre-flight this ticket
    targets is win32-only, so a PermissionError carrying no winerror (e.g.
    a POSIX EACCES) -- or one carrying an unrelated winerror such as 145
    (ERROR_DIR_NOT_EMPTY, a real cleanup regression, not a lock) -- must
    NOT be treated as ambient, and must be re-raised unchanged on the very
    first attempt.

    Follow-up fix: the text-signature fallback (`_AMBIENT_TEXT_SIGNATURES`,
    which includes "permission denied") must not re-open this hole. It
    exists only for the tool layer's generic `except WorktreeError` tail,
    which can carry git's raw wording without a lock-typed __cause__; for
    an OSError (PermissionError included) the winerror check above is
    already authoritative, so the text path must not second-guess it. This
    covers the realistic POSIX PermissionError shapes -- `PermissionError(
    errno, strerror)` and the stdlib's own `str()` rendering of one, both
    of which contain the literal substring "permission denied"."""
    no_winerror = PermissionError("denied")
    assert _is_ambient_handle_error(no_winerror) is False

    unrelated_winerror = PermissionError("directory not empty")
    unrelated_winerror.winerror = 145
    assert _is_ambient_handle_error(unrelated_winerror) is False

    errno_style = PermissionError(13, "Permission denied")
    assert _is_ambient_handle_error(errno_style) is False

    posix_style = PermissionError("[Errno 13] Permission denied: '/some/path'")
    assert _is_ambient_handle_error(posix_style) is False

    for exc in (no_winerror, unrelated_winerror, errno_style, posix_style):
        calls = {"n": 0}

        def _op(exc=exc):
            calls["n"] += 1
            raise exc

        with pytest.raises(PermissionError) as excinfo:
            _remove_with_ambient_retry(
                _op, what="no-winerror probe", attempts=5, base_delay=0.0
            )

        assert excinfo.value is exc
        assert calls["n"] == 1


def test_ambient_retry_text_fallback_still_applies_to_non_oserror():
    """Positive guard: narrowing the text-signature fallback to
    `not isinstance(cur, OSError)` must not silently delete its real
    purpose. The fallback exists for the tool layer's generic `except
    WorktreeError` tail (tools/worktree.py), which can wrap git's raw
    permission wording in a plain `ValueError`/`WorktreeError` with no
    lock-typed __cause__ -- that non-OSError shape must still be
    classified as ambient via the text path."""
    assert (
        _is_ambient_handle_error(
            ValueError("fatal: could not remove: Permission denied")
        )
        is True
    )

    tool_layer_shape = WorktreeError(
        "failed to remove worktree 'x': Permission denied"
    )
    assert _is_ambient_handle_error(tool_layer_shape) is True


def test_ambient_retry_does_not_retry_compound_removal_block():
    """Ticket #141 review fix: `WorktreeRemovalBlockedError` subclasses
    `WorktreeDirLockedError` (it fires when a directory lock AND real
    uncommitted/untracked changes block `remove()` at the same time --
    ticket #103/#120). A naive `isinstance(cur, WorktreeDirLockedError)`
    check would misclassify this compound failure as ambient, retry it,
    and then emit the AMBIENT-HANDLE banner -- which never mentions the
    dirty-paths half -- masking a genuine dirty-worktree bug. It must be
    recognized as NOT ambient, both bare and wrapped in the
    `ValueError(...) from exc` shape tools/worktree.py:973-984 actually
    raises, and re-raised unchanged on the very first attempt."""
    blocked = WorktreeRemovalBlockedError(
        worktree_id="x", killed=[], kill_attempted=False, dirty_paths=["notes.txt"]
    )
    assert _is_ambient_handle_error(blocked) is False

    wrapped = ValueError(
        f'{blocked} (blocked_by: "dir_locked", "uncommitted_changes"; '
        f"required_flags: kill_blocking_processes=True, force=True)"
    )
    wrapped.__cause__ = blocked
    assert _is_ambient_handle_error(wrapped) is False

    bare_calls = {"n": 0}

    def _op_bare():
        bare_calls["n"] += 1
        raise blocked

    with pytest.raises(WorktreeRemovalBlockedError) as excinfo:
        _remove_with_ambient_retry(
            _op_bare, what="compound probe (bare)", attempts=5, base_delay=0.0
        )
    assert excinfo.value is blocked
    assert bare_calls["n"] == 1

    wrapped_calls = {"n": 0}

    def _op_wrapped():
        wrapped_calls["n"] += 1
        raise wrapped

    with pytest.raises(ValueError) as excinfo2:
        _remove_with_ambient_retry(
            _op_wrapped, what="compound probe (wrapped)", attempts=5, base_delay=0.0
        )
    assert excinfo2.value is wrapped
    assert wrapped_calls["n"] == 1


def test_ambient_retry_ignores_refusal_texts():
    assert (
        _is_ambient_handle_error(
            ValueError("invalid path 'x': not a git repository")
        )
        is False
    )


def test_ambient_retry_exhausted_budget_fails_with_ambient_handle_banner():
    calls = {"n": 0}
    blocker = WorktreeDirLockedError("wt-blocked", killed=[])

    def _always_blocked():
        calls["n"] += 1
        raise blocker

    # pytest.fail.Exception is _pytest.outcomes.Failed (a BaseException
    # subclass); use the public alias rather than importing the private
    # _pytest.outcomes module.
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _remove_with_ambient_retry(
            _always_blocked,
            what="always-blocked probe",
            attempts=3,
            base_delay=0.0,
        )

    msg = str(excinfo.value)
    assert "AMBIENT-HANDLE (ticket #141)" in msg
    assert "WorktreeDirLockedError" in msg  # original engine type
    assert str(blocker) in msg  # original engine message
    assert calls["n"] == 3  # budget exactly consumed


def test_ambient_retry_default_budget_is_five_attempts():
    assert _AMBIENT_RETRY_ATTEMPTS == 5


def test_ambient_retry_banner_names_the_operation():
    def _always_blocked():
        raise WorktreeDirLockedError("wt-id", killed=[])

    with pytest.raises(pytest.fail.Exception) as excinfo:
        _remove_with_ambient_retry(
            _always_blocked,
            what="the-named-operation probe",
            attempts=1,
            base_delay=0.0,
        )

    assert "the-named-operation probe" in str(excinfo.value)


_HARDENED_REMOVAL_SITES = (
    "test_create_list_remove_roundtrip",
    "test_create_then_remove_without_force_succeeds_with_untracked_contract_dir",
    "test_create_tracked_contract_dir_gets_no_injected_gitignore",
    "test_create_preserves_gitignore_with_star_then_negation",
    "test_create_preserves_non_utf8_existing_gitignore_bytes",
)


def test_real_git_removal_sites_are_ambient_hardened():
    for name in _HARDENED_REMOVAL_SITES:
        source = inspect.getsource(globals()[name])
        assert "_remove_with_ambient_retry(" in source, (
            f"{name} performs a real-git removal but is not routed through "
            f"_remove_with_ambient_retry (ticket #141)"
        )


_DETERMINISTIC_REFUSAL_TESTS_NOT_HARDENED = (
    "test_tool_worktree_remove_blocked_by_both_conditions_names_both_flags",
    "test_tool_worktree_remove_blocked_after_kill_attempt_still_names_both_flags",
    "test_tool_worktree_remove_dir_locked_raises_valueerror",
    "test_tool_worktree_remove_kill_blocking_processes_forwarded",
    "test_tool_worktree_remove_default_kill_false_forwarded",
    "test_tool_worktree_remove_killed_pids_in_response",
    "test_tool_worktree_remove_default_empty_killed_pids",
    "test_tool_worktree_remove_killed_pids_cmdline_is_decoded_and_raw_preserved",
    "test_tool_worktree_remove_unknown_checkout_target_reason_defensive_text",
    "test_tool_worktree_remove_empty_string_id_not_absent",
    "test_tool_worktree_remove_not_found_still_soft_error",
    "test_tool_worktree_remove_teardown_before_remove_wrapper_contract",
    "test_tool_worktree_remove_teardown_before_remove_force_forwarded",
    "test_tool_worktree_remove_teardown_before_remove_not_found_soft_error",
    "test_soft_error_dicts_carry_machine_readable_code",
    "test_soft_error_code_absent_on_success",
)


def test_deterministic_refusal_tests_are_not_ambient_hardened():
    for name in _DETERMINISTIC_REFUSAL_TESTS_NOT_HARDENED:
        source = inspect.getsource(globals()[name])
        assert "_remove_with_ambient_retry(" not in source, (
            f"{name} is a deterministic refusal/lock-mapping test and must "
            f"not be wrapped in _remove_with_ambient_retry (ticket #141)"
        )


# ---------------------------------------------------------------------------
# WP #162 R2 (ticket #157): worktree_create normalises the GitCommandError
# raised for an invalid branch name instead of leaking git's raw argv --
# including the store's absolute target path -- verbatim.
# ---------------------------------------------------------------------------

# Mirrors the planned `_INVALID_BRANCH_STDERR_MARKERS` production constant
# (to be added to worktree.py in the implement phase -- this tests phase
# writes no production code). Defined locally so the driving test and the
# marker-table test below have a single source of truth for "which
# git-emitted phrasing counts as an invalid-branch-name marker" without
# importing a symbol that does not exist in the source tree yet.
_INVALID_BRANCH_STDERR_MARKERS = (
    "not a valid branch name",
    "invalid branch name",
    "not a valid ref name",
)


def test_worktree_create_invalid_branch_name_normalizes_git_stderr(
    temp_repo: Path, tmp_path: Path
):
    """R2 driving test (ticket #157): a branch name git itself rejects
    (contains '..', '~', '^', ':') must surface a normalised
    `invalid_branch_name:` error instead of the raw `GitCommandError` text,
    which leaks the full argv of the failed `git worktree add` command --
    including the store's absolute target path -- verbatim.

    Real git, no mocking -- mirrors the ticket's own repro exactly.

    Expected RED reason: no arm in worktree_create classifies
    GitCommandError today; the generic `except WorktreeError` tail at
    worktree.py:807-808 re-raises `str(exc)` unchanged, i.e. `git command
    failed (exit ...): git worktree add -b <branch> <store path> main
    \\nfatal: ...` -- so `message.startswith("invalid_branch_name:")` fails
    immediately (confirmed directly against this environment's real git
    before writing this test).
    """
    mgr, fns = _make_tool_fixtures(tmp_path)
    branch = "bad..branch~name^:test"

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root=str(temp_repo), branch=branch)

    message = str(exc_info.value)
    assert message.startswith("invalid_branch_name:")
    assert f"'{branch}'" in message
    assert any(marker in message.lower() for marker in _INVALID_BRANCH_STDERR_MARKERS)
    assert "Hint:" in message
    # Negatives: none of the raw git argv/exit-code/store-path leak survives.
    assert "git worktree add" not in message
    assert "exit " not in message
    assert str(tmp_path) not in message
    assert tmp_path.as_posix() not in message
    # Lower-case "hint:" would be git's own trailing hint line -- pins the
    # "prefer the marker line, not the last line" correction; ours is "Hint:".
    assert "hint:" not in message


@pytest.mark.parametrize(
    "stderr_line",
    [
        "fatal: 'bad..branch~name^:test' is not a valid branch name",
        "fatal: invalid branch name: 'bad..name'",
        "fatal: 'refs/heads/bad..name' is not a valid ref name",
        "FATAL: 'Bad..Name' Is Not A Valid Branch Name",
    ],
    ids=[
        "not-a-valid-branch-name",
        "invalid-branch-name",
        "not-a-valid-ref-name",
        "mixed-case",
    ],
)
def test_worktree_create_invalid_branch_marker_table(tmp_path: Path, stderr_line: str):
    """R2 edge (a): marker-table test -- the executable definition of the
    trigger. Every marker, wrapped in a realistic single-line stderr, plus a
    mixed-case variant whose casing differs exactly where the marker sits
    (genuinely exercising the case-insensitivity guarantee), must produce
    the `invalid_branch_name:` message. Mock-based, precedent:
    `mgr.create = MagicMock(side_effect=exc)` at
    `test_worktree_create_setup_failed_raises_valueerror_not_runtimeerror`
    above.

    Expected RED reason: same as the driving test -- no classifying arm
    exists yet, so the raised message is `str(exc)` unchanged and never
    starts with `invalid_branch_name:`.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", "bad..name", "/store/x", "main"],
        128,
        stderr_line,
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch="bad..name")

    assert str(exc_info.value).startswith("invalid_branch_name:")


def test_worktree_create_marker_free_git_error_stays_unrewritten(tmp_path: Path):
    """R2 edge (b): no-collateral pinning test. A `GitCommandError` whose
    stderr carries no branch-name marker at all (a credential-prompt
    failure) must NOT be rewritten. Not tautological: the precondition is
    only "it raises"; the assertion is that no rewriting occurred -- the
    raised object is a ValueError carrying the injected exception's own
    text, unrewritten, still carrying "git command failed (exit 128):".

    Expected to already pass -- today's baseline (the generic
    `except WorktreeError` tail) already re-raises `str(exc)` unchanged for
    every GitCommandError, marker or not.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", "ok", "/store/x", "main"],
        128,
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch="ok")

    assert str(exc_info.value) == str(exc)
    assert "invalid_branch_name:" not in str(exc_info.value)


def test_worktree_create_empty_stderr_git_error_stays_unrewritten(tmp_path: Path):
    """R2 edge (c1): `stderr=""` hits the empty-`splitlines()` path the
    planned classifier must guard against. Same observable as edge (b):
    unrewritten ValueError.

    Expected to already pass -- today's baseline unconditionally re-raises
    `str(exc)` regardless of stderr content.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", "ok", "/store/x", "main"], 128, ""
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch="ok")

    assert str(exc_info.value) == str(exc)
    assert "invalid_branch_name:" not in str(exc_info.value)


def test_worktree_create_none_stderr_git_error_stays_unrewritten(tmp_path: Path):
    """R2 edge (c2): `stderr=None` hits the `isinstance(..., str)` guard the
    planned classifier must have. Orchestrator addendum resolution ("minor
    untestable::F2"): the engine's own `.stderr` is always `str`
    (`_run_git` uses `Popen(..., text=True)`), so this state does not occur
    naturally -- it is constructed defensively in two steps: build the
    instance with a valid `str` stderr first (so `GitCommandError.__init__`
    succeeds and `str(exc)` is well-formed), then set `exc.stderr = None` on
    the instance afterwards. This exercises the guard while keeping
    `str(exc)` stable for the equality assertion below. The guard is
    defensive; the engine does not produce this state today.

    Expected to already pass -- today's baseline unconditionally re-raises
    `str(exc)` regardless of `.stderr`.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", "ok", "/store/x", "main"],
        128,
        "fatal: some unrelated failure",
    )
    exc.stderr = None
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch="ok")

    assert str(exc_info.value) == str(exc)
    assert "invalid_branch_name:" not in str(exc_info.value)


def test_worktree_create_invalid_branch_multiline_stderr_uses_marker_line(
    tmp_path: Path,
):
    """R2 edge (d): multi-line stderr -- the marker line, not the first or
    last line, is what must be surfaced (real git emits a trailing `hint:`
    line, so "last non-empty line" cleaning would surface the hint instead
    of the informative `fatal:` line -- this is the plan's correction over
    an earlier design). Fully mock-controlled stderr, so no git-version
    dependence.

    Expected RED reason: same as the driving test -- no classifying arm
    exists yet, so none of these assertions about *which* line got
    extracted can hold; the raised message is `str(exc)` unchanged.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", "b", "/store/x", "main"],
        128,
        "warning: some unrelated noise\n"
        "fatal: 'b' is not a valid branch name\n"
        "hint: see the manual",
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch="b")

    message = str(exc_info.value)
    assert "is not a valid branch name" in message
    assert "unrelated noise" not in message
    assert "hint: see" not in message


def test_worktree_create_invalid_branch_interpolates_branch_and_reason(
    tmp_path: Path,
):
    """R2 tightening (test-critic round-1 note 1, major): guards against a
    hard-coded constant message. Every other test in this module either
    reuses one branch name/reason combination or only asserts a known
    marker substring (itself a fixed phrase), so a naive implementation
    that emits a literal, unparameterised string could still satisfy them.
    This test uses a branch name distinct from every other test here, and
    a distinctive, test-controlled reason phrase appended to the matched
    stderr line that cannot plausibly appear in any hard-coded string, and
    asserts BOTH survive into the raised message -- forcing the
    implementation to interpolate `branch` and to read the matched stderr
    line rather than emit a constant.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    branch = "totally-different-probe-branch"
    distinctive_reason = "zzz-probe-marker-8f3c2a-unique"
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", branch, "/store/probe", "main"],
        128,
        f"fatal: '{branch}' is not a valid branch name: {distinctive_reason}",
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch=branch)

    message = str(exc_info.value)
    assert message.startswith("invalid_branch_name:")
    assert f"'{branch}'" in message
    assert distinctive_reason in message


def test_worktree_create_invalid_branch_reason_strips_fatal_prefix_and_collapses_whitespace(
    tmp_path: Path,
):
    """R2 tightening (test-critic round-1 note 2, major): pins the
    reason-line cleaning rule the plan requires -- a leading `fatal: `/
    `error: `/`warning: ` prefix (case-insensitive) is stripped, and
    internal whitespace in the surfaced reason is collapsed to single
    spaces. Fully mock-controlled stderr, so nothing depends on the local
    git version. The extra whitespace is placed around, not inside, the
    marker phrase itself so marker detection is unaffected -- only the
    cleaning rule is under test here.
    """
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    branch = "whitespace-probe-branch"
    exc = GitCommandError(
        ["git", "worktree", "add", "-b", branch, "/store/probe2", "main"],
        128,
        f"FATAL:   '{branch}'   is not a valid branch name",
    )
    mgr.create = MagicMock(side_effect=exc)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root="/repo", branch=branch)

    message = str(exc_info.value)
    assert message.startswith("invalid_branch_name:")
    # No leftover "fatal:" (any case) prefix on the reason survives.
    assert "fatal:" not in message.lower()
    # Internal whitespace in the reason is collapsed -- no run of two or
    # more spaces survives anywhere in the message.
    assert "  " not in message
