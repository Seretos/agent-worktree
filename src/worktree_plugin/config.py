"""Composition root for plugin configuration.

Resolves ``store_root`` from (in precedence order):
1. ``store_root`` field in ``.seretos/worktree.yml`` (or the path given by
   ``WORKTREE_CONFIG``).
2. ``WORKTREE_STORE_ROOT`` environment variable.
3. ``~/agent-worktree-store`` (hard-coded fallback).

Usage::

    from worktree_plugin.config import build_manager_config
    manager = WorktreeManager(config=build_manager_config())
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

import lib_python_config
from lib_python_worktree import ManagerConfig

__all__ = (
    "PluginConfig",
    "build_manager_config",
    "load_plugin_config",
    "resolve_store_root",
)


class PluginConfig(BaseModel):
    """Validated representation of ``.seretos/worktree.yml``.

    Fields are intentionally minimal — contract fields are deferred to a
    later phase.  ``extra="forbid"`` ensures unknown keys in the config file
    surface as validation errors immediately at startup rather than being
    silently ignored.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    store_root: Optional[str] = None


def load_plugin_config(cwd: Optional[Path] = None) -> PluginConfig:
    """Load and validate the plugin config from ``.seretos/worktree.yml``.

    Searches upward from *cwd* (defaults to ``Path.cwd()``) for a
    ``.seretos/worktree.yml`` file, honoring the ``WORKTREE_CONFIG``
    environment variable as a direct override.

    Returns a default ``PluginConfig()`` when no file is found.
    ``lib_python_config.ConfigError`` and ``pydantic.ValidationError``
    propagate unchanged so startup fails loud.
    """

    path, _searched = lib_python_config.resolve_config_path(
        cwd=cwd or Path.cwd(),
        config_dir=".seretos",
        filenames=("worktree.yml",),
        override_env="WORKTREE_CONFIG",
        home_default=True,
    )
    if path is not None:
        data = lib_python_config.load_yaml(path)
        return PluginConfig.model_validate(data)
    return PluginConfig()


def resolve_store_root(plugin_config: PluginConfig) -> Path:
    """Resolve the absolute ``store_root`` path from config + env.

    Precedence:
    1. ``plugin_config.store_root`` (if set and non-empty).
    2. ``WORKTREE_STORE_ROOT`` environment variable.
    3. ``~/agent-worktree-store``.
    """

    if plugin_config.store_root:
        value = plugin_config.store_root
    else:
        env_val = os.environ.get("WORKTREE_STORE_ROOT")
        if env_val:
            value = env_val
        else:
            value = str(Path.home() / "agent-worktree-store")
    return Path(value).expanduser().resolve()


def build_manager_config(cwd: Optional[Path] = None) -> ManagerConfig:
    """Build a ``ManagerConfig`` ready for ``WorktreeManager``.

    Convenience function that wires together ``load_plugin_config`` and
    ``resolve_store_root`` in a single call.
    """

    return ManagerConfig(store_root=resolve_store_root(load_plugin_config(cwd)))
