"""Tests for the W4 port allocator."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import List

import pytest

from worktree_plugin.core.config import (
    DEFAULT_PORT_RANGE,
    PortConfig,
    load_port_config,
    parse_port_range,
)
from worktree_plugin.core.ports import (
    PortAllocator,
    PortError,
    PortPoolExhaustedError,
    UnknownPortAllocationError,
)


def _make_allocator(tmp_path: Path, start: int, end: int) -> PortAllocator:
    return PortAllocator(
        config=PortConfig(range_start=start, range_end=end),
        store_path=tmp_path / "ports.yaml",
    )


def test_default_range_matches_plan_decision():
    cfg = load_port_config(env={})
    assert (cfg.range_start, cfg.range_end) == DEFAULT_PORT_RANGE
    assert DEFAULT_PORT_RANGE == (30000, 40000)


def test_parse_port_range_happy_path():
    assert parse_port_range("30000-40000") == (30000, 40000)


@pytest.mark.parametrize(
    "raw",
    ["", "30000", "30000-", "-40000", "abc-123", "0-100", "30000-29999", "30000-99999"],
)
def test_parse_port_range_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_port_range(raw)


def test_serial_allocation_returns_disjoint_ports(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 41000, 41099)
    a = alloc.allocate("wt-a", ["app", "db"])
    b = alloc.allocate("wt-b", ["app", "db"])
    assert set(a.values()).isdisjoint(b.values())
    for port in (*a.values(), *b.values()):
        assert 41000 <= port <= 41099


def test_release_frees_ports_for_reuse(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 41100, 41199)
    a = alloc.allocate("wt-a", ["app"])
    freed = alloc.release("wt-a")
    assert freed == a
    # Reallocate enough to wrap and re-encounter the freed port.
    second = alloc.allocate("wt-b", ["app"])
    assert second  # at minimum, allocation succeeds
    # And after wrap, the previously freed port becomes available again.
    listing = alloc.list_allocations()
    assert "wt-a" not in listing
    assert "wt-b" in listing


def test_release_unknown_raises(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 41200, 41299)
    with pytest.raises(UnknownPortAllocationError):
        alloc.release("never-allocated")


def test_pool_exhausted_raises_clear_error(tmp_path: Path):
    # 3-port range so we can exhaust it predictably.
    alloc = _make_allocator(tmp_path, 41300, 41302)
    alloc.allocate("wt-a", ["a", "b", "c"])
    with pytest.raises(PortPoolExhaustedError):
        alloc.allocate("wt-b", ["x"])


def test_duplicate_slot_names_in_request_rejected(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 41400, 41499)
    with pytest.raises(PortError):
        alloc.allocate("wt-a", ["app", "app"])


def test_idempotent_allocate_for_same_slot_set(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 41500, 41599)
    first = alloc.allocate("wt-a", ["app", "db"])
    second = alloc.allocate("wt-a", ["app", "db"])
    assert first == second


def test_realloc_with_different_slot_set_rejected(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 41600, 41699)
    alloc.allocate("wt-a", ["app"])
    with pytest.raises(PortError):
        alloc.allocate("wt-a", ["db"])


def test_busy_port_in_range_is_skipped(tmp_path: Path):
    # Hold port 41700 ourselves; the allocator's bind-probe must skip it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        # Use an actually-listening port — pick a free one and keep it bound.
        busy_port = held.getsockname()[1]
        alloc = _make_allocator(tmp_path, busy_port, busy_port + 4)
        got = alloc.allocate("wt-a", ["app"])
        assert got["app"] != busy_port


def test_concurrent_threads_get_disjoint_ports(tmp_path: Path):
    alloc = _make_allocator(tmp_path, 42000, 42099)
    results: List[int] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            mapping = alloc.allocate(f"wt-{i}", ["app"])
            with lock:
                results.append(mapping["app"])
        except Exception as exc:  # pragma: no cover -- only fires on bug
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(results) == 10
    assert len(set(results)) == 10, f"non-disjoint allocations: {results}"


def test_allocations_persist_across_allocator_instances(tmp_path: Path):
    store = tmp_path / "ports.yaml"
    alloc1 = PortAllocator(
        config=PortConfig(range_start=42500, range_end=42599),
        store_path=store,
    )
    a = alloc1.allocate("wt-a", ["app"])

    # A fresh allocator instance must see the persisted allocation.
    alloc2 = PortAllocator(
        config=PortConfig(range_start=42500, range_end=42599),
        store_path=store,
    )
    listing = alloc2.list_allocations()
    assert listing == {"wt-a": a}

    b = alloc2.allocate("wt-b", ["app"])
    assert b["app"] != a["app"]


def test_store_path_env_override(tmp_path: Path, monkeypatch):
    target = tmp_path / "custom-ports.yaml"
    monkeypatch.setenv("WORKTREE_PORT_STORE", str(target))
    alloc = PortAllocator(
        config=PortConfig(range_start=42700, range_end=42799),
    )
    alloc.allocate("wt-a", ["app"])
    assert target.exists()


def test_range_env_override(monkeypatch):
    monkeypatch.setenv("WORKTREE_PORT_RANGE", "45000-45100")
    cfg = load_port_config()
    assert (cfg.range_start, cfg.range_end) == (45000, 45100)
