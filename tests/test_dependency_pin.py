import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from worktree_plugin.config import load_plugin_config

# Pinned git-URL dependencies: distribution name -> expected version.
PINS = {
    "lib-python-worktree": "0.3.16",
    "lib-python-config": "0.1.4",
}


@pytest.mark.parametrize("name", sorted(PINS))
def test_dependency_pinned(name):
    expected = PINS[name]
    installed = version(name)
    assert installed == expected, (
        f"expected {name}=={expected}, but installed version is {installed!r}. "
        f"A stale .venv does not auto-resolve to a bumped git-URL pin -- "
        f"`pip install -e \".[test]\"` alone will not fix this. To repair, run: "
        f".venv/Scripts/python.exe -m pip install --force-reinstall --no-deps "
        f'"{name} @ git+https://github.com/seretos-agents/{name}@v{expected}"'
    )

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)
    deps = pyproject["project"]["dependencies"]
    pin = next(dep for dep in deps if dep.startswith(name))
    # Full-string match (not just an `@v{expected}` suffix check): a suffix-only
    # check would pass even if the org/repo URL portion were wrong (e.g. a typo'd
    # org name), as long as the version tag happened to match. Pinning the whole
    # string catches a URL mismatch too (plan-critic round 1 note, #208).
    expected_pin = (
        f"{name} @ git+https://github.com/seretos-agents/{name}@v{expected}"
    )
    assert pin == expected_pin, (
        f"expected pyproject.toml's {name} dependency entry to "
        f"equal {expected_pin!r}, got {pin!r}"
    )


def test_load_plugin_config_env_override_unpatched_on_pinned_config_lib(
    tmp_path, monkeypatch
):
    """Installed lib-python-config is v0.1.4 AND parses WORKTREE_CONFIG, unpatched."""
    assert version("lib-python-config") == PINS["lib-python-config"]

    cfg_file = tmp_path / "elsewhere" / "custom.yml"
    cfg_file.parent.mkdir()
    cfg_file.write_text("store_root: /env/override/store\n", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_CONFIG", str(cfg_file))

    cfg = load_plugin_config(cwd=tmp_path)
    assert cfg.store_root == "/env/override/store"


def test_load_plugin_config_discovers_seretos_worktree_yml_unpatched(
    tmp_path, monkeypatch
):
    """Installed lib-python-config is v0.1.4 AND discovers .seretos/worktree.yml
    from a working directory (no env override), unpatched."""
    assert version("lib-python-config") == PINS["lib-python-config"]

    monkeypatch.delenv("WORKTREE_CONFIG", raising=False)
    # lib-python-config walks git project boundaries: mark tmp_path as a repo.
    (tmp_path / ".git").mkdir()
    config_dir = tmp_path / ".seretos"
    config_dir.mkdir()
    (config_dir / "worktree.yml").write_text(
        "store_root: /discovered/store\n", encoding="utf-8"
    )
    workdir = tmp_path / "sub" / "dir"
    workdir.mkdir(parents=True)

    cfg = load_plugin_config(cwd=workdir)
    assert cfg.store_root == "/discovered/store"
