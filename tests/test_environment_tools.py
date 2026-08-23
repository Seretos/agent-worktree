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

import base64
import inspect
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    InMemoryStateStore,
    InvalidRepoError,
    ManagerConfig,
    SetupOutcome,
    WorktreeDirLockedError,
    WorktreeManager,
    WorktreeRecord,
    WorktreeRemovalBlockedError,
    YamlStateStore,
    primary_id_for,
)
from lib_python_worktree.core.state import ShadowedContract
from worktree_plugin.tools.worktree import (
    _default_stop_variant,
    _invalid_path_error_text,
    _repo_roots_for_scope_all,
    register,
)


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

    with pytest.raises(ValueError) as excinfo:
        fns["environment_list"](path=str(non_repo))

    # Additional edge-case coverage (ticket #139 Part A), extending this
    # existing test rather than duplicating it: an existing-but-not-a-git-
    # repo directory produces a reason with only ONE `repo_root` token
    # (the engine's "not a git repository: ..." text has no second
    # occurrence), unlike the nonexistent-directory driving test below
    # where the token appears twice.
    msg = str(excinfo.value)
    assert msg.startswith("invalid path '"), f"got: {msg!r}"
    assert re.search(r"\brepo_root\b", msg) is None, f"got: {msg!r}"


def test_environment_list_invalid_target_error_names_the_tool_parameter(
    tmp_path: Path,
):
    """Driving test (ticket #139 Part A): environment_list's own parameter
    is `path`, not `repo_root` -- but today it bare-re-raises the engine's
    `InvalidRepoError` verbatim, which names the engine-internal
    `repo_root`. A NONEXISTENT directory is used deliberately: the engine's
    reason for that case is `f"repo_root does not exist: {path}"`, i.e.
    `repo_root` appears TWICE (once in the message prefix built from
    `exc.repo_root`, once again inside the reason text) -- proving that a
    prefix-only fix would be insufficient; the mechanical `_invalid_path_
    error_text` rewording (already used by worktree_remove/environment_
    start/environment_stop, ticket #123) must catch both occurrences.

    RED (pre-fix): `environment_list`'s `except InvalidRepoError` clause
    re-raises `str(exc)` unchanged, so the message both starts with
    `invalid repo_root '...'` (fails the `invalid path '` prefix check) and
    still contains a bare `repo_root` token in the reason (fails the
    sanitized-absence check).

    Temp-dir trap guard (mandatory per the #123 reviewer's catch): pytest
    bakes this test function's own name into `tmp_path`, so (i) the target
    directory is named neutrally (`not-a-repo`, no `path`/`repo_root`
    substring) and this test's own name contains neither token either;
    (ii) every assertion below sanitises the given path, its `Path.resolve()`
    form, AND the backslash-doubled repr form of both (the message embeds a
    Windows path via `repr()`, which doubles backslashes) out of the message
    before checking for a stray `repo_root` token -- the raw string is never
    asserted on directly.
    """
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    missing_dir = tmp_path / "not-a-repo"  # neutral name; deliberately never created

    with pytest.raises(ValueError) as excinfo:
        fns["environment_list"](path=str(missing_dir))

    msg = str(excinfo.value)
    sanitized = msg
    for token in (
        str(missing_dir),
        str(missing_dir.resolve()),
        str(missing_dir).replace("\\", "\\\\"),
        str(missing_dir.resolve()).replace("\\", "\\\\"),
    ):
        sanitized = sanitized.replace(token, "<DIR>")

    assert re.search(r"\brepo_root\b", sanitized) is None, (
        f"leaked repo_root token in sanitized message: {sanitized!r}"
    )
    assert re.match(r"^invalid path '", msg), f"got: {msg!r}"


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


# ---- Ticket #150: repos allow-list narrows the scope="all" fan-out ----


def test_environment_list_repos_filter_limits_fanout(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)
    repo3 = _make_repo(tmp_path, "repo3")
    _git("branch", "feature/wt3", cwd=repo3)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")
    rec3 = mgr.create(str(repo3), "feature/wt3")

    result = fns["environment_list"](
        path=str(repo1), scope="all", repos=[str(repo2)]
    )
    ids = {e["id"] for e in result}
    assert rec1.id in ids
    assert rec2.id in ids
    assert rec3.id not in ids


def test_environment_list_repos_filter_matches_parent_prefix(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    repo1 = _make_repo(parent, "repoA")
    _git("branch", "feature/a", cwd=repo1)
    repo2 = _make_repo(parent, "repoB")
    _git("branch", "feature/b", cwd=repo2)
    outsider = _make_repo(tmp_path, "outsider")
    _git("branch", "feature/o", cwd=outsider)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/a")
    rec2 = mgr.create(str(repo2), "feature/b")
    rec_outsider = mgr.create(str(outsider), "feature/o")

    result = fns["environment_list"](
        path=str(repo1), scope="all", repos=[str(parent)]
    )
    ids = {e["id"] for e in result}
    assert rec1.id in ids
    assert rec2.id in ids
    assert rec_outsider.id not in ids


def test_environment_list_repos_filter_tolerates_unmatched_entry(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")

    never_tracked = tmp_path / "never-tracked"  # deliberately never created
    result = fns["environment_list"](
        path=str(repo1), scope="all", repos=[str(never_tracked)]
    )
    ids = {e["id"] for e in result}
    repo_scope_ids = {
        e["id"] for e in fns["environment_list"](path=str(repo1), scope="repo")
    }
    assert ids == repo_scope_ids
    assert rec2.id not in ids


def test_environment_list_repos_filter_keeps_current_repo_first_and_single_is_current(
    tmp_path: Path,
):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)
    repo3 = _make_repo(tmp_path, "repo3")
    _git("branch", "feature/wt3", cwd=repo3)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec1 = mgr.create(str(repo1), "feature/wt1")
    rec2 = mgr.create(str(repo2), "feature/wt2")
    rec3 = mgr.create(str(repo3), "feature/wt3")
    repo1_ids = {primary_id_for(repo1), rec1.id}

    result = fns["environment_list"](
        path=str(repo1), scope="all", repos=[str(repo2)]
    )
    # The current repo's own entries are always placed first -- before any
    # fanned-out entry from another repo -- regardless of the repos filter.
    assert result[0]["id"] in repo1_ids
    assert rec3.id not in {e["id"] for e in result}

    current = [e for e in result if e["is_current"]]
    assert len(current) == 1
    assert current[0]["id"] in repo1_ids


def test_environment_list_repos_empty_list_equals_repo_scope(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    mgr.create(str(repo1), "feature/wt1")
    mgr.create(str(repo2), "feature/wt2")

    result_repo = fns["environment_list"](path=str(repo1), scope="repo")
    result_all_empty_repos = fns["environment_list"](
        path=str(repo1), scope="all", repos=[]
    )
    assert {e["id"] for e in result_all_empty_repos} == {
        e["id"] for e in result_repo
    }


def test_environment_list_repos_with_scope_repo_raises_valueerror(
    tmp_path: Path, temp_repo: Path
):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    with pytest.raises(ValueError, match="repos"):
        fns["environment_list"](
            path=str(temp_repo), scope="repo", repos=[str(temp_repo)]
        )

    with pytest.raises(ValueError, match="repos"):
        fns["environment_list"](path=str(temp_repo), scope="repo", repos=[])


def test_repo_roots_for_scope_all_applies_repos_filter(tmp_path: Path):
    repo1 = _make_repo(tmp_path, "repo1")
    _git("branch", "feature/wt1", cwd=repo1)
    repo2 = _make_repo(tmp_path, "repo2")
    _git("branch", "feature/wt2", cwd=repo2)
    repo3 = _make_repo(tmp_path, "repo3")
    _git("branch", "feature/wt3", cwd=repo3)

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    mgr.create(str(repo1), "feature/wt1")
    mgr.create(str(repo2), "feature/wt2")
    mgr.create(str(repo3), "feature/wt3")

    filtered = _repo_roots_for_scope_all(mgr, str(repo1), repos=[str(repo2)])
    filtered_resolved = {Path(r).resolve() for r in filtered}
    assert repo1.resolve() in filtered_resolved
    assert repo2.resolve() in filtered_resolved
    assert repo3.resolve() not in filtered_resolved

    # No-regression arm: repos=None must remain fully unfiltered.
    unfiltered = _repo_roots_for_scope_all(mgr, str(repo1), repos=None)
    unfiltered_resolved = {Path(r).resolve() for r in unfiltered}
    assert repo1.resolve() in unfiltered_resolved
    assert repo2.resolve() in unfiltered_resolved
    assert repo3.resolve() in unfiltered_resolved


def test_environment_list_repos_is_optional_in_tool_schema(tmp_path: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    schema = tools["environment_list"].parameters
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    assert "repos" in properties
    assert "repos" not in required
    assert "path" in required
    assert "scope" not in required


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

    result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](checkout_path=str(orphan_path)),
        what="worktree_remove(orphan by checkout_path)",
    )

    assert "error" not in result
    assert re.search(r"-untracked-[0-9a-f]{8}$", result["id"])
    assert result["status"] == "removed"
    assert not orphan_path.exists()
    assert mgr.state.list() == []


def test_worktree_remove_orphan_leaves_branch_intact(tmp_path: Path, temp_repo: Path):
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    orphan_path = _make_orphan(temp_repo, tmp_path)

    result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](checkout_path=str(orphan_path), force=True),
        what="worktree_remove(orphan, force=True)",
    )

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

    result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](checkout_path=rec.path),
        what="worktree_remove(tracked worktree by checkout_path)",
    )

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
    """Ticket #123: classify_checkout() raises InvalidRepoError (a
    WorktreeError) before any store/removal logic runs when checkout_path
    isn't a usable git repository. Before the fix, the wrapper's catch-all
    `except WorktreeError` passed the engine's message through verbatim,
    which names the engine-internal `repo_root` parameter even though the
    caller passed `checkout_path` -- this tool has no `repo_root`
    parameter at all. The re-worded message must name `checkout_path`
    instead, never leak `repo_root`, and still carry the offending path
    (as its basename, since `!r` doubles backslashes on Windows) and the
    rejection reason."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](checkout_path=str(non_repo))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert non_repo.name in msg
    assert "not a git repository" in msg


def test_worktree_remove_checkout_path_nonexistent(tmp_path: Path):
    """Ticket #123 edge case: a checkout_path that does not exist on disk
    produces reason text `repo_root does not exist: ...` from the engine --
    this proves the fix's token substitution rewrites the reason *body*,
    not just the `invalid repo_root ...:` prefix (a prefix-only fix would
    still leak `repo_root` here)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    missing = tmp_path / "missing-checkout"

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](checkout_path=str(missing))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert missing.name in msg
    assert "does not exist" in msg


def test_worktree_remove_checkout_path_is_a_file(tmp_path: Path):
    """Ticket #123 edge case: a checkout_path pointing at a file (not a
    directory) produces reason text `repo_root is not a directory: ...`."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("hi", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](checkout_path=str(a_file))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert a_file.name in msg
    assert "not a directory" in msg


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


def test_environment_start_invalid_checkout_path_names_checkout_path(
    tmp_path: Path,
):
    """Ticket #123: an unusable checkout_path leaks via classify_checkout()'s
    InvalidRepoError, caught by environment_start's catch-all
    `except (WorktreeError, ProcessLifecycleError)` before the fix -- which
    passes the engine's `repo_root`-naming text straight through even
    though environment_start has no `repo_root` parameter. The re-worded
    message must name `checkout_path`, never leak `repo_root`, and still
    carry the offending path's basename and the rejection reason."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    with pytest.raises(ValueError) as excinfo:
        fns["environment_start"](checkout_path=str(non_repo))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert non_repo.name in msg
    assert "not a git repository" in msg


def test_environment_start_invalid_checkout_path_nonexistent(tmp_path: Path):
    """Ticket #123 edge case: proves the reason-body rewrite, not just the
    `invalid repo_root ...:` prefix -- same rationale as
    test_worktree_remove_checkout_path_nonexistent."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    missing = tmp_path / "missing-checkout"

    with pytest.raises(ValueError) as excinfo:
        fns["environment_start"](checkout_path=str(missing))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert missing.name in msg
    assert "does not exist" in msg


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


def test_environment_stop_invalid_checkout_path_names_checkout_path(
    tmp_path: Path,
):
    """Ticket #123: empirically confirmed (repro script) that
    environment_stop raises here rather than returning a soft not-found
    dict -- classify_checkout() runs, and fails, before any state-store
    lookup. Same leak/fix rationale as environment_start's counterpart."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    with pytest.raises(ValueError) as excinfo:
        fns["environment_stop"](checkout_path=str(non_repo))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert non_repo.name in msg
    assert "not a git repository" in msg


def test_environment_stop_invalid_checkout_path_nonexistent(tmp_path: Path):
    """Ticket #123 edge case: proves the reason-body rewrite, not just the
    `invalid repo_root ...:` prefix."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    missing = tmp_path / "missing-checkout"

    with pytest.raises(ValueError) as excinfo:
        fns["environment_stop"](checkout_path=str(missing))

    msg = str(excinfo.value)
    assert "checkout_path" in msg
    assert "repo_root" not in msg
    assert missing.name in msg
    assert "does not exist" in msg


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
        assert len(cmd) == 5
        assert cmd[:4] == [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
        ]
        decoded_run_line = base64.b64decode(cmd[4]).decode("utf-16-le")
    else:
        assert cmd == ["bash", "-c", "start-worker.sh"]
        decoded_run_line = cmd[2]
    assert "start-worker.sh" in decoded_run_line
    assert "start-web.sh" not in decoded_run_line

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


# ---- Ticket #127 ----
#
# `environment_start`'s `variant` parameter defaults to `"default"`, but
# resolving that default was previously documented as limited to a lone
# *unnamed* `start:` step. As of the pinned v0.3.5 (upstream
# lib-python-worktree#112), the lone-step fallback fires for a single
# `start:` step regardless of whether it carries a `name:` key -- but the
# wrapper's own docstrings, AGENTS.md, and SKILL.md still claimed the
# narrower, unnamed-only behaviour. These tests protect the corrected
# claim, and the once-undocumented consequence: when the fallback resolves
# a *named* step, `record.variants[role]` stores that step's own name (not
# the literal string `"default"`). Ticket #139 Part B closed the gap this
# left open -- `environment_stop(variant="default")` now DOES resolve
# against that role anyway, via this wrapper's compensating pre-resolution
# (see `_default_stop_variant` and the "symmetry" tests below).


def test_environment_start_docstring_documents_lone_named_step_default_fallback(
    tmp_path: Path,
):
    """Claim under protection: the `variant="default"` lone-step fallback
    fires for a single `start:` step REGARDLESS of whether that step
    carries a `name:` key (v0.3.5 / upstream lib-python-worktree#112) --
    not only for a lone *unnamed* step, which is what the pre-#127 wording
    claimed ("resolves to the lone unnamed step for back-compat"). The
    multi-step failure mode (two-or-more steps with none named `"default"`
    still raise `ValueError` listing the available names) must remain
    documented alongside the correction."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_start"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    assert re.search(
        r"\b(single|lone|exactly one)\b[^.]{0,160}"
        r"\b(regardless|even if|whether it is named|named or unnamed)\b",
        norm,
    ), "docstring must state the lone-step fallback covers a named step too"

    assert "resolves to the lone unnamed step for back-compat" not in norm, (
        "stale claim: the lone-step default fallback is not limited to "
        "unnamed steps as of v0.3.5 / upstream #112"
    )

    assert re.search(r"valueerror[^.]{0,200}available|available[^.]{0,200}valueerror", norm), (
        "the multi-step-with-no-default-named-step failure mode "
        "(ValueError listing available names) must stay documented"
    )


def test_stop_variant_default_symmetry_is_documented(tmp_path: Path):
    """Claim under protection (ticket #139 Part B, rewritten from the former
    test_stop_variant_default_asymmetry_is_documented): when the tier-3
    lone-step `variant="default"` fallback resolves a NAMED step, the
    *engine* records `record.variants[role] = step.name`, not the literal
    string `"default"` -- but `environment_stop(variant="default")`
    afterwards DOES resolve against that role anyway, because this wrapper
    pre-resolves a bare `"default"` to the contract's lone named step
    before ever calling the engine. Both `environment_start`'s and
    `environment_stop`'s docstrings must document this corrected symmetry
    explicitly, not just describe the two tools' `variant` behaviour in
    isolation from each other -- and the stale "will not resolve" claim
    must be gone from both.

    RED (pre-fix): the docstrings state the opposite ("will not resolve"),
    so the affirmative-resolve pattern below finds nothing and the
    stale-claim assertion fires on both docstrings.
    """
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    for tool_name in ("environment_start", "environment_stop"):
        doc = fns[tool_name].__doc__ or ""
        norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

        found_symmetry = False
        found_stale = False
        for m in re.finditer(r"default", norm):
            idx = m.start()
            window = norm[max(0, idx - 500) : idx + 500]
            if "variants" not in window:
                continue
            if re.search(r"\b(does|will|can)\b[^.]{0,120}resolv", window):
                found_symmetry = True
            if re.search(
                r"(will not|does not|won't|cannot|never)[^.]{0,120}resolv", window
            ):
                found_stale = True
        assert found_symmetry, (
            f"{tool_name}'s docstring must document that the lone-step "
            'default fallback\'s recorded variant name still lets a later '
            'environment_stop(variant="default") resolve, via this '
            "wrapper's compensating pre-resolution (ticket #139 Part B)"
        )
        assert not found_stale, (
            f"{tool_name}'s docstring still contains the stale asymmetry "
            'claim (environment_stop(variant="default") will not resolve) '
            "-- must be fully replaced, not merely supplemented"
        )


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


# ---- Ticket #139 Part B: environment_stop(variant="default") resolves a
# lone-named-step environment (wrapper-side compensation for the engine's
# documented start()/stop() asymmetry) ----


def test_stop_variant_default_resolves_lone_named_start_step_end_to_end(
    tmp_path: Path, temp_repo: Path
):
    """Driving test: the ticket's verbatim reproduction. A contract with a
    single `start:` step named `main`; `environment_start(...)` with no
    explicit `variant` succeeds (resolving via the engine's own tier-3
    lone-step fallback); `environment_stop(..., variant="default")` must
    resolve and stop it.

    Real `WorktreeManager` + `InMemoryStateStore` via `_make_tool_fixtures`;
    a real contract written with `_write_contract`. Only the process
    spawn/kill primitives (`_lifecycle_start`/`_lifecycle_stop`) are
    patched -- with fakes that record `rec.pids[role]`/
    `rec.variants[role] = variant` exactly as the real engine call sites do
    -- so no real process is spawned (cross-platform, no leaks; no
    sys.platform pinning, #137) while every bit of `manager.start()`'s/
    `manager.stop()`'s own real variant-resolution logic (including the
    `variant=step.name or variant` substitution) still runs unpatched.
    Asserting the start fake received `variant == "main"` pins that engine
    substitution, so a future engine bump (e.g. #138's v0.3.6) that changes
    it would surface here.

    RED (pre-fix): `record.variants == {"main": "main"}` after start, so
    `manager.stop(..., variant="default")` finds zero matches in
    `record.variants` and raises `VariantResolutionError` ->
    `environment_stop` raises `ValueError` -- the `"error" not in
    stop_result` assertion below never even gets that far; the call raises
    before returning.
    """
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    _write_contract(
        temp_repo,
        """
version: 1
isolation: full
start:
  - name: main
    run: irrelevant-command
""",
    )

    captured_start: dict = {}
    captured_stop: dict = {}

    def _fake_lifecycle_start(worktree_id, cmd, *, store, role, variant, env, cwd):
        captured_start["variant"] = variant
        captured_start["role"] = role
        rec = store.get(worktree_id)
        rec.pids[role] = 99999
        rec.variants[role] = variant
        rec.status = "running"
        store.update(rec)
        return rec

    def _fake_lifecycle_stop(worktree_id, *, store, role, timeout, kill_orphans):
        captured_stop["role"] = role
        rec = store.get(worktree_id)
        rec.pids.pop(role, None)
        rec.variants.pop(role, None)
        rec.status = "stopped" if not rec.pids else rec.status
        store.update(rec)
        return rec

    with patch(
        "lib_python_worktree.core.manager._lifecycle_start",
        side_effect=_fake_lifecycle_start,
    ):
        start_result = fns["environment_start"](checkout_path=str(temp_repo))

    assert "error" not in start_result
    # Pins the engine's own tier-3 substitution (manager.py's
    # `variant=step.name or variant`): a bare variant="default" call
    # resolved to the step's own name "main", not the literal "default".
    assert captured_start["variant"] == "main"
    assert start_result["variants"] == {"main": "main"}

    with patch(
        "lib_python_worktree.core.manager._lifecycle_stop",
        side_effect=_fake_lifecycle_stop,
    ):
        stop_result = fns["environment_stop"](
            environment_id=start_result["id"], variant="default"
        )

    assert "error" not in stop_result
    assert captured_stop["role"] == "main"
    assert "main" not in stop_result.get("pids", {})


def test_default_stop_variant_lone_unnamed_step_already_recorded_as_default(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage (already passes): a lone UNNAMED start
    step is recorded by the engine under the literal "default" itself
    (tier 2 of the engine's own resolution, unrelated to the ticket #112
    tier-3 fallback this ticket is about) -- precedence rule 2 leaves this
    untouched: record.variants already contains "default", so the helper
    returns "default" unchanged without even reading the contract."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    seeded = _seed_record(
        mgr,
        pids={"main": 1},
        variants={"main": "default"},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    result = _default_stop_variant(mgr, seeded.id, None)
    assert result == "default"


def test_default_stop_variant_step_literally_named_default_already_recorded(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage (already passes): a start step whose
    own name literally IS "default" is, likewise, already recorded under
    "default" by the engine's exact-match tier -- precedence rule 2 again
    leaves this untouched."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    seeded = _seed_record(
        mgr,
        pids={"web": 1},
        variants={"web": "default"},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    result = _default_stop_variant(mgr, seeded.id, None)
    assert result == "default"


def test_default_stop_variant_multi_step_contract_degrades_to_default(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage (already passes): a contract with TWO
    OR MORE start: steps must not be resolved by this helper (mirrors the
    engine's own tier-3 rule, which only ever fires for a single step) --
    degrades to "default" unchanged, preserving today's
    VariantResolutionError failure mode for this case
    (test_environment_stop_variant_resolution_failure_raises_valueerror
    already covers that failure end-to-end)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    _write_contract(
        temp_repo,
        """
version: 1
isolation: full
start:
  - name: web
    run: irrelevant-command
  - name: worker
    run: irrelevant-command
""",
    )
    seeded = _seed_record(
        mgr,
        pids={"web": 1},
        variants={"web": "web"},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    result = _default_stop_variant(mgr, seeded.id, None)
    assert result == "default"


def test_default_stop_variant_no_contract_degrades_to_default(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage (ticket #139 Part B risk: 'best-effort
    lookup raising'): no contract at all at repo_root must degrade silently
    to "default" -- the helper must never raise, and the original engine
    error text for whatever is actually wrong must survive verbatim at the
    call site (this is exercised end-to-end by
    test_environment_stop_variant_resolution_failure_raises_valueerror's
    zero-match-typo case, which never even reaches this helper's contract
    read since role/variant resolution happens inside manager.stop()
    itself; this test isolates the helper's own degrade-on-no-contract
    path directly)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    seeded = _seed_record(
        mgr,
        pids={"main": 1},
        variants={"main": "main"},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    result = _default_stop_variant(mgr, seeded.id, None)
    assert result == "default"


def test_default_stop_variant_unreadable_contract_degrades_to_default(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage (ticket #139 Part B risk: 'best-effort
    lookup raising'): a contract that exists but fails to parse must also
    degrade silently to "default" rather than raising out of this
    best-effort helper and breaking environment_stop for an unrelated
    reason."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    _write_contract(temp_repo, "not: [valid, yaml, contract")
    seeded = _seed_record(
        mgr,
        pids={"main": 1},
        variants={"main": "main"},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    result = _default_stop_variant(mgr, seeded.id, None)
    assert result == "default"


def test_default_stop_variant_unknown_environment_id_degrades_to_default(
    tmp_path: Path,
):
    """Additional edge-case coverage: an unknown environment_id (no record
    to look up) must degrade to "default" unchanged, so the engine's own
    not-found handling at the environment_stop call site is entirely
    unaffected."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = _default_stop_variant(mgr, "no-such-id", None)
    assert result == "default"


def test_default_stop_variant_resolves_via_checkout_path_for_primary(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage: the helper's second addressing path
    (environment_id omitted, checkout_path given) must resolve a lone
    named start step for the PRIMARY checkout too, mirroring
    environment_start's own checkout_path cold-start addressing. Exercises
    classify_checkout() -> primary_id_for() -> manager.state.get(), the
    branch none of the other _default_stop_variant tests reach (they all
    address by environment_id)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    _write_contract(
        temp_repo,
        """
version: 1
isolation: full
start:
  - name: main
    run: irrelevant-command
""",
    )
    primary_id = primary_id_for(temp_repo)
    seeded = WorktreeRecord(
        id=primary_id,
        repo_root=str(temp_repo),
        branch=None,
        path=str(temp_repo),
        status="running",
        backing="primary",
        pids={"main": 1},
        variants={"main": "main"},
    )
    mgr.state.add(seeded)

    result = _default_stop_variant(mgr, None, str(temp_repo))
    assert result == "main"


def test_default_stop_variant_resolves_via_checkout_path_for_linked_worktree(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage: the helper's checkout_path fallback
    path match over manager.state.list() (no environment_id, checkout_path
    doesn't resolve to a primary) must also resolve a lone named start step
    for a LINKED worktree, exercising the resolved-path-match branch none
    of the other tests reach.

    Ticket #139 fix-cycle regression guard: also seeds the repo's PRIMARY
    record *first*, with `variants` already containing `"default"` (a
    plausible prior state -- e.g. started via the unnamed-step tier).
    `classify_checkout()` documents that `repo_root` is always the main
    clone's root regardless of which checkout `checkout_path` belongs to
    (`checkout.py:63-64`), so before the fix,
    `_default_stop_variant`'s checkout_path resolution did
    `manager.state.get(primary_id_for(info.repo_root))` unconditionally --
    returning this SAME primary id whether `checkout_path` pointed at the
    primary or at a linked worktree of that repo -- so it found the
    primary's record instead of the linked worktree's own, and precedence
    rule 2 (`"default" in record.variants.values()`) short-circuited to
    `"default"` unchanged instead of resolving to the linked worktree's own
    `"main"` step. The fix must check `info.backing` before doing the
    primary lookup, mirroring `WorktreeManager._resolve_target()`."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    _write_contract(
        temp_repo,
        """
version: 1
isolation: full
start:
  - name: main
    run: irrelevant-command
""",
    )
    primary_id = primary_id_for(temp_repo)
    primary_record = WorktreeRecord(
        id=primary_id,
        repo_root=str(temp_repo),
        branch=None,
        path=str(temp_repo),
        status="running",
        backing="primary",
        pids={"main": 1},
        variants={"main": "default"},
    )
    mgr.state.add(primary_record)

    created = mgr.create(str(temp_repo), "feature/wt")
    seeded = mgr.state.get(created.id)
    seeded.pids["main"] = 1
    seeded.variants["main"] = "main"
    mgr.state.update(seeded)

    result = _default_stop_variant(mgr, None, created.path)
    assert result == "main"


def test_environment_stop_variant_none_path_unaffected_by_default_helper(
    tmp_path: Path, temp_repo: Path
):
    """Additional edge-case coverage (already passes): variant=None (the
    parameter's actual default -- i.e. variant omitted entirely) must never
    engage _default_stop_variant at all; only an explicit variant="default"
    does. Regression guard for the call site's `if variant == "default":`
    guard, distinct from test_environment_stop_no_role_no_variant_still_
    stops_main which covers the same scenario from the role-resolution
    side."""
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
        result = fns["environment_stop"](environment_id=record.id, variant=None)

    assert "error" not in result
    assert captured["role"] == "main"


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


# ---- Ticket #130: docstring / SKILL / README sweep ----
#
# Four re-sliced findings originally filed as #124 (misplaced-contract
# CAUTION is a silent no-op), #125 (environment_stop primary-vs-linked /
# environment_start's three addressing outcomes), #128 (start_log_path
# role-casing mismatch). This block covers #124, #125, and #128's
# environment_start/environment_stop side; #126 and #128's SKILL/AGENTS.md/
# README.md side live in tests/test_plugin_manifest.py, and #126's
# worktree_remove side lives in tests/test_worktree_tools.py.


def test_environment_start_docstring_no_longer_claims_silent_misplaced_contract(
    tmp_path: Path,
):
    """Claim under protection (ticket #130, re-slicing #124): a contract
    placed only in a worktree checkout (not at repo_root) is still a
    {"status": "ready", "pids": {}} no-op, but it is a diagnosable one --
    the same response carries no_op_reason: "contract-misplaced" (distinct
    from "no-contract") -- and the stale "silent .../no error to indicate
    the misplacement" claim must be gone."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_start"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    assert "no_op_reason" in norm
    assert "contract-misplaced" in norm
    assert "with no error to indicate the misplacement" not in norm

    found_silent_far_from_diagnosis = False
    for m in re.finditer(r"(?<!not )\bsilent\b", norm):
        idx = m.start()
        window = norm[max(0, idx - 300) : idx + 300]
        if "contract-misplaced" in window or "misplacement" in window:
            found_silent_far_from_diagnosis = True
    assert not found_silent_far_from_diagnosis, (
        "docstring must not describe the misplaced-contract case as "
        "(unqualified) 'silent' near its diagnosis -- it is diagnosable "
        "via no_op_reason; 'not silent' is fine"
    )


def test_environment_start_response_carries_engine_shadowed_contract(tmp_path: Path):
    """Tripwire for the engine-owned shadowed_contract diagnostic documented
    in ticket #130 (upstream lib-python-worktree #100). This wrapper only
    passes the field through via _record_to_dict's asdict(record) -- it
    does not compute it -- so this test already passes today (it is not the
    driving test for the docs). Its purpose is to fail loudly if a future
    lib-python-worktree bump renames, drops, or stops populating
    WorktreeRecord.shadowed_contract, which would otherwise leave the
    newly-added documentation silently lying."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    record = WorktreeRecord(
        id="wt-130-shadow-test",
        repo_root=str(tmp_path / "repo-root"),
        branch="feature/shadow",
        path=str(tmp_path / "store" / "wt-130-shadow-test"),
        status="running",
        pids={"main": 4242},
    )
    record.shadowed_contract = ShadowedContract(
        path="/wt/.seretos/worktree-setup.yml",
        used_path="/repo-root/.seretos/worktree-setup.yml",
        reason="differs",
        message="checkout-local contract differs from the one used",
    )

    with patch.object(mgr, "start", return_value=record):
        result = fns["environment_start"](environment_id=record.id)

    assert "shadowed_contract" in result
    shadowed = result["shadowed_contract"]
    assert isinstance(shadowed, dict)
    assert set(shadowed.keys()) == {"path", "used_path", "reason", "message"}
    assert shadowed["reason"] == "differs"

    # Second case: the engine leaves shadowed_contract unset (None) --
    # the key must still be present, just with a None value.
    record_no_shadow = WorktreeRecord(
        id="wt-130-noshadow-test",
        repo_root=str(tmp_path / "repo-root2"),
        branch="feature/noshadow",
        path=str(tmp_path / "store" / "wt-130-noshadow-test"),
        status="running",
        pids={"main": 4243},
    )
    assert record_no_shadow.shadowed_contract is None

    with patch.object(mgr, "start", return_value=record_no_shadow):
        result_none = fns["environment_start"](environment_id=record_no_shadow.id)

    assert "shadowed_contract" in result_none
    assert result_none["shadowed_contract"] is None


def test_environment_stop_docstring_distinguishes_primary_from_linked(tmp_path: Path):
    """Claim under protection (ticket #130, re-slicing #125 section 2a; fix
    #130 blocking finding 1): a linked worktree's tracked-but-never-started
    role is not a not-found condition -- the engine's graceful no-op path
    runs contract stop: steps best-effort and always sets
    stop_attempt.outcome == "no_process_recorded", but status only becomes
    "stopped" if no other role is still tracked in pids (and the record
    wasn't already "stop_incomplete"/"orphaned"); otherwise status is left
    unchanged. Only the primary (no record until its first environment_start)
    and a genuinely unknown target yield the soft not-found dict."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_stop"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    found = False
    for m in re.finditer(r"not_found", norm):
        idx = m.start()
        window = norm[max(0, idx - 600) : idx + 600]
        if "linked" in window and "stopped" in window and "no_process_recorded" in window:
            found = True
            break
    assert found, (
        "environment_stop docstring must contrast a linked worktree's "
        "tracked-but-never-started role (no_process_recorded, status "
        "stopped) against the primary/unknown-target not_found case, "
        "within the same section"
    )

    # E8: the not_running paragraph must be scoped so it does not read as
    # contradicting the linked-worktree paragraph above.
    found_scoping = False
    for m in re.finditer(r"not_running", norm):
        idx = m.start()
        window = norm[max(0, idx - 600) : idx + 600]
        if "processnotrunningerror" in window:
            found_scoping = True
            break
    assert found_scoping, (
        "environment_stop docstring's not_running paragraph must name "
        "ProcessNotRunningError to scope it against the linked-worktree "
        "graceful no-op path"
    )


# ---- Ticket #139 Part C: code: "not_running" reachability ----


def test_environment_stop_not_running_is_reachable_via_concurrent_pid_removal(
    tmp_path: Path, temp_repo: Path
):
    """Driving test: `code: "not_running"` (mapping the engine's
    `ProcessNotRunningError`) is a genuinely LIVE branch for the pinned
    engine v0.3.5, not dead code -- it fires when the pid entry for the
    resolved role disappears between `WorktreeManager.stop()`'s own
    snapshot check (record fetched early via `_resolve_target()`,
    manager.py ~:1510) and the delegated `process_lifecycle.stop()`'s own,
    independent, fresh `store.get(worktree_id)` re-read (process_lifecycle.py
    ~:2802) immediately before it decides whether to raise
    (`role not in record.pids`, ~:2809). A concurrent writer -- another
    `environment_stop` call for the same role, or an `environment_list`
    reconcile pass pruning a dead pid -- can close that window; the
    `YamlStateStore` is explicitly designed for multi-process access
    (portalocker-guarded re-reads on every `.get()`, yaml_store.py ~:542-545
    / ~:634-650).

    Only the concurrent MUTATION is simulated here (removing the pid/variant
    entry from the store between the two reads, as a real concurrent writer
    would); `ProcessNotRunningError` itself is raised by real, unpatched
    engine code (`lib_python_worktree.core.process_lifecycle.stop`), so this
    is genuine evidence the branch is reachable, not a construction that
    merely asserts what the wrapper does with an exception it invented.

    This requirement's "RED" is reachability itself, not a code change: it
    passes on today's code by design, which IS the evidence that keeping
    the branch (rather than removing it as suspected-dead) is correct. Its
    documentation counterpart (test_environment_stop_docstring_scopes_
    not_running_reachability below) is the driving test with a genuine RED.

    Nothing spawns a real process (the seeded pid is never touched -- it is
    removed from the store before process_lifecycle.stop's own read), so
    this is fast and OS-agnostic; no sys.platform pinning (#137).
    """
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    record = _seed_record(
        mgr,
        pids={"main": 99999},
        variants={},
        repo_root=str(temp_repo),
        path=str(temp_repo),
    )

    from lib_python_worktree.core import process_lifecycle as _real_process_lifecycle

    def _concurrent_removal_then_real_stop(
        worktree_id, *, store, role, timeout, kill_orphans
    ):
        # Simulate the concurrent writer: a second environment_stop call (or
        # an environment_list reconcile pass) that removed this role's pid
        # entry between manager.stop()'s snapshot and this function's own
        # fresh re-read below.
        rec = store.get(worktree_id)
        rec.pids.pop(role, None)
        rec.variants.pop(role, None)
        store.update(rec)
        # Delegate to the REAL engine function -- ProcessNotRunningError is
        # raised by unpatched code, not fabricated here.
        return _real_process_lifecycle.stop(
            worktree_id,
            store=store,
            role=role,
            timeout=timeout,
            kill_orphans=kill_orphans,
        )

    with patch(
        "lib_python_worktree.core.manager._lifecycle_stop",
        side_effect=_concurrent_removal_then_real_stop,
    ):
        result = fns["environment_stop"](environment_id=record.id)

    assert result.get("code") == "not_running"
    assert "error" in result


def test_environment_stop_never_started_role_is_no_op_not_not_running(
    tmp_path: Path, temp_repo: Path
):
    """Additional coverage (ticket #139 Part C): the OTHER, non-concurrent
    path to a missing pid -- a role that was simply never started on a
    tracked linked worktree -- must NOT take the not_running branch at all.
    `manager.stop()`'s own pre-emption (manager.py ~:1556,
    `effective_role not in record.pids`) catches this before ever
    delegating to `process_lifecycle.stop()`, returning the graceful
    `no_process_recorded` no-op instead. No patching: a real
    `WorktreeManager`, a real tracked linked worktree, a role that was
    never started. This test may already pass -- it pins the pre-emption so
    a future removal of it would be caught (only a docstring test,
    test_environment_stop_docstring_documents_role_vs_variant, exists for
    this today)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    mgr.create(str(temp_repo), "feature/wt")
    record = next(r for r in mgr.state.list() if r.backing == "worktree")

    result = fns["environment_stop"](environment_id=record.id, role="never-started")

    assert "code" not in result
    assert result["stop_attempt"]["outcome"] == "no_process_recorded"


def test_environment_stop_docstring_scopes_not_running_reachability(tmp_path: Path):
    """Driving test (docs, ticket #139 Part C2): environment_stop's
    docstring must scope not_running's reachability to a concrete
    concurrent-pid-removal window, version-scoped to the pinned engine, and
    distinguish it from the tracked-but-never-started no_process_recorded
    no-op -- replacing the prior honest-but-non-committal framing that
    merely listed not_running as one of several soft outcomes without ever
    saying when it can actually occur.

    RED (pre-fix): the docstring names not_running/ProcessNotRunningError
    but never uses the word "concurrent" near it, and states no
    version-scoping -- the assertion below fails.
    """
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_stop"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    found = False
    for m in re.finditer(r"not_running", norm):
        idx = m.start()
        window = norm[max(0, idx - 400) : idx + 900]
        if (
            "concurrent" in window
            and "no_process_recorded" in window
            and re.search(r"v0\.3\.5|version-scoped", window)
        ):
            found = True
            break
    assert found, (
        "environment_stop docstring must scope not_running's reachability "
        "to a concrete concurrent-pid-removal window, version-scoped to "
        "the pinned engine, and distinguish it from no_process_recorded"
    )


def test_environment_start_docstring_consolidates_three_addressing_outcomes(
    tmp_path: Path,
):
    """Claim under protection (ticket #130, re-slicing #125 section 2b): all
    three addressing outcomes -- ValueError (neither given), ValueError
    (both given, disagree), and the soft not_found dict (well-formed pair,
    target doesn't exist) -- must be named within the "Addressing the
    target" section itself, not merely present ~180 lines apart elsewhere
    in the docstring."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_start"].__doc__ or ""

    start_idx = doc.find("Addressing the target")
    end_idx = doc.find("Multiple named ``start:`` steps")
    assert start_idx != -1 and end_idx != -1 and end_idx > start_idx
    section = doc[start_idx:end_idx]

    assert "not_found" in section, (
        "the Addressing the target section must name the soft not_found "
        "outcome, not just the two ValueError outcomes"
    )
    assert section.count("ValueError") >= 2, (
        "the Addressing the target section must still name both "
        "ValueError outcomes (neither given; both given but disagreeing)"
    )


def test_environment_start_docstring_documents_start_log_path_role_casing(
    tmp_path: Path,
):
    """Claim under protection (ticket #146, correcting #130/#128): start_log_path's
    filename is a *case-preserving* slug of role (never lower-cased), while
    pids/record.variants key on role verbatim -- fully-qualified as
    Seretos/lib-python-worktree#111 so it is never confused with this repo's
    own closed #111 (thread-leak ticket). v0.3.7 fixed the upstream
    lower-casing bug that #111 originally reported; the residual caveat is
    the case-insensitive-filesystem interleaving, not lower-casing."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    doc = fns["environment_start"].__doc__ or ""
    norm = re.sub(r"\s+", " ", doc.replace("``", "").replace("**", "")).lower()

    occurrences = list(re.finditer(r"start_log_path", norm))
    assert occurrences

    found = False
    for m in occurrences:
        idx = m.start()
        window = norm[max(0, idx - 100) : idx + 900]
        if (
            "seretos/lib-python-worktree#111" in window
            and ("lower" in window or "slug" in window)
            and "preserv" in window
            and "pids" in window
        ):
            found = True
            break

    assert found, (
        "docstring must have at least one start_log_path mention whose "
        "surrounding window fully-qualifies the upstream #111 reference, "
        "names the case-preserving slug behaviour, and mentions pids"
    )


# ---------------------------------------------------------------------------
# Ticket #123: InvalidRepoError re-wording (_invalid_path_error_text)
# ---------------------------------------------------------------------------
#
# R1-R3 above cover the wrapper-level behaviour (worktree_remove,
# environment_start, environment_stop each re-word the engine's
# InvalidRepoError to name checkout_path). R4 below unit-tests the
# rewording helper directly; R5 covers its identity guard (never rename a
# path the wrapper didn't itself receive); R6 is the negative control
# proving worktree_create's genuinely-named `repo_root` parameter is left
# alone.


def test_invalid_path_error_text_preserves_reason_without_token(tmp_path: Path):
    """R4: a reason with no `repo_root` token at all (e.g. an unexpected
    'git rev-parse' output failure) must survive completely intact -- the
    mechanical `\\brepo_root\\b` substitution is a no-op here, proving no
    diagnostic detail is ever silently dropped for a reason shape the
    rewording helper doesn't specifically know about."""
    exc = InvalidRepoError("/x/y", "unexpected 'git rev-parse' output: 'garbage'")

    msg = _invalid_path_error_text(exc, param_name="checkout_path")

    assert msg == "invalid checkout_path '/x/y': unexpected 'git rev-parse' output: 'garbage'"


def test_invalid_path_error_text_rewrites_token_in_future_reason(tmp_path: Path):
    """R4: an invented reason (standing in for a future engine reason not
    enumerated anywhere in this wrapper) that DOES contain the `repo_root`
    token must still have it rewritten -- the substitution is mechanical,
    not an allow-list of known reasons, so it keeps working for engine
    reasons that don't exist yet."""
    exc = InvalidRepoError("/x/y", "repo_root failed an invented future check: /x/y")

    msg = _invalid_path_error_text(exc, param_name="checkout_path")

    assert "repo_root" not in msg
    assert "checkout_path failed an invented future check: /x/y" in msg


def test_invalid_path_error_text_word_boundary_leaves_lookalikes_alone(tmp_path: Path):
    """R4 edge case: `repo_roots` and `my_repo_root` must NOT be touched by
    the substitution -- proves the `\\b` word-boundary anchors are doing
    real work, not a bare (unanchored) string replace that would also
    mangle these lookalike identifiers."""
    exc = InvalidRepoError(
        "/x/y", "repo_roots list exhausted; my_repo_root was already tried"
    )

    msg = _invalid_path_error_text(exc, param_name="checkout_path")

    assert "repo_roots list exhausted; my_repo_root was already tried" in msg


def test_worktree_remove_invalid_checkout_path_identity_guard_different_path(
    tmp_path: Path,
):
    """R5: the wrapper must never rename a path it did not itself receive.
    If the engine's InvalidRepoError names some OTHER path (e.g. one it
    resolved internally) than the checkout_path the caller actually
    passed, the wrapper must leave the engine's message untouched --
    including its `repo_root` wording -- rather than mislabelling a path
    that isn't the one the caller gave it.

    This test may already pass before the production fix: the identity
    guard is new code, but the pre-fix generic `except WorktreeError`
    catch-all already passes str(exc) through verbatim too, so this is an
    expected-already-passing regression guard, not a false RED."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    other_path = str(tmp_path / "some-other-place")

    def _fake_remove(*args, **kwargs):
        raise InvalidRepoError(other_path, f"repo_root does not exist: {other_path}")

    with patch.object(mgr, "remove", side_effect=_fake_remove):
        with pytest.raises(ValueError) as excinfo:
            fns["worktree_remove"](checkout_path=str(tmp_path / "not-what-was-raised"))

    msg = str(excinfo.value)
    assert "repo_root" in msg


def test_environment_start_invalid_checkout_path_identity_guard_checkout_path_none(
    tmp_path: Path,
):
    """R5 additional edge case: exercises the `checkout_path is not None`
    short-circuit -- when the caller addressed the target purely by
    environment_id (checkout_path=None), the guard must not even attempt
    the equality comparison, and the engine's message passes through
    unchanged. Also an expected-already-passing guard (see the docstring
    above for why)."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    other_path = str(tmp_path / "some-other-place")

    def _fake_start(*args, **kwargs):
        raise InvalidRepoError(other_path, f"repo_root does not exist: {other_path}")

    with patch.object(mgr, "start", side_effect=_fake_start):
        with pytest.raises(ValueError) as excinfo:
            fns["environment_start"](environment_id="some-id")

    msg = str(excinfo.value)
    assert "repo_root" in msg


def test_worktree_create_invalid_repo_root_still_names_repo_root(tmp_path: Path):
    """R6 (negative control): worktree_create's parameter really is named
    `repo_root` -- ticket #123's fix must be scoped to checkout_path-only
    tools (worktree_remove, environment_start, environment_stop), never a
    blanket string replacement that would also mangle worktree_create's
    correctly-named error. Expected to pass both before and after the fix."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_create"](repo_root=str(non_repo), branch="feature/wt")

    msg = str(excinfo.value)
    assert "repo_root" in msg
    assert "checkout_path" not in msg


# ---- Ticket #159: v0.3.10 worktree_remove/_teardown redesign regression
# coverage (upstream lib-python-worktree#135). These four E2E scenarios --
# dirty-tree refusal, dirty-tree forced removal, teardown-before-delete
# ordering, and orphan-by-checkout_path removal -- are each their own
# driving test, deliberately not conflated (attempt 1 of this ticket failed
# planning review by reusing one test to claim coverage of two of these). ----


def _write_teardown_probe_contract(
    repo: Path, tmp_path: Path, marker_name: str
) -> Path:
    """Write a `.seretos/worktree-setup.yml` at `repo` with a single
    `teardown:` step that runs a standalone probe script (written to
    `tmp_path`, OUTSIDE any checkout). The probe reads `README.md` from its
    own current working directory -- which the engine sets to the checkout
    being removed for the duration of the teardown step -- and writes
    `tmp_path/marker_name` recording both that cwd and the README content it
    saw. Returns the marker's path.

    This is only obtainable while the checkout still exists: if `teardown:`
    ran AFTER the checkout was deleted, the probe's cwd/README would already
    be gone, the script would fail, `SetupRunner`'s step failure is swallowed
    (ticket #117's documented policy: a teardown step failure must never
    block `git worktree remove`), and no marker would ever be written at
    all. A marker existing, with `readme=` matching the committed content, is
    therefore direct proof teardown ran before deletion -- not merely that
    it ran at some point.
    """
    marker = tmp_path / marker_name
    probe_script = tmp_path / f"{marker_name}.probe.py"
    probe_lines = [
        "import os",
        "from pathlib import Path",
        f"marker = Path({str(marker)!r})",
        "cwd = os.getcwd()",
        'readme = Path("README.md").read_text(encoding="utf-8").rstrip(chr(10))',
        'marker.write_text("cwd=" + cwd + "\\nreadme=" + readme + "\\n", '
        'encoding="utf-8")',
    ]
    probe_script.write_text("\n".join(probe_lines) + "\n", encoding="utf-8")

    # Shell-agnostic, space-safe interpreter+script invocation (test-critic
    # round-1, test-code Major 2, plus a reviewer round-2 finding on this
    # same line): a leading `&` PowerShell call operator is a syntax error
    # under `bash -c` on POSIX, so `&`-based quoting is out. But a fully
    # UNQUOTED `<py> <script>` pair (the previous shape here) is only safe
    # when neither path contains a space -- `sys.executable` is the actual
    # running interpreter's path, not a fixture-derived one, and commonly
    # contains a space on Windows (e.g. under `C:\Program Files\...`), so
    # that assumption was wrong.
    #
    # `_cmdline_token` below instead leaves each path's leading run of
    # "plain" characters (up to its first space or backslash) UNQUOTED, then
    # wraps everything from that point on in a double-quoted segment, with
    # no whitespace between the two pieces. This round-trips through both
    # engines with the *same literal text*, no per-shell branching needed:
    # - PowerShell decides command-mode vs. expression-mode parsing from
    #   the very first character of a statement; a *quoted* leading token
    #   is parsed as an expression and silently never launched without `&`
    #   (see above), but our token starts with an unquoted character, so
    #   PowerShell stays in command mode -- and an unquoted segment
    #   immediately followed by a quoted segment (no space between them)
    #   merges into a single token, so the whole thing is invoked as one
    #   command/argument. PowerShell also never treats backslash as an
    #   escape character (quoted or not), so the quoted tail's backslashes
    #   survive unchanged.
    # - `bash -c` merges adjacent quoted/unquoted segments into a single
    #   word the same way. Its hazard is the mirror image of PowerShell's:
    #   outside quotes, a backslash escapes (and is stripped along with)
    #   the next character, which would silently eat a Windows path's
    #   directory separators -- so every backslash must live inside the
    #   quoted segment. Per POSIX, a backslash inside double quotes is only
    #   special before `$`, `` ` ``, `"`, `\`, or a newline; none of those
    #   follow a backslash in an ordinary Windows path, so the quoted tail
    #   survives unchanged there too. (A plain fully-double-quoted path
    #   would NOT be safe in general for this same reason if a backslash
    #   happened to immediately precede one of those characters -- e.g. a
    #   trailing backslash butting up against the closing quote -- which is
    #   why this leaves a prefix unquoted rather than quoting the whole
    #   token.)
    def _cmdline_token(path_str: str) -> str:
        for i, ch in enumerate(path_str):
            if ch in (" ", "\\"):
                return f'{path_str[:i]}"{path_str[i:]}"'
        return path_str

    run_line = f"{_cmdline_token(sys.executable)} {_cmdline_token(str(probe_script))}"
    _write_contract(
        repo,
        "version: 1\n"
        "isolation: full\n"
        "teardown:\n"
        "  - name: probe\n"
        f"    run: {run_line}\n",
    )
    return marker


def _parse_marker(marker: Path) -> Tuple[str, str]:
    text = marker.read_text(encoding="utf-8")
    lines = text.splitlines()
    cwd_line = next(l for l in lines if l.startswith("cwd="))
    readme_line = next(l for l in lines if l.startswith("readme="))
    return cwd_line[len("cwd=") :], readme_line[len("readme=") :]


def test_worktree_remove_dirty_tree_refuses_without_force(
    tmp_path: Path, temp_repo: Path
):
    """R2: a checkout with real dirt (a modified TRACKED file -- deliberately
    not `.seretos/` content, which is exempted) is not removed when
    `force=False`. Deterministic refusal -- not wrapped in
    `_remove_with_ambient_retry`."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")

    readme_path = Path(rec.path) / "README.md"
    readme_path.write_text("dirty edit\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](environment_id=rec.id)

    msg = str(excinfo.value)
    assert "uncommitted changes" in msg
    assert "force=True" in msg

    # Nothing was touched: the checkout, its dirt, and the state record all
    # survive the refused attempt.
    assert Path(rec.path).exists()
    assert readme_path.read_text(encoding="utf-8") == "dirty edit\n"
    assert mgr.state.list() == [rec]


def test_worktree_remove_dirty_tree_succeeds_with_force(
    tmp_path: Path, temp_repo: Path
):
    """R3: the same real-dirt checkout IS removed when `force=True`."""
    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")

    readme_path = Path(rec.path) / "README.md"
    readme_path.write_text("dirty edit\n", encoding="utf-8")

    result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=rec.id, force=True),
        what="worktree_remove(dirty tree, force=True)",
    )

    assert "error" not in result
    assert result["id"] == rec.id
    assert result["status"] == "removed"
    assert not Path(rec.path).exists()
    assert mgr.state.list() == []


def test_worktree_remove_runs_teardown_steps_before_deleting_checkout(
    tmp_path: Path, temp_repo: Path
):
    """R4: contract `teardown:` steps execute while the checkout still
    exists -- see `_write_teardown_probe_contract`'s docstring for why the
    marker's mere existence, with matching README content, discriminates
    "ran before delete" from "ran after delete" (which would produce no
    marker at all, not a wrong one)."""
    marker = _write_teardown_probe_contract(
        temp_repo, tmp_path, "teardown-marker.txt"
    )

    mgr, fns, tools = _make_tool_fixtures(tmp_path)
    rec = mgr.create(str(temp_repo), "feature/wt")

    result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=rec.id),
        what="worktree_remove(teardown-before-delete probe)",
    )

    assert "error" not in result
    assert result["status"] == "removed"
    assert not Path(rec.path).exists()

    assert marker.exists(), (
        "teardown marker was never written -- either the teardown step "
        "never ran at all, or it ran AFTER the checkout was already "
        "deleted (cwd/README gone), failed, and was silently swallowed"
    )
    recorded_cwd, recorded_readme = _parse_marker(marker)
    assert recorded_readme == "hello", f"unexpected README content seen: {recorded_readme!r}"
    assert Path(recorded_cwd).resolve() == Path(rec.path).resolve()


def test_worktree_remove_orphan_by_checkout_path_through_redesigned_teardown(
    tmp_path: Path, temp_repo: Path
):
    """R5: an orphan linked worktree (on disk, never persisted in the
    manager's store) is SUCCESSFULLY removed when addressed by
    `checkout_path` -- a genuine success case, not a refusal, and not the
    same test as the primary-refusal tests. This must also run the
    redesigned teardown for the untracked target (the engine loads the
    contract from `record.repo_root`, independent of whether the record
    itself was ever persisted)."""
    orphan_path = _make_orphan(temp_repo, tmp_path)
    marker = _write_teardown_probe_contract(
        temp_repo, tmp_path, "orphan-teardown-marker.txt"
    )

    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    result = _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](checkout_path=str(orphan_path)),
        what="worktree_remove(orphan by checkout_path, redesigned teardown)",
    )

    assert "error" not in result
    assert re.search(r"-untracked-[0-9a-f]{8}$", result["id"])
    assert result["status"] == "removed"
    assert not orphan_path.exists()
    assert mgr.state.list() == []

    branches = subprocess.run(
        ["git", "branch", "--list", "orphan-branch"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "orphan-branch" in branches

    assert marker.exists(), (
        "teardown marker was never written for the untracked/orphan "
        "removal target -- teardown must run for this target too"
    )
    recorded_cwd, recorded_readme = _parse_marker(marker)
    assert recorded_readme == "hello", f"unexpected README content seen: {recorded_readme!r}"
    assert Path(recorded_cwd).resolve() == orphan_path.resolve()


def _prove_teardown_marker_mechanism_reachable(
    mgr: WorktreeManager, fns: dict, temp_repo: Path, marker: Path
) -> None:
    """Positive control for primary-refusal probes (test-critic round-1
    finding, plan-layer Major 1): create an ordinary linked worktree against
    the same contract/repo and remove it normally, proving the teardown
    probe marker mechanism is actually live BEFORE a caller relies on the
    marker's absence to mean anything. Without this control, an
    implementation where teardown silently no-ops for this contract (or for
    unpersisted/primary-shaped targets generally) would make a later
    "marker absent" assertion pass vacuously, never having proven the
    marker mechanism was reachable at all.

    Deliberately kept as its own module-level helper, not inlined into a
    deterministic-refusal test body: this control's own removal is an
    ordinary, ambient-retriable success (ticket #141), so it legitimately
    uses `_remove_with_ambient_retry` -- but
    `test_deterministic_refusal_tests_are_not_ambient_hardened` scans each
    registered refusal test's own source text for that exact call, and a
    refusal test merely *calling* this helper (rather than containing the
    retry call inline) keeps that scan accurate: it is the refusal test's
    own `worktree_remove` attempt that must never be retried, not this
    unrelated control step.
    """
    control_rec = mgr.create(str(temp_repo), "feature/r6-positive-control")
    _remove_with_ambient_retry(
        lambda: fns["worktree_remove"](environment_id=control_rec.id),
        what="worktree_remove(R6 positive control)",
    )
    assert marker.exists(), (
        "positive control failed -- the teardown probe marker was not "
        "written even for an ordinary (non-primary) removal against this "
        "same contract, so its absence after the primary-refusal attempt "
        "below would not prove anything about the primary guard"
    )
    marker.unlink()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("addressing", ["environment_id", "checkout_path"])
def test_worktree_remove_primary_refused_before_any_teardown_phase_runs(
    tmp_path: Path, temp_repo: Path, addressing: str, force: bool
):
    """R6: removing the primary/main clone is refused structurally, before
    any teardown work, regardless of `force`, on both addressing modes.
    Deliberately not a copy of the existing primary-refusal tests: adds the
    pre-teardown-marker-absence dimension -- proving the guard fires BEFORE
    any teardown phase, not merely that it fires at all."""
    marker = _write_teardown_probe_contract(
        temp_repo, tmp_path, "primary-teardown-marker.txt"
    )

    mgr, fns, tools = _make_tool_fixtures(tmp_path)

    _prove_teardown_marker_mechanism_reachable(mgr, fns, temp_repo, marker)

    if addressing == "environment_id":
        started = fns["environment_start"](checkout_path=str(temp_repo))
        assert "error" not in started
        kwargs = {"environment_id": started["id"], "force": force}
    else:
        kwargs = {"checkout_path": str(temp_repo), "force": force}

    with pytest.raises(ValueError) as excinfo:
        fns["worktree_remove"](**kwargs)

    msg = str(excinfo.value)
    assert "primary" in msg
    assert "backing" in msg

    assert temp_repo.exists()
    assert (temp_repo / ".git").exists()
    readme_content = (temp_repo / "README.md").read_text(encoding="utf-8")
    assert readme_content == "hello\n"

    assert not marker.exists(), (
        "teardown marker was written despite the primary refusal -- the "
        "primary guard must fire BEFORE any teardown phase runs"
    )


# ---- Ticket #141: ambient-handle hardening ----

_MIRROR_BEGIN = "# ---- ticket #141 ambient-handle retry helper (MIRRORED) " + "BEGIN ----"
_MIRROR_END = "# ---- ticket #141 ambient-handle retry helper (MIRRORED) " + "END ----"


def _extract_mirrored_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(_MIRROR_BEGIN)
    end = text.index(_MIRROR_END) + len(_MIRROR_END)
    return text[start:end]


def test_ambient_retry_helper_copies_are_identical():
    this_file = Path(__file__)
    sibling_file = this_file.with_name("test_worktree_tools.py")

    this_block = _extract_mirrored_block(this_file)
    sibling_block = _extract_mirrored_block(sibling_file)

    assert this_block == sibling_block, (
        "The ticket #141 ambient-handle retry helper has drifted between "
        "test_environment_tools.py and test_worktree_tools.py -- they must "
        "stay byte-identical."
    )


_HARDENED_REMOVAL_SITES = (
    "test_worktree_remove_untracked_orphan_by_checkout_path",
    "test_worktree_remove_orphan_leaves_branch_intact",
    "test_worktree_remove_by_checkout_path_on_tracked_worktree",
    "test_worktree_remove_dirty_tree_succeeds_with_force",
    "test_worktree_remove_runs_teardown_steps_before_deleting_checkout",
    "test_worktree_remove_orphan_by_checkout_path_through_redesigned_teardown",
)


def test_real_git_removal_sites_are_ambient_hardened():
    for name in _HARDENED_REMOVAL_SITES:
        source = inspect.getsource(globals()[name])
        assert "_remove_with_ambient_retry(" in source, (
            f"{name} performs a real-git removal but is not routed through "
            f"_remove_with_ambient_retry (ticket #141)"
        )


_DETERMINISTIC_REFUSAL_TESTS_NOT_HARDENED = (
    "test_worktree_remove_primary_raises_even_with_force",
    "test_worktree_remove_primary_by_checkout_path_refused_even_unstarted",
    "test_worktree_remove_unknown_id_still_soft_error",
    "test_worktree_remove_untracked_id_soft_error_names_checkout_path",
    "test_worktree_remove_checkout_path_and_id_mismatch_raises_valueerror",
    "test_worktree_remove_with_neither_target_raises_valueerror",
    "test_worktree_remove_missing_target_error_names_environment_id",
    "test_worktree_remove_checkout_path_outside_any_repo",
    "test_worktree_remove_checkout_path_nonexistent",
    "test_worktree_remove_checkout_path_is_a_file",
    "test_worktree_remove_invalid_checkout_path_identity_guard_different_path",
    "test_worktree_remove_dirty_tree_refuses_without_force",
    "test_worktree_remove_primary_refused_before_any_teardown_phase_runs",
)


def test_deterministic_refusal_tests_are_not_ambient_hardened():
    for name in _DETERMINISTIC_REFUSAL_TESTS_NOT_HARDENED:
        source = inspect.getsource(globals()[name])
        assert "_remove_with_ambient_retry(" not in source, (
            f"{name} is a deterministic refusal/lock-mapping test and must "
            f"not be wrapped in _remove_with_ambient_retry (ticket #141)"
        )
