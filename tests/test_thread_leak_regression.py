"""Regression tests for the Windows worktree_remove hang.

Four prior rounds (agent-worktree #111, #159, #169, #176) measured daemon
*thread count* across repeated ``worktree_create`` -> ``worktree_remove``
cycles. That quantity plateaus under lib-python-worktree's process-wide
``_MAX_WEDGED_HANDLE_WORKERS`` cap, so those tests eventually went green
(behind ``xfail``, then not) without the actual user-visible symptom ever
being fixed: every ``worktree_remove`` call on Windows unconditionally pays
a systemwide handle scan
(``lib_python_worktree.core.teardown._find_blocking_processes``, reached
from both ``_phase_gate_a_blocking_preflight`` and ``_phase_orphan_scan``
regardless of whether anything actually blocks removal), which can take
many seconds -- observed, pre-fix, to exceed this project's 300s
per-test timeout entirely.

Ticket #181 bumps the pinned engine to v0.3.13 (upstream PR #155, "fix: end
the Windows remove() hang chain by subtraction") and replaces the
thread-count proxy with what a user actually feels: wall-clock duration of
``worktree_remove``, plus a spy proving the systemwide scan is not reached
at all when nothing holds the worktree. Thread count is kept only as a
non-gating diagnostic. See ticket #179 for the fix-verification history and
#111/#159/#169/#176 for the four rounds that measured the wrong quantity.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib_python_worktree import InMemoryStateStore, ManagerConfig, WorktreeManager


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_tool_fixtures(tmp_path: Path):
    """Return (mgr, fn_map) for tool-layer tests.

    Local copy of the helper in tests/test_worktree_tools.py:420-433 -- kept
    local to this file rather than imported across test modules, per the
    approved plan.
    """
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


def _make_repo_with_branches(repo: Path, branch_names: list[str]) -> None:
    """Init a git repo at *repo* with an initial commit and one branch per
    name in *branch_names*, all created up front (per the approved plan)."""
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    for name in branch_names:
        _git("branch", name, cwd=repo)


# Shared by both tests below rather than a per-test formula (see the
# approved plan's "Mechanism balance" section). 15.0s is a quarter of the
# project's inherited timeout=60 (pyproject.toml) -- the literal "well
# under the 60s timeout" bound the ticket names, made measurable.
_REMOVE_TOTAL_BUDGET_SEC = 15.0


def _make_scan_spy(monkeypatch):
    """Patch lib_python_worktree.core.teardown's own binding of
    ``_find_blocking_processes`` with a counting wrapper that delegates to
    the real function, and return a mutable one-item call counter dict.

    ``teardown.py`` imports the function via
    ``from .process_lifecycle import _find_blocking_processes`` -- a
    ``from``-import binding. Patching
    ``process_lifecycle._find_blocking_processes`` would not be visible to
    any of ``teardown``'s call sites, which each call the name bound into
    ``teardown``'s own module namespace. ``raising=True`` so an upstream
    rename/relocation fails loudly instead of silently patching a stale,
    no-longer-consulted attribute.
    """
    from lib_python_worktree.core import teardown as teardown_mod

    real_find_blocking_processes = teardown_mod._find_blocking_processes
    calls = {"count": 0}

    def _counting_wrapper(*args, **kwargs):
        calls["count"] += 1
        return real_find_blocking_processes(*args, **kwargs)

    # raising=True is intentional, not an oversight: if a future
    # lib_python_worktree release renamed or removed this binding outright
    # (rather than just gating it out of the unheld-worktree path), both
    # tests below must fail loudly here with an AttributeError at patch
    # time -- not silently install a vacuous spy that never gets called and
    # let calls["count"] == 0 pass for the wrong reason.
    monkeypatch.setattr(
        "lib_python_worktree.core.teardown._find_blocking_processes",
        _counting_wrapper,
        raising=True,
    )
    return calls


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "_win_handle_holders and _find_blocking_processes Pass 1c are "
        "sys.platform == 'win32'-gated in lib_python_worktree.core.process_lifecycle"
    ),
)
def test_create_remove_cycles_are_fast(tmp_path: Path, capsys, monkeypatch):
    """worktree_remove on an unheld worktree must return promptly and must
    never reach the systemwide blocking-process handle scan.

    Driving test for agent-worktree #179/#181. Prior rounds (#111/#159/
    #169/#176) measured thread *count*, which plateaus under
    lib-python-worktree's process-wide worker cap even while the scan
    itself keeps running and costing seconds per call. This test asserts
    wall-clock duration directly -- what a user actually feels -- plus a
    spy proving the scan is never invoked at all for an unheld worktree; a
    fast machine could otherwise pass a timing bound while still paying the
    scan under the hood.

    Shape: 2 warm-up cycles (let any one-time/lazy initialisation settle,
    matching this module's historical shape) -> 6 measured cycles, each
    timing only the ``worktree_remove`` call.
    """
    calls = _make_scan_spy(monkeypatch)

    branch_names = [f"feature/leak-{i:02d}" for i in range(8)]
    repo = tmp_path / "src-repo"
    _make_repo_with_branches(repo, branch_names)

    mgr, fns = _make_tool_fixtures(tmp_path)

    def _cycle(branch: str) -> float:
        create_result = fns["worktree_create"](repo_root=str(repo), branch=branch)
        assert "error" not in create_result, f"create failed: {create_result}"
        remove_start = time.perf_counter()
        remove_result = fns["worktree_remove"](environment_id=create_result["id"])
        remove_duration = time.perf_counter() - remove_start
        assert "error" not in remove_result, f"remove failed: {remove_result}"
        return remove_duration

    total_start = time.perf_counter()
    for i in range(2):
        _cycle(branch_names[i])

    remove_durations: list[float] = []
    for i in range(2, 8):
        remove_durations.append(_cycle(branch_names[i]))
    total_duration = time.perf_counter() - total_start

    diagnostic_thread_count = len(threading.enumerate())

    with capsys.disabled():
        print(
            f"\n[ticket #181] per-cycle worktree_remove durations (s): "
            f"{remove_durations}"
        )
        print(
            f"[ticket #181] total create+remove wall-clock, 8 cycles (s): "
            f"{total_duration:.3f}"
        )
        print(
            f"[ticket #181] teardown._find_blocking_processes call count: "
            f"{calls['count']}"
        )
        print(
            f"[ticket #181] live thread count (diagnostic only, does not "
            f"gate the assertion): {diagnostic_thread_count}"
        )

    slowest = max(remove_durations)
    median_duration = statistics.median(remove_durations)
    total_remove = sum(remove_durations)

    assert slowest < 5.0, (
        f"slowest worktree_remove took {slowest:.3f}s (bound 5.0s) -- "
        f"per-cycle durations={remove_durations}"
    )
    assert median_duration < 2.0, (
        f"median worktree_remove duration was {median_duration:.3f}s "
        f"(bound 2.0s) -- per-cycle durations={remove_durations}"
    )
    assert total_remove < _REMOVE_TOTAL_BUDGET_SEC, (
        f"{len(remove_durations)} removes took {total_remove:.3f}s total "
        f"(bound {_REMOVE_TOTAL_BUDGET_SEC}s) -- per-cycle durations="
        f"{remove_durations}"
    )
    assert total_duration < 30.0, (
        f"the full 8-cycle create+remove run took {total_duration:.3f}s "
        f"total (bound 30.0s, well under the project's inherited 60s "
        f"per-test timeout) -- per-cycle remove durations={remove_durations}"
    )
    assert calls["count"] == 0, (
        f"teardown._find_blocking_processes was called {calls['count']} "
        f"time(s) across {len(remove_durations)} removes of an unheld "
        f"worktree -- expected 0: the systemwide blocking-process scan "
        f"must never run when nothing holds the worktree; per-cycle "
        f"durations={remove_durations}"
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "_win_handle_holders and _find_blocking_processes Pass 1c are "
        "sys.platform == 'win32'-gated in lib_python_worktree.core.process_lifecycle"
    ),
)
def test_create_remove_cycles_with_kill_blocking_processes_are_fast(
    tmp_path: Path, capsys, monkeypatch
):
    """Same requirement as test_create_remove_cycles_are_fast, through the
    kill_blocking_processes=True call site (ticket #44).

    Code read (see the approved plan): ctx.kill_blocking_processes is only
    consulted after a blocker has already been found (teardown.py's Gate A
    confirmation and the orphan-scan phase), so for an unheld worktree this
    call site's code path is byte-for-byte identical to the default one --
    there is no legitimate carve-out for a weaker spy bound here. Smaller N
    (2 warm-up + 3 measured) matches this module's historical shape for
    this call site.
    """
    calls = _make_scan_spy(monkeypatch)

    branch_names = [f"feature/leak-kill-{i:02d}" for i in range(5)]
    repo = tmp_path / "src-repo"
    _make_repo_with_branches(repo, branch_names)

    mgr, fns = _make_tool_fixtures(tmp_path)

    def _cycle(branch: str) -> float:
        create_result = fns["worktree_create"](repo_root=str(repo), branch=branch)
        assert "error" not in create_result, f"create failed: {create_result}"
        remove_start = time.perf_counter()
        remove_result = fns["worktree_remove"](
            environment_id=create_result["id"],
            kill_blocking_processes=True,
        )
        remove_duration = time.perf_counter() - remove_start
        assert "error" not in remove_result, f"remove failed: {remove_result}"
        return remove_duration

    total_start = time.perf_counter()
    for i in range(2):
        _cycle(branch_names[i])

    remove_durations: list[float] = []
    for i in range(2, 5):
        remove_durations.append(_cycle(branch_names[i]))
    total_duration = time.perf_counter() - total_start

    diagnostic_thread_count = len(threading.enumerate())

    with capsys.disabled():
        print(
            f"\n[ticket #181] (kill_blocking_processes) per-cycle "
            f"worktree_remove durations (s): {remove_durations}"
        )
        print(
            f"[ticket #181] (kill_blocking_processes) total create+remove "
            f"wall-clock, 5 cycles (s): {total_duration:.3f}"
        )
        print(
            f"[ticket #181] (kill_blocking_processes) "
            f"teardown._find_blocking_processes call count: {calls['count']}"
        )
        print(
            f"[ticket #181] (kill_blocking_processes) live thread count "
            f"(diagnostic only, does not gate the assertion): "
            f"{diagnostic_thread_count}"
        )

    slowest = max(remove_durations)
    median_duration = statistics.median(remove_durations)
    total_remove = sum(remove_durations)

    assert slowest < 5.0, (
        f"slowest worktree_remove(kill_blocking_processes=True) took "
        f"{slowest:.3f}s (bound 5.0s) -- per-cycle durations="
        f"{remove_durations}"
    )
    assert median_duration < 2.0, (
        f"median worktree_remove(kill_blocking_processes=True) duration "
        f"was {median_duration:.3f}s (bound 2.0s) -- per-cycle durations="
        f"{remove_durations}"
    )
    assert total_remove < _REMOVE_TOTAL_BUDGET_SEC, (
        f"{len(remove_durations)} removes took {total_remove:.3f}s total "
        f"(bound {_REMOVE_TOTAL_BUDGET_SEC}s) -- per-cycle durations="
        f"{remove_durations}"
    )
    assert total_duration < 20.0, (
        f"the full 5-cycle create+remove(kill_blocking_processes=True) run "
        f"took {total_duration:.3f}s total (bound 20.0s, well under the "
        f"project's inherited 60s per-test timeout) -- per-cycle remove "
        f"durations={remove_durations}"
    )
    assert calls["count"] == 0, (
        f"teardown._find_blocking_processes was called {calls['count']} "
        f"time(s) across {len(remove_durations)} "
        f"removes(kill_blocking_processes=True) of an unheld worktree -- "
        f"expected 0: kill_blocking_processes adds no scan for a worktree "
        f"nothing is blocking (ctx.kill_blocking_processes is only "
        f"consulted after a blocker is already found), so this must match "
        f"the default call site's zero-call bound exactly; per-cycle "
        f"durations={remove_durations}"
    )


def test_thread_leak_tests_are_no_longer_xfail():
    """Guard against a repeat of the #111/#159/#169/#176 pattern: keep an
    ``xfail`` mark (or an inflated per-test timeout) in place and call the
    hang "fixed" without it actually being fixed.

    This module's own source must carry neither an xfail marker nor a
    per-test timeout-override marker now that the upstream fix (ticket
    #181) is proven. The needle strings are built by concatenation so this
    guard's own assertions/messages can't accidentally match themselves.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    xfail_marker = "pytest" + ".mark.xfail"
    timeout_override_marker = "pytest" + ".mark.timeout("

    assert xfail_marker not in source, (
        "tests/test_thread_leak_regression.py must not reintroduce an "
        "xfail marker -- ticket #181 closes the #111/#159/#169/#176 "
        "pattern of leaving the escape hatch in place and calling the hang "
        "'fixed'"
    )
    assert timeout_override_marker not in source, (
        "tests/test_thread_leak_regression.py must not override the "
        "project-wide timeout=60 (pyproject.toml) with a per-test timeout "
        "marker -- that would hide a real regression behind an inflated "
        "ceiling instead of failing loudly within the shared 60s bound"
    )
