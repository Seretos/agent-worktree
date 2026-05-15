"""MCP-config loader for the agent-worktree plugin.

Phase 1 reads from process env. `WORKTREE_PORT_RANGE` accepts ``"30000-40000"``.
Defaults follow plan decisions:
- D1 (W4, Option B): port range ``30000-40000``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

DEFAULT_PORT_RANGE: Tuple[int, int] = (30000, 40000)
PORT_RANGE_ENV = "WORKTREE_PORT_RANGE"
PORT_STORE_ENV = "WORKTREE_PORT_STORE"


@dataclass(frozen=True)
class PortConfig:
    range_start: int
    range_end: int  # inclusive

    @property
    def size(self) -> int:
        return self.range_end - self.range_start + 1


def parse_port_range(raw: str) -> Tuple[int, int]:
    """Parse ``"<start>-<end>"`` into an inclusive tuple."""

    if "-" not in raw:
        raise ValueError(
            f"invalid port range {raw!r}; expected 'START-END' (inclusive)"
        )
    start_s, end_s = raw.split("-", 1)
    try:
        start = int(start_s.strip())
        end = int(end_s.strip())
    except ValueError as exc:
        raise ValueError(f"port range bounds must be integers: {raw!r}") from exc
    if start <= 0 or end <= 0:
        raise ValueError(f"port range must use positive ports: {raw!r}")
    if end < start:
        raise ValueError(f"port range end must be >= start: {raw!r}")
    if end > 65535:
        raise ValueError(f"port range end exceeds 65535: {raw!r}")
    return start, end


def load_port_config(env: Optional[dict] = None) -> PortConfig:
    environ = env if env is not None else os.environ
    raw = environ.get(PORT_RANGE_ENV)
    if raw:
        start, end = parse_port_range(raw)
    else:
        start, end = DEFAULT_PORT_RANGE
    return PortConfig(range_start=start, range_end=end)


__all__ = (
    "DEFAULT_PORT_RANGE",
    "PORT_RANGE_ENV",
    "PORT_STORE_ENV",
    "PortConfig",
    "load_port_config",
    "parse_port_range",
)
