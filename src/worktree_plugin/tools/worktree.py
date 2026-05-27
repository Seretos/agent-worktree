"""FastMCP tools for the worktree lifecycle (W2).

These wrap ``WorktreeManager`` and shape its outputs into MCP-friendly
plain-dict payloads.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import WorktreeError, WorktreeManager, WorktreeRecord


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

        If ``branch`` does not exist yet, pass ``base`` (an existing branch or
        commit-ish) to create it. Returns the canonical worktree record.
        """

        try:
            record = manager.create(repo_root=repo_root, branch=branch, base=base)
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_list() -> List[Dict[str, Any]]:
        """List all worktrees currently tracked by this MCP session."""

        return [_record_to_dict(r) for r in manager.list()]

    @mcp.tool()
    def worktree_remove(worktree_id: str, force: bool = False) -> Dict[str, Any]:
        """Remove a tracked worktree by id.

        Passes through to the manager's teardown hook (W8 will extend this
        with full teardown semantics).
        """

        try:
            record = manager.remove(worktree_id, force=force)
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)


__all__ = ("register",)
