"""FastMCP tools for the worktree lifecycle (W2).

These wrap ``WorktreeManager`` and shape its outputs into MCP-friendly
plain-dict payloads.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import WorktreeError, WorktreeManager, WorktreeNotFoundError, WorktreeRecord


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

        ``base`` is required when ``branch`` does not already exist (pass an
        existing branch name or commit-ish to create it from); ignored when
        ``branch`` already exists.

        Returns the canonical worktree record. The ``ports`` field is a list of
        named port reservations declared in setup config — populated after setup
        runs, empty (``[]``) for isolation ``none`` worktrees or before setup
        has executed; agents read it to discover which host ports the worktree's
        services are bound to.
        """

        try:
            record = manager.create(repo_root=repo_root, branch=branch, base=base)
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_list() -> List[Dict[str, Any]]:
        """List all worktrees currently tracked in the server's in-memory state
        (process-scoped; does not survive a server restart).

        Each entry mirrors a ``WorktreeRecord``. The ``ports`` field is a list
        of named port reservations declared in setup config — populated after
        setup runs, empty (``[]``) for isolation ``none`` worktrees or before
        setup has executed; agents read it to discover which host ports the
        worktree's services are bound to.
        """

        return [_record_to_dict(r) for r in manager.list()]

    @mcp.tool()
    def worktree_remove(worktree_id: str, force: bool = False) -> Dict[str, Any]:
        """Remove a tracked worktree by id.

        Passes through to the manager's teardown hook (W8 will extend this
        with full teardown semantics).

        Returns the removed worktree record on success. The ``ports`` field is
        a list of named port reservations declared in setup config — populated
        after setup runs, empty (``[]``) for isolation ``none`` worktrees or
        before setup has executed; agents read it to discover which host ports
        the worktree's services are bound to.

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


__all__ = ("register",)
