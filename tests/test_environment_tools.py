"""Tests for the ticket #99 tool-surface split: the checkout lifecycle
(`worktree_create`/`worktree_remove`) vs. the environment lifecycle
(`environment_list`/`environment_start`/`environment_stop`), including the
`checkout_path` addressing deviation (D1) and the `scope="all"` fan-out
(D2).

These are the driving (RED-first) tests for the split. Mechanical retargets
of the pre-existing `worktree_start`/`worktree_stop`/`worktree_remove`
coverage live in `test_worktree_tools.py`; this file covers only what's new:
the split tool surface itself, `environment_list`, the `checkout_path`
addressing path, and the hard primary-removal refusal.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    InMemoryStateStore,
    ManagerConfig,
    SetupOutcome,
    WorktreeManager,
    WorktreeRecord,
    YamlStateStore,
    primary_id_for,
)
from worktree_plugin.tools.worktree import register


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(base: Path, name: str = "src-repo") -> Path:
    """Build a real temp git repo with a committed README and a
    non-checked-out `feature/wt` branch that `create()` can check out."""
    repo = base / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def _write_contract(path: Path, content: str) -> None:
    seretos = path / ".seretos"
    seretos.mkdir(parents=True, exist_ok=True)
    (seretos / "worktree-setup.yml").write_text(content, encoding="utf-8")


def _make_orphan(repo: Path, base: Path) -> Path:
    """Create a linked git worktree via a raw ``git worktree add`` subprocess
    call, bypassing ``manager.create()`` entirely so nothing is ever
    persisted to the state store. Simulates a checkout that shows up in
    ``git worktree list --porcelain`` (and thus in ``environment_list`` with
    ``tracked: False``) but was never created through this plugin -- an
    orphan/untracked linked worktree (ticket #113)."""
    orphan_path = base / "orphan-wt"
    _git("worktree", "add", "-b", "orphan-branch", str(orphan_path), cwd=repo)
    return orphan_path


def _make_tool_fixtures(tmp_path: Path) -> Tuple[WorktreeManager, dict, dict]:
    """Return (manager, {name: fn}, {name: Tool}) for the split tool surface,
    against an InMemoryStateStore-backed manager rooted at tmp_path/"store"."""
    store_root = tmp_path / "store"
    mgr = WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    tools = mcp._tool_manager._tools
    fns = {name: t.fn for name, t in tools.items()}
    return mgr, fns, tools


@pytest.fixture
def temp_repo(tmp_path: Path) -> Iterator[Path]:
    repo = _make_repo(tmp_path)
    _git("branch", "feature/wt", cwd=repo)
    yield repo


# ---- R1: the registered tool surface is exactly the split five ----


def test_registered_tool_surface_is_the_split_five(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    assert set(tools.keys()) == {
        "worktree_create",
        "worktree_remove",
        "environment_list",
        "environment_start",
        "environment_stop",
    }


# ---- R2: environment_list scopes to one repo regardless of vantage point ----


def test_environment_list_scopes_to_one_repo_from_either_vantage(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")

    expected_ids = {primary_id_for(repo1), rec1.id}

    subdir = Path(rec1.path) / "subdir"
    subdir.mkdir()

    for path in (str(repo1), rec1.path, str(subdir)):
        result = fns["environment_list"](path=path)
        ids = {e["id"] for e in result}
        assert ids == expected_ids, f"for path={path!r}: got ids {ids}"
        assert rec2.id not in ids


def test_environment_list_invalid_path_raises_valueerror(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    with pytest.raises(ValueError):
        fns["environment_list"](path=str(non_repo))


def test_environment_list_entry_shape(tmp_path: Path, temp_repo: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    mgr.create(str(temp_repo), "feature/wt")

    result = fns["environment_list"](path=str(temp_repo))
    assert result, "expected at least one entry"
    for entry in result:
        for key in (
            "is_current",
            "backing",
            "status",
            "pids",
            "ports",
            "setup_status",
            "setup_outcome",
            "tracked",
        ):
            assert key in entry, f"{key!r} missing from entry: {entry}"
        assert isinstance(entry["setup_outcome"], dict) or entry["setup_outcome"] is None, (
            f"setup_outcome must be a nested dict (via asdict) or None: {entry['setup_outcome']!r}"
        )


# ---- Ticket #117: setup_status derived SOLELY from setup_outcome, never
# from record.status (full decoupling) ----


@pytest.mark.parametrize(
    "status",
    ["created", "running", "ready", "stopped", "setup_failed"],
)
def test_environment_list_setup_status_unknown_without_setup_outcome(
    tmp_path: Path, temp_repo: Path, status: str
):
    """A record with no ``setup_outcome`` (the ``setup:`` hook was never
    reached -- a legacy record, an adopted record, or synthesised entry)
    must report ``"unknown"`` regardless of ``status`` -- even
    ``"setup_failed"``. This is the strict-decoupling/legacy-record case:
    ``status`` must never be consulted as a fallback."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    rec.status = status
    rec.setup_outcome = None
    mgr.state.update(rec)

    result = fns["environment_list"](path=str(temp_repo))
    entry = next(e for e in result if e["id"] == rec.id)
    assert entry["setup_status"] == "unknown"


def test_environment_list_setup_status_completed(tmp_path: Path, temp_repo: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    rec.setup_outcome = SetupOutcome(status="completed", steps_run=2)
    # Prove decoupling: status has since moved on to something unrelated.
    rec.status = "running"
    mgr.state.update(rec)

    result = fns["environment_list"](path=str(temp_repo))
    entry = next(e for e in result if e["id"] == rec.id)
    assert entry["setup_status"] == "completed"


def test_environment_list_setup_status_failed_survives_status_rewrite(
    tmp_path: Path, temp_repo: Path
):
    """The ticket's exact reported symptom: setup_status used to alias the
    overall run status, so a "failed" setup outcome would disappear once
    something else (e.g. a later stop) rewrote record.status. It must
    survive that rewrite."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    rec.setup_outcome = SetupOutcome(
        status="failed",
        message="boom",
        failed_step_index=1,
        failed_step_name="install",
        returncode=1,
    )
    rec.status = "stopped"
    mgr.state.update(rec)

    result = fns["environment_list"](path=str(temp_repo))
    entry = next(e for e in result if e["id"] == rec.id)
    assert entry["setup_status"] == "failed"


def test_environment_list_setup_status_skipped_is_distinct_from_unknown(
    tmp_path: Path
):
    repo_a = _make_repo(tmp_path, "repo-a")
    _git("branch", "feature/a", cwd=repo_a)
    repo_b = _make_repo(tmp_path, "repo-b")
    _git("branch", "feature/b", cwd=repo_b)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec_skipped = mgr.create(str(repo_a), "feature/a")
    rec_skipped.setup_outcome = SetupOutcome(status="skipped", steps_run=0)
    mgr.state.update(rec_skipped)

    rec_unknown = mgr.create(str(repo_b), "feature/b")
    rec_unknown.setup_outcome = None
    mgr.state.update(rec_unknown)

    result_a = fns["environment_list"](path=str(repo_a))
    entry_skipped = next(e for e in result_a if e["id"] == rec_skipped.id)
    assert entry_skipped["setup_status"] == "skipped"

    result_b = fns["environment_list"](path=str(repo_b))
    entry_unknown = next(e for e in result_b if e["id"] == rec_unknown.id)
    assert entry_unknown["setup_status"] == "unknown"


def test_environment_list_setup_status_decoupling_holds_under_scope_all(
    tmp_path: Path
):
    """The setup_outcome-based derivation isn't scope-local: it must hold
    identically whether an entry is listed directly (scope="repo") or
    fanned out from another repo's vantage point (scope="all")."""
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec1.setup_outcome = SetupOutcome(status="failed", message="boom")
    rec1.status = "stopped"
    mgr.state.update(rec1)

    rec2 = mgr.create(str(repo2), "feature/wt2")
    rec2.setup_outcome = SetupOutcome(status="completed", steps_run=1)
    rec2.status = "running"
    mgr.state.update(rec2)

    result_all = fns["environment_list"](path=str(repo1), scope="all")
    entry1 = next(e for e in result_all if e["id"] == rec1.id)
    entry2 = next(e for e in result_all if e["id"] == rec2.id)
    assert entry1["setup_status"] == "failed"
    assert entry2["setup_status"] == "completed"


# ---- R3: the primary is synthesised without ever writing state ----


def test_environment_list_synthesises_primary_without_writing_state(
    tmp_path: Path, temp_repo: Path
):
    state_dir = tmp_path / "state"
    state = YamlStateStore(state_dir=state_dir)
    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"), state=state
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fns = {name: t.fn for name, t in mcp._tool_manager._tools.items()}

    state_file = state_dir / "state.yaml"
    before = state_file.read_bytes() if state_file.exists() else None

    result1 = fns["environment_list"](path=str(temp_repo))
    result2 = fns["environment_list"](path=str(temp_repo))

    after = state_file.read_bytes() if state_file.exists() else None
    assert before == after, "environment_list must never write state.yaml"
    assert result1 == result2, "listing twice must be idempotent"

    primary_entries = [e for e in result1 if e["backing"] == "primary"]
    assert len(primary_entries) == 1
    entry = primary_entries[0]
    assert entry["tracked"] is False
    assert entry["id"] == primary_id_for(temp_repo)
    assert mgr.state.list() == [], "no primary record must be persisted by listing alone"

    # The id round-trips: once materialised by a real start, the same id
    # reappears, now tracked.
    started = fns["environment_start"](checkout_path=str(temp_repo))
    assert "error" not in started
    result3 = fns["environment_list"](path=str(temp_repo))
    primary_entry3 = next(e for e in result3 if e["backing"] == "primary")
    assert primary_entry3["id"] == primary_id_for(temp_repo)
    assert primary_entry3["tracked"] is True


# ---- R4: is_current flips with the vantage path ----


def test_environment_list_is_current_flips_with_vantage(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    subdir = Path(rec.path) / "sub"
    subdir.mkdir()

    result_root = fns["environment_list"](path=str(temp_repo))
    primary = next(e for e in result_root if e["backing"] == "primary")
    wt = next(e for e in result_root if e["id"] == rec.id)
    assert primary["is_current"] is True
    assert wt["is_current"] is False
    assert sum(1 for e in result_root if e["is_current"]) == 1

    result_wt = fns["environment_list"](path=rec.path)
    primary2 = next(e for e in result_wt if e["backing"] == "primary")
    wt2 = next(e for e in result_wt if e["id"] == rec.id)
    assert primary2["is_current"] is False
    assert wt2["is_current"] is True
    assert sum(1 for e in result_wt if e["is_current"]) == 1

    result_sub = fns["environment_list"](path=str(subdir))
    wt3 = next(e for e in result_sub if e["id"] == rec.id)
    assert wt3["is_current"] is True
    assert sum(1 for e in result_sub if e["is_current"]) == 1


# ---- R5: environment_list requires path; D2 scope="all" fan-out ----


def test_environment_list_requires_path(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    assert "path" in tools["environment_list"].parameters.get("required", [])

    with pytest.raises(TypeError):
        fns["environment_list"]()


def test_environment_list_scope_all_crosses_repos(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")

    result_repo_scope = fns["environment_list"](path=str(repo1), scope="repo")
    ids_repo = {e["id"] for e in result_repo_scope}
    assert rec2.id not in ids_repo

    result_all = fns["environment_list"](path=str(repo1), scope="all")
    ids_all = {e["id"] for e in result_all}
    assert rec1.id in ids_all
    assert rec2.id in ids_all


def test_environment_list_scope_all_deduplicates_current_repo(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/a", cwd=repo1)
    _git("branch", "feature/b", cwd=repo1)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec_a = mgr.create(str(repo1), "feature/a")
    rec_b = mgr.create(str(repo1), "feature/b")

    result = fns["environment_list"](path=str(repo1), scope="all")
    ids = [e["id"] for e in result]
    assert len(ids) == len(set(ids)), f"duplicate entries in scope=all result: {ids}"
    assert rec_a.id in ids and rec_b.id in ids


def test_environment_list_scope_all_has_single_is_current(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    mgr.create(str(repo1), "feature/wt1")
    mgr.create(str(repo2), "feature/wt2")

    result = fns["environment_list"](path=str(repo1), scope="all")
    current = [e for e in result if e["is_current"]]
    assert len(current) == 1
    assert current[0]["backing"] == "primary"
    assert Path(current[0]["path"]).resolve() == repo1.resolve()


def test_environment_list_scope_all_entry_shape_matches_repo_scope(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    mgr.create(str(repo1), "feature/wt1")
    mgr.create(str(repo2), "feature/wt2")

    repo_result = fns["environment_list"](path=str(repo1), scope="repo")
    all_result = fns["environment_list"](path=str(repo1), scope="all")

    expected_keys = set(repo_result[0].keys())
    for entry in all_result:
        assert set(entry.keys()) == expected_keys, (
            f"entry shape mismatch under scope=all: {entry.keys()} != {expected_keys}"
        )


def _force_rmtree(path: Path) -> None:
    """``shutil.rmtree`` that tolerates Windows read-only files (git objects
    are written read-only), clearing the bit and retrying on failure."""
    import os
    import shutil
    import stat

    def _on_error(func, target, exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_on_error)


def test_environment_list_scope_all_skips_stale_repo_root(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")

    # Wipe repo2's clone entirely -- its root becomes stale/invalid, but
    # rec2's own worktree checkout (under the store) is untouched.
    _force_rmtree(repo2)

    result = fns["environment_list"](path=str(repo1), scope="all")
    ids = {e["id"] for e in result}
    assert rec1.id in ids
    assert rec2.id not in ids


def test_environment_list_unknown_scope_raises_valueerror(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    with pytest.raises(ValueError):
        fns["environment_list"](path=str(temp_repo), scope="bogus")


# ---- R6: worktree_remove refuses a primary, even with force=True ----


def test_worktree_remove_primary_raises_even_with_force(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    started = fns["environment_start"](checkout_path=str(temp_repo))
    assert "error" not in started
    primary_id = started["id"]

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id=primary_id)
    msg = str(excinfo.value)
    assert "primary" in msg
    assert "backing" in msg

    with pytest.raises(ValueError) as excinfo2:
        fns["worktree_remove"](environment_id=primary_id, force=True)
    msg2 = str(excinfo2.value)
    assert "primary" in msg2
    assert "backing" in msg2

    # The primary checkout was never touched.
    assert temp_repo.exists()


def test_worktree_remove_unknown_id_still_soft_error(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    result = fns["worktree_remove"](environment_id="definitely-unknown-99999")
    assert isinstance(result, dict)
    assert "error" in result
    # An ordinary (non-untracked-shaped) unknown id gets the original bare
    # format -- no raw engine-exception suffix, since the engine's plain
    # "No worktree tracked with id '...'" text carries no remedy hint worth
    # surfacing here. This preserves backward compatibility for callers who
    # may depend on the exact bare-error string shape (ticket #113 review
    # fix).
    assert result["error"] == "environment 'definitely-unknown-99999' not found"
    assert "tracked with id" not in result["error"]
    # Ticket #112: additive machine-readable code alongside the unchanged
    # error text above.
    assert result["code"] == "not_found"


# ---- Ticket #113: untracked orphan recovery via checkout_path ----
#
# An orphaned/untracked linked worktree shows up in `environment_list` with
# a well-formed-looking id (`<slug>-untracked-<8hex>`), but that id is a
# one-way hash of the checkout path, not a state-store key -- it can never
# resolve through `worktree_remove(environment_id=...)`. `checkout_path` is
# the only way to address it. `manager.remove()` already supports this at
# the engine layer (v0.3.2); these tests drive `worktree_remove`'s
# `checkout_path` forwarding.


def test_worktree_remove_untracked_orphan_by_checkout_path(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    orphan_path = _make_orphan(temp_repo, tmp_path)

    result = fns["worktree_remove"](checkout_path=str(orphan_path))

    assert "error" not in result
    assert re.search(r"-untracked-[0-9a-f]{8}$", result["id"])
    assert result["status"] == "removed"
    assert not orphan_path.exists()
    assert mgr.state.list() == []


def test_worktree_remove_orphan_leaves_branch_intact(tmp_path: Path, temp_repo: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    orphan_path = _make_orphan(temp_repo, tmp_path)

    result = fns["worktree_remove"](checkout_path=str(orphan_path), force=True)

    assert "error" not in result
    branches = subprocess.run(
        ["git", "branch", "--list", "orphan-branch"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "orphan-branch" in branches


def test_worktree_remove_by_checkout_path_on_tracked_worktree(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    assert mgr.state.list() == [rec]

    result = fns["worktree_remove"](checkout_path=rec.path)

    assert "error" not in result
    assert result["id"] == rec.id
    assert result["status"] == "removed"
    assert mgr.state.list() == []


def test_worktree_remove_checkout_path_and_id_mismatch_raises_valueerror(
    tmp_path: Path
):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    mgr.create(str(repo2), "feature/wt2")

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id=rec1.id, checkout_path=str(repo2))

    # The message must be re-worded to name the wrapper's own
    # `environment_id` parameter, not the engine-internal `worktree_id`
    # (ticket #119).
    msg = str(excinfo.value)
    assert "resolved to id" in msg
    assert "environment_id" in msg
    assert "worktree_id" not in msg

    # Neither worktree was touched by the failed, mismatched call.
    assert Path(rec1.path).exists()


def test_worktree_remove_with_neither_target_raises_valueerror(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    with pytest.raises(ValueError):
        fns["worktree_remove"]()


def test_worktree_remove_missing_target_error_names_environment_id(tmp_path: Path):
    """Ticket #119: the ValueError raised when neither environment_id nor
    checkout_path is given must be re-worded to name worktree_remove's own
    parameters and itself by name -- not the engine-internal `worktree_id`
    parameter or engine-API vocabulary (start()/stop()/remove())."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"]()

    msg = str(excinfo.value)
    assert "worktree_remove" in msg
    assert "environment_id" in msg
    assert "checkout_path" in msg
    assert "worktree_id" not in msg


def test_worktree_remove_checkout_path_outside_any_repo(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    # Observed engine behaviour: classify_checkout() raises InvalidRepoError
    # (a WorktreeError) before any store/removal logic runs, which the tool
    # wrapper's catch-all `except WorktreeError` maps to a raised
    # ValueError -- not a soft error dict, since this isn't a "target not
    # found" condition but an invalid argument.
    with pytest.raises(ValueError):
        fns["worktree_remove"](checkout_path=str(non_repo))


@pytest.mark.parametrize("pre_start", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_worktree_remove_primary_by_checkout_path_refused_even_unstarted(
    tmp_path: Path, temp_repo: Path, pre_start: bool, force: bool
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    if pre_start:
        started = fns["environment_start"](checkout_path=str(temp_repo))
        assert "error" not in started

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](checkout_path=str(temp_repo), force=force)
    msg = str(excinfo.value)
    assert "primary" in msg
    assert "backing" in msg

    # The primary checkout was never touched.
    assert temp_repo.exists()
    assert (temp_repo / ".git").exists()


def test_worktree_remove_untracked_id_soft_error_names_checkout_path(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    _make_orphan(temp_repo, tmp_path)

    listing = fns["environment_list"](path=str(temp_repo))
    orphan_entry = next(
        e for e in listing if e["backing"] == "worktree" and e["tracked"] is False
    )
    orphan_id = orphan_entry["id"]

    result = fns["worktree_remove"](environment_id=orphan_id)

    assert isinstance(result, dict)
    assert "error" in result
    assert orphan_id in result["error"]
    assert "not found" in result["error"]
    assert "checkout_path" in result["error"]
    # Ticket #112: additive machine-readable code alongside the unchanged
    # error text above.
    assert result["code"] == "not_found"


# ---- R7: environment_start/stop work against both the primary and a
# linked worktree ----


def test_environment_start_stop_against_primary_and_worktree(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")

    assert mgr.state.list() == [rec], "only the linked worktree is tracked so far"

    # ---- Primary leg: addressed by checkout_path (the cold-start path) ----
    start_primary = fns["environment_start"](checkout_path=str(temp_repo))
    assert "error" not in start_primary
    assert start_primary["id"] == primary_id_for(temp_repo)
    assert start_primary["backing"] == "primary"
    assert start_primary["status"] in {"ready", "running"}
    assert any(r.id == start_primary["id"] for r in mgr.state.list()), (
        "the primary record must exist only after start()"
    )

    stop_primary = fns["environment_stop"](checkout_path=str(temp_repo))
    assert "error" not in stop_primary
    assert stop_primary["status"] == "stopped"
    assert "main" not in stop_primary.get("pids", {})

    # ---- Worktree leg: addressed by environment_id ----
    start_wt = fns["environment_start"](environment_id=rec.id)
    assert "error" not in start_wt
    assert start_wt["id"] == rec.id
    assert start_wt["backing"] == "worktree"
    assert start_wt["status"] in {"ready", "running"}

    stop_wt = fns["environment_stop"](environment_id=rec.id)
    assert "error" not in stop_wt
    assert stop_wt["status"] == "stopped"
    assert "main" not in stop_wt.get("pids", {})


def test_environment_start_by_id_after_materialisation(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    first = fns["environment_start"](checkout_path=str(temp_repo))
    assert "error" not in first
    primary_id = first["id"]

    second = fns["environment_start"](environment_id=primary_id)
    assert "error" not in second
    assert second["id"] == primary_id


def test_environment_start_id_and_path_mismatch_raises(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")

    with pytest.raises(ValueError) as excinfo:
        fns["environment_start"](environment_id=rec1.id, checkout_path=str(repo2))

    # The underlying resolution still originates from the engine's
    # CheckoutTargetError, but the tool re-words its text (ticket #119) to
    # name the wrapper's own `environment_id` parameter instead of the
    # engine-internal `worktree_id` -- assert on the distinctive "resolved
    # to id" wording (preserved verbatim by the re-wording) plus the
    # renamed parameter.
    msg = str(excinfo.value)
    assert "resolved to id" in msg
    assert "environment_id" in msg
    assert "worktree_id" not in msg


def test_environment_start_with_neither_target_raises(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    with pytest.raises(ValueError):
        fns["environment_start"]()


def test_environment_start_missing_target_error_names_environment_id(
    tmp_path: Path,
):
    """Ticket #119: same as worktree_remove's missing-target driving test,
    but for environment_start -- must name environment_start itself and
    environment_id/checkout_path, never worktree_id."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        fns["environment_start"]()

    msg = str(excinfo.value)
    assert "environment_start" in msg
    assert "environment_id" in msg
    assert "checkout_path" in msg
    assert "worktree_id" not in msg


def test_environment_stop_unmaterialised_primary_soft_error(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_stop"](checkout_path=str(temp_repo))

    assert isinstance(result, dict)
    assert "error" in result
    assert "not found" in result["error"]
    assert mgr.state.list() == [], "stop() must never materialise a primary record"


def test_environment_stop_missing_target_error_names_environment_id(tmp_path: Path):
    """Ticket #119: same as worktree_remove's missing-target driving test,
    but for environment_stop -- must name environment_stop itself and
    environment_id/checkout_path, never worktree_id."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        fns["environment_stop"]()

    msg = str(excinfo.value)
    assert "environment_stop" in msg
    assert "environment_id" in msg
    assert "checkout_path" in msg
    assert "worktree_id" not in msg


def test_environment_stop_id_and_path_mismatch_error_names_environment_id(
    tmp_path: Path,
):
    """Ticket #119: environment_stop's id/checkout_path mismatch error must
    also be re-worded to name environment_id, not worktree_id."""
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")

    with pytest.raises(ValueError) as excinfo:
        fns["environment_stop"](environment_id=rec1.id, checkout_path=str(repo2))

    msg = str(excinfo.value)
    assert "resolved to id" in msg
    assert "environment_id" in msg
    assert "worktree_id" not in msg


def test_environment_start_contract_variant_and_env_injection_unchanged(
    tmp_path: Path,
):
    """AC8 evidence: contract-driven start (real .seretos/worktree-setup.yml,
    unchanged v1 schema) still selects the correct named `start:` step and
    injects WORKTREE_ID/WORKTREE_PATH/WORKTREE_PORT_* env vars, unaffected
    by the environment_* tool-surface split.
    """
    wt_path = tmp_path / "store" / "repo" / "wt-ac8-test-12345678"
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

    worktree_id = "wt-ac8-test-12345678"
    record = WorktreeRecord(
        id=worktree_id,
        repo_root=str(repo_root),
        branch="feature/ac8-test",
        path=str(wt_path),
        status="created",
        ports={"web": 8080, "db": 5432},
    )
    state = InMemoryStateStore()
    state.add(record)

    mgr = WorktreeManager(
        config=ManagerConfig(store_root=tmp_path / "store"), state=state
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools["environment_start"].fn

    captured: dict = {}

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd, variant=None):
        captured["cmd"] = cmd
        captured["env"] = env
        record.status = "running"
        record.pids = {role: 99999}
        return record

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        result = fn(environment_id=worktree_id, variant="worker")

    assert "error" not in result
    cmd_str = " ".join(captured["cmd"])
    assert "start-worker.sh" in cmd_str
    assert "start-web.sh" not in cmd_str

    env = captured["env"]
    assert env.get("WORKTREE_ID") == worktree_id
    assert env.get("WORKTREE_PATH") == str(wt_path)
    assert env.get("WORKTREE_PORT_WEB") == "8080"
    assert env.get("WORKTREE_PORT_DB") == "5432"


# ---- Ticket #103 ----
# environment_start's "nothing ran" outcomes -- no contract at all,
# isolation: none, a contract present but with no start: steps, and a
# contract misplaced in the checkout instead of repo_root -- must be
# distinguishable from a real start, and the contract schema must be
# discoverable from the tool docstrings alone.


def test_environment_start_without_contract_reports_no_contract(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is False
    assert result["steps_run"] == 0
    assert result["no_op_reason"] == "no-contract"
    assert result["contract_path"].endswith(".seretos/worktree-setup.yml")
    assert str(temp_repo.resolve()).replace("\\", "/") in result["contract_path"].replace(
        "\\", "/"
    )
    # Non-breaking: the pre-existing fields are unchanged.
    assert result["status"] == "ready"
    assert result["pids"] == {}


def test_environment_start_with_start_step_reports_contract_read(
    tmp_path: Path, temp_repo: Path
):
    _write_contract(
        temp_repo,
        "version: 1\nisolation: full\nstart:\n  - run: echo hi\n",
    )
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd, variant=None):
        rec = store.get(worktree_id)
        rec.status = "running"
        rec.pids = {role: 4242}
        store.update(rec)
        return rec

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["contract_path"].endswith(".seretos/worktree-setup.yml")
    assert result["contract_isolation"] == "full"
    assert result["steps_run"] == 1
    assert result["no_op_reason"] is None
    assert result["status"] == "running"


@pytest.mark.parametrize("engine_status", ["exited", "running"])
def test_environment_start_real_start_reports_steps_run_for_any_status(
    tmp_path: Path, temp_repo: Path, engine_status: str
):
    """Ticket #103 regression, determinised for #107.

    The engine's `_lifecycle_start` sets `record.pids[role]`
    unconditionally, but `record.status` only reaches "running" if the
    process survives the engine's ~0.25s early-exit wait; a fast-exiting
    command (`echo hi`) instead leaves `status == "exited"`. Before the
    #103 fix, `_contract_diagnostics` required `status == "running"` to
    report a real start, so a real start whose process exited quickly was
    misreported as `steps_run == 0`, `no_op_reason == "no-start-steps"` --
    indistinguishable from the true no-op case #103 exists to separate out.

    Ticket #107: the original version of this test raced a real `echo hi`
    against that 0.25s wait and asserted `status == "exited"` as a
    precondition, which flaked on `windows-latest` when the process
    outlived the wait. The state is now injected rather than raced for, so
    BOTH states are exercised deterministically on every run.
    """
    _write_contract(
        temp_repo,
        "version: 1\nisolation: full\nstart:\n  - run: echo hi\n",
    )
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd, variant=None):
        rec = store.get(worktree_id)
        # Mirrors the engine: pids is set unconditionally; only status and
        # returncode depend on surviving the early-exit wait.
        rec.pids = {role: 4242}
        rec.status = engine_status
        rec.returncode = 0 if engine_status == "exited" else None
        store.update(rec)
        return rec

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    # Sanity check that the injected state actually reached the response --
    # this is the relocated guard: it can no longer flake, but the "exited"
    # case is still guaranteed to be exercised on every run.
    assert result["status"] == engine_status
    assert result["pids"] == {"main": 4242}
    assert result["contract_found"] is True
    assert result["contract_isolation"] == "full"
    assert result["steps_run"] == 1
    assert result["no_op_reason"] is None


def test_environment_start_real_process_start_reports_real_start_e2e(
    tmp_path: Path, temp_repo: Path
):
    """End-to-end companion to the parametrised test above: a real,
    unmocked start of a real `echo hi` process, proving the diagnostics
    contract holds against the actual engine and not only against a
    patched `_lifecycle_start`.

    Ticket #107: every timing-dependent assertion is deliberately absent.
    Whether the process survives the engine's ~0.25s early-exit wait is a
    wall-clock race, so `status` is only asserted to be one of the two
    legitimate outcomes -- never pinned to either. The assertions that
    matter (`pids` populated, `steps_run == 1`, `no_op_reason is None`)
    key on `record.pids`, which the engine sets unconditionally, so they
    hold in both outcomes. The state-specific regression guard lives in
    the parametrised test above, not here.
    """
    _write_contract(
        temp_repo,
        "version: 1\nisolation: full\nstart:\n  - run: echo hi\n",
    )
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["contract_isolation"] == "full"
    assert result["pids"], "a real process should have been spawned"
    assert result["steps_run"] == 1
    assert result["no_op_reason"] is None
    assert result["status"] in {"exited", "running"}, (
        "both outcomes are legitimate -- which one occurs is a wall-clock "
        "race against the engine's early-exit wait (ticket #107); the "
        "state-specific assertions live in the parametrised test above"
    )


def test_environment_start_isolation_none_reports_isolation_none(
    tmp_path: Path, temp_repo: Path
):
    _write_contract(temp_repo, "version: 1\nisolation: none\n")
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["contract_isolation"] == "none"
    assert result["steps_run"] == 0
    assert result["no_op_reason"] == "isolation-none"


def test_environment_start_contract_without_start_block_reports_no_start_steps(
    tmp_path: Path, temp_repo: Path
):
    _write_contract(
        temp_repo,
        "version: 1\nisolation: full\nsetup:\n  - run: echo setup\n",
    )
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["contract_isolation"] == "full"
    assert result["steps_run"] == 0
    assert result["no_op_reason"] == "no-start-steps"


def test_environment_start_contract_only_in_checkout_reports_misplaced(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    # Contract placed only in the linked worktree checkout, not repo_root.
    _write_contract(
        Path(rec.path), "version: 1\nisolation: full\nstart:\n  - run: echo hi\n"
    )

    result = fns["environment_start"](environment_id=rec.id)

    assert "error" not in result
    assert result["contract_found"] is False
    assert result["no_op_reason"] == "contract-misplaced"
    assert result["steps_run"] == 0


def test_environment_start_primary_same_path_never_reports_misplaced(
    tmp_path: Path, temp_repo: Path
):
    """Primary case: record.path == record.repo_root. A contract there is
    found normally and must never be misreported as 'misplaced'."""
    _write_contract(temp_repo, "version: 1\nisolation: none\n")
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["no_op_reason"] != "contract-misplaced"


def test_environment_start_diagnostics_degrade_for_unreachable_repo_root(
    tmp_path: Path,
):
    """A nonexistent repo_root/path pair (`/r`, `/p`) hits the ordinary
    "no-contract" branch cleanly -- `Path.exists()` returns False rather
    than raising -- so this is NOT the `except (OSError, ContractError)`
    degrade path (see `test_environment_start_contract_unreadable_degrades_
    without_raising` below for that). This test only proves that an
    unreachable repo_root doesn't crash the diagnostics helper and lands on
    the correct, specific `no_op_reason`, not merely that the key exists.
    """
    from unittest.mock import MagicMock

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    record = WorktreeRecord(
        id="wt-id",
        repo_root="/r",
        branch="b",
        path="/p",
        status="running",
        pids={"main": 12345},
    )
    mgr.start = MagicMock(return_value=record)

    result = fns["environment_start"](environment_id="wt-id")

    assert "error" not in result
    assert result["contract_found"] is False
    assert result["no_op_reason"] == "no-contract"


def test_environment_start_contract_unreadable_degrades_without_raising(
    tmp_path: Path, temp_repo: Path
):
    """Exercises `_contract_diagnostics`'s `except (OSError, ContractError)`
    branch for real, without weakening `environment_start`'s exception
    handling (out of scope -- see the ticket).

    The engine's `manager.start()` unconditionally re-reads and validates
    the contract itself (`lib_python_worktree.core.manager.start`) before
    `_contract_diagnostics` ever runs its own second read -- confirmed by
    reproducing this directly: writing a genuinely malformed-YAML contract
    (`isolation: [full` -- an unterminated flow sequence) and calling
    `environment_start` raises `ContractError` straight out of
    `manager.start()`, never reaching `_contract_diagnostics` at all. That
    `ContractError` is not a `WorktreeError`, so it escapes `environment_
    start`'s `except (WorktreeError, ProcessLifecycleError)` -- exactly the
    known, out-of-scope gap the ticket names. So a literally-malformed file
    on disk cannot reach the helper's own try/except through a real call at
    all; it always blows up one frame earlier, in the engine.

    What CAN reach the helper's except clause is the TOCTOU window its own
    docstring documents: `_contract_diagnostics` performs a *second*,
    independent read of the same contract file after `manager.start()`'s
    read already succeeded. Simulating that second read failing --
    independently of the first, real, successful read `manager.start()`
    performs -- reproduces exactly that race deterministically.

    Two sub-cases, both reaching the same except clause but with different
    `record.pids` state left over from `manager.start()`'s own successful
    read/spawn (ticket #103 fix: `record.pids` is the authority here, not
    the failed second read):

    1. A real process genuinely spawned (`role in record.pids`) before the
       second read fails -- this must NOT be misreported as a no-op:
       `steps_run == 1`, `no_op_reason is None`.
    2. No process spawned at all (`role not in record.pids`, e.g. a
       contract with no `start:` steps) when the second read fails --
       this is a genuine no-op: `steps_run == 0`, `no_op_reason ==
       "contract-unreadable"`.

    Both sub-cases keep `contract_found is True` (the file did exist) and
    `contract_isolation is None` (the second, failed read never parsed an
    isolation value), and neither call raises.
    """
    from lib_python_worktree import ContractError

    # Sub-case 1: a real start happened -- `role` lands in `record.pids` --
    # and only the diagnostics helper's own second read fails.
    _write_contract(
        temp_repo,
        "version: 1\nisolation: full\nstart:\n  - run: echo hi\n",
    )
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    with patch(
        "worktree_plugin.tools.worktree.load_contract",
        side_effect=ContractError("boom"),
    ):
        result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["contract_isolation"] is None
    assert result["steps_run"] == 1
    assert result["no_op_reason"] is None


def test_environment_start_contract_unreadable_with_no_spawn_stays_no_op(
    tmp_path: Path, temp_repo: Path
):
    """Sub-case 2 of the except-clause degrade (see the sibling test's
    docstring): when `manager.start()`'s own successful read found no
    `start:` steps to run at all, nothing was ever spawned for `role`, so
    `role not in record.pids`. If the diagnostics helper's own second read
    then fails, this is a genuine no-op -- not a real start -- and must
    still report `steps_run == 0`, `no_op_reason == "contract-unreadable"`.
    """
    from lib_python_worktree import ContractError

    _write_contract(
        temp_repo,
        "version: 1\nisolation: full\n",
    )
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    with patch(
        "worktree_plugin.tools.worktree.load_contract",
        side_effect=ContractError("boom"),
    ):
        result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in result
    assert result["contract_found"] is True
    assert result["contract_isolation"] is None
    assert result["steps_run"] == 0
    assert result["no_op_reason"] == "contract-unreadable"


def test_environment_start_soft_errors_carry_no_diagnostic_keys(tmp_path: Path):
    from unittest.mock import MagicMock

    from lib_python_worktree import ProcessAlreadyRunningError, WorktreeNotFoundError

    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    mgr.start = MagicMock(side_effect=WorktreeNotFoundError("wt-missing"))
    not_found = fns["environment_start"](environment_id="wt-missing")
    assert "error" in not_found
    assert "contract_found" not in not_found

    mgr.start = MagicMock(
        side_effect=ProcessAlreadyRunningError("wt-id", "main", 12345)
    )
    already_running = fns["environment_start"](environment_id="wt-id")
    assert "error" in already_running
    assert "contract_found" not in already_running


def test_worktree_create_docstring_documents_contract_schema(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["worktree_create"].__doc__ or ""

    for token in (
        ".seretos/worktree-setup.yml",
        "repo_root",
        "setup:",
        "run:",
        "version:",
        "isolation:",
    ):
        assert token in doc, f"worktree_create docstring missing {token!r}"


def test_worktree_remove_docstring_documents_checkout_path_and_orphan_case(
    tmp_path: Path,
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["worktree_remove"].__doc__ or ""

    for token in (
        "checkout_path",
        "-untracked-",
        "untracked",
    ):
        assert token in doc, f"worktree_remove docstring missing {token!r}"


def test_environment_start_docstring_lists_all_contract_keys(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_start"].__doc__ or ""

    for token in (
        "setup:",
        "start:",
        "stop:",
        "teardown:",
        "ports:",
        "version:",
        "isolation:",
        "default",
        "contract_found",
        "contract_path",
        "contract_isolation",
        "steps_run",
        "no_op_reason",
    ):
        assert token in doc, f"environment_start docstring missing {token!r}"


# ---- Ticket #118: role vs variant addressing model ----
#
# environment_start's "role" and "variant" parameters are independent
# (role is the tracking/addressing key a pid is filed under; variant only
# selects which contract start: step ran) but were previously documented in
# total isolation from each other, with no explanation of how they interact.
# This block also drives environment_stop's new `variant` parameter (ticket
# #104's engine-level `stop(variant=...)` resolution, newly exposed through
# the MCP tool surface), including the `role=None` sentinel fix required to
# forward "role omitted" vs "role explicitly main" correctly.


def test_environment_start_docstring_explains_role_vs_variant(tmp_path: Path):
    """The docstring must explain, in one connected section, that `role`
    defaults to "main" regardless of which `variant` is requested -- not
    just document each parameter in isolation."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_start"].__doc__ or ""

    assert "regardless" in doc.lower()

    # Both "role" and "variant" must appear together within the same
    # explanatory section (not just anywhere in the docstring, which the
    # pre-existing isolated parameter entries would already satisfy).
    idx = doc.lower().find("regardless")
    assert idx != -1
    window = doc[max(0, idx - 400) : idx + 400]
    assert "role" in window
    assert "variant" in window


def test_environment_stop_docstring_documents_role_vs_variant(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_stop"].__doc__ or ""

    assert "variant" in doc


def _seed_record(
    mgr: WorktreeManager,
    *,
    pids: dict,
    variants: dict,
    repo_root: str,
    path: str,
) -> WorktreeRecord:
    record = WorktreeRecord(
        id="wt-118-test",
        repo_root=repo_root,
        branch="feature/wt",
        path=path,
        status="running",
        pids=dict(pids),
        variants=dict(variants),
    )
    mgr.state.add(record)
    return record


def test_environment_stop_variant_resolves_to_started_role(
    tmp_path: Path, temp_repo: Path
):
    """BR1+BR2 combined: calling environment_stop(variant=...) with NO role
    given must resolve to the role that was actually started with that
    variant (here "web", not the "main" default) -- this simultaneously
    exercises the new variant-resolution behavior and the role=None sentinel
    fix (forwarding a hardcoded "main" would have broken this)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    record = _seed_record(
        mgr,
        pids={"web": 99999},
        variants={"web": "gui"},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    captured: dict = {}

    def _fake_lifecycle_stop(worktree_id, *, store, role, timeout, kill_orphans):
        captured["role"] = role
        rec = store.get(worktree_id)
        rec.pids.pop(role, None)
        rec.variants.pop(role, None)
        rec.status = "stopped" if not rec.pids else rec.status
        store.update(rec)
        return rec

    with patch(
        "lib_python_worktree.core.manager._lifecycle_stop",
        side_effect=_fake_lifecycle_stop,
    ):
        result = fns["environment_stop"](environment_id=record.id, variant="gui")

    assert "error" not in result
    assert captured["role"] == "web"
    assert "web" not in result.get("pids", {})


def test_environment_stop_no_role_no_variant_still_stops_main(
    tmp_path: Path, temp_repo: Path
):
    """Back-compat regression: environment_stop(environment_id=...) with
    neither role nor variant given must still stop role="main", exactly as
    before the role=None sentinel change."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    record = _seed_record(
        mgr,
        pids={"main": 12345},
        variants={},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    captured: dict = {}

    def _fake_lifecycle_stop(worktree_id, *, store, role, timeout, kill_orphans):
        captured["role"] = role
        rec = store.get(worktree_id)
        rec.pids.pop(role, None)
        rec.status = "stopped" if not rec.pids else rec.status
        store.update(rec)
        return rec

    with patch(
        "lib_python_worktree.core.manager._lifecycle_stop",
        side_effect=_fake_lifecycle_stop,
    ):
        result = fns["environment_stop"](environment_id=record.id)

    assert "error" not in result
    assert captured["role"] == "main"
    assert "main" not in result.get("pids", {})


_VARIANT_RESOLUTION_FAILURE_CASES = [
    pytest.param(
        {"main": 1},
        {},
        None,
        "nonexistent-variant",
        id="zero-match-typo",
    ),
    pytest.param(
        {"web": 1, "worker": 2},
        {"web": "shared", "worker": "shared"},
        None,
        "shared",
        id="ambiguous-multiple-roles",
    ),
    pytest.param(
        {"web": 1},
        {"web": "gui"},
        "worker",
        "gui",
        id="role-variant-disagreement",
    ),
]


@pytest.mark.parametrize(
    "pids,variants,role,variant",
    _VARIANT_RESOLUTION_FAILURE_CASES,
)
def test_environment_stop_variant_resolution_failure_raises_valueerror(
    tmp_path: Path,
    temp_repo: Path,
    pids: dict,
    variants: dict,
    role,
    variant: str,
):
    """All three of the engine's variant-resolution failure modes (zero-
    match/unknown variant, ambiguous/multiple-match, and role/variant
    disagreement) must raise ValueError uniformly -- there is no soft
    {"code": "not_running"}-style dict for any of them."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    record = _seed_record(
        mgr,
        pids=pids,
        variants=variants,
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    kwargs = {"environment_id": record.id, "variant": variant}
    if role is not None:
        kwargs["role"] = role

    with pytest.raises(ValueError) as excinfo:
        fns["environment_stop"](**kwargs)

    msg = str(excinfo.value)
    # The engine's own message uses role=/variant= vocabulary; the wrapper's
    # hint is appended, never replacing it.
    assert variant in msg
    assert "hint" in msg.lower()
    assert "role=" in msg or "role" in msg.lower()
