"""Regression tests for ticket #111.

A black-box re-test sweep reported unbounded, linear daemon-thread growth
(+~62 to +~148 threads per cycle, no plateau) across repeated
``worktree_create`` -> ``worktree_remove`` cycles on Windows, despite prior
fixes in #90/#106/#109. Ticket #114 bumped the pin to lib-python-worktree
v0.3.3; these tests measure, in-process, whether that pin actually closed the
leak by driving the same MCP tool callables the ticket reports against
(``worktree_create``/``worktree_remove``), not the manager API directly.
Growth is measured by thread *object identity* (``threading.enumerate()``
``Thread`` objects -- not the OS-recycled ``Thread.ident`` integers --
diffed against a post-warm-up baseline, after a short poll-until-stable
settle), not by raw ``threading.active_count()`` -- so unrelated ambient
thread activity in the host pytest process cannot be mistaken for the leak,
and a baseline thread that exits mid-run can never have its recycled ident
mistaken for a still-alive survivor. Newly-appeared, still-alive threads are
further filtered to the known leak signature -- the ``_BoundedQueryWorker``
worker threads, which surface under Python's default thread naming as
``"Thread-N (_run)"`` -- so an unrelated persistent thread started by
pytest/coverage/another plugin during a measured cycle cannot be mistaken
for the leak either. The unfiltered "any newly-appeared thread" count is
kept alongside the filtered leak-signature count in the diagnostics/
assertion message so the two are never conflated; if the unfiltered count
materially exceeds the filtered one (by more than the same plateau
tolerance used for the growth assertion itself), that is flagged as a loud
filter-drift warning and folded into the assertion, since it would
otherwise mean the leak-signature filter has stopped matching (e.g. an
upstream rename of the worker thread) while the underlying leak continues
unnoticed -- exactly the false-green failure mode this divergence check
exists to catch. Any measured cycle whose thread snapshot fails to settle
within the poll window is named in the diagnostics and excluded from the
value the growth assertion is computed over (a non-converged sample can
look identical to a real leak without being one); if every measured cycle
fails to settle, the test fails outright with a clear message rather than
silently asserting over an empty set.

**Known limitation:** the baseline snapshot is taken *after* the 2 warm-up
cycles, not before them, so any thread leaked during those first two
create/remove calls is baked into the baseline and can never register as
growth. The warm-up itself is deliberate and is being kept despite this:
without it, one-time/lazy initialisation (e.g. a module-level thread pool
spun up on first use) would be miscounted as leak growth on cycle 1,
producing false positives on every run -- a worse failure mode than the
blind spot it trades for. Today's leak is linear and unbounded from the
very first call (see the empirical result below), so this blind spot does
not hide it in practice; a hypothetical future fix that only closed an
"early calls" leak path while leaving today's "leaks forever, linearly"
path open would pass this guard silently. A pre-warm-up thread count is
printed in the diagnostics purely for visibility into this blind spot; it
does not gate the assertion.

The leak mechanism, as originally read from a stale v0.3.1 install during
planning: on Windows, ``WorktreeManager._teardown`` unconditionally calls
``_find_blocking_processes``, whose Pass 1c calls ``_win_handle_holders``,
which spins up a ``_BoundedQueryWorker`` backed by a daemon
``threading.Thread`` for every scan. In that older build, the worker was
never joined/shut down, so every remove leaked >=1 live thread
unconditionally.

**Empirical result against the actually-installed v0.3.3** (see the xfail
reason below, and ticket #111's change report for the full measured series):
v0.3.3 substantially reduced -- but did not eliminate -- the leak. It added
an explicit ``_MAX_WEDGED_HANDLE_WORKERS`` process-wide cap and
unconditionally joins/shuts down each scan's *initial* worker via a
``try/finally: worker.close()``. That closes the old unconditional leak, but
a scan's initial worker is *always* created regardless of whether the cap is
already full (a deliberate design choice -- see the "It is always shut down
via the try/finally below" comment block in the installed
``_win_handle_holders``), and if the query it runs genuinely wedges in
``NtQueryObject`` (documented by Microsoft to hang indefinitely for some
handle types, e.g. named pipes with no listener), that thread is never
joined and never counted against the cap for *future* scans -- so a
long-lived host process making many *sequential* ``worktree_remove`` calls
still leaks roughly one thread per call, once the cap has filled. These
tests are the empirical proof.

**Cross-reference (ticket #112):** a *separate* investigation into an
intermittent "Connection closed" symptom on Windows considered this ticket's
thread leak as a candidate cause and **falsified** it -- see
``tests/test_signal_resilience.py``'s module docstring for the full writeup.
The real #112 mechanism is a Windows ``CTRL_BREAK_EVENT`` delivery ambiguity
in the pinned engine's ``_send_graceful_signal``, entirely independent of
``_win_handle_holders``/the handle-scan code path this file's tests exercise.
``tests/test_signal_resilience.py::test_default_environment_stop_never_reaches_handle_scan``
is the executable proof that a default (``kill_orphans=False``)
``environment_stop`` call never reaches this file's leak-implicated code
path at all.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib_python_worktree import InMemoryStateStore, ManagerConfig, WorktreeManager


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _settle_thread_snapshot(
    min_stable_checks: int = 2, poll_interval: float = 0.1, max_wait: float = 1.5
) -> tuple[dict[threading.Thread, str], bool]:
    """Poll live threads until two consecutive samples agree on the exact set
    of ``Thread`` *objects* (or *max_wait* elapses), then return
    ``({thread: name}, settled)`` for that sample.

    ``threading.active_count()``/``threading.enumerate()`` counts every live
    thread in the whole pytest process, including ambient ones unrelated to
    the leak under test. Sampling by the ``Thread`` object itself (rather
    than raw count, and rather than ``Thread.ident``) lets callers diff the
    *set* of threads against a baseline, so unrelated ambient activity nets
    out and only genuinely new, still-alive threads count as growth.
    ``Thread.ident`` is an OS-recycled integer: if a baseline thread exits
    during the measured window, the OS can hand its ident to a brand-new
    leaked worker, which would then look "already in baseline" and be
    silently excluded -- a false negative. Keying off the object itself
    (callers hold a reference for the lifetime of the comparison, so it
    can't be garbage-collected and its id() reused) sidesteps that
    entirely, at no extra cost. The short poll-until-stable also avoids
    miscounting a thread that is merely mid-teardown (e.g. between
    create/remove returning and its worker thread actually exiting) as a
    permanent survivor, without a flat unconditional sleep on every cycle.

    The returned ``settled`` flag is False if two consecutive samples never
    matched within *max_wait* -- i.e. the snapshot may still reflect
    mid-teardown churn rather than a stable end state. Callers should
    surface this (e.g. in diagnostics) rather than assert on it: a
    non-converged sample looks identical to a real leak but isn't one.
    """
    deadline = time.monotonic() + max_wait
    prev_threads: frozenset[threading.Thread] | None = None
    consecutive = 0
    snapshot: dict[threading.Thread, str] = {}
    while True:
        snapshot = {t: t.name for t in threading.enumerate()}
        current = frozenset(snapshot)
        if current == prev_threads:
            consecutive += 1
            if consecutive >= min_stable_checks:
                return snapshot, True
        else:
            consecutive = 1
        prev_threads = current
        if time.monotonic() >= deadline:
            return snapshot, False
        time.sleep(poll_interval)


_WORKER_THREAD_NAME_SUFFIX = "(_run)"


def _is_leak_signature_thread(name: str) -> bool:
    """True if *name* matches the known leaked-worker thread signature.

    lib-python-worktree's ``_BoundedQueryWorker`` spins up unnamed
    ``threading.Thread(target=self._run, daemon=True)`` instances. Python's
    default thread naming appends the target callable's name in parentheses
    when no explicit name is given, so these threads surface as e.g.
    ``"Thread-7 (_run)"``. This is a deliberate name-based coupling to a
    third-party internal, justified by the ticket #111 evidence that every
    observed survivor matched this exact pattern -- it exists so that an
    unrelated persistent thread started by pytest/coverage/another plugin
    during a measured cycle is never mistaken for the leak. If upstream
    ever renames or restructures the worker, this filter simply stops
    matching; the unfiltered "any newly-appeared thread" count surfaced
    alongside it (see the callers below) is what keeps that drift visible
    instead of silently turning into a false "no leak" pass.
    """
    return name.endswith(_WORKER_THREAD_NAME_SUFFIX)


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


_XFAIL_REASON = (
    "lib-python-worktree v0.3.3 substantially reduced but did not eliminate a "
    "daemon-thread leak in _win_handle_holders(): _BoundedQueryWorker threads "
    "that genuinely wedge inside NtQueryObject (documented to hang "
    "indefinitely for certain handle types) are capped process-wide at "
    "_MAX_WEDGED_HANDLE_WORKERS=8 for *replacement* workers created within a "
    "single scan, but every scan -- i.e. every worktree_remove call on "
    "Windows, since manager._teardown reaches _find_blocking_processes Pass "
    "1c unconditionally -- still always creates its own initial worker "
    "regardless of whether the cap is already full, and that worker's thread "
    "is never joined if it wedges. So once the cap fills, each subsequent "
    "sequential scan leaks one more permanent thread the cap does not bound. "
    "Measured empirically (agent-worktree#111, 2026-08-17): baseline+8 by the "
    "end of the first scan (cap fill), then +1 thread per cycle for the "
    "remaining measured cycles, linear with no plateau. Re-confirmed with "
    "object-identity (Thread object, not the OS-recycled Thread.ident) "
    "measurement, isolating the leak from ambient thread activity, and with "
    "survivors additionally filtered to the known leak-signature thread name "
    "('Thread-N (_run)'): the leak-signature-filtered and unfiltered counts "
    "are identical, confirming every survivor is exactly a leaked "
    "'Thread-N (_run)' worker thread, +1 newly-appeared survivor per "
    "measured cycle, matching the raw-count figures above. Supersedes "
    "upstream Seretos/lib-python-worktree#90; tracked here as "
    "agent-worktree#111. Flip to a plain (non-xfail) test once a fixed pin "
    "lands."
)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "_win_handle_holders and _find_blocking_processes Pass 1c are "
        "sys.platform == 'win32'-gated in lib_python_worktree.core.process_lifecycle"
    ),
)
@pytest.mark.timeout(300)
@pytest.mark.xfail(strict=False, reason=_XFAIL_REASON)
def test_create_remove_cycles_do_not_leak_threads(tmp_path: Path, capsys):
    """Repeated worktree_create -> worktree_remove cycles through the
    registered MCP tool callables must not accumulate newly-appeared,
    still-alive leak-signature threads (identified by Thread object,
    relative to a post-warm-up baseline, then filtered to the known
    ``_BoundedQueryWorker`` ``"(_run)"`` name signature) linearly; the
    survivor count must plateau.

    Shape: 2 warm-up cycles (let any one-time thread-pool/module-level setup
    settle -- see the module docstring's "Known limitation" note on the
    baseline-after-warm-up blind spot this trades for) -> snapshot baseline
    thread objects -> 6 measured cycles, each settled and diffed against the
    baseline -> assert growth <= 2. A leaking engine abandons >=1 daemon
    thread per remove() and never joins it, so growth over 6 cycles would be
    >= 6 and this assertion would fail. Cycles whose snapshot never
    converges within the poll window are excluded from the growth figure the
    assertion uses (but still printed and named in ``unsettled_labels``),
    and a materially higher unfiltered-vs-filtered growth is treated as a
    filter-drift signal that fails the test even if the filtered growth
    alone would look like a plateau.
    """
    branch_names = [f"feature/leak-{i:02d}" for i in range(8)]
    repo = tmp_path / "src-repo"
    _make_repo_with_branches(repo, branch_names)

    pre_warmup_thread_count = len(threading.enumerate())

    mgr, fns = _make_tool_fixtures(tmp_path)

    series: list[dict[threading.Thread, str]] = []
    unsettled_labels: list[str] = []

    def _cycle(branch: str) -> tuple[dict[threading.Thread, str], bool]:
        create_result = fns["worktree_create"](repo_root=str(repo), branch=branch)
        assert "error" not in create_result, f"create failed: {create_result}"
        remove_result = fns["worktree_remove"](
            environment_id=create_result["id"]
        )
        assert "error" not in remove_result, f"remove failed: {remove_result}"
        return _settle_thread_snapshot()

    # Warm-up: let any one-time setup (module import side effects, etc.)
    # happen before establishing the baseline.
    for i in range(2):
        snapshot, settled = _cycle(branch_names[i])
        series.append(snapshot)
        if not settled:
            unsettled_labels.append(f"warm-up cycle {i}")

    baseline, baseline_settled = _settle_thread_snapshot()
    if not baseline_settled:
        unsettled_labels.append("baseline")
    baseline_threads = set(baseline)
    series_measured: list[dict[threading.Thread, str]] = []
    unfiltered_series: list[dict[threading.Thread, str]] = []
    cycle_settled: list[bool] = []

    for i in range(2, 8):
        snapshot, settled = _cycle(branch_names[i])
        cycle_settled.append(settled)
        if not settled:
            unsettled_labels.append(f"measured cycle {i - 2}")
        new_threads = {
            t: name for t, name in snapshot.items() if t not in baseline_threads
        }
        unfiltered_series.append(new_threads)
        survivors = {
            t: name for t, name in new_threads.items() if _is_leak_signature_thread(name)
        }
        series_measured.append(survivors)

    growth_series = [len(s) for s in series_measured]
    unfiltered_growth_series = [len(s) for s in unfiltered_series]
    survivor_names = sorted({name for s in series_measured for name in s.values()})

    # Non-converged cycles are excluded from the value the assertion is
    # computed over -- a mid-teardown snapshot looks identical to a real
    # leak but isn't one (see _settle_thread_snapshot's docstring) -- while
    # still being fully visible in the diagnostics above and in
    # unsettled_labels.
    settled_growth_series = [
        g for g, ok in zip(growth_series, cycle_settled) if ok
    ]
    settled_unfiltered_growth_series = [
        g for g, ok in zip(unfiltered_growth_series, cycle_settled) if ok
    ]

    with capsys.disabled():
        print(
            f"\n[ticket #111] pre-warm-up thread count (diagnostic only, "
            f"does not gate the assertion): {pre_warmup_thread_count}"
        )
        print(f"[ticket #111] warm-up thread counts: {[len(s) for s in series]}")
        print(f"[ticket #111] baseline thread count after warm-up: {len(baseline_threads)}")
        print(
            f"[ticket #111] measured leak-signature survivor-count series "
            f"(6 cycles): {growth_series}"
        )
        print(
            f"[ticket #111] measured unfiltered (any new thread) count series "
            f"(6 cycles): {unfiltered_growth_series}"
        )
        print(f"[ticket #111] survivor thread names: {survivor_names}")
        print(f"[ticket #111] cycles that failed to settle: {unsettled_labels}")

    if not settled_growth_series:
        pytest.fail(
            f"All {len(cycle_settled)} measured cycles failed to settle "
            f"within the poll window (unsettled_labels={unsettled_labels}) "
            "-- there is no converged sample left to compute a survivor-"
            "growth figure from. This is a runner/timing problem (see "
            "_settle_thread_snapshot's max_wait), not evidence either way "
            "about the leak; a guard that vacuously passed on an empty set "
            "here would be worse than no guard at all."
        )

    growth = max(settled_growth_series)
    unfiltered_growth = max(settled_unfiltered_growth_series)
    # If the unfiltered ("any newly-appeared thread") growth materially
    # exceeds the leak-signature-filtered growth, the filter itself may have
    # stopped matching (e.g. upstream renamed the worker thread) while the
    # underlying leak continues -- which would otherwise let `growth <= 2`
    # go green while threads are still piling up. The same plateau tolerance
    # (2) used for the growth assertion is reused as the divergence
    # threshold so a single incidental ambient thread doesn't trip this.
    filter_drift = (unfiltered_growth - growth) > 2

    with capsys.disabled():
        print(f"[ticket #111] growth (leak-signature): {growth}")
        print(f"[ticket #111] growth (unfiltered): {unfiltered_growth}")
        if filter_drift:
            print(
                f"[ticket #111] *** FILTER-DRIFT WARNING ***: unfiltered "
                f"growth ({unfiltered_growth}) exceeds leak-signature-"
                f"filtered growth ({growth}) by more than the plateau "
                f"tolerance -- the '{_WORKER_THREAD_NAME_SUFFIX}' name "
                "filter in _is_leak_signature_thread may no longer match "
                "the actual leaking worker thread (e.g. an upstream rename "
                "of _BoundedQueryWorker), which would let a real, ongoing "
                "leak pass this guard silently. Investigate before trusting "
                "a green result here."
            )

    assert growth <= 2 and not filter_drift, (
        f"{growth} newly-appeared, still-alive leak-signature thread(s) "
        f"(name matches '{_WORKER_THREAD_NAME_SUFFIX}') accumulated over "
        f"{len(settled_growth_series)} converged create/remove cycle(s) "
        f"(of 6 measured) relative to baseline "
        f"(per-cycle leak-signature survivor counts={growth_series}, "
        f"per-cycle unfiltered new-thread counts={unfiltered_growth_series}, "
        f"survivor thread names={survivor_names}, "
        f"cycles that failed to settle={unsettled_labels}, "
        f"filter_drift={filter_drift}) -- expected a plateau (growth <= 2) "
        f"with no filter drift, not linear per-cycle growth or a "
        f"filtered/unfiltered divergence"
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "_win_handle_holders and _find_blocking_processes Pass 1c are "
        "sys.platform == 'win32'-gated in lib_python_worktree.core.process_lifecycle"
    ),
)
@pytest.mark.timeout(300)
@pytest.mark.xfail(
    strict=False,
    reason=(
        _XFAIL_REASON
        + " This variant exercises the kill_blocking_processes=True call site "
        "(manager.py's other _find_blocking_processes/_kill_blocking_processes "
        "reach, ticket #44) and shows the same defect (measured growth=3 over "
        "3 cycles here vs. the <=2 plateau bound)."
    ),
)
def test_create_remove_cycles_with_kill_blocking_processes_do_not_leak_threads(
    tmp_path: Path, capsys
):
    """Same plateau requirement as test_create_remove_cycles_do_not_leak_threads,
    but through the kill_blocking_processes=True call site (manager.py's other
    _find_blocking_processes/_kill_blocking_processes reach, ticket #44).

    Smaller N (2 warm-up + 3 measured) since this path additionally exercises
    _kill_blocking_processes, which itself calls _find_blocking_processes.
    Same non-converged-cycle exclusion and filter-drift check as the other
    test in this module -- see its docstring and the module docstring's
    "Known limitation" note for the rationale.
    """
    branch_names = [f"feature/leak-kill-{i:02d}" for i in range(5)]
    repo = tmp_path / "src-repo"
    _make_repo_with_branches(repo, branch_names)

    pre_warmup_thread_count = len(threading.enumerate())

    mgr, fns = _make_tool_fixtures(tmp_path)

    series: list[dict[threading.Thread, str]] = []
    unsettled_labels: list[str] = []

    def _cycle(branch: str) -> tuple[dict[threading.Thread, str], bool]:
        create_result = fns["worktree_create"](repo_root=str(repo), branch=branch)
        assert "error" not in create_result, f"create failed: {create_result}"
        remove_result = fns["worktree_remove"](
            environment_id=create_result["id"],
            kill_blocking_processes=True,
        )
        assert "error" not in remove_result, f"remove failed: {remove_result}"
        return _settle_thread_snapshot()

    for i in range(2):
        snapshot, settled = _cycle(branch_names[i])
        series.append(snapshot)
        if not settled:
            unsettled_labels.append(f"warm-up cycle {i}")

    baseline, baseline_settled = _settle_thread_snapshot()
    if not baseline_settled:
        unsettled_labels.append("baseline")
    baseline_threads = set(baseline)
    series_measured: list[dict[threading.Thread, str]] = []
    unfiltered_series: list[dict[threading.Thread, str]] = []
    cycle_settled: list[bool] = []

    for i in range(2, 5):
        snapshot, settled = _cycle(branch_names[i])
        cycle_settled.append(settled)
        if not settled:
            unsettled_labels.append(f"measured cycle {i - 2}")
        new_threads = {
            t: name for t, name in snapshot.items() if t not in baseline_threads
        }
        unfiltered_series.append(new_threads)
        survivors = {
            t: name for t, name in new_threads.items() if _is_leak_signature_thread(name)
        }
        series_measured.append(survivors)

    growth_series = [len(s) for s in series_measured]
    unfiltered_growth_series = [len(s) for s in unfiltered_series]
    survivor_names = sorted({name for s in series_measured for name in s.values()})

    # See the other test's matching comment: non-converged cycles are
    # excluded from the assertion's growth figure but stay fully visible in
    # the diagnostics below and in unsettled_labels.
    settled_growth_series = [
        g for g, ok in zip(growth_series, cycle_settled) if ok
    ]
    settled_unfiltered_growth_series = [
        g for g, ok in zip(unfiltered_growth_series, cycle_settled) if ok
    ]

    with capsys.disabled():
        print(
            f"\n[ticket #111] (kill_blocking_processes) pre-warm-up thread "
            f"count (diagnostic only, does not gate the assertion): "
            f"{pre_warmup_thread_count}"
        )
        print(f"[ticket #111] (kill_blocking_processes) warm-up thread counts: {[len(s) for s in series]}")
        print(f"[ticket #111] (kill_blocking_processes) baseline thread count: {len(baseline_threads)}")
        print(
            f"[ticket #111] (kill_blocking_processes) measured leak-signature "
            f"survivor-count series (3 cycles): {growth_series}"
        )
        print(
            f"[ticket #111] (kill_blocking_processes) measured unfiltered "
            f"(any new thread) count series (3 cycles): {unfiltered_growth_series}"
        )
        print(f"[ticket #111] (kill_blocking_processes) survivor thread names: {survivor_names}")
        print(f"[ticket #111] (kill_blocking_processes) cycles that failed to settle: {unsettled_labels}")

    if not settled_growth_series:
        pytest.fail(
            f"All {len(cycle_settled)} measured cycles failed to settle "
            f"within the poll window (unsettled_labels={unsettled_labels}) "
            "-- there is no converged sample left to compute a survivor-"
            "growth figure from. This is a runner/timing problem (see "
            "_settle_thread_snapshot's max_wait), not evidence either way "
            "about the leak; a guard that vacuously passed on an empty set "
            "here would be worse than no guard at all."
        )

    growth = max(settled_growth_series)
    unfiltered_growth = max(settled_unfiltered_growth_series)
    # Same divergence check and rationale as the other test in this module.
    filter_drift = (unfiltered_growth - growth) > 2

    with capsys.disabled():
        print(f"[ticket #111] (kill_blocking_processes) growth (leak-signature): {growth}")
        print(f"[ticket #111] (kill_blocking_processes) growth (unfiltered): {unfiltered_growth}")
        if filter_drift:
            print(
                f"[ticket #111] (kill_blocking_processes) *** FILTER-DRIFT "
                f"WARNING ***: unfiltered growth ({unfiltered_growth}) "
                f"exceeds leak-signature-filtered growth ({growth}) by more "
                f"than the plateau tolerance -- the "
                f"'{_WORKER_THREAD_NAME_SUFFIX}' name filter in "
                "_is_leak_signature_thread may no longer match the actual "
                "leaking worker thread (e.g. an upstream rename of "
                "_BoundedQueryWorker), which would let a real, ongoing leak "
                "pass this guard silently. Investigate before trusting a "
                "green result here."
            )

    assert growth <= 2 and not filter_drift, (
        f"{growth} newly-appeared, still-alive leak-signature thread(s) "
        f"(name matches '{_WORKER_THREAD_NAME_SUFFIX}') accumulated over "
        f"{len(settled_growth_series)} converged "
        f"create/remove(kill_blocking_processes=True) cycle(s) (of 3 "
        f"measured) relative to baseline "
        f"(per-cycle leak-signature survivor counts={growth_series}, "
        f"per-cycle unfiltered new-thread counts={unfiltered_growth_series}, "
        f"survivor thread names={survivor_names}, "
        f"cycles that failed to settle={unsettled_labels}, "
        f"filter_drift={filter_drift}) -- expected a plateau (growth <= 2) "
        f"with no filter drift"
    )
