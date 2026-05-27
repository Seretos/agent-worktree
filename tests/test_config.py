"""Tests for worktree_plugin.config (composition root).

Covers store_root resolution precedence, YAML config loading, error
propagation, and the PluginConfig pydantic model validation.

``resolve_config_path`` walks git-project boundaries, so files placed in a
bare ``tmp_path`` (which is not a git repo) are not discovered. The tests
that exercise file-loading use the ``WORKTREE_CONFIG`` env-override path so
the file is always found regardless of git context.  Tests that only care
about the "no file found" code path just pass a non-git ``tmp_path`` with no
``WORKTREE_CONFIG`` set.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import lib_python_config
from worktree_plugin.config import (
    PluginConfig,
    build_manager_config,
    load_plugin_config,
    resolve_store_root,
)


# ---------------------------------------------------------------------------
# resolve_store_root — unit tests (no file I/O)
# ---------------------------------------------------------------------------


def test_resolve_store_root_default(monkeypatch):
    """No config, no env -> falls back to ~/agent-worktree-store."""
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    result = resolve_store_root(PluginConfig())
    assert result == (Path.home() / "agent-worktree-store").resolve()
    assert result.is_absolute()


def test_resolve_store_root_env_wins_over_default(tmp_path, monkeypatch):
    """WORKTREE_STORE_ROOT beats the default fallback."""
    target = tmp_path / "custom-store"
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(target))
    result = resolve_store_root(PluginConfig())
    assert result == target.resolve()


def test_resolve_store_root_config_wins_over_env(tmp_path, monkeypatch):
    """store_root from PluginConfig beats WORKTREE_STORE_ROOT."""
    env_path = tmp_path / "env-store"
    config_path = tmp_path / "config-store"
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(env_path))
    result = resolve_store_root(PluginConfig(store_root=str(config_path)))
    assert result == config_path.resolve()


def test_resolve_store_root_relative_becomes_absolute(monkeypatch):
    """A relative store_root in the config is resolved to an absolute path."""
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    result = resolve_store_root(PluginConfig(store_root="relative/path"))
    assert result.is_absolute()


def test_resolve_store_root_config_beats_env_unit(tmp_path, monkeypatch):
    """Direct unit: PluginConfig(store_root=...) beats WORKTREE_STORE_ROOT."""
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(tmp_path / "env"))
    config_root = tmp_path / "from-config"
    r = resolve_store_root(PluginConfig(store_root=str(config_root)))
    assert r == config_root.resolve()


# ---------------------------------------------------------------------------
# load_plugin_config — file discovery via WORKTREE_CONFIG override
# ---------------------------------------------------------------------------


def test_load_plugin_config_no_file_no_env(tmp_path, monkeypatch):
    """No config file and no WORKTREE_CONFIG -> returns default PluginConfig.

    tmp_path is not a git repo, so walk_project_boundaries yields nothing;
    ~/. seretos/worktree.yml typically does not exist either.  The resolver
    returns (None, searched), and we fall back to PluginConfig().
    """
    monkeypatch.delenv("WORKTREE_CONFIG", raising=False)
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    # Monkeypatch resolve_config_path so this test is hermetic regardless of
    # whether ~/.seretos/worktree.yml happens to exist on the CI machine.
    with patch.object(
        lib_python_config, "resolve_config_path", return_value=(None, [])
    ):
        cfg = load_plugin_config(cwd=tmp_path)
    assert isinstance(cfg, PluginConfig)
    assert cfg.store_root is None


def test_load_plugin_config_reads_config_file(tmp_path, monkeypatch):
    """When WORKTREE_CONFIG points at a file, store_root is read from it."""
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    cfg_file = tmp_path / "worktree.yml"
    cfg_file.write_text("store_root: /some/path\n", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))
    cfg = load_plugin_config(cwd=tmp_path)
    assert cfg.store_root == "/some/path"


def test_load_plugin_config_empty_file_is_default(tmp_path, monkeypatch):
    """An empty config file is treated as default PluginConfig."""
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    cfg_file = tmp_path / "worktree.yml"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))
    cfg = load_plugin_config(cwd=tmp_path)
    assert cfg.store_root is None


def test_load_plugin_config_unknown_key_raises_validation_error(
    tmp_path, monkeypatch
):
    """An unknown key in the config file propagates as pydantic ValidationError
    (extra='forbid' on PluginConfig)."""
    cfg_file = tmp_path / "worktree.yml"
    cfg_file.write_text("store_root: /ok\nfoo_unknown: bar\n", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))
    with pytest.raises(ValidationError):
        load_plugin_config(cwd=tmp_path)


def test_load_plugin_config_malformed_yaml_raises_config_error(
    tmp_path, monkeypatch
):
    """Malformed YAML propagates as lib_python_config.ConfigError."""
    cfg_file = tmp_path / "worktree.yml"
    cfg_file.write_text("version: 1\n  bad: indent: here\n", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))
    with pytest.raises(lib_python_config.ConfigError):
        load_plugin_config(cwd=tmp_path)


def test_load_plugin_config_env_override(tmp_path, monkeypatch):
    """WORKTREE_CONFIG pointing at a file is used instead of discovery."""
    cfg_file = tmp_path / "custom.yml"
    cfg_file.write_text("store_root: /override\n", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))
    cfg = load_plugin_config(cwd=tmp_path)
    assert cfg.store_root == "/override"


# ---------------------------------------------------------------------------
# Full precedence integration: config file > env > default
# ---------------------------------------------------------------------------


def test_full_precedence_config_file_wins(tmp_path, monkeypatch):
    """Config file store_root wins over WORKTREE_STORE_ROOT."""
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(tmp_path / "env-store"))
    config_store = tmp_path / "file-store"
    cfg_file = tmp_path / "worktree.yml"
    cfg_file.write_text(f"store_root: {config_store}\n", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))
    cfg = load_plugin_config(cwd=tmp_path)
    assert resolve_store_root(cfg) == config_store.resolve()


def test_full_precedence_env_wins_over_default(tmp_path, monkeypatch):
    """WORKTREE_STORE_ROOT wins over ~/agent-worktree-store default."""
    env_store = tmp_path / "env-store"
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(env_store))
    with patch.object(
        lib_python_config, "resolve_config_path", return_value=(None, [])
    ):
        cfg = load_plugin_config(cwd=tmp_path)
    assert resolve_store_root(cfg) == env_store.resolve()


# ---------------------------------------------------------------------------
# build_manager_config
# ---------------------------------------------------------------------------


def test_build_manager_config_returns_manager_config(tmp_path, monkeypatch):
    """build_manager_config wires load_plugin_config + resolve_store_root."""
    from lib_python_worktree import ManagerConfig

    monkeypatch.delenv("WORKTREE_CONFIG", raising=False)
    monkeypatch.delenv("WORKTREE_STORE_ROOT", raising=False)
    with patch.object(
        lib_python_config, "resolve_config_path", return_value=(None, [])
    ):
        mc = build_manager_config(cwd=tmp_path)
    assert isinstance(mc, ManagerConfig)
    assert mc.store_root == (Path.home() / "agent-worktree-store").resolve()


def test_build_manager_config_respects_env(tmp_path, monkeypatch):
    """build_manager_config picks up WORKTREE_STORE_ROOT."""
    monkeypatch.delenv("WORKTREE_CONFIG", raising=False)
    target = tmp_path / "my-store"
    monkeypatch.setenv("WORKTREE_STORE_ROOT", str(target))
    with patch.object(
        lib_python_config, "resolve_config_path", return_value=(None, [])
    ):
        mc = build_manager_config(cwd=tmp_path)
    assert mc.store_root == target.resolve()
