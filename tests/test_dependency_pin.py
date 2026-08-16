from importlib.metadata import version


def test_lib_python_worktree_pinned_to_v0_3_3():
    installed = version("lib-python-worktree")
    assert installed == "0.3.3", (
        f"expected lib-python-worktree==0.3.3, but installed version is {installed!r}"
    )
