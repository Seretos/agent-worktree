"""Integration tests for the W2 core tools.

These exercise real ``git worktree`` operations against a temporary repo, as
required by the planning comment's Verifikation section.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

import lib_python_worktree.core.manager as manager_module
from lib_python_worktree import (
    BranchAlreadyCheckedOutError,
    BranchNotFoundError,
    DuplicateWorktreeError,
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
)
from lib_python_worktree.core.manager import _run_git


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


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

    removed = manager.remove(rec.id)
    assert removed.id == rec.id
    assert not Path(rec.path).exists()
    assert manager.list() == []


def test_create_unknown_branch_without_base(
    manager: WorktreeManager, temp_repo: Path
):
    with pytest.raises(BranchNotFoundError):
        manager.create(str(temp_repo), "feature/does-not-exist")


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

    result = fn(worktree_id=unknown_id)

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

    monkeypatch.setattr(manager_module.subprocess, "Popen", _HangingPopen)

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
    monkeypatch.setattr(manager_module.subprocess, "Popen", _CapturingPopen)

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

    monkeypatch.setattr(manager_module.subprocess, "Popen", _RecordingPopen)
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


def test_tool_worktree_get_returns_record(tmp_path: Path, temp_repo: Path):
    """worktree_get must return the correct record for a known id."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    # Create via the manager directly so we have a known record.
    rec = mgr.create(str(temp_repo), "feature/alpha")

    result = fns["worktree_get"](worktree_id=rec.id)

    assert result["id"] == rec.id
    assert result["branch"] == rec.branch
    assert result["path"] == rec.path
    assert result["repo_root"] == rec.repo_root
    assert "error" not in result


def test_tool_worktree_get_unknown_id_returns_soft_error(tmp_path: Path):
    """worktree_get with an unknown id must return a soft-error dict
    ({"error": "..."}) rather than raising, mirroring worktree_remove."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    unknown_id = "definitely-unknown-id-99999"
    result = fns["worktree_get"](worktree_id=unknown_id)

    assert isinstance(result, dict), f"Expected dict, got: {type(result)}"
    assert "error" in result, f"Expected 'error' key, got: {result}"
    assert unknown_id in result["error"], (
        f"Expected unknown_id in error message, got: {result['error']}"
    )


def test_tool_worktree_get_empty_store(tmp_path: Path):
    """worktree_get on a fresh (empty) manager must return a soft-error dict."""
    mgr, fns = _make_tool_fixtures(tmp_path)

    result = fns["worktree_get"](worktree_id="any-id-12345678")

    assert isinstance(result, dict)
    assert "error" in result


def test_tool_worktree_list_filters_by_repo_root(tmp_path: Path):
    """worktree_list(repo_root=...) must return only records for that repo;
    omitting repo_root returns all worktrees across all repos."""
    # Build two separate repos, each with a non-checked-out branch so git
    # allows adding a worktree for it.
    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo1)
    _git("config", "user.email", "test@example.com", cwd=repo1)
    _git("config", "user.name", "Test", cwd=repo1)
    (repo1 / "README.md").write_text("r1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo1)
    _git("commit", "-q", "-m", "init", cwd=repo1)
    _git("branch", "feature/wt1", cwd=repo1)

    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo2)
    _git("config", "user.email", "test@example.com", cwd=repo2)
    _git("config", "user.name", "Test", cwd=repo2)
    (repo2 / "README.md").write_text("r2\n", encoding="utf-8")
    _git("add", "-A", cwd=repo2)
    _git("commit", "-q", "-m", "init", cwd=repo2)
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")

    # Filtered to repo1 only.
    filtered = fns["worktree_list"](repo_root=str(repo1))
    assert len(filtered) == 1
    assert filtered[0]["id"] == rec1.id

    # Unfiltered returns both.
    all_records = fns["worktree_list"]()
    assert len(all_records) == 2
    ids = {r["id"] for r in all_records}
    assert rec1.id in ids
    assert rec2.id in ids


def test_tool_worktree_list_filter_resolves_subdir(tmp_path: Path):
    """worktree_list filter uses Path.resolve() for comparison. A symlink
    pointing directly at the repo root resolves to the same path as
    record.repo_root and therefore matches. A plain subdirectory does NOT
    match — the filter is an exact-path comparison after resolve(), not a
    git-root traversal.

    This test verifies the symlink case on platforms where symlinks are
    available, and falls back to verifying the non-match case for plain
    subdirectories.
    """
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # feature/wt is not the currently-checked-out branch.
    _git("branch", "feature/wt", cwd=repo)

    mgr, fns = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(repo), "feature/wt")

    # A plain subdirectory does NOT match (filter is exact path after resolve).
    subdir = repo / "subdir"
    subdir.mkdir()
    filtered_subdir = fns["worktree_list"](repo_root=str(subdir))
    assert filtered_subdir == [], (
        "Plain subdirectory should not match; filter is exact-path, not git-root traversal"
    )

    # A symlink pointing at the repo root DOES match because resolve() follows
    # the symlink to the same canonical path as record.repo_root.
    symlink = tmp_path / "repo-symlink"
    try:
        symlink.symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Symlinks may require elevated privileges on Windows — skip that half.
        return

    filtered_sym = fns["worktree_list"](repo_root=str(symlink))
    assert len(filtered_sym) == 1, (
        f"Symlink pointing at repo root should match; got: {filtered_sym}"
    )
    assert filtered_sym[0]["id"] == rec.id


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


def _make_stopped_record(worktree_id: str = "wt-id") -> WorktreeRecord:
    """Return a minimal WorktreeRecord with status='stopped' and no pids."""
    return WorktreeRecord(
        id=worktree_id,
        repo_root="/r",
        branch="b",
        path="/p",
        status="stopped",
        pids={},
    )


def test_worktree_start_stop_tools_registered(tmp_path: Path):
    """Both worktree_start and worktree_stop must be registered as MCP tools."""
    mgr, fns = _make_tool_fixtures(tmp_path)
    assert "worktree_start" in fns, "worktree_start not registered"
    assert "worktree_stop" in fns, "worktree_stop not registered"


def test_tool_worktree_start_returns_record(tmp_path: Path):
    """Happy path: worktree_start returns a dict with status='running' and pids set."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    result = fns["worktree_start"](worktree_id="wt-id")

    assert isinstance(result, dict)
    assert "error" not in result
    assert result["status"] == "running"
    assert result["pids"] == {"main": 12345}
    mgr.start.assert_called_once_with("wt-id", role="main", env=None, cwd=None, variant="default")


def test_tool_worktree_start_unknown_id_returns_soft_error(tmp_path: Path):
    """worktree_start with an unknown id must return a soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))

    result = fns["worktree_start"](worktree_id="wt-missing")

    assert isinstance(result, dict)
    assert "error" in result
    assert "wt-missing" in result["error"]


def test_tool_worktree_start_already_running_returns_soft_error(tmp_path: Path):
    """worktree_start when already running must return soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(
        side_effect=ProcessAlreadyRunningError("wt-id", "main", 99)
    )

    result = fns["worktree_start"](worktree_id="wt-id")

    assert isinstance(result, dict)
    assert "error" in result
    # Must not raise; soft error only.


def test_tool_worktree_start_engine_error_raises_valueerror(tmp_path: Path):
    """worktree_start on a generic ProcessLifecycleError must raise ValueError."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(side_effect=ProcessLifecycleError("engine failure"))

    with pytest.raises(ValueError):
        fns["worktree_start"](worktree_id="wt-id")


def test_tool_worktree_stop_returns_record(tmp_path: Path):
    """Happy path: worktree_stop returns a dict with status='stopped'."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    result = fns["worktree_stop"](worktree_id="wt-id")

    assert isinstance(result, dict)
    assert "error" not in result
    assert result["status"] == "stopped"
    assert result["pids"] == {}
    mgr.stop.assert_called_once_with("wt-id", role="main", timeout=10.0, kill_orphans=False)


def test_tool_worktree_stop_unknown_id_returns_soft_error(tmp_path: Path):
    """worktree_stop with an unknown id must return a soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))

    result = fns["worktree_stop"](worktree_id="wt-missing")

    assert isinstance(result, dict)
    assert "error" in result
    assert "wt-missing" in result["error"]


def test_tool_worktree_stop_not_running_returns_soft_error(tmp_path: Path):
    """worktree_stop when no process is running must return soft-error dict, not raise."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(
        side_effect=ProcessNotRunningError("wt-id", "main")
    )

    result = fns["worktree_stop"](worktree_id="wt-id")

    assert isinstance(result, dict)
    assert "error" in result
    # Must not raise; soft error only.


def test_tool_worktree_stop_engine_error_raises_valueerror(tmp_path: Path):
    """worktree_stop on a generic ProcessLifecycleError must raise ValueError."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.stop = MagicMock(side_effect=ProcessLifecycleError("engine failure"))

    with pytest.raises(ValueError):
        fns["worktree_stop"](worktree_id="wt-id")


def test_tool_worktree_start_custom_role_and_cwd_forwarded(tmp_path: Path):
    """worktree_start must forward custom role and cwd to manager.start (no cmd)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = WorktreeRecord(
        id="wt-id", repo_root="/r", branch="b", path="/p",
        status="running", pids={"worker": 42},
    )
    mgr.start = MagicMock(return_value=record)

    fns["worktree_start"](
        worktree_id="wt-id",
        role="worker",
        cwd="/custom/cwd",
    )

    call_args = mgr.start.call_args
    # Only worktree_id as positional; role and cwd as kwargs; no cmd anywhere.
    assert call_args.args == ("wt-id",)
    assert call_args.kwargs == {"role": "worker", "env": None, "cwd": "/custom/cwd", "variant": "default"}
    # Confirm no command list was passed.
    all_args = list(call_args.args) + list(call_args.kwargs.values())
    assert not any(isinstance(a, list) for a in all_args), (
        "No command list should be forwarded to manager.start"
    )


def test_tool_worktree_start_no_start_configured_raises_valueerror(tmp_path: Path):
    """worktree_start raises ValueError when the contract has no start: command.

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
        fns["worktree_start"](worktree_id="wt-id")


def test_tool_worktree_stop_custom_role_and_timeout_forwarded(tmp_path: Path):
    """worktree_stop must forward custom role and timeout to manager.stop."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    fns["worktree_stop"](worktree_id="wt-id", role="worker", timeout=5.0)

    mgr.stop.assert_called_once_with("wt-id", role="worker", timeout=5.0, kill_orphans=False)


# ---- Ticket #51: worktree_start variant + env, worktree_stop kill_orphans ----


def test_tool_worktree_start_variant_forwarded(tmp_path: Path):
    """worktree_start must forward variant='unity-gui' to manager.start."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    fns["worktree_start"](worktree_id="wt-id", variant="unity-gui")

    call_args = mgr.start.call_args
    assert call_args.kwargs["variant"] == "unity-gui"


def test_tool_worktree_start_env_forwarded(tmp_path: Path):
    """worktree_start must forward env dict to manager.start."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    fns["worktree_start"](worktree_id="wt-id", env={"K": "v"})

    call_args = mgr.start.call_args
    assert call_args.kwargs["env"] == {"K": "v"}


def test_tool_worktree_start_default_forwards_variant_and_env_explicitly(tmp_path: Path):
    """Default worktree_start call must pass variant='default' and env=None explicitly."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_running_record()
    mgr.start = MagicMock(return_value=record)

    fns["worktree_start"](worktree_id="wt-id")

    call_args = mgr.start.call_args
    assert call_args.kwargs["variant"] == "default"
    assert call_args.kwargs["env"] is None


def test_tool_worktree_start_unknown_variant_raises_valueerror(tmp_path: Path):
    """worktree_start raises ValueError when manager raises WorktreeError for unknown variant."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.start = MagicMock(
        side_effect=WorktreeError("no start: step named 'bogus' found ...")
    )

    with pytest.raises(ValueError, match="no start: step named 'bogus'"):
        fns["worktree_start"](worktree_id="wt-id", variant="bogus")


def test_tool_worktree_stop_kill_orphans_forwarded(tmp_path: Path):
    """worktree_stop with kill_orphans=True must forward that flag to manager.stop."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    fns["worktree_stop"](worktree_id="wt-id", kill_orphans=True)

    call_args = mgr.stop.call_args
    assert call_args.kwargs["kill_orphans"] is True


def test_tool_worktree_stop_default_forwards_kill_orphans_false(tmp_path: Path):
    """Default worktree_stop call must pass kill_orphans=False explicitly."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_stopped_record()
    mgr.stop = MagicMock(return_value=record)

    fns["worktree_stop"](worktree_id="wt-id")

    call_args = mgr.stop.call_args
    assert call_args.kwargs["kill_orphans"] is False


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

    fns["worktree_remove"](worktree_id="wt-id", kill_blocking_processes=True)

    mgr.remove.assert_called_once_with(
        "wt-id", force=False, kill_blocking_processes=True
    )


def test_tool_worktree_remove_default_kill_false_forwarded(tmp_path: Path):
    """worktree_remove without kill_blocking_processes must forward False to
    manager.remove (the default must not silently drop the kwarg)."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    record = _make_removed_record()
    mgr.remove = MagicMock(return_value=record)

    fns["worktree_remove"](worktree_id="wt-id")

    mgr.remove.assert_called_once_with(
        "wt-id", force=False, kill_blocking_processes=False
    )


def test_tool_worktree_remove_killed_pids_in_response(tmp_path: Path):
    """When manager.remove returns a record with killed_pids, the response
    dict must include a non-empty killed_pids list with correct fields."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    killed = [KilledProcessInfo(pid=1234, name="devenv.exe", cmdline=["devenv.exe", "/x"])]
    record = _make_removed_record(killed_pids=killed)
    mgr.remove = MagicMock(return_value=record)

    result = fns["worktree_remove"](worktree_id="wt-id", kill_blocking_processes=True)

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

    result = fns["worktree_remove"](worktree_id="wt-id")

    assert isinstance(result, dict)
    assert "error" not in result
    assert "killed_pids" in result
    assert result["killed_pids"] == []


def test_tool_worktree_remove_dir_locked_raises_valueerror(tmp_path: Path):
    """When manager.remove raises WorktreeDirLockedError (directory still
    locked after kill attempt), the tool must raise ValueError."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(
        side_effect=WorktreeDirLockedError("wt-id", killed=[])
    )

    with pytest.raises(ValueError):
        fns["worktree_remove"](worktree_id="wt-id", kill_blocking_processes=True)


def test_tool_worktree_remove_not_found_still_soft_error(tmp_path: Path):
    """Adding kill_blocking_processes must not break the existing soft-error
    path: WorktreeNotFoundError must still return {"error": ...} dict."""
    from unittest.mock import MagicMock

    mgr, fns = _make_tool_fixtures(tmp_path)
    mgr.remove = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))

    result = fns["worktree_remove"](
        worktree_id="wt-missing", kill_blocking_processes=True
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

    result = fns["worktree_remove"](worktree_id="wt-48")

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

    result = fns["worktree_remove"](worktree_id="wt-48-force", force=True)

    # Call contract: force=True forwarded correctly.
    mgr.remove.assert_called_once_with(
        "wt-48-force", force=True, kill_blocking_processes=False
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

    result = fns["worktree_remove"](worktree_id="wt-48-missing")

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


# ---- Ticket #60: env passthrough and variant selection verification ----


def _write_contract(path: Path, content: str) -> None:
    """Write content to .seretos/worktree-setup.yml under path."""
    seretos = path / ".seretos"
    seretos.mkdir(parents=True, exist_ok=True)
    (seretos / "worktree-setup.yml").write_text(content, encoding="utf-8")


def test_tool_worktree_start_env_vars_reach_child(tmp_path: Path):
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
    fn = mcp._tool_manager._tools["worktree_start"].fn

    captured: dict = {}

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd):
        captured["env"] = env
        # Return the record with status updated to "running" so the tool succeeds.
        record.status = "running"
        record.pids = {role: 99999}
        return record

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        fn(worktree_id=worktree_id)

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


def test_tool_worktree_start_variant_selects_correct_step(tmp_path: Path):
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
    fn = mcp._tool_manager._tools["worktree_start"].fn

    captured: dict = {}

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd):
        captured["cmd"] = cmd
        record.status = "running"
        record.pids = {role: 99999}
        return record

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        fn(worktree_id=worktree_id, variant="worker")

    assert "cmd" in captured, "_lifecycle_start was not called"
    cmd_str = " ".join(captured["cmd"])
    assert "start-worker.sh" in cmd_str, (
        f"Expected 'start-worker.sh' in cmd, got: {captured['cmd']!r}"
    )
    assert "start-web.sh" not in cmd_str, (
        f"Expected 'start-web.sh' NOT in cmd when variant='worker', got: {captured['cmd']!r}"
    )


# ---- Ticket #60: worktree_get setup_status enrichment ----


@pytest.mark.parametrize(
    "status,expected_setup_status",
    [
        ("running", "running"),
        ("ready", "ready"),
        ("stopped", "unknown"),
        ("created", "unknown"),
        ("setup_failed", "failed"),
    ],
)
def test_worktree_get_setup_status_derived_from_status(
    tmp_path: Path, status: str, expected_setup_status: str
):
    """worktree_get must derive setup_status from the record's status field.

    Covers: running->running, ready->ready, stopped->unknown, created->unknown, setup_failed->failed.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    store_root = tmp_path / "store"
    state = InMemoryStateStore()
    record = WorktreeRecord(
        id="wt-get-status-test",
        repo_root="/r",
        branch="b",
        path="/p",
        status=status,
    )
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["worktree_get"].fn

    result = fn(worktree_id="wt-get-status-test")

    assert "error" not in result, f"Unexpected error: {result}"
    assert result.get("setup_status") == expected_setup_status, (
        f"For status={status!r}: expected setup_status={expected_setup_status!r}, "
        f"got {result.get('setup_status')!r}"
    )


def test_worktree_get_setup_status_present_in_result(tmp_path: Path):
    """setup_status key must always be present in the worktree_get result dict
    (not absent for any record, regardless of status value)."""
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    store_root = tmp_path / "store"
    state = InMemoryStateStore()
    record = WorktreeRecord(
        id="wt-always-has-setup-status",
        repo_root="/r",
        branch="b",
        path="/p",
        status="created",
    )
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=state,
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["worktree_get"].fn

    result = fn(worktree_id="wt-always-has-setup-status")

    assert "setup_status" in result, (
        f"'setup_status' key missing from worktree_get result: {result!r}"
    )


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
