from importlib.metadata import version


def test_lib_python_worktree_pinned_to_v0_3_0():
    installed = version("lib-python-worktree")
    assert installed == "0.3.0", (
        f"expected lib-python-worktree==0.3.0, but installed version is {installed!r}"
    )
