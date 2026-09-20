"""Driving tests for ticket #195: workflow guards.

R3 -- staging logic lives in ONE script called by both release.yml and
      test.yml (no inline copy that can drift).
R4 -- every job that invokes a repo file obtains the workspace.
R5 -- every release.yml job is classified; every non-publishing one has a
      pull_request counterpart in test.yml.

Each guard is a plain function proved to have teeth by a synthetic case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
STAGE_SCRIPT_PATH = ".github/scripts/stage-release-zip.sh"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _run_text(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


# --------------------------------------------------------------------------
# R4 guard
# --------------------------------------------------------------------------

_REPO_FILE_INVOCATION = re.compile(
    r"\.github/scripts/|(?:^|[\s\"'])(?:\./)?scripts/|python3? -m pytest|"
    r"pip install -e|(?:^|\s)(?:bash|sh|pwsh|python3?)\s+\S+\.(?:sh|ps1|py)\b|"
    r"(?:^|\s)make\b",
    re.M,
)


def invokes_repo_file(job: dict) -> bool:
    if _REPO_FILE_INVOCATION.search(_run_text(job)):
        return True
    return any(str(s.get("uses", "")).startswith("./") for s in job.get("steps", []))


def obtains_workspace(job: dict) -> bool:
    for s in job.get("steps", []):
        uses = str(s.get("uses", ""))
        if uses.startswith("actions/checkout"):
            return True
        if uses.startswith("actions/download-artifact") and (
            s.get("with") or {}
        ).get("name") == "stamped-source":
            return True
    return False


def jobs_missing_workspace(jobs: dict) -> list[str]:
    return sorted(n for n, j in jobs.items() if invokes_repo_file(j) and not obtains_workspace(j))


_ALL_JOBS = [
    (wf.name, job_name)
    for wf in sorted(WORKFLOWS_DIR.glob("*.yml"))
    for job_name in _load(wf.name)["jobs"]
]


@pytest.mark.parametrize("workflow,job_name", _ALL_JOBS)
def test_every_job_invoking_a_repo_file_obtains_the_workspace(workflow, job_name):
    job = _load(workflow)["jobs"][job_name]
    assert not (invokes_repo_file(job) and not obtains_workspace(job)), (
        f"{workflow}:{job_name} invokes a repo file without actions/checkout"
    )


def test_guard_flags_a_synthetic_job_without_checkout():
    jobs = {
        "bad": {"steps": [{"run": "bash .github/scripts/stage-release-zip.sh a b c d"}]},
        "bad_ps": {"steps": [{"run": "./scripts/build.ps1 -Clean"}]},
        "bad_local_action": {"steps": [{"uses": "./.github/actions/x"}]},
        "good": {"steps": [{"uses": "actions/checkout@v4"}, {"run": "./scripts/build.ps1"}]},
        "good_artifact": {
            "steps": [
                {"uses": "actions/download-artifact@v4", "with": {"name": "stamped-source"}},
                {"run": "./scripts/build.ps1"},
            ]
        },
        "wrong_artifact": {
            "steps": [
                {"uses": "actions/download-artifact@v4", "with": {"name": "bin-linux"}},
                {"run": "./scripts/build.ps1"},
            ]
        },
    }
    assert jobs_missing_workspace(jobs) == ["bad", "bad_local_action", "bad_ps", "wrong_artifact"]


def test_dispatch_sparse_scripts_checkout_satisfies_the_guard():
    jobs = _load("dispatch.yml")["jobs"]
    assert jobs_missing_workspace(jobs) == []


# --------------------------------------------------------------------------
# R5 guard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Mirror:
    test_job: str


@dataclass(frozen=True)
class Publishing:
    reason: str


@dataclass(frozen=True)
class Exempt:
    reason: str


RELEASE_JOB_COVERAGE = {
    "stamp": Exempt("pre-flight requires HEAD == main tip; covered by #184 script tests"),
    "build": Mirror("build"),
    "assemble": Publishing("pushes orphan branch, creates Release, dispatches marketplace"),
}


def unclassified_release_jobs(release_jobs: dict, table: dict) -> list[str]:
    return sorted(set(release_jobs) - set(table))


def coverage_problems(release_jobs: dict, test_wf: dict, table: dict) -> list[str]:
    problems = [f"unclassified release job: {j}" for j in unclassified_release_jobs(release_jobs, table)]
    test_jobs = test_wf["jobs"]
    # PyYAML parses the bare `on` key as boolean True.
    triggers = test_wf.get("on", test_wf.get(True))
    runs_on_pr = "pull_request" in (triggers if isinstance(triggers, (dict, list)) else [triggers])
    for name, cls in table.items():
        if isinstance(cls, Mirror):
            if cls.test_job not in test_jobs:
                problems.append(f"{name}: mirror {cls.test_job!r} missing from test.yml")
            elif not runs_on_pr:
                problems.append(f"{name}: test.yml does not run on pull_request")
        elif isinstance(cls, Publishing) and name in test_jobs:
            problems.append(f"{name}: Publishing job must not be mirrored into test.yml")
    return problems


def test_every_release_job_is_classified_and_mirrored():
    problems = coverage_problems(
        _load("release.yml")["jobs"], _load("test.yml"), RELEASE_JOB_COVERAGE
    )
    assert problems == []


def test_guard_flags_an_unclassified_release_job():
    jobs = {"stamp": {}, "build": {}, "assemble": {}, "brand_new": {}}
    assert unclassified_release_jobs(jobs, RELEASE_JOB_COVERAGE) == ["brand_new"]
    test_wf = {"on": {"pull_request": None}, "jobs": {"build": {}}}
    assert coverage_problems(jobs, test_wf, RELEASE_JOB_COVERAGE) == [
        "unclassified release job: brand_new"
    ]


def test_guard_flags_missing_mirror_and_mirrored_publishing_job():
    jobs = {"stamp": {}, "build": {}, "assemble": {}}
    no_build = {"on": {"pull_request": None}, "jobs": {"pytest": {}}}
    assert any("mirror" in p for p in coverage_problems(jobs, no_build, RELEASE_JOB_COVERAGE))
    leaked = {"on": {"pull_request": None}, "jobs": {"build": {}, "assemble": {}}}
    assert any("Publishing" in p for p in coverage_problems(jobs, leaked, RELEASE_JOB_COVERAGE))


# --------------------------------------------------------------------------
# R3 -- one script, two callers
# --------------------------------------------------------------------------


def test_staging_logic_lives_in_one_script_called_by_both_workflows():
    assemble = _load("release.yml")["jobs"]["assemble"]
    stage_step = next(s for s in assemble["steps"] if s.get("id") == "stage")
    run = stage_step["run"]
    assert STAGE_SCRIPT_PATH in run
    for inline in ("cp -a stamped/", "python3 - <<", "EXECS"):
        assert inline not in run, f"inline staging logic remains in release.yml: {inline!r}"
    test_runs = [_run_text(j) for j in _load("test.yml")["jobs"].values()]
    assert any(STAGE_SCRIPT_PATH in t for t in test_runs), (
        "no test.yml job invokes the shared staging script"
    )


def test_build_script_is_invoked_by_both_release_build_and_test_build():
    assert "scripts/build.ps1" in _run_text(_load("release.yml")["jobs"]["build"])
    test_jobs = _load("test.yml")["jobs"]
    assert "build" in test_jobs
    assert "scripts/build.ps1" in _run_text(test_jobs["build"])
