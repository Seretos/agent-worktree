"""FastMCP server bootstrap for the agent-worktree plugin."""

from __future__ import annotations

import logging
import signal

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import WorktreeManager
from worktree_plugin.config import build_manager_config
from worktree_plugin.tools import worktree as worktree_tools

mcp = FastMCP("worktree")

# Single process-level manager. Replaced/reconfigured in tests by injecting
# a fresh ``WorktreeManager`` and re-registering tools against a private
# FastMCP instance.
_manager = WorktreeManager(config=build_manager_config())
worktree_tools.register(mcp, _manager)

_logger = logging.getLogger(__name__)


def _ignore_and_log(signum: int, frame) -> None:
    """SIGBREAK handler: log and swallow the event instead of letting
    Python's default disposition terminate this process (ticket #112).

    On Windows, ``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)`` -- what
    ``os.kill(pid, signal.CTRL_BREAK_EVENT)`` maps to -- takes a *process
    group id*, not an arbitrary target pid, so a stray ctrl-break can in
    principle land on this server's own console-attached process instead
    of (or in addition to) the intended target. As of the pinned v0.3.12,
    the engine's own ``_send_graceful_signal`` (see
    ``tests/test_signal_resilience.py``'s module docstring for the full
    mechanism, with exact source line citations) refuses/skips issuing
    ``CTRL_BREAK_EVENT`` at all unless the caller has confirmed
    process-group leadership (upstream PR #151), which closes off the
    non-group-leader call sites that used to make this stray-delivery
    scenario possible from inside the engine. This handler remains
    installed as a backstop / defence-in-depth layer: it swallows any
    SIGBREAK that still reaches this process -- whether from some other
    source entirely, or as a hedge against the engine's guard -- rather
    than letting the interpreter's default SIGBREAK disposition kill the
    process.

    Deliberately never uses ``print()``/writes to stdout: under the stdio
    MCP transport, stdout *is* the JSON-RPC channel, and any stray byte
    there would corrupt the protocol stream. Logging goes through the
    standard ``logging`` module instead.

    Returns ``None`` -- the value Python's signal machinery expects from a
    handler that has fully handled the event and wants execution to
    continue normally.
    """
    _logger.warning(
        "Received signal %s (SIGBREAK) -- ignoring. This is expected when "
        "a stray Windows CTRL_BREAK_EVENT aimed at a different process is "
        "delivered to this server's console/process group instead (ticket "
        "#112); it does not indicate this server was asked to shut down.",
        signum,
    )
    return None


def _install_signal_guards() -> None:
    """Install the SIGBREAK guard so a stray Windows CTRL_BREAK_EVENT aimed
    at a different process cannot kill this server (ticket #112).

    As of the pinned v0.3.12, this guard is a backstop / defence-in-depth
    layer on top of the engine's own ``_send_graceful_signal`` group-leader
    check (upstream PR #151) rather than the only mitigation in play; see
    ``_ignore_and_log``'s docstring above for the full picture.

    POSIX has no ``SIGBREAK`` -- this is a no-op there (and in any test
    environment where the attribute has been removed to simulate that).

    ``SIGINT`` is deliberately left completely untouched: this function
    never calls ``signal.signal`` for it, and never will -- Ctrl+C must
    keep working exactly as Python's default disposition already handles
    it.

    Tradeoff (intentional): this handler unconditionally swallows *every*
    ``SIGBREAK`` it receives, including a hypothetical legitimate one aimed
    at this process itself (e.g. an operator's own Ctrl+Break, or some
    launcher/supervisor that might use ``CTRL_BREAK_EVENT`` for graceful
    teardown). Windows delivers no metadata distinguishing "stray, meant
    for a child sharing our console" from "intentional, meant for us", so
    there is no way to swallow only the former. This is accepted because
    MCP stdio clients never use ``CTRL_BREAK_EVENT`` to stop this server --
    they terminate it by killing the process or closing stdin -- so no
    legitimate in-band shutdown path is broken by this unconditional
    guard. ``SIGINT`` (Ctrl+C) remains fully functional and untouched as
    the interactive stop mechanism.

    Idempotent: calling this more than once simply re-installs the same
    handler; safe to call from ``main()`` on every startup.
    """
    if not hasattr(signal, "SIGBREAK"):
        return
    signal.signal(signal.SIGBREAK, _ignore_and_log)  # type: ignore[attr-defined]


def main() -> None:
    _install_signal_guards()
    mcp.run()
