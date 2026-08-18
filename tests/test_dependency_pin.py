from importlib.metadata import version


def test_lib_python_worktree_pinned_to_v0_3_4():
    installed = version("lib-python-worktree")
    assert installed == "0.3.4", (
        f"expected lib-python-worktree==0.3.4, but installed version is {installed!r}. "
        f"A stale .venv does not auto-resolve to a bumped git-URL pin -- "
        f"`pip install -e \".[test]\"` alone will not fix this. To repair, run: "
        f".venv/Scripts/python.exe -m pip install --force-reinstall --no-deps "
        f'"lib-python-worktree @ git+https://github.com/Seretos/lib-python-worktree@v0.3.4"'
    )
