"""Integration tests for the W2 core tools.

These exercise real ``git worktree`` operations against a temporary repo, as
required by the planning comment's Verifikation section.
"""

from __future__ import annotations

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
    rec = manager.create(str(temp_repo), "feature/new", base="main")
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
    mgr.start.assert_called_once_with("wt-id", role="main", cwd=None)


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
    mgr.stop.assert_called_once_with("wt-id", role="main", timeout=10.0)


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
    assert call_args.kwargs == {"role": "worker", "cwd": "/custom/cwd"}
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

    mgr.stop.assert_called_once_with("wt-id", role="worker", timeout=5.0)


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
