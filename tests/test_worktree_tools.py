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

from worktree_plugin.core.manager import (
    BranchNotFoundError,
    DuplicateWorktreeError,
    ManagerConfig,
    WorktreeManager,
    WorktreeNotFoundError,
)
from worktree_plugin.core.state import InMemoryStateStore


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
