"""Executable evidence for ticket #112 ("Connection closed (intermittent)").

Ticket #111 investigated an *earlier* symptom (unbounded daemon-thread
growth across ``worktree_create``/``worktree_remove`` cycles) and pinned
lib-python-worktree to v0.3.3 to substantially reduce it. Ticket #112 reports
a *different* symptom -- the MCP client's stdio connection intermittently
reporting "Connection closed" around worktree/environment lifecycle calls on
Windows -- that persisted after the #111 pin bump. Investigation for #112
falsified #111's thread leak as the cause of *this* symptom and pinned the
actual mechanism below. This module is the executable proof of both halves:
that #111 is not the cause (R2), and that the real mechanism reproduces and
is fixable (R1's sibling driving test in the same run, plus the raw repro
here).

The real mechanism
-------------------
On Windows, ``os.kill(pid, signal.CTRL_BREAK_EVENT)`` maps to the Win32 API
``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)`` -- but that API's second
argument is a **process *group* id**, not an arbitrary target PID. It is
only guaranteed to hit the intended process when *pid* is itself the leader
of its own process group (i.e. was spawned with
``CREATE_NEW_PROCESS_GROUP``, which makes the new process's own pid double
as its group id). Sent to any other PID, the OS is free to deliver the
event to whichever process(es) that "group id" actually names on the
system -- including, if the calling/host process (this MCP server) happens
to share that console/group, **the server itself**.

The pinned engine (``lib-python-worktree`` v0.3.11, importable at
``lib_python_worktree.core.process_lifecycle`` -- see
``.venv/Lib/site-packages/lib_python_worktree/core/process_lifecycle.py``
in this checkout) has exactly this gap:

- ``_send_graceful_signal()`` (process_lifecycle.py:819-836, verified
  against v0.3.11) does
  ``os.kill(pid, signal.CTRL_BREAK_EVENT)`` unconditionally on
  ``sys.platform == "win32"``, with **no check** that *pid* is a process
  group leader.
- It is called from two non-group-leader call sites:
  - ``_kill_process_tree`` (process_lifecycle.py:1579, verified against
    v0.3.11; called from the redesigned teardown module's
    ``_phase_stop_processes``), signalling every
    node of a discovered process *tree* -- children/grandchildren of the
    tracked pid, which are emphatically not group leaders of their own.
  - The orphan scan inside ``_kill_blocking_processes``, itself reached
    from ``stop()`` (process_lifecycle.py:3165, verified against v0.3.11),
    same story for orphaned grandchildren.
- ``_spawn_detached`` (process_lifecycle.py:441, comment at 469-479,
  verified against v0.3.11) spawns the tracked
  child with ``CREATE_NEW_PROCESS_GROUP`` alone, **without**
  ``DETACHED_PROCESS`` -- documented in-source (see the comment at that
  exact location) as deliberate, because ``DETACHED_PROCESS`` would sever
  the child from *every* console and make ``GenerateConsoleCtrlEvent``
  undeliverable to it at all. The tradeoff: the child (and, transitively,
  anything it spawns) stays attached to the *same console* the MCP server
  itself is attached to. A ctrl-break with a group id that doesn't cleanly
  resolve to just the intended child's own group can therefore be delivered
  back to that shared console -- reaching the server process too.
- POSIX already guards exactly this class of mistake:
  ``_signal_process_group`` (process_lifecycle.py:1416-1453, verified
  against v0.3.11) refuses to
  ``os.killpg`` unless *pid* is confirmed to be the leader of its own group
  (``os.getpgid(pid) == pid``) and that group is not the caller's own
  (``os.getpgid(pid) != os.getpgid(0)``). **Windows has no equivalent
  guard** -- ``_send_graceful_signal`` never performs an analogous check
  before calling ``os.kill(..., CTRL_BREAK_EVENT)``. That asymmetry is the
  defect.

Status at v0.3.12 (ticket #176)
--------------------------------
Upstream PR #151, shipped in the now-pinned v0.3.12, closed the gap
described above: ``_send_graceful_signal`` gained a ``group_leader``
keyword and now refuses/skips issuing ``CTRL_BREAK_EVENT`` at all unless
the caller has confirmed process-group leadership, bringing Windows to
parity with the POSIX guard already described below. The investigation
record above (written against v0.3.11) is kept as-is for its historical
and diagnostic value; the engine-side "no check"/"no equivalent guard"
language it uses describes that pinned version, not the current one. This
plugin's own SIGBREAK handler (``worktree_plugin.server``) remains
installed as a backstop / defence-in-depth layer rather than the sole
mitigation -- see its docstring for the current framing. The tests below
continue to guard this plugin's own handler and remain valid regardless
of the engine-side fix.

Manual repro recipe (outside pytest, for a future reader chasing a pin
bump): start any long-lived Windows console process (e.g.
``python -c "import time; time.sleep(60)"``) without
``CREATE_NEW_PROCESS_GROUP``, so it shares its console/group with the
caller. From another process in the same console, call
``os.kill(<that pid>, signal.CTRL_BREAK_EVENT)``. Observe that the ctrl-break
also reaches the *caller's own* console-attached process, not just the
named pid -- this is the same ambiguity ``GenerateConsoleCtrlEvent``'s
group-id semantics create for the engine's non-group-leader call sites
above.

Why #111 is not the cause (R2 below)
-------------------------------------
#111's own defect class was daemon threads leaked by
``_win_handle_holders`` (a ``_BoundedQueryWorker`` spun up per handle scan),
reached via ``_find_blocking_processes`` Pass 1c -- itself only reached from
``manager._teardown`` (``worktree_remove``'s teardown path) and from
``environment_stop``'s **opt-in** ``kill_orphans=True`` orphan scan
(process_lifecycle.py's ``stop()``, gated by ``if kill_orphans:`` just
before the scan runs). A *default* ``environment_stop`` call
(``kill_orphans=False``, the parameter's own default) never reaches that
gate at all -- it is a structurally different code path from the one this
ticket's symptom traces to (the ungated, unconditional
``_send_graceful_signal`` calls above). R2 proves this by driving the
registered ``environment_stop``/``environment_list`` MCP tool callables
through a real start/stop cycle with a monkeypatched
``_win_handle_holders`` that raises if invoked -- it must never fire.

Windows-only (mirrors the ``skipif`` gating style in
``tests/test_thread_leak_regression.py:230-236``): the mechanism this module
proves and guards is entirely ``sys.platform == "win32"``-specific --
``signal.CTRL_BREAK_EVENT``/``signal.SIGBREAK`` do not exist as OS-level
concepts elsewhere, and the engine code paths cited above are themselves
``sys.platform == "win32"``-gated.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PLATFORM_REASON = (
    "CTRL_BREAK_EVENT/SIGBREAK and the process-group signal-delivery "
    "mechanism this module proves and guards against are entirely "
    "Windows-specific; the engine code paths involved are themselves "
    "sys.platform == 'win32'-gated in lib_python_worktree.core.process_lifecycle"
)
_windows_only = pytest.mark.skipif(sys.platform != "win32", reason=_PLATFORM_REASON)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(base: Path, name: str = "src-repo") -> Path:
    """Build a real temp git repo with a committed README.

    Local copy of the equivalent helper in test_environment_tools.py --
    kept local to this file rather than imported across test modules, per
    the approved plan (mirroring test_thread_leak_regression.py's
    ``_make_tool_fixtures`` precedent for the same rationale).
    """
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


def _make_tool_fixtures(tmp_path: Path):
    """Return (mgr, fn_map) for tool-layer tests.

    Local copy of the helper in tests/test_worktree_tools.py:420-433 -- kept
    local to this file rather than imported across test modules, per the
    approved plan.
    """
    from mcp.server.fastmcp import FastMCP
    from lib_python_worktree import InMemoryStateStore, ManagerConfig, WorktreeManager
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


# ---------------------------------------------------------------------------
# R2: #111 falsification -- a default environment_stop never reaches the
# #111 handle-scan code path at all.
# ---------------------------------------------------------------------------


@_windows_only
@pytest.mark.timeout(60)
def test_default_environment_stop_never_reaches_handle_scan(tmp_path: Path, monkeypatch):
    """A default (``kill_orphans=False``) ``environment_stop`` call -- and
    ``environment_list`` -- must never invoke
    ``lib_python_worktree.core.process_lifecycle._win_handle_holders``, the
    function at the heart of #111's leak. This is the executable proof that
    #111's defect class and #112's symptom are unrelated: #112's mechanism
    (see this module's docstring) fires unconditionally, on every graceful
    stop signal, entirely independent of ``kill_orphans``/the handle scan.
    """
    import lib_python_worktree.core.process_lifecycle as process_lifecycle

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "_win_handle_holders must never be invoked by a default "
            "(kill_orphans=False) environment_stop or by environment_list "
            "-- #111's defect class is structurally unrelated to #112's "
            "mechanism (see this module's docstring)"
        )

    monkeypatch.setattr(process_lifecycle, "_win_handle_holders", _must_not_be_called)

    repo = _make_repo(tmp_path)
    # Deliberately long-lived (mirrors the time.sleep(30)-style pattern used
    # by test_server_survives_ctrl_break_event below), rather than an
    # immediately-exiting command like `echo hi`. This keeps the test from
    # depending on the non-obvious engine internal that an early-exiting
    # process's pid is still tracked/handled gracefully by stop() -- with a
    # genuinely running process, environment_stop() always exercises a real,
    # unambiguous graceful-stop path instead of racing process exit.
    _write_contract(
        repo,
        'version: 1\nisolation: full\nstart:\n  - run: python -c "import time; time.sleep(30)"\n',
    )

    mgr, fns = _make_tool_fixtures(tmp_path)

    listed = fns["environment_list"](path=str(repo))
    assert listed, "expected at least the synthesised primary entry"

    started = fns["environment_start"](checkout_path=str(repo))
    assert "error" not in started, f"start failed: {started}"

    stopped = fns["environment_stop"](checkout_path=str(repo))
    assert "error" not in stopped, f"stop failed: {stopped}"
    assert stopped["status"] == "stopped"


# ---------------------------------------------------------------------------
# R2 (raw repro): the mechanism is real -- an unguarded process-group leader
# genuinely dies from CTRL_BREAK_EVENT with no signal handler installed.
# ---------------------------------------------------------------------------


@_windows_only
@pytest.mark.timeout(30)
def test_unguarded_child_dies_on_ctrl_break():
    """Raw repro of the #112 mechanism's *effect*: a process spawned with
    ``CREATE_NEW_PROCESS_GROUP`` and no ``CTRL_BREAK_EVENT``/``SIGBREAK``
    handler dies when it receives one, via Python's default (SIG_DFL)
    disposition.

    This test intentionally installs **no** guard -- it is the repro
    artifact, not the fix. It is expected to keep passing both before and
    after the Phase B guard lands (it never installs the guard), and its
    purpose is proving the underlying OS mechanism is real and stays real,
    as a control paired against ``test_server_survives_ctrl_break_event``
    (which does install the guard and must NOT die the same way).

    Safety: only ``child.pid`` -- the spawned child's own new process group
    (guaranteed distinct from this test process's group by
    ``CREATE_NEW_PROCESS_GROUP``) -- is ever signalled. Never process group
    0 or this test runner's own group.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = child.stdout.readline()
        assert ready_line.strip() == "ready", (
            f"child did not report ready in time: {ready_line!r}"
        )

        os.kill(child.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]

        try:
            returncode = child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "unguarded child survived CTRL_BREAK_EVENT for 5s -- the "
                "#112 mechanism this test exists to reproduce did not fire; "
                "see this module's docstring for the expected mechanism"
            )

        assert returncode != 0, (
            f"expected the unguarded child to terminate abnormally from "
            f"CTRL_BREAK_EVENT (no handler installed -> SIG_DFL), got a "
            f"clean returncode={returncode}"
        )
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


# ---------------------------------------------------------------------------
# R1: the SIGBREAK guard actually protects the server (driving test).
# ---------------------------------------------------------------------------


@_windows_only
@pytest.mark.timeout(30)
def test_server_survives_ctrl_break_event():
    """R1 driving test (RED before Phase B, GREEN after).

    Same shape as ``test_unguarded_child_dies_on_ctrl_break`` above, except
    the spawned child installs ``worktree_plugin.server._install_signal_guards``
    before reporting ready. With the guard installed, the same
    ``CTRL_BREAK_EVENT`` that kills the unguarded control above must NOT
    kill this process.

    RED (pre-Phase-B): ``ImportError``/``AttributeError`` from the child's
    ``-c`` script (``_install_signal_guards`` does not exist yet), surfaced
    as a non-zero/garbage returncode and no "ready" line -- or, if the
    import somehow succeeds without a real guard, the child dying the same
    way the control test's child does.
    GREEN (post-Phase-B): the child prints "ready", receives the
    ctrl-break, and is still alive after the settle window.

    Safety: only ``child.pid`` (the spawned child's own new process group)
    is ever signalled, matching the sibling repro test above.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, time; "
                "from worktree_plugin.server import _install_signal_guards; "
                "_install_signal_guards(); "
                "print('ready', flush=True); "
                "time.sleep(30)"
            ),
        ],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = child.stdout.readline()
        assert ready_line.strip() == "ready", (
            f"guarded child did not report ready in time (stdout so far: "
            f"{ready_line!r}) -- likely an ImportError/AttributeError "
            f"before _install_signal_guards exists"
        )

        os.kill(child.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]

        # Settle window: give the OS time to actually deliver the event and
        # (if unguarded) for the default disposition to terminate the
        # process, before checking it is still alive.
        time.sleep(1.5)

        assert child.poll() is None, (
            "guarded child died despite the installed SIGBREAK guard -- "
            "the #112 fix in worktree_plugin.server did not hold"
        )
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
        else:
            child.wait(timeout=5)


# ---------------------------------------------------------------------------
# R1 additional coverage: guard-installation contract, in-process.
# ---------------------------------------------------------------------------


@_windows_only
def test_install_signal_guards_is_idempotent():
    """Calling ``_install_signal_guards`` more than once must not raise and
    must leave the same handler installed (re-installing is harmless)."""
    import worktree_plugin.server as server

    server._install_signal_guards()
    server._install_signal_guards()

    assert signal.getsignal(signal.SIGBREAK) is server._ignore_and_log  # type: ignore[attr-defined]


def test_install_signal_guards_noop_without_sigbreak(monkeypatch):
    """On a platform with no ``signal.SIGBREAK`` (POSIX, and this test
    artificially simulates that on any platform including Windows by
    removing the attribute for its duration), ``_install_signal_guards``
    must be a pure no-op -- it must never call ``signal.signal`` at all."""
    import worktree_plugin.server as server

    if hasattr(signal, "SIGBREAK"):
        monkeypatch.delattr(signal, "SIGBREAK", raising=False)

    calls = []
    monkeypatch.setattr(signal, "signal", lambda *a, **k: calls.append((a, k)))

    server._install_signal_guards()

    assert calls == [], (
        f"expected no signal.signal() call when SIGBREAK is absent, got: {calls!r}"
    )


def test_install_signal_guards_leaves_sigint_at_default():
    """SIGINT's disposition must be completely untouched by
    ``_install_signal_guards`` -- makes the plan's Q3 contract executable."""
    import worktree_plugin.server as server

    before = signal.getsignal(signal.SIGINT)
    server._install_signal_guards()
    after = signal.getsignal(signal.SIGINT)

    assert after == before, (
        f"SIGINT disposition changed: before={before!r}, after={after!r}"
    )


def test_main_installs_guards_before_run(monkeypatch):
    """``main()`` must call ``_install_signal_guards()`` before ``mcp.run()``,
    never after -- installing the guard after the server is already running
    the event loop would be too late to protect the startup window."""
    import worktree_plugin.server as server

    call_order: list[str] = []

    monkeypatch.setattr(
        server, "_install_signal_guards", lambda: call_order.append("install")
    )
    monkeypatch.setattr(server.mcp, "run", lambda: call_order.append("run"))

    server.main()

    assert call_order == ["install", "run"], (
        f"expected _install_signal_guards() before mcp.run(), got order: {call_order!r}"
    )


def test_ignore_and_log_returns_none_and_logs(caplog, capsys):
    """The handler must swallow the event (return ``None``, the sentinel
    Python's signal machinery expects to continue normal execution), log at
    WARNING, and never write anything to stdout -- stdout is the JSON-RPC
    transport under stdio, so any stray print there would corrupt the
    protocol stream."""
    import logging

    import worktree_plugin.server as server

    fake_signum = getattr(signal, "SIGBREAK", signal.SIGTERM)

    with caplog.at_level(logging.WARNING, logger="worktree_plugin.server"):
        result = server._ignore_and_log(fake_signum, None)

    assert result is None
    assert any(record.levelno == logging.WARNING for record in caplog.records), (
        f"expected a WARNING-level log record, got: {caplog.records!r}"
    )

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"handler must never write to stdout (the JSON-RPC transport), got: {captured.out!r}"
    )
