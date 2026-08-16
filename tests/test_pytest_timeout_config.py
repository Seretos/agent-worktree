"""Regression tests for ticket #105.

The suite has a load-dependent daemon-thread leak in ``lib-python-worktree``
(upstream ``Seretos/lib-python-worktree#90``) that, on affected machines, can
wedge a test indefinitely instead of failing. These tests guard the
``pytest-timeout`` configuration that turns that hang into a loud, fast test
failure instead of a silent one.
"""

import importlib.metadata
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# Env vars that could silently override or disable the ini-level "timeout"
# key under test (e.g. set by a CI runner or a developer's shell). Stripped
# from every subprocess.run() below so the child's config source is
# deterministically just the tmp_path pyproject.toml, not ambient state.
_ENV_VARS_TO_STRIP = (
    "PYTEST_ADDOPTS",
    "PYTEST_TIMEOUT",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
)


def _isolated_subprocess_env():
    env = os.environ.copy()
    for key in _ENV_VARS_TO_STRIP:
        env.pop(key, None)
    return env


def test_per_test_timeout_is_configured(request):
    # Ini-level timeout value is set and parses to the expected bound.
    assert float(request.config.getini("timeout")) == 60

    # The plugin providing the "timeout" ini key/marker is actually loaded,
    # not just an unrelated ini key that happens to parse.
    assert request.config.pluginmanager.hasplugin("timeout")

    # The dependency is actually installed (mirrors tests/test_dependency_pin.py).
    assert importlib.metadata.version("pytest-timeout")

    # The dependency is declared in pyproject.toml's test extra, not merely
    # present transitively or globally.
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)
    test_deps = pyproject["project"]["optional-dependencies"]["test"]
    assert any(dep.startswith("pytest-timeout") for dep in test_deps), (
        f"expected a pytest-timeout entry in [project.optional-dependencies].test, "
        f"got {test_deps!r}"
    )


def test_hanging_test_is_failed_not_hung(tmp_path):
    # Configure the timeout purely via ini config (mirroring the real
    # [tool.pytest.ini_options] timeout = 60 in this repo's pyproject.toml),
    # NOT via an explicit "--timeout=1" CLI flag. A CLI flag would only prove
    # pytest-timeout mechanically works when force-enabled on the command
    # line -- it would say nothing about whether the ini-level "timeout" key
    # this repo actually relies on is still being picked up during a normal
    # `pytest` invocation (e.g. if an addopts collision, a marker override,
    # or the key silently being dropped ever broke that path).
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntimeout = 1\n"
    )

    hang_test = tmp_path / "test_hang.py"
    hang_test.write_text(
        "import time\n"
        "\n"
        "\n"
        "def test_hangs_forever():\n"
        "    while True:\n"
        "        time.sleep(0.1)\n"
    )

    # Deliberately NOT "-q" here: quiet mode never prints pytest's
    # "collected N item(s)" line, in *any* outcome (verified empirically,
    # including the genuine hard-kill case below) -- so it can't serve as
    # positive evidence that collection succeeded and the target test
    # actually started running. Without that marker, an early/unrelated
    # subprocess failure (e.g. pytest-timeout silently missing, so the ini
    # "timeout" key is rejected as an unknown config option before
    # collection even starts) would be indistinguishable from a genuine
    # mid-run hard-kill, since neither prints a "passed"/"failed" summary
    # line either.
    # Explicit env isolation: build the child env from a copy of the
    # current environment with PYTEST_ADDOPTS / PYTEST_TIMEOUT /
    # PYTEST_DISABLE_PLUGIN_AUTOLOAD stripped, rather than inheriting the
    # parent process's environment unchanged. Without this, an ambient
    # PYTEST_TIMEOUT (which pytest-timeout's get_env_settings() consults
    # *before* the ini "timeout" key -- see pytest_timeout.get_env_settings)
    # or PYTEST_ADDOPTS containing "--timeout=..." could silently override
    # the ini-level timeout=1 this test relies on, making the outcome
    # dependent on ambient shell/CI state instead of purely the tmp_path
    # pyproject.toml under test.
    child_env = _isolated_subprocess_env()

    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "test_hang.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env=child_env,
    )
    elapsed = time.monotonic() - start

    combined = result.stdout + result.stderr

    # On Windows the "thread" timeout method is used (SIGALRM is POSIX-only).
    # pytest-timeout's timeout_timer() writes the "Timeout" separator/stack
    # dump to the terminal, then in a `finally` block explicitly flushes the
    # terminal writer, stdout, and stderr *before* calling os._exit(1) to
    # hard-kill the process (see pytest_timeout.timeout_timer in the
    # installed pytest-timeout source). That flush-then-exit ordering is
    # unconditional, so the marker text is expected to reliably survive the
    # hard kill rather than being lost to buffering.
    #
    # As a defensive fallback in case flush behaviour ever differs on a
    # future Python/pytest-timeout version, also accept the case where the
    # process was hard-killed before it could print its normal pass/fail
    # summary line -- that absence is itself evidence of the os._exit()
    # short-circuit this test is meant to catch. But the fallback must not
    # fire on an early/unrelated failure that never exercised the timeout
    # kill at all (e.g. pytest-timeout silently missing/unregistered, so the
    # ini "timeout" key is rejected as an unknown config option before
    # collection starts: pytest exits nonzero with a usage error and no
    # "passed"/"failed" summary either). So the fallback additionally requires
    # positive evidence that pytest actually collected and started running
    # the target test -- "collected 1 item" is only ever printed once
    # collection has succeeded, so its presence rules out both the usage-
    # error case above and a collection error (which prints
    # "collected 0 items / 1 error" instead, never "collected 1 item").
    #
    # That "collected and started, no summary" signature alone is still not
    # sufficient, though: a hard crash unrelated to pytest-timeout occurring
    # right after collection (e.g. a segfault, or the target test itself
    # calling os._exit()) would print "collected 1 item" and then die before
    # any pass/fail summary, satisfying collected_and_ran and summary_absent
    # near-instantly -- without the timeout kill ever actually firing.
    # Guard against that by additionally requiring the subprocess call's
    # wall-clock duration to be consistent with the test having actually run
    # for close to the configured 1s ini timeout. A near-instant unrelated
    # crash returns well under that bound and is correctly rejected.
    #
    # Known, accepted residual risk (see ticket #105 review history): the fallback
    # branch below is a defensive heuristic, not deterministic proof the timeout
    # mechanism itself fired -- env stripping doesn't isolate ambient plugin
    # autoload, and the elapsed-time floor only rules out fast unrelated crashes.
    # In practice the primary `"Timeout" in combined` check above fires on every
    # genuine timeout observed during development; this fallback exists as a
    # defense-in-depth backstop and has not itself been the deciding branch in
    # any real run so far.
    timeout_marker_present = "Timeout" in combined
    collected_and_ran = "collected 1 item" in combined
    summary_absent = "passed" not in combined and "failed" not in combined
    ran_long_enough_for_timeout = elapsed >= 0.9
    assert timeout_marker_present or (
        collected_and_ran and summary_absent and ran_long_enough_for_timeout
    ), (
        f"expected either a timeout marker or a hard-killed run that had "
        f"actually collected and started the target test (no normal "
        f"pass/fail summary) and took long enough to be consistent with "
        f"the configured 1s timeout (elapsed={elapsed:.2f}s) in pytest "
        f"output, got:\n{combined}"
    )
    assert result.returncode != 0

    # Companion assertion: the bound doesn't produce false positives on a
    # normal, fast test in the same directory. Reuses the same tmp_path
    # pyproject.toml ini config (timeout = 1) written above, rather than its
    # own "--timeout=1" CLI flag, for consistency with the ini-driven
    # mechanism this test now exercises.
    fast_test = tmp_path / "test_fast.py"
    fast_test.write_text(
        "def test_returns_quickly():\n"
        "    assert 1 + 1 == 2\n"
    )

    fast_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_fast.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env=child_env,
    )
    assert fast_result.returncode == 0, (
        f"expected fast test to pass under the tmp_path ini timeout=1 config, "
        f"got:\n{fast_result.stdout}{fast_result.stderr}"
    )
