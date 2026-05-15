"""Global port allocator for worktree slots (W4).

Decisions from the plan-comment:
- D1 (Option B): default range ``30000-40000``, see ``core.config``.
- D2 (Option B): "free" = not in our store AND bindable on ``127.0.0.1``.

State persistence uses ``<store_path>`` (default ``~/.agent-worktree/ports.yaml``)
guarded by ``portalocker`` for cross-process safety. W7 will migrate this into
the shared state file; until then the schema is intentionally narrow:

    {
      "next_port": <int>,                    # cursor for linear search
      "allocations": {
        "<worktree_id>": { "<slot>": <int>, ... }
      }
    }
"""

from __future__ import annotations

import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional

import portalocker
import yaml

from worktree_plugin.core.config import (
    DEFAULT_PORT_RANGE,
    PORT_STORE_ENV,
    PortConfig,
    load_port_config,
)


DEFAULT_STORE_PATH = Path("~/.agent-worktree/ports.yaml").expanduser()


class PortError(RuntimeError):
    """Base class for allocator failures surfaced to the MCP tool."""


class PortPoolExhaustedError(PortError):
    """Raised when no free port can be found in the configured range."""


class UnknownPortAllocationError(PortError):
    """Raised when ``release`` is called for an id the allocator doesn't know."""


@dataclass
class _State:
    next_port: int
    allocations: Dict[str, Dict[str, int]]

    @classmethod
    def empty(cls, start: int) -> "_State":
        return cls(next_port=start, allocations={})

    def to_yaml_dict(self) -> dict:
        return {"next_port": self.next_port, "allocations": self.allocations}

    @classmethod
    def from_yaml_dict(cls, data: Optional[dict], start: int) -> "_State":
        if not data:
            return cls.empty(start)
        return cls(
            next_port=int(data.get("next_port", start)),
            allocations={
                str(k): {str(sk): int(sv) for sk, sv in (v or {}).items()}
                for k, v in (data.get("allocations") or {}).items()
            },
        )

    def all_used_ports(self) -> set[int]:
        return {p for slots in self.allocations.values() for p in slots.values()}


def _is_port_bindable(port: int) -> bool:
    """Probe whether ``127.0.0.1:port`` accepts a fresh bind (D2, Option B)."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR=0 is the default; we explicitly avoid SO_REUSEADDR=1
        # because that lets us "succeed" on a port someone else is using.
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


class PortAllocator:
    """Globally-coordinated allocator for worktree port slots.

    Threads within one process are coordinated by ``_thread_lock``;
    cross-process safety comes from ``portalocker`` around the YAML state
    file.
    """

    def __init__(
        self,
        config: Optional[PortConfig] = None,
        store_path: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.config = config or load_port_config(dict(env) if env else None)
        environ = env if env is not None else os.environ
        raw = environ.get(PORT_STORE_ENV)
        if store_path is not None:
            self.store_path = Path(store_path)
        elif raw:
            self.store_path = Path(raw).expanduser()
        else:
            self.store_path = DEFAULT_STORE_PATH
        self._thread_lock = threading.Lock()

    # ---- public API ----

    def allocate(
        self, worktree_id: str, slot_names: Iterable[str]
    ) -> Dict[str, int]:
        """Allocate one port per slot name. Returns the slot→port mapping.

        Idempotent for an already-allocated ``worktree_id``: if the exact same
        slot set was previously allocated, the existing mapping is returned;
        otherwise a ``PortError`` is raised so callers don't silently mutate.
        """

        names = list(slot_names)
        seen: set[str] = set()
        for n in names:
            if n in seen:
                raise PortError(f"duplicate slot name in request: {n}")
            seen.add(n)

        with self._thread_lock, self._locked_state() as ctx:
            state = ctx["state"]
            existing = state.allocations.get(worktree_id)
            if existing is not None:
                if set(existing.keys()) == set(names):
                    return dict(existing)
                raise PortError(
                    f"worktree {worktree_id!r} already has allocations "
                    f"for slots {sorted(existing)}; cannot extend in W4"
                )

            used = state.all_used_ports()
            new_mapping: Dict[str, int] = {}
            cursor = state.next_port
            for slot in names:
                port = self._find_free_port(cursor, used)
                new_mapping[slot] = port
                used.add(port)
                cursor = port + 1
                if cursor > self.config.range_end:
                    cursor = self.config.range_start

            state.allocations[worktree_id] = new_mapping
            state.next_port = cursor
            ctx["dirty"] = True
            return dict(new_mapping)

    def release(self, worktree_id: str) -> Dict[str, int]:
        """Release all slots for ``worktree_id``. Returns the freed mapping."""

        with self._thread_lock, self._locked_state() as ctx:
            state = ctx["state"]
            mapping = state.allocations.pop(worktree_id, None)
            if mapping is None:
                raise UnknownPortAllocationError(
                    f"no port allocation for worktree {worktree_id!r}"
                )
            ctx["dirty"] = True
            return mapping

    def list_allocations(self) -> Dict[str, Dict[str, int]]:
        with self._thread_lock, self._locked_state() as ctx:
            return {k: dict(v) for k, v in ctx["state"].allocations.items()}

    # ---- internals ----

    def _find_free_port(self, cursor: int, used: set[int]) -> int:
        start, end = self.config.range_start, self.config.range_end
        if cursor < start or cursor > end:
            cursor = start
        size = self.config.size
        port = cursor
        for _ in range(size):
            if port not in used and _is_port_bindable(port):
                return port
            port += 1
            if port > end:
                port = start
        raise PortPoolExhaustedError(
            f"no free port in range {start}-{end} (allocator pool exhausted)"
        )

    @contextmanager
    def _locked_state(self) -> Iterator[dict]:
        """Open the YAML store under an exclusive cross-process lock.

        Yields a small dict ``{"state": _State, "dirty": bool}``. Setting
        ``dirty = True`` causes the file to be rewritten through the same
        handle on exit — important on Windows where a second writer can't
        open a file already held under an exclusive lock by portalocker.
        """

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        # 'r+' wants an existing file, 'w+' truncates. Use 'a+' + manual seek
        # so we can both read existing state and rewrite it through fh.
        # LOCK_EX | LOCK_NB so portalocker honors `timeout` (which uses
        # exponential backoff between retries). Pure LOCK_EX is a blocking
        # syscall and ignores the timeout parameter.
        with portalocker.Lock(
            str(self.store_path),
            mode="a+",
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            timeout=10,
        ) as fh:
            fh.seek(0)
            raw = fh.read()
            data = yaml.safe_load(raw) if raw.strip() else None
            state = _State.from_yaml_dict(
                data if isinstance(data, dict) else None,
                self.config.range_start,
            )
            ctx = {"state": state, "dirty": False}
            try:
                yield ctx
            finally:
                if ctx["dirty"]:
                    text = yaml.safe_dump(
                        state.to_yaml_dict(), sort_keys=True
                    )
                    fh.seek(0)
                    fh.truncate()
                    fh.write(text)
                    fh.flush()


__all__ = (
    "DEFAULT_PORT_RANGE",
    "DEFAULT_STORE_PATH",
    "PortAllocator",
    "PortError",
    "PortPoolExhaustedError",
    "UnknownPortAllocationError",
)
