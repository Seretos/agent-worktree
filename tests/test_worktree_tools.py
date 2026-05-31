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
    ManagerConfig,
    WorktreeManager,
    WorktreeNotFoundError,
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
