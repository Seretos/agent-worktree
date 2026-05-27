"""FastMCP server bootstrap for the agent-worktree plugin."""

from __future__ import annotations

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


def main() -> None:
    mcp.run()
