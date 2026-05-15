"""Contract module: parser + schema for `.worktree-setup.yml`.

Public surface:
- ``WorktreeContract`` — the validated top-level model.
- ``load`` / ``load_text`` — file/string loaders.
- ``ContractError`` / ``ContractValidationError`` — typed errors.
"""

from __future__ import annotations

from worktree_plugin.contract.loader import (
    ContractError,
    ContractValidationError,
    load,
    load_text,
)
from worktree_plugin.contract.schema import (
    Isolation,
    PortSlot,
    Step,
    WorktreeContract,
)

__all__ = (
    "ContractError",
    "ContractValidationError",
    "Isolation",
    "PortSlot",
    "Step",
    "WorktreeContract",
    "load",
    "load_text",
)
