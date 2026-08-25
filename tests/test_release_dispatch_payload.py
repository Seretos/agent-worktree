"""Driving tests for ticket #170: `changelog` in the marketplace dispatch
payload.

Both `.github/workflows/release.yml` (job `assemble`, step "Dispatch to
agent-marketplace") and `.github/workflows/dispatch.yml` (job `dispatch`,
same step name) currently POST a `client_payload` built by an unquoted bash
heredoc that raw-splices `${NAME}`/`${DESC}` into a JSON literal. Ticket #170
rebuilds both with `jq -n --arg ...`, adds a `changelog` key sourced from
`gh api .../releases/generate-notes`, truncates it at 30000 chars, and warns
+ omits it on fetch failure/empty/whitespace/`null` body -- without ever
aborting the dispatch itself.

This module has two independent layers (mirroring
tests/test_wrapper_script_args.py's shape, plan step 5):

  (a) Static guards that parse the two workflow YAML files and inspect the
      "Dispatch to agent-marketplace" step's `run` string as *text*. No
      external tool dependency -- these run on every CI leg.
  (b) Tests that drive the *real* `bash` + `jq` interpreters against that
      exact `run` text, with stub `gh`/`curl` scripts prepended to PATH and
      a fixture `.claude-plugin/plugin.json` in a tmp_path. Skipped when
      `bash` or `jq` is not on PATH.

Ticket #170 is a behavioural change (a new `changelog` key appears in a
payload that previously never had one, sourced from a live API call with
failure/truncation handling) so this module follows TDD: every assertion
below is expected to fail against today's heredoc-based `run` text, for the
reasons documented inline. Only after the workflow YAML is rewritten
(implementation phase, not this dispatch) do these turn green.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
DISPATCH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dispatch.yml"
WORKFLOWS = [RELEASE_WORKFLOW, DISPATCH_WORKFLOW]
WORKFLOW_IDS = ["release.yml", "dispatch.yml"]

STEP_NAME = "Dispatch to agent-marketplace"

# The 9 top-level client_payload keys that exist today (plan: "changelog"
# makes 10, GitHub's documented repository_dispatch max).
EXISTING_KEYS = {
    "name",
    "description",
    "repo",
    "category",
    "version",
    "ref",
    "icon",
    "description_url",
    "tags",
}


# ---------------------------------------------------------------------------
# Shared YAML/text helpers (layer a and b both use these).
# ---------------------------------------------------------------------------


def _load_workflow(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _find_step(workflow: dict, step_name: str) -> dict:
    """Return the first step dict named ``step_name`` in any job."""
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                return step
    raise AssertionError(f"no step named {step_name!r} found in workflow")


def _dispatch_run_text(path: Path) -> str:
    workflow = _load_workflow(path)
    step = _find_step(workflow, STEP_NAME)
    run = step.get("run")
    assert isinstance(run, str), (
        f"{path.name}: {STEP_NAME!r} step has no string 'run:' body"
    )
    return run


def _dispatch_step_env(path: Path) -> dict:
    workflow = _load_workflow(path)
    step = _find_step(workflow, STEP_NAME)
    return step.get("env") or {}


# ---------------------------------------------------------------------------
# Layer (a) -- static guards, no external-tool dependency.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_both_workflow_files_parse_as_valid_yaml(workflow):
    """Coverage check (plan behaviour 5) -- already true today, not a driving
    assertion; disclosed here as a retained sanity guard, not a fabricated
    RED."""
    data = _load_workflow(workflow)
    assert "jobs" in data


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_run_text_contains_no_heredoc(workflow):
    """Driving test (plan behaviour 2 static guard) -- the payload must be
    built via `jq -n --arg ...`, never an unquoted `<<EOF` heredoc that
    raw-splices shell variables into a JSON literal.

    RED today: both workflows build client_payload with `-d @- <<EOF`.
    """
    run_text = _dispatch_run_text(workflow)
    assert "<<EOF" not in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text still uses an "
        f"unquoted '<<EOF' heredoc to build client_payload -- ticket #170 "
        f"requires rebuilding the payload with `jq -n --arg ...` instead, "
        f"since raw variable splicing into a JSON literal is not "
        f"JSON-safe (unescaped quotes/backticks/$()/newlines break it)"
    )
    assert "<<'EOF'" not in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text still uses a "
        f"quoted \"<<'EOF'\" heredoc -- same requirement as above"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_run_text_confines_github_expressions_to_env(workflow):
    """Driving test (plan behaviour 2 static guard, plan step 5) -- every
    `${{ ... }}` GitHub Actions expression must live in the step's `env:`
    mapping, never spliced directly into the `run:` shell text.

    RED today for both workflows: each run text directly splices
    `${{ github.repository }}` three times (the `repo` field, the `icon`
    URL, and the `description_url` URL) instead of routing it through a
    `REPO` env var.
    """
    run_text = _dispatch_run_text(workflow)
    assert "${{" not in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text contains a raw "
        f"'${{{{' GitHub Actions expression -- ticket #170 requires every "
        f"such expression to be confined to the step's env: mapping instead"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_run_text_declares_changelog_key(workflow):
    """Driving test (plan behaviour 1, static half) -- the client_payload
    literal/jq program must name a `changelog` key.

    RED today: neither workflow's payload mentions "changelog" anywhere.
    """
    run_text = _dispatch_run_text(workflow)
    assert "changelog" in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text does not mention "
        f"'changelog' anywhere -- ticket #170 requires a changelog key in "
        f"client_payload, sourced from the release's generated notes"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_run_text_warns_on_fetch_failure(workflow):
    """Driving test (plan behaviour 4, static half) -- a failed/empty/null
    notes fetch must emit a `::warning::` annotation, never abort the step.

    RED today: neither workflow's run text contains '::warning::'.
    """
    run_text = _dispatch_run_text(workflow)
    assert "::warning::" in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text does not emit "
        f"'::warning::' anywhere -- ticket #170 requires warning and "
        f"omitting the changelog key (not aborting the dispatch) when the "
        f"notes fetch fails, is empty, whitespace-only, or literal 'null'"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_run_text_mentions_truncation_boundary(workflow):
    """Driving test (plan behaviour 3, static half) -- the ~30KB truncation
    boundary must appear in the run text.

    RED today: neither workflow's run text mentions 30000/truncat* anywhere.
    """
    run_text = _dispatch_run_text(workflow)
    assert "30000" in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text does not mention "
        f"the 30000-char truncation boundary from ticket #170"
    )
    assert "truncat" in run_text.lower(), (
        f"{workflow.name}: {STEP_NAME!r} step's run text does not mention "
        f"truncation anywhere"
    )


def test_release_and_dispatch_run_text_are_byte_identical():
    """Driving test (plan behaviour 5, plan step 6) -- no shared script/
    composite action; duplication between the two workflows is pinned by
    this drift guard instead.

    RED today: release.yml's run text starts with a `DESC=$(jq -r
    '.description' .claude-plugin/plugin.json)` line that dispatch.yml's
    does not have (dispatch.yml gets DESC from its own env instead), so the
    two run strings differ.
    """
    release_run = _dispatch_run_text(RELEASE_WORKFLOW)
    dispatch_run = _dispatch_run_text(DISPATCH_WORKFLOW)
    assert release_run == dispatch_run, (
        "release.yml and dispatch.yml's 'Dispatch to agent-marketplace' "
        "run: blocks must be byte-identical (ticket #170 plan step 6 -- "
        "duplication is pinned by this drift guard instead of a shared "
        "script/composite action)\n"
        f"--- release.yml ---\n{release_run}\n"
        f"--- dispatch.yml ---\n{dispatch_run}"
    )


def test_dispatch_workflow_declares_contents_write_permission():
    """Driving test (plan behaviour 5, plan step 2) -- dispatch.yml needs
    `permissions: contents: write` at the workflow level so its `GH_TOKEN:
    ${{ secrets.GITHUB_TOKEN }}` can authenticate the `gh api
    .../generate-notes` call.

    RED today: dispatch.yml declares no top-level `permissions:` key at all.
    """
    workflow = _load_workflow(DISPATCH_WORKFLOW)
    permissions = workflow.get("permissions")
    assert isinstance(permissions, dict), (
        "dispatch.yml must declare a workflow-level 'permissions:' mapping "
        f"(ticket #170) -- found {permissions!r}"
    )
    assert permissions.get("contents") == "write", (
        "dispatch.yml's workflow-level 'permissions:' must include "
        f"'contents: write' (ticket #170) -- found {permissions!r}"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_env_declares_required_vars(workflow):
    """Driving test (plan behaviour 5, plan step 5) -- both steps' `env:`
    must declare GH_TOKEN/REPO/TAG/VERSION (every `${{ ... }}` expression
    confined to env, per plan step 5).

    RED today: neither workflow's step env declares GH_TOKEN or REPO.
    """
    env = _dispatch_step_env(workflow)
    required = {"GH_TOKEN", "REPO", "TAG", "VERSION"}
    missing = required - env.keys()
    assert not missing, (
        f"{workflow.name}: {STEP_NAME!r} step's env: mapping is missing "
        f"{sorted(missing)} (ticket #170 requires GH_TOKEN, REPO, TAG, "
        f"VERSION all declared in env: so the run: body never splices a "
        f"'${{{{ ... }}}}' expression directly) -- found keys {sorted(env.keys())}"
    )


# ---------------------------------------------------------------------------
# Layer (b) -- real bash + jq harness.
# ---------------------------------------------------------------------------


def _resolve(tool: str) -> str | None:
    return shutil.which(tool)


BASH = _resolve("bash")
JQ = _resolve("jq")

# The stub gh/curl only need to exist on PATH; real gh/curl are never
# invoked by the tests below (curl is real on this machine but the stub
# shadows it via a PATH prefix, since the point is to intercept the POST,
# not perform it).
_SKIP_REASON = None
if BASH is None:
    _SKIP_REASON = "no bash on PATH -- skipping real bash+jq dispatch harness"
elif JQ is None:
    _SKIP_REASON = "no jq on PATH -- skipping real bash+jq dispatch harness"

requires_bash_and_jq = pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))

DEFAULT_NOTES_BODY = "Release notes for @octocat, see #123 for details."

STUB_GH = """#!/usr/bin/env bash
# Stub `gh` for tests -- handles only the one invocation shape this dispatch
# step is expected to use: `gh api repos/.../releases/generate-notes -f
# tag_name=... --jq '.body'`. Behaviour is controlled entirely by env vars so
# the same stub script serves every failure-mode case.
set -u
if [ "${STUB_GH_FAIL:-0}" = "1" ]; then
  echo "stub gh: simulated generate-notes failure" >&2
  exit 1
fi
if [ -n "${STUB_GH_BODY_FILE:-}" ]; then
  cat "${STUB_GH_BODY_FILE}"
else
  printf '%s' "${STUB_GH_BODY:-}"
fi
"""

STUB_CURL = """#!/usr/bin/env bash
# Stub `curl` for tests -- captures whatever stdin the dispatch step pipes
# to it (the payload built by `jq -n ...`) instead of actually POSTing.
set -u
cat > "${STUB_CURL_OUTPUT_FILE:?STUB_CURL_OUTPUT_FILE not set}"
exit 0
"""


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def dispatch_harness(tmp_path):
    """Prepare a tmp_path with a fixture plugin.json, stub gh/curl on PATH,
    and a helper to execute a workflow's dispatch run: text against it."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "agent-worktree",
                "description": "MCP server for git worktree lifecycle management.",
                "version": "0.0.0",
            }
        ),
        encoding="utf-8",
    )

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "gh", STUB_GH)
    _make_executable(bin_dir / "curl", STUB_CURL)

    payload_path = tmp_path / "payload.json"

    def run(workflow: Path, *, gh_fail=False, gh_body=None, gh_body_file=None, extra_env=None):
        run_text = _dispatch_run_text(workflow)
        script_path = tmp_path / "run.sh"
        script_path.write_text(run_text, encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            {
                "GH_PAT": "stub-pat",
                "GH_TOKEN": "stub-token",
                "REPO": "Seretos/agent-worktree",
                "VERSION": "1.2.3",
                "NAME": "agent-worktree",
                "TAG": "agent-worktree--v1.2.3",
                "STUB_CURL_OUTPUT_FILE": str(payload_path),
                "STUB_GH_FAIL": "1" if gh_fail else "0",
            }
        )
        if gh_body is not None:
            env["STUB_GH_BODY"] = gh_body
        if gh_body_file is not None:
            env["STUB_GH_BODY_FILE"] = str(gh_body_file)
        if extra_env:
            env.update(extra_env)

        proc = subprocess.run(
            [BASH, str(script_path)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc, payload_path

    return run


@requires_bash_and_jq
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_payload_carries_changelog_verbatim_and_stays_within_key_limit(dispatch_harness, workflow):
    """Driving test (plan behaviour 1, dynamic half).

    - changelog carries the fetched notes verbatim (mentions/#123 untouched)
    - client_payload has <=10 top-level keys
    - the 9 pre-existing keys are unchanged in value
    """
    proc, payload_path = dispatch_harness(workflow, gh_body=DEFAULT_NOTES_BODY)
    assert proc.returncode == 0, (
        f"dispatch step exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert payload_path.exists(), f"payload.json was never written\nstdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    client_payload = payload["client_payload"]

    assert len(client_payload) <= 10, (
        f"client_payload has {len(client_payload)} top-level keys (max 10 "
        f"per GitHub's repository_dispatch docs): {sorted(client_payload)}"
    )
    assert client_payload.get("changelog") == DEFAULT_NOTES_BODY, (
        f"changelog was not carried verbatim -- got {client_payload.get('changelog')!r}"
    )
    assert client_payload["name"] == "agent-worktree"
    assert client_payload["version"] == "1.2.3"
    assert client_payload["ref"] == "agent-worktree--v1.2.3"
    assert client_payload["repo"] == "Seretos/agent-worktree"
    assert client_payload["category"] == "mcp"
    assert client_payload["tags"] == ["git", "environment"]


@requires_bash_and_jq
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_hostile_changelog_round_trips_exactly_through_json(dispatch_harness, workflow):
    """Driving test (plan behaviour 2, dynamic half) -- every value must be
    JSON-escaped via jq, never raw-spliced. A hostile changelog body
    containing double quotes, backticks, `$()`, and real newlines must
    round-trip exactly through json.loads (a raw heredoc splice would either
    corrupt the payload or execute the `$()` as a shell command)."""
    hostile = 'Notes with "quotes", `backticks`, $(rm -rf /tmp/should-not-run), and\nreal\nnewlines.'
    proc, payload_path = dispatch_harness(workflow, gh_body=hostile)
    assert proc.returncode == 0, (
        f"dispatch step exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["client_payload"]["changelog"] == hostile


@requires_bash_and_jq
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_oversized_changelog_is_truncated_at_30000_chars(dispatch_harness, workflow, tmp_path):
    """Driving test (plan behaviour 3, dynamic half) -- an ~80000-char body
    is truncated to the first 30000 chars plus a truncation suffix naming
    the release URL."""
    big_body_file = tmp_path / "big_body.txt"
    big_body_file.write_text("x" * 80000, encoding="utf-8")

    proc, payload_path = dispatch_harness(workflow, gh_body_file=big_body_file)
    assert proc.returncode == 0, (
        f"dispatch step exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    changelog = payload["client_payload"]["changelog"]

    assert changelog.startswith("x" * 30000), "truncated body must keep the first 30000 chars verbatim"
    assert len(changelog) > 30000, "truncated body must carry a suffix after the 30000-char cutoff"
    assert "truncated" in changelog.lower()
    expected_url = "https://github.com/Seretos/agent-worktree/releases/tag/agent-worktree--v1.2.3"
    assert expected_url in changelog, f"truncation suffix must name the release URL, got: {changelog[-200:]!r}"


@requires_bash_and_jq
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_changelog_at_exactly_30000_chars_is_not_truncated(dispatch_harness, workflow, tmp_path):
    """Additional coverage (plan behaviour 3 boundary case) -- a body of
    exactly 30000 chars must pass through untouched, no suffix appended."""
    boundary_body_file = tmp_path / "boundary_body.txt"
    boundary_body_file.write_text("y" * 30000, encoding="utf-8")

    proc, payload_path = dispatch_harness(workflow, gh_body_file=boundary_body_file)
    assert proc.returncode == 0, (
        f"dispatch step exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    changelog = payload["client_payload"]["changelog"]
    assert changelog == "y" * 30000, "a body exactly at the 30000-char boundary must not be truncated"


@requires_bash_and_jq
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_oversized_multibyte_changelog_stays_valid_utf8_json(dispatch_harness, workflow, tmp_path):
    """Additional coverage (plan behaviour 3 edge case) -- truncation must be
    Unicode-codepoint-safe: an oversized multi-byte body must still produce
    valid UTF-8/JSON output, not a body cut mid-codepoint."""
    multibyte_body_file = tmp_path / "multibyte_body.txt"
    multibyte_body_file.write_text("é中\U0001f600" * 20000, encoding="utf-8")

    proc, payload_path = dispatch_harness(workflow, gh_body_file=multibyte_body_file)
    assert proc.returncode == 0, (
        f"dispatch step exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    # json.loads itself proves the bytes are valid UTF-8/JSON; a mid-codepoint
    # cut would raise UnicodeDecodeError or json.JSONDecodeError here.
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    changelog = payload["client_payload"]["changelog"]
    assert len(changelog) >= 30000
    assert "truncated" in changelog.lower()


@requires_bash_and_jq
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
@pytest.mark.parametrize(
    "case_name,gh_kwargs",
    [
        ("gh_api_fails", {"gh_fail": True}),
        ("empty_body", {"gh_body": ""}),
        ("whitespace_only_body", {"gh_body": "   \n\t  "}),
        ("literal_null_body", {"gh_body": "null"}),
    ],
)
def test_failed_or_empty_notes_fetch_warns_and_omits_changelog(dispatch_harness, workflow, case_name, gh_kwargs):
    """Driving test (plan behaviour 4, dynamic half) -- a failed, empty,
    whitespace-only, or literal-"null" notes fetch must each: exit 0, print
    a ::warning:: naming the tag, still write a parseable payload.json with
    the changelog key entirely absent, and leave the other keys intact. The
    dispatch itself must still succeed."""
    proc, payload_path = dispatch_harness(workflow, **gh_kwargs)
    assert proc.returncode == 0, (
        f"[{case_name}] dispatch step must exit 0 even when the notes fetch "
        f"fails/is empty -- the dispatch itself must still succeed "
        f"(exit {proc.returncode})\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "::warning::" in proc.stdout, (
        f"[{case_name}] expected a ::warning:: annotation on stdout, got: {proc.stdout!r}"
    )
    assert "agent-worktree--v1.2.3" in proc.stdout, (
        f"[{case_name}] ::warning:: must name the tag, got: {proc.stdout!r}"
    )
    assert payload_path.exists(), f"[{case_name}] payload.json was never written"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    client_payload = payload["client_payload"]
    assert "changelog" not in client_payload, (
        f"[{case_name}] changelog key must be omitted entirely, got: {client_payload.get('changelog')!r}"
    )
    assert client_payload["name"] == "agent-worktree"
    assert client_payload["ref"] == "agent-worktree--v1.2.3"
    assert client_payload["repo"] == "Seretos/agent-worktree"
