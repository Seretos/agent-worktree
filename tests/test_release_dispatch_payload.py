"""Driving tests for ticket #170 (`changelog` in the marketplace dispatch
payload) and ticket #184 (real changelogs for the orphan-tag release flow).

Both `.github/workflows/release.yml` (job `assemble`, step "Dispatch to
agent-marketplace") and `.github/workflows/dispatch.yml` (job `dispatch`,
same step name) POST a `client_payload` to the agent-marketplace repo.
Ticket #170 rebuilt the payload with `jq -n --arg ...` and added a
`changelog` key; ticket #184 moved all of that payload-building logic out of
the workflow YAML entirely, into the shared
`.github/scripts/marketplace-payload.sh` script (see
tests/test_release_scripts.py for that script's own driving tests).

This module now carries only the static, text-level guards on the two
workflow YAML files -- no external tool dependency, so these run on every CI
leg:

  - the "Dispatch to agent-marketplace" step's `run:` text contains no
    heredoc, no raw `${{ ... }}` expression, and actually delegates to
    `.github/scripts/marketplace-payload.sh`;
  - both workflows' `run:` text for that step stays byte-identical (the
    duplication ticket #170 introduced is still pinned by this guard, even
    though the duplicated text itself is now a two-line delegation rather
    than ~70 lines of inline jq);
  - `dispatch.yml`'s workflow-level `permissions:` and its required `env:`
    vars and sparse-checkout of `main`'s `.github/scripts`.

A "Layer (b)" of six `dispatch_harness`-based tests (~18 parametrized
instances) used to live here, driving the real `bash`+`jq` interpreters
against the step's old *inline* run: text with stub `gh`/`curl` scripts.
Ticket #184 retired it -- see the retirement note at the end of this file
for why, and where the equivalent (now more precise) coverage moved.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Ticket #184 retires the three checks that used to live here
# (test_dispatch_step_run_text_declares_changelog_key,
# test_dispatch_step_run_text_warns_on_fetch_failure,
# test_dispatch_step_run_text_mentions_truncation_boundary): once the payload
# is built by the shared `.github/scripts/marketplace-payload.sh` (see
# test_dispatch_step_invokes_shared_marketplace_payload_script below), the
# "changelog"/"::warning::"/"30000"/"truncat*" strings move out of the
# workflow YAML's `run:` text entirely and into that script -- asserting
# their presence *in run_text* would then assert something false about a
# correct implementation. The equivalent, more precise coverage now lives in
# tests/test_release_scripts.py's marketplace-payload.sh tests (R3):
# test_marketplace_payload_hostile_changelog_round_trips_byte_for_byte,
# test_marketplace_payload_empty_body_warns_and_omits_key,
# test_marketplace_payload_truncates_above_30000_chars, and friends.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_invokes_shared_marketplace_payload_script(workflow):
    """Driving test (ticket #184, R4) -- the payload-building logic must be
    delegated to `.github/scripts/marketplace-payload.sh`, not stay inline
    in the workflow `run:` text (which is how ticket #170 originally built
    it, and how #184's own predecessor bug -- sourcing the changelog from
    `releases/generate-notes` against the orphan tag -- got in).

    RED today: neither workflow's run text mentions
    'marketplace-payload.sh' anywhere; both still carry the full inline jq
    program (identifiable by its `def truncated_changelog:` function).
    """
    run_text = _dispatch_run_text(workflow)
    assert "marketplace-payload.sh" in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text does not invoke "
        f"'marketplace-payload.sh' -- ticket #184 requires the payload-"
        f"building logic to move into the shared "
        f".github/scripts/marketplace-payload.sh script"
    )
    assert "def truncated_changelog" not in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text still contains the "
        f"old inline jq truncation function -- this logic must move into "
        f".github/scripts/marketplace-payload.sh, not stay duplicated in "
        f"the workflow YAML"
    )
    assert "releases/generate-notes" not in run_text, (
        f"{workflow.name}: {STEP_NAME!r} step's run text must never call "
        f"releases/generate-notes against the orphan tag directly -- that "
        f"reproduces the empty-notes bug ticket #184 fixes; the changelog "
        f"must come from marketplace-payload.sh's `gh release view` call "
        f"instead"
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


def test_dispatch_workflow_declares_contents_read_permission():
    """Driving test (ticket #184) -- dispatch.yml's `permissions:` must drop
    from `contents: write` (ticket #170's requirement, when this workflow's
    own `gh api .../generate-notes` call needed write-capable auth) to
    `contents: read`: #184 removes that inline `generate-notes` call
    entirely (see test_dispatch_step_invokes_shared_marketplace_payload_script
    above) -- the only remaining `gh` calls (`gh release view` inside
    marketplace-payload.sh, `gh api .../git/refs/...` reads) are read-only,
    so `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` no longer needs write access.

    RED today: dispatch.yml still declares 'contents: write'.
    """
    workflow = _load_workflow(DISPATCH_WORKFLOW)
    permissions = workflow.get("permissions")
    assert isinstance(permissions, dict), (
        "dispatch.yml must declare a workflow-level 'permissions:' mapping "
        f"(ticket #184) -- found {permissions!r}"
    )
    assert permissions.get("contents") == "read", (
        "dispatch.yml's workflow-level 'permissions:' must be "
        f"'contents: read' (ticket #184 -- no write-requiring call remains "
        f"in this workflow) -- found {permissions!r}"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=WORKFLOW_IDS)
def test_dispatch_step_env_declares_required_vars(workflow):
    """Driving test (ticket #170 + #184) -- both steps' `env:` must declare
    every var the shared `.github/scripts/marketplace-payload.sh` needs
    (GH_TOKEN/REPO/TAG/VERSION from #170, plus PLUGIN_JSON/MAX_CHANGELOG_LEN
    added by #184 now that those were previously computed/literal inline in
    the workflow's own jq program -- see
    tests/test_release_scripts.py::test_marketplace_payload_fails_loudly_when_required_env_var_missing
    for the script-level half of this requirement).

    RED today: neither workflow's step env declares PLUGIN_JSON or
    MAX_CHANGELOG_LEN (both are still inline literals/lookups, not env
    vars).
    """
    env = _dispatch_step_env(workflow)
    required = {"GH_TOKEN", "REPO", "TAG", "VERSION", "PLUGIN_JSON", "MAX_CHANGELOG_LEN"}
    missing = required - env.keys()
    assert not missing, (
        f"{workflow.name}: {STEP_NAME!r} step's env: mapping is missing "
        f"{sorted(missing)} (ticket #184 requires GH_TOKEN, REPO, TAG, "
        f"VERSION, PLUGIN_JSON, MAX_CHANGELOG_LEN all declared in env: so "
        f"the shared marketplace-payload.sh script receives every value it "
        f"needs and the run: body never splices a '${{{{ ... }}}}' expression "
        f"directly) -- found keys {sorted(env.keys())}"
    )


def test_dispatch_workflow_sparse_checks_out_main_scripts():
    """Driving test (ticket #184, R4) -- dispatch.yml's only checkout is
    `ref: release`, which carries no `.github/` tree at all, so it cannot
    reach `.github/scripts/marketplace-payload.sh` on its own. #184 adds a
    second `actions/checkout@v4` step: `ref: main`, sparse-checked-out to
    just `.github/scripts`, landed at `path: .ci-scripts` (plan Approach
    section, dispatch.yml bullet).

    RED today: dispatch.yml has exactly one checkout step
    ("Checkout release branch", ref: release) -- no second checkout of
    main's .github/scripts exists.
    """
    workflow = _load_workflow(DISPATCH_WORKFLOW)
    checkout_steps = [
        step
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    main_scripts_checkouts = [
        step
        for step in checkout_steps
        if (step.get("with") or {}).get("ref") == "main"
        and ".github/scripts" in str((step.get("with") or {}).get("sparse-checkout", ""))
    ]
    assert main_scripts_checkouts, (
        "dispatch.yml must have a second actions/checkout@v4 step with "
        "ref: main and sparse-checkout: .github/scripts (ticket #184) -- "
        f"found checkout steps: {checkout_steps}"
    )
    assert (main_scripts_checkouts[0].get("with") or {}).get("path") == ".ci-scripts", (
        "dispatch.yml's main-scripts checkout must land at path: .ci-scripts "
        f"(plan Approach section) -- found "
        f"{(main_scripts_checkouts[0].get('with') or {}).get('path')!r}"
    )



# ---------------------------------------------------------------------------
# Ticket #184 retires "Layer (b)" that used to live here: six
# `dispatch_harness`-based tests (~18 parametrized instances) that drove the
# real bash+jq interpreters against the "Dispatch to agent-marketplace"
# step's *inline* run: text with stub gh/curl scripts. That run: text no
# longer contains any payload-building logic at all (see
# test_dispatch_step_invokes_shared_marketplace_payload_script above) --
# it is now a two-line delegation to the shared
# .github/scripts/marketplace-payload.sh, which needs a $SCRIPTS env var
# only release.yml/dispatch.yml themselves supply (via $RUNNER_TEMP/
# ci-scripts and .ci-scripts/.github/scripts respectively), so driving it
# from a bare tmp_path here would test the harness's own plumbing, not this
# repo's behaviour, and would break on every future change to that
# plumbing without anything actually being wrong. The equivalent, more
# precise coverage moved to tests/test_release_scripts.py's
# marketplace-payload.sh tests (R3): hostile-changelog round-trip,
# gh-release-view-failure-is-fatal, empty/whitespace/null-body warn+omit,
# 30000-char truncation (boundary + custom MAX_CHANGELOG_LEN), and the
# oversized multibyte-safety case (test_marketplace_payload_oversized_
# multibyte_changelog_stays_valid_utf8_json), added there to close the one
# gap this retirement would otherwise have left uncovered.
# ---------------------------------------------------------------------------
