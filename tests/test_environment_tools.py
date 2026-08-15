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

import subprocess
from pathlib import Path
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    InMemoryStateStore,
    ManagerConfig,
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
            "tracked",
        ):
            assert key in entry, f"{key!r} missing from entry: {entry}"


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
def test_environment_list_setup_status_derivations(
    tmp_path: Path, temp_repo: Path, status: str, expected_setup_status: str
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")
    rec.status = status
    mgr.state.update(rec)

    result = fns["environment_list"](path=str(temp_repo))
    entry = next(e for e in result if e["id"] == rec.id)
    assert entry["setup_status"] == expected_setup_status


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

    # The message must originate from the engine's CheckoutTargetError, not
    # wrapper-side validation -- assert on its distinctive wording.
    assert "resolved to id" in str(excinfo.value)


def test_environment_start_with_neither_target_raises(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    with pytest.raises(ValueError):
        fns["environment_start"]()


def test_environment_stop_unmaterialised_primary_soft_error(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = fns["environment_stop"](checkout_path=str(temp_repo))

    assert isinstance(result, dict)
    assert "error" in result
    assert "not found" in result["error"]
    assert mgr.state.list() == [], "stop() must never materialise a primary record"


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

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, env, cwd):
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
