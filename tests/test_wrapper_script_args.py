"""Regression tests for scripts/test.ps1 CLI argument forwarding (ticket #132).

scripts/test.ps1's header comment has always documented three usages,
including forwarding extra CLI arguments straight through to pytest:

    pwsh -File scripts/test.ps1
    pwsh -File scripts/test.ps1 -v                  # verbose
    pwsh -File scripts/test.ps1 tests/test_config.py # single file

Before the ticket #132 fix, the script declared ``[CmdletBinding()]`` with an
empty ``param()`` block. That makes PowerShell treat the script as an
*advanced function*, which -- independent of any declared parameter -- adds
PowerShell's built-in common parameters (-Verbose, -Debug, -OutVariable,
-WarningAction, -PipelineVariable, ...). Those common parameters interfere
with pytest's own short flags at PowerShell's own argument-binding stage,
before a single line of the script body runs:

  * ``-v`` / ``-d`` prefix-match uniquely against ``-Verbose`` / ``-Debug``
    and are silently absorbed as common parameters -- never reaching
    pytest, and never raising an error either.
  * ``-o`` / ``-w`` / ``-p`` are *ambiguous* prefixes among several common
    parameters (e.g. ``-OutVariable``/``-OutBuffer``) and hard-error.
  * A bare positional argument (a test path) hard-errors immediately,
    since an advanced function with no declared parameters accepts no
    positional arguments at all.

This module has two independent layers (plan step 5):

  (a) ``test_script_is_not_an_advanced_function`` and its companions are a
      static guard with **no pwsh dependency**. They run on every CI leg,
      including the ubuntu-22.04 one, so a green Linux run is not vacuous.
  (b) The remaining tests drive PowerShell's *real* parameter binder against
      the real ``scripts/test.ps1`` param block (extracted via the
      PowerShell language parser, without executing the script body -- that
      would recursively re-run the whole suite and rebuild .venv). They
      require ``pwsh`` (or, on Windows, ``powershell.exe``) on PATH and are
      skipped when neither is available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_SCRIPT = REPO_ROOT / "scripts" / "test.ps1"


def _read_target_script_text() -> str:
    return TARGET_SCRIPT.read_text(encoding="utf-8")


def _non_comment_lines(text: str) -> str:
    """Strip full-line PowerShell comments (lines starting with '#').

    The guard below must scan actual code, not the header comment block --
    which necessarily *names* [CmdletBinding()] and [Parameter(...)] in
    prose while explaining why they must not be re-added.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Behaviour 4 -- static guard, no pwsh required (runs on every CI leg).
# ---------------------------------------------------------------------------


def test_script_is_not_an_advanced_function():
    """scripts/test.ps1 must stay a *simple* script (ticket #132).

    Either [CmdletBinding()] or any [Parameter(...)] attribute turns a
    PowerShell script into an "advanced function", which adds PowerShell's
    built-in common parameters (-Verbose, -Debug, -OutVariable,
    -WarningAction, -PipelineVariable, ...). Those common parameters
    prefix-match pytest's own short flags: "-v" silently binds to -Verbose
    instead of reaching pytest, and "-o"/"-w"/"-p" are ambiguous prefixes
    that hard-error. Do not re-add [CmdletBinding()] or a declared
    [Parameter(ValueFromRemainingArguments=$true)] parameter -- keep the
    script a simple script relying on the automatic $args variable.
    """
    code = _non_comment_lines(_read_target_script_text())
    assert "[CmdletBinding(" not in code, (
        "scripts/test.ps1 must not carry [CmdletBinding(...)] in any form "
        "(ticket #132), e.g. [CmdletBinding()] or "
        "[CmdletBinding(PositionalBinding=$false)]: it turns the script "
        "into a PowerShell 'advanced function' whose built-in common "
        "parameters (-Verbose, -Debug, -OutVariable, -WarningAction, "
        "-PipelineVariable, ...) prefix-match and swallow pytest's own "
        "short flags such as -v/-d/-o/-w/-p."
    )
    assert "[Parameter(" not in code, (
        "scripts/test.ps1 must not declare any [Parameter(...)] parameter "
        "(ticket #132): a declared [Parameter(ValueFromRemainingArguments"
        "=$true)] parameter (the ticket's own suggested patch) still turns "
        "the script into an advanced function and reintroduces the same "
        "common-parameter hazard as [CmdletBinding()] -- see the guard "
        "above."
    )


def test_script_still_captures_and_forwards_pytest_args():
    code = _non_comment_lines(_read_target_script_text())
    assert "$PytestArgs = $args" in code, (
        "scripts/test.ps1 must still capture the automatic $args variable "
        "into $PytestArgs"
    )
    assert "Invoke-Py -m pytest @PytestArgs" in code, (
        "scripts/test.ps1 must still splat $PytestArgs into the pytest "
        "invocation"
    )


def test_script_header_documents_all_three_usages():
    text = _read_target_script_text()
    assert "pwsh -File scripts/test.ps1" in text
    assert "pwsh -File scripts/test.ps1 -v" in text
    assert "pwsh -File scripts/test.ps1 tests/test_config.py" in text


# ---------------------------------------------------------------------------
# Behaviours 1-3 -- real PowerShell parameter-binding harness.
# ---------------------------------------------------------------------------


def _resolve_pwsh() -> str | None:
    exe = shutil.which("pwsh")
    if exe:
        return exe
    if sys.platform == "win32":
        exe = shutil.which("powershell.exe")
        if exe:
            return exe
    return None


PWSH = _resolve_pwsh()

# Cases exercised through the real PowerShell parameter binder. Each case
# invokes a scriptblock built from the *real* scripts/test.ps1 param block
# (extracted via the language parser) with the given argument list, exactly
# as `pwsh -File scripts/test.ps1 <args...>` would bind them.
CASES = [
    {"name": "no_args", "args": []},
    {"name": "v", "args": ["-v"]},
    {"name": "d", "args": ["-d"]},
    {"name": "o_cache_dir", "args": ["-o", "cache_dir=/tmp/x"]},
    {"name": "w", "args": ["-w"]},
    {"name": "p_no_cacheprovider", "args": ["-p", "no:cacheprovider"]},
    {"name": "positional_path", "args": ["tests/test_config.py"]},
    {"name": "k_expr", "args": ["-k", "not slow"]},
    {"name": "maxfail", "args": ["--maxfail=1"]},
    {"name": "mixed_order", "args": ["-x", "-q", "tests/test_config.py"]},
    {"name": "path_with_space", "args": ["tests/some dir/test_x.py"]},
]

# Step 1: a small extractor script parses the real target script (path given
# via env var, to avoid nesting one parameter-binding problem inside
# another) and reconstructs its param block *including any
# [CmdletBinding()]/[Parameter(...)] attributes*. ParamBlockAst.Extent.Text
# alone covers only the "param(...)" text -- any preceding attributes live
# in a separate .Attributes collection with their own Extent -- so omitting
# them would silently test a different, simpler param block than the one
# actually in the file (verified empirically while writing this harness).
_EXTRACTOR_SCRIPT = r"""
$targetPath = $env:WRAPPER_TEST_TARGET_SCRIPT

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($targetPath, [ref]$tokens, [ref]$parseErrors)

$paramFragment = 'param()'
if ($ast.ParamBlock) {
    $attrTexts = @($ast.ParamBlock.Attributes | ForEach-Object { $_.Extent.Text })
    if ($attrTexts.Count -gt 0) {
        $paramFragment = ($attrTexts -join "`n") + "`n" + $ast.ParamBlock.Extent.Text
    } else {
        $paramFragment = $ast.ParamBlock.Extent.Text
    }
}

[PSCustomObject]@{ ParamFragment = $paramFragment } | ConvertTo-Json -Compress
"""

# Step 2: the extracted param fragment is written to the front of a small
# synthetic *file* that mirrors the real script's own `$PytestArgs = $args`
# capture. Each case is then run as its own `pwsh -File <synthetic> <args>`
# subprocess, passed real argv elements (via Python's subprocess arg list,
# with no extra shell or PowerShell-source quoting) -- exactly the way the
# real `pwsh -File scripts/test.ps1 <args...>` receives its arguments.
#
# This per-case-subprocess design replaces an earlier version of this
# harness that instead built one scriptblock and invoked it once per case
# via `& $sb @caseArgs` (array splatting). That was empirically wrong:
# PowerShell's array splat always binds elements *positionally*, so it can
# never reproduce a bare `-v` being recognized as a named-parameter token
# (-> silently bound to the common -Verbose parameter) the way a real
# `-File` invocation does. Only an actual `-File` invocation carries that
# real argument-classification behavior, which is exactly the behavior
# ticket #132 is about.
_CASE_BODY = r"""
$PytestArgs = $args
# $args is $null (not an empty array) when every incoming argument bound to
# a declared/common parameter -- e.g. a bare `-v` silently absorbed as
# -Verbose leaves nothing unbound. @($null) then wraps to a one-element
# array containing null, not an empty array, so normalize explicitly.
if ($null -eq $PytestArgs) { $PytestArgs = @() }
$boundKeys = @()
if ($PSBoundParameters) { $boundKeys = @($PSBoundParameters.Keys) }
[PSCustomObject]@{ Args = @($PytestArgs); Bound = @($boundKeys) } | ConvertTo-Json -Compress
"""


@pytest.fixture(scope="module")
def binding_results():
    if PWSH is None:
        pytest.skip("no pwsh/powershell.exe on PATH -- skipping real PowerShell binding harness")

    with tempfile.TemporaryDirectory(prefix="wrapper_script_args_") as tmp:
        tmp_path = Path(tmp)

        extractor_path = tmp_path / "extractor.ps1"
        extractor_path.write_text(_EXTRACTOR_SCRIPT, encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env["WRAPPER_TEST_TARGET_SCRIPT"] = str(TARGET_SCRIPT)

        extractor_proc = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(extractor_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert extractor_proc.returncode == 0, (
            f"param-block extractor failed to run (exit {extractor_proc.returncode}):\n"
            f"stdout={extractor_proc.stdout!r}\nstderr={extractor_proc.stderr!r}"
        )
        param_fragment = json.loads(extractor_proc.stdout)["ParamFragment"]

        case_script_path = tmp_path / "case_script.ps1"
        case_script_path.write_text(
            param_fragment + "\n" + _CASE_BODY, encoding="utf-8", newline="\n"
        )

        results: dict[str, dict] = {}
        for case in CASES:
            proc = subprocess.run(
                [PWSH, "-NoProfile", "-NonInteractive", "-File", str(case_script_path), *case["args"]],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                payload = json.loads(proc.stdout)
                results[case["name"]] = {
                    "args": payload["Args"],
                    "bound": payload["Bound"],
                    "error": None,
                }
            else:
                results[case["name"]] = {
                    "args": [],
                    "bound": [],
                    "error": (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip(),
                }

    return results


def test_no_arguments_yields_empty_forwarded_list(binding_results):
    """Behaviour 3 -- the bare, no-argument invocation keeps working."""
    result = binding_results["no_args"]
    assert result["error"] is None, f"unexpected binding error: {result['error']}"
    assert result["args"] == []


def test_short_flag_v_is_forwarded_and_not_bound_as_common_parameter(binding_results):
    """Behaviour 1 (driving test) -- "-v" reaches pytest, not -Verbose."""
    result = binding_results["v"]
    assert result["error"] is None, f"unexpected binding error for -v: {result['error']}"
    assert result["args"] == ["-v"], (
        f"-v was not forwarded verbatim to $args (got {result['args']!r}); "
        f"bound PowerShell parameters: {result['bound']!r}"
    )
    assert result["bound"] == [], (
        f"-v was absorbed as a PowerShell common parameter {result['bound']!r} "
        f"instead of reaching pytest -- this is exactly the silent failure "
        f"mode the ticket's own suggested patch "
        f"([Parameter(ValueFromRemainingArguments=$true)]) would reintroduce"
    )


@pytest.mark.parametrize(
    "case_name,expected_args",
    [
        ("d", ["-d"]),
        ("o_cache_dir", ["-o", "cache_dir=/tmp/x"]),
        ("w", ["-w"]),
        ("p_no_cacheprovider", ["-p", "no:cacheprovider"]),
    ],
)
def test_common_parameter_prefix_flags_are_forwarded_verbatim(binding_results, case_name, expected_args):
    """Behaviour 1 edge cases -- other common-parameter-prefix flags."""
    result = binding_results[case_name]
    assert result["error"] is None, f"unexpected binding error for {case_name}: {result['error']}"
    assert result["args"] == expected_args, (
        f"{case_name} was not forwarded verbatim (got {result['args']!r})"
    )
    assert result["bound"] == [], (
        f"{case_name} was absorbed as a PowerShell common parameter "
        f"{result['bound']!r} instead of reaching pytest"
    )


def test_positional_path_argument_is_forwarded(binding_results):
    """Behaviour 2 (driving test) -- a positional test path reaches pytest."""
    result = binding_results["positional_path"]
    assert result["error"] is None, f"unexpected binding error: {result['error']}"
    assert result["args"] == ["tests/test_config.py"]


@pytest.mark.parametrize(
    "case_name,expected_args",
    [
        ("k_expr", ["-k", "not slow"]),
        ("maxfail", ["--maxfail=1"]),
        ("mixed_order", ["-x", "-q", "tests/test_config.py"]),
        ("path_with_space", ["tests/some dir/test_x.py"]),
    ],
)
def test_positional_and_mixed_argument_edge_cases_are_forwarded(binding_results, case_name, expected_args):
    """Behaviour 2 edge cases -- quoting, order, and paths with spaces."""
    result = binding_results[case_name]
    assert result["error"] is None, f"unexpected binding error for {case_name}: {result['error']}"
    assert result["args"] == expected_args, (
        f"{case_name} did not round-trip in order (got {result['args']!r})"
    )
