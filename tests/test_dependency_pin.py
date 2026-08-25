import tomllib
from importlib.metadata import version
from pathlib import Path


def test_lib_python_worktree_pinned_to_v0_3_11():
    installed = version("lib-python-worktree")
    assert installed == "0.3.11", (
        f"expected lib-python-worktree==0.3.11, but installed version is {installed!r}. "
        f"A stale .venv does not auto-resolve to a bumped git-URL pin -- "
        f"`pip install -e \".[test]\"` alone will not fix this. To repair, run: "
        f".venv/Scripts/python.exe -m pip install --force-reinstall --no-deps "
        f'"lib-python-worktree @ git+https://github.com/Seretos/lib-python-worktree@v0.3.11"'
    )

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)
    deps = pyproject["project"]["dependencies"]
    pin = next(dep for dep in deps if dep.startswith("lib-python-worktree"))
    assert pin.endswith("@v0.3.11"), (
        f"expected pyproject.toml's lib-python-worktree dependency entry to "
        f"end in '@v0.3.11', got {pin!r}"
    )
