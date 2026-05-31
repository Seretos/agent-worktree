"""FastMCP tools for the worktree lifecycle (W2).

These wrap ``WorktreeManager`` and shape its outputs into MCP-friendly
plain-dict payloads.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    ProcessAlreadyRunningError,
    ProcessLifecycleError,
    ProcessNotRunningError,
    WorktreeError,
    WorktreeManager,
    WorktreeNotFoundError,
    WorktreeRecord,
)


def _record_to_dict(record: WorktreeRecord) -> Dict[str, Any]:
    return asdict(record)


def register(mcp: FastMCP, manager: WorktreeManager) -> None:
    """Register the W2 tools against the given FastMCP server."""

    @mcp.tool()
    def worktree_create(
        repo_root: str,
        branch: str,
        base: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a git worktree for ``branch`` rooted at ``repo_root``.

        ``base`` must be the name of an existing local branch (not a SHA,
        ``HEAD``, or remote ref); omit when ``branch`` already exists.

        The ``ports`` field is a dict mapping port name to host port number;
        empty dict ``{}`` for ``isolation: none`` worktrees or before setup
        runs. Agents read it to discover which host ports the worktree's
        services are bound to.

        Returns the canonical worktree record. Fields of note:

        - ``id``: follows the pattern ``<repo-slug>-<branch-slug>-<8-hex>``
          where slugs are lower-case ASCII with non-alphanumeric runs collapsed
          to ``-``; ids are not stable across remove/re-create cycles.
        - ``path``: absolute checkout location under
          ``<store_root>/<repo_slug>/<id>/`` where ``store_root`` defaults to
          ``~/agent-worktree-store`` or the value of ``$WORKTREE_STORE_ROOT``.
        - ``warning`` (optional): present when ``repo_root`` was silently
          re-rooted to the actual git repository root (e.g. when a subdirectory
          was passed). The field contains the original and resolved paths.
        """

        try:
            record = manager.create(repo_root=repo_root, branch=branch, base=base)
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc

        result = _record_to_dict(record)

        # Emit a warning when the caller's repo_root was silently re-rooted to
        # the actual git repository root (e.g. a subdirectory was passed).
        resolved_input = Path(repo_root).expanduser().resolve()
        resolved_record = Path(record.repo_root).resolve()
        if resolved_input != resolved_record:
            result["warning"] = (
                f"repo_root was re-rooted from '{repo_root}' to '{record.repo_root}'"
            )

        return result

    @mcp.tool()
    def worktree_list(repo_root: Optional[str] = None) -> List[Dict[str, Any]]:
        """List worktrees currently tracked in the server's in-memory state
        (process-scoped; does not survive a server restart).

        Parameters
        ----------
        repo_root:
            Optional path to a git repository root. When provided, only
            worktrees whose ``repo_root`` resolves to the same directory are
            returned (subdirectory paths are resolved via ``Path.resolve()``
            before comparison). Omit to return all worktrees across all repos.

        Each entry mirrors a ``WorktreeRecord``. The ``ports`` field is a dict
        mapping port name to host port number; empty dict ``{}`` for
        ``isolation: none`` worktrees or before setup runs. Agents read it to
        discover which host ports the worktree's services are bound to.
        """

        records = manager.list()

        if repo_root is not None:
            resolved_filter = Path(repo_root).expanduser().resolve()
            records = [
                r for r in records
                if Path(r.repo_root).resolve() == resolved_filter
            ]

        return [_record_to_dict(r) for r in records]

    @mcp.tool()
    def worktree_remove(worktree_id: str, force: bool = False) -> Dict[str, Any]:
        """Remove a tracked worktree by id.

        Passes through to the manager's teardown hook (W8 will extend this
        with full teardown semantics).

        Returns the removed worktree record on success. The ``ports`` field is
        a dict mapping port name to host port number; empty dict ``{}`` for
        ``isolation: none`` worktrees or before setup runs. Agents read it to
        discover which host ports the worktree's services are bound to.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        try:
            record = manager.remove(worktree_id, force=force)
        except WorktreeNotFoundError:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_start(
        worktree_id: str,
        role: str = "main",
        cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a detached process for a tracked worktree.

        The command to run is **not** supplied by the caller — it is read from
        the worktree contract's ``start:`` field in ``.seretos/worktree-setup.yml``
        inside the worktree. Exactly one ``start:`` step must be configured;
        a missing or ambiguous ``start:`` surfaces as a ``ValueError``.

        Parameters
        ----------
        worktree_id:
            The id of the worktree (as returned by ``worktree_create`` or
            ``worktree_list``).
        role:
            Logical role name for the process; defaults to ``"main"``. Multiple
            processes can be attached to one worktree under different roles.
        cwd:
            Working directory for the spawned process. When omitted the worktree
            path is used by the underlying engine.

        The operation is idempotent in the sense that if a process is already
        running under the given ``role``, this tool returns a soft error dict
        ``{"error": "..."}`` rather than raising, so callers can treat the
        already-running case gracefully.

        On success returns the canonical worktree record dict. Fields of note:

        - ``status``: ``"running"`` when the process started successfully.
        - ``pids``: a dict mapping role name to PID (e.g. ``{"main": 12345}``).
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` before port setup runs.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        try:
            record = manager.start(worktree_id, role=role, cwd=cwd)
        except WorktreeNotFoundError:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        except ProcessAlreadyRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_stop(
        worktree_id: str,
        role: str = "main",
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Stop the process running under a given role for a tracked worktree.

        Parameters
        ----------
        worktree_id:
            The id of the worktree (as returned by ``worktree_create`` or
            ``worktree_list``).
        role:
            Logical role name of the process to stop; defaults to ``"main"``.
        timeout:
            Seconds to wait for graceful shutdown (SIGTERM/CtrlBreak) before
            the process is forcibly killed (SIGKILL/TerminateProcess). Defaults
            to ``10.0``.

        Any contract ``stop:`` steps defined in ``.seretos/worktree-setup.yml``
        are executed best-effort before the graceful SIGTERM/CtrlBreak signal is
        sent; failures in those steps are logged but do not prevent the process
        from being stopped.

        The operation is idempotent in the sense that if no process is running
        under the given ``role``, this tool returns a soft error dict
        ``{"error": "..."}`` rather than raising, so callers can treat the
        already-stopped case gracefully.

        On success returns the canonical worktree record dict. Fields of note:

        - ``status``: ``"stopped"`` after the process has been terminated.
        - ``pids``: a dict mapping role name to PID; the stopped role's entry
          is removed once the process exits.
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` for worktrees with no port setup.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        try:
            record = manager.stop(worktree_id, role=role, timeout=timeout)
        except WorktreeNotFoundError:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        except ProcessNotRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_get(worktree_id: str) -> Dict[str, Any]:
        """Retrieve a single tracked worktree by id without removing it.

        Returns the canonical worktree record on success. Fields of note:

        - ``id``: follows the pattern ``<repo-slug>-<branch-slug>-<8-hex>``
          where slugs are lower-case ASCII with non-alphanumeric runs collapsed
          to ``-``; ids are not stable across remove/re-create cycles.
        - ``path``: absolute checkout location under
          ``<store_root>/<repo_slug>/<id>/`` where ``store_root`` defaults to
          ``~/agent-worktree-store`` or the value of ``$WORKTREE_STORE_ROOT``.
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` for ``isolation: none`` worktrees or before setup runs.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        record = manager.state.get(worktree_id)
        if record is None:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        return _record_to_dict(record)


__all__ = ("register",)
