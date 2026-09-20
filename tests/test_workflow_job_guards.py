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


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _gated_jobs(name: str, test_jobs: dict) -> list[str]:
    """Jobs in `name`'s needs chain (itself included) carrying an `if:`."""
    seen: set[str] = set()
    todo = [name]
    gated = []
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in test_jobs:
            continue
        seen.add(cur)
        if "if" in (test_jobs[cur] or {}):
            gated.append(cur)
        todo.extend(_needs(test_jobs[cur] or {}))
    return sorted(gated)


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
            else:
                for gated in _gated_jobs(cls.test_job, test_jobs):
                    problems.append(
                        f"{name}: mirror {cls.test_job!r} is gated by an `if:` on {gated!r}"
                        " and may not run on pull_request"
                    )
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


def test_guard_flags_a_mirror_gated_by_an_if_condition():
    jobs = {"stamp": {}, "build": {}, "assemble": {}}
    pr = {"pull_request": None}
    direct = {"on": pr, "jobs": {"build": {"if": "github.event_name != 'pull_request'"}}}
    problems = coverage_problems(jobs, direct, RELEASE_JOB_COVERAGE)
    assert any("gated by an `if:`" in p for p in problems), problems
    via_needs = {
        "on": pr,
        "jobs": {"build": {"needs": ["prep"]}, "prep": {"if": "github.ref == 'refs/heads/main'"}},
    }
    problems = coverage_problems(jobs, via_needs, RELEASE_JOB_COVERAGE)
    assert any("gated by an `if:`" in p and "prep" in p for p in problems), problems
    clean = {"on": pr, "jobs": {"build": {"needs": "prep"}, "prep": {}}}
    assert coverage_problems(jobs, clean, RELEASE_JOB_COVERAGE) == []


# --------------------------------------------------------------------------
# R3 -- one script, two callers
# --------------------------------------------------------------------------




def _command_lines(run: str) -> list[str]:
    """Non-blank, non-comment lines of a run block, stripped."""
    return [
        ln.strip()
        for ln in str(run).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _invokes(run: str, path: str, *, launcher: str | None = None) -> bool:
    """True when some command line INVOKES `path` in command position
    (optionally via a required `launcher`), i.e. not inside a comment or an
    echo/printf argument."""
    if launcher:
        lead = rf"{launcher}(?:\s+-\S+)*\s+"
    else:
        lead = r"(?:(?:bash|sh|pwsh|powershell)(?:\s+-\S+)*\s+)?"
    pat = re.compile(rf"^{lead}(?:\./)?{re.escape(path)}(?:\s|$)")
    return any(pat.match(ln) for ln in _command_lines(run))


MAX_STAGE_STEP_LINES = 12
_INLINE_STAGING_MARKERS = ("<<", "cp -a", "zipfile")


def staging_delegation_problems(release_wf: dict, test_wf: dict) -> list[str]:
    steps = release_wf["jobs"].get("assemble", {}).get("steps", [])
    stage = next((s for s in steps if s.get("id") == "stage"), None)
    if stage is None:
        return ["release.yml assemble has no step with id 'stage'"]
    problems = []
    run = str(stage.get("run", ""))
    if not _invokes(run, STAGE_SCRIPT_PATH, launcher="bash"):
        problems.append(f"release.yml stage step does not invoke `bash {STAGE_SCRIPT_PATH}`")
    lines = _command_lines(run)
    for ln in lines:
        for marker in _INLINE_STAGING_MARKERS:
            if marker in ln:
                problems.append(f"release.yml stage step keeps inline staging logic ({marker!r}): {ln}")
    if len(lines) > MAX_STAGE_STEP_LINES:
        problems.append(
            f"release.yml stage step has {len(lines)} command lines (> {MAX_STAGE_STEP_LINES}):"
            " staging logic must live in the script"
        )
    package = test_wf["jobs"].get("package")
    if package is None:
        problems.append("test.yml has no `package` job")
    elif not any(_invokes(str(s.get("run", "")), STAGE_SCRIPT_PATH) for s in package.get("steps", [])):
        problems.append(f"test.yml package job has no step invoking {STAGE_SCRIPT_PATH}")
    return problems


def test_staging_logic_lives_in_one_script_called_by_both_workflows():
    assert staging_delegation_problems(_load("release.yml"), _load("test.yml")) == []


def test_staging_guard_flags_renamed_inline_copy_and_decoy_mentions():
    inline = "\n".join(
        [f"# delegates to {STAGE_SCRIPT_PATH}", f"echo {STAGE_SCRIPT_PATH}"]
        + ['cp -a "$SRC"/x "$STAGE"/', "python - <<'PY'", "import zipfile", "PY"]
        + [f"echo filler {i}" for i in range(15)]
    )
    release = {"jobs": {"assemble": {"steps": [{"id": "stage", "run": inline}]}}}
    test = {
        "jobs": {
            "pytest": {"steps": [{"run": f"bash {STAGE_SCRIPT_PATH} a b c d"}]},
            "package": {"steps": [{"run": f"echo {STAGE_SCRIPT_PATH}"}]},
        }
    }
    problems = staging_delegation_problems(release, test)
    assert any("does not invoke" in p for p in problems)
    assert any("inline staging logic" in p for p in problems)
    assert any("command lines" in p for p in problems)
    assert any("package job has no step" in p for p in problems)
    good_run = (
        f'# stage\nbash {STAGE_SCRIPT_PATH} stamped bins "$STAGE" "$ZIP"\necho "x=1" >> "$GITHUB_OUTPUT"'
    )
    good_release = {"jobs": {"assemble": {"steps": [{"id": "stage", "run": good_run}]}}}
    good_test = {"jobs": {"package": {"steps": [{"run": f"bash {STAGE_SCRIPT_PATH} a b c d"}]}}}
    assert staging_delegation_problems(good_release, good_test) == []


# --- build.ps1 invoked structurally by both build jobs -------------------

REQUIRED_BUILD_OS = {"windows-latest", "ubuntu-22.04"}
_BUILD_CMD = re.compile(
    r"^(?:pwsh(?:\s+-\S+)*\s+)?(?:\./)?scripts/build\.ps1(?=\s)(?=.*\s-Clean\b)(?=.*\s-Package\b)"
)


def _matrix_os(job: dict) -> set[str]:
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    oses = set(matrix["os"]) if isinstance(matrix.get("os"), list) else set()
    for entry in matrix.get("include") or []:
        if isinstance(entry, dict) and "os" in entry:
            oses.add(entry["os"])
    return oses


def build_job_problems(job: dict) -> list[str]:
    problems = []
    steps = job.get("steps", [])
    if not any(_BUILD_CMD.match(ln) for s in steps for ln in _command_lines(str(s.get("run", "")))):
        problems.append("no step runs `./scripts/build.ps1 -Clean -Package` as a command")
    missing = REQUIRED_BUILD_OS - _matrix_os(job)
    if missing:
        problems.append(f"matrix does not cover {sorted(missing)}")
    if not any(
        str(s.get("uses", "")).startswith("actions/upload-artifact")
        and str((s.get("with") or {}).get("path", "")).startswith("bin")
        for s in steps
    ):
        problems.append("no upload-artifact step uploading bin/")
    return problems


def test_build_script_is_invoked_by_both_release_build_and_test_build():
    assert build_job_problems(_load("release.yml")["jobs"]["build"]) == []
    test_jobs = _load("test.yml")["jobs"]
    assert "build" in test_jobs, "test.yml has no `build` job"
    assert build_job_problems(test_jobs["build"]) == []


def test_build_guard_flags_decoy_single_os_and_no_upload():
    decoy = {
        "steps": [
            {"run": "echo scripts/build.ps1 -Clean -Package"},
            {"run": "# ./scripts/build.ps1 -Clean -Package"},
        ]
    }
    assert len(build_job_problems(decoy)) == 3
    single_os = {
        "strategy": {"matrix": {"include": [{"os": "windows-latest"}]}},
        "steps": [
            {"shell": "pwsh", "run": "./scripts/build.ps1 -Clean -Package"},
            {"uses": "actions/upload-artifact@v4", "with": {"path": "bin/"}},
        ],
    }
    assert build_job_problems(single_os) == ["matrix does not cover ['ubuntu-22.04']"]
    good = {
        "strategy": {"matrix": {"os": ["windows-latest", "ubuntu-22.04"]}},
        "steps": [
            {"run": "./scripts/build.ps1 -Clean -Package"},
            {"uses": "actions/upload-artifact@v4", "with": {"path": "bin/"}},
        ],
    }
    assert build_job_problems(good) == []
