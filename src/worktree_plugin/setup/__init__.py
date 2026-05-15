"""Setup-script execution for the agent-worktree plugin (W5)."""

from worktree_plugin.setup.runner import (
    SetupFailedError,
    SetupResult,
    SetupRunner,
    SetupStep,
    SetupStepResult,
    log_dir_for,
)

__all__ = (
    "SetupFailedError",
    "SetupResult",
    "SetupRunner",
    "SetupStep",
    "SetupStepResult",
    "log_dir_for",
)
