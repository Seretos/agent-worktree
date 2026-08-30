"""Driving tests for ticket #184: real changelogs for the orphan-tag release
flow.

Three new shell scripts under `.github/scripts/` carry the behavioural core
of this ticket -- none of them exist yet (this module is written during the
`tests` phase, before any production code lands), so every test below is
expected to fail against a genuinely missing script, for the reasons
documented inline. Only after the `implement` phase writes the three scripts
do these turn green.

  * `prev-release-tag.sh` (R1)      -- strict-semver predecessor resolution,
                                       pure text processing (awk + sort),
                                       no git/network dependency.
  * `preflight-src-tags.sh` (R2)    -- the `stamp`-job pre-flight: four
                                       ordered checks, each `::error::` +
                                       `exit 1` before any side effect.
  * `marketplace-payload.sh` (R3)   -- the shared dispatch-payload builder,
                                       changelog sourced from the *published*
                                       release body (`gh release view`),
                                       never a fresh `generate-notes` call.

All three are invoked through real `bash` (Git-for-Windows bash by absolute
path on Windows -- see `_resolve_git_bash` below; a bare `bash` on PATH on
Windows resolves to the WSL stub and fails, per ticket #184 Amendment §3)
with stub `gh` executables prepended to PATH, so no live GitHub API call
happens.

Ticket #184 forwarded plan-critic notes (rev. 3, honoured explicitly below):
  1. The predecessor-tag lookup via `gh api .../matching-refs/tags/...`
     pages at 30 items by default -- `test_preflight_paginates_beyond_30_candidate_tags`
     exercises a >30-candidate list and asserts both the correct highest tag
     and the literal `--paginate` flag in the recorded `gh` invocation.
  2. `fetch-depth: 0` on the release job's checkout is a workflow-YAML
     concern, not a script concern -- out of scope for this module.
  3. `marketplace-payload.sh` needs 5 required env vars (REPO/TAG/VERSION/
     PLUGIN_JSON/MAX_CHANGELOG_LEN -- GH_TOKEN is consumed by the `gh`
     binary itself, not read as a script variable) -- see
     `test_marketplace_payload_fails_loudly_when_required_env_var_missing`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
PREV_RELEASE_TAG_SCRIPT = SCRIPTS_DIR / "prev-release-tag.sh"
PREFLIGHT_SCRIPT = SCRIPTS_DIR / "preflight-src-tags.sh"
MARKETPLACE_PAYLOAD_SCRIPT = SCRIPTS_DIR / "marketplace-payload.sh"


# ---------------------------------------------------------------------------
# Interpreter resolution -- per ticket #184 Amendment §3: on Windows, only
# Git-for-Windows bash by *absolute path* is usable; `bash` bare on PATH
# resolves to the WSL stub there and fails. Never fall back to
# shutil.which("bash") on Windows.
# ---------------------------------------------------------------------------


def _resolve_git_bash() -> str | None:
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).is_file():
                return candidate
        return None
    return shutil.which("bash")


GIT_BASH = _resolve_git_bash()
GIT_REAL = shutil.which("git")
JQ = shutil.which("jq")

requires_git_bash = pytest.mark.skipif(
    GIT_BASH is None,
    reason=(
        "no usable bash found -- on Windows neither Git-for-Windows bash.exe "
        "path exists; on POSIX no bash on PATH"
    ),
)

requires_git_bash_and_jq = pytest.mark.skipif(
    GIT_BASH is None or JQ is None,
    reason="requires both a usable bash and jq on PATH",
)


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ===========================================================================
# R1 -- prev-release-tag.sh
# ===========================================================================


def _run_prev_release_tag(candidate_lines: list[str], tag: str) -> subprocess.CompletedProcess:
    stdin_text = "".join(line + "\n" for line in candidate_lines)
    return subprocess.run(
        [GIT_BASH, str(PREV_RELEASE_TAG_SCRIPT), tag],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=15,
    )


@requires_git_bash
@pytest.mark.parametrize(
    "case_name,candidates,tag,expected",
    [
        (
            "numeric_minor_ordering_not_lexicographic",
            ["agent-worktree--v0.1.9", "agent-worktree--v0.1.10"],
            "agent-worktree--v0.2.0",
            "agent-worktree--v0.1.10",
        ),
        (
            "prerelease_numeric_id_ordering",
            ["agent-worktree--v1.0.0-rc.2", "agent-worktree--v1.0.0-rc.10"],
            "agent-worktree--v1.1.0",
            "agent-worktree--v1.0.0-rc.10",
        ),
        (
            "release_outranks_prerelease_of_same_core",
            ["agent-worktree--v1.0.0-rc.10", "agent-worktree--v1.0.0"],
            "agent-worktree--v1.1.0",
            "agent-worktree--v1.0.0",
        ),
        (
            "more_prerelease_ids_outranks_fewer",
            ["agent-worktree--v1.0.0-alpha", "agent-worktree--v1.0.0-alpha.1"],
            "agent-worktree--v1.1.0",
            "agent-worktree--v1.0.0-alpha.1",
        ),
        (
            "self_excluded_even_when_present_in_candidates",
            ["agent-worktree--v1.0.0", "agent-worktree--v0.9.0"],
            "agent-worktree--v1.0.0",
            "agent-worktree--v0.9.0",
        ),
        (
            "foreign_plugin_tags_ignored",
            ["agent-worktree--v0.5.0", "agent-comfy--v9.9.9"],
            "agent-worktree--v0.6.0",
            "agent-worktree--v0.5.0",
        ),
        (
            "src_marker_tags_ignored",
            ["agent-worktree--v0.5.0", "src/agent-worktree--v0.5.9"],
            "agent-worktree--v0.6.0",
            "agent-worktree--v0.5.0",
        ),
        (
            "refs_tags_prefix_tolerated",
            ["refs/tags/agent-worktree--v0.5.0"],
            "agent-worktree--v0.6.0",
            "agent-worktree--v0.5.0",
        ),
        (
            "malformed_candidate_tags_ignored",
            ["agent-worktree--v1.2", "agent-worktree--v01.2.3", "agent-worktree--v1.0.0"],
            "agent-worktree--v1.1.0",
            "agent-worktree--v1.0.0",
        ),
        (
            "empty_candidates_yields_empty",
            [],
            "agent-worktree--v0.1.0",
            "",
        ),
    ],
)
def test_prev_release_tag_resolves_correct_predecessor(case_name, candidates, tag, expected):
    """Driving test (R1) -- prev-release-tag.sh must print the strict-semver
    highest candidate tag other than `$1` itself, filtered to
    `^agent-worktree--v<strict semver>$` (after an optional `refs/tags/`
    strip), ordered via a fixed-width sort key -- never `sort -V`, whose
    prerelease handling differs between ubuntu-22.04 coreutils and
    Git-for-Windows.

    RED today: `.github/scripts/prev-release-tag.sh` does not exist.
    """
    proc = _run_prev_release_tag(candidates, tag)
    assert proc.returncode == 0, (
        f"[{case_name}] expected exit 0, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == expected, (
        f"[{case_name}] expected {expected!r}, got {proc.stdout!r}\nstderr={proc.stderr!r}"
    )


@requires_git_bash
@pytest.mark.parametrize(
    "version,expected_code",
    [
        ("1.2.3", 0),
        ("1.2.3-rc.1", 0),
        ("01.2.3", 1),
        ("1.2", 1),
        ("1.2.3+build", 1),
        ("1.2.3-", 1),
    ],
)
def test_prev_release_tag_check_version_mode(version, expected_code):
    """Driving test (R1) -- `prev-release-tag.sh --check-version <V>` must
    exit 0 for a strict-semver version and exactly 1 (not merely "nonzero",
    so a missing script's generic exit-127 failure cannot masquerade as a
    correct rejection) for a malformed one -- this is the same grammar used
    to filter stdin candidates above, and replaces release.yml's looser
    inline regex.

    RED today: script does not exist (bash exits 127, not 0 or 1).
    """
    proc = subprocess.run(
        [GIT_BASH, str(PREV_RELEASE_TAG_SCRIPT), "--check-version", version],
        input="",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == expected_code, (
        f"--check-version {version!r}: expected exit {expected_code}, got "
        f"{proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


# ===========================================================================
# R2 -- preflight-src-tags.sh
# ===========================================================================

# Stub `gh` for the preflight harness. Behaviour is entirely env-var driven
# so one stub script serves every case; every invocation's raw argv is
# appended to STUB_GH_ARGV_LOG for post-hoc assertions (e.g. the --paginate
# flag check).
STUB_GH_PREFLIGHT = """#!/usr/bin/env bash
set -u
if [ -n "${STUB_GH_ARGV_LOG:-}" ]; then
  printf '%s\\n' "$*" >> "${STUB_GH_ARGV_LOG}"
fi

ALL="$*"

case "$ALL" in
  *"git/refs/heads/main"*)
    printf '%s' "${STUB_MAIN_SHA:-}"
    exit 0
    ;;
  *"git/matching-refs/tags/"*)
    if [ -n "${STUB_CANDIDATES_FILE:-}" ]; then
      cat "${STUB_CANDIDATES_FILE}"
    else
      printf '%s' "${STUB_CANDIDATES:-}"
    fi
    exit 0
    ;;
  *"git/refs/tags/src/"*)
    TAIL="${ALL##*git/refs/tags/src/}"
    TAG_PART="${TAIL%% *}"
    if [ "$TAG_PART" = "${STUB_TARGET_SRC_TAG:-__unset_target__}" ]; then
      if [ "${STUB_TARGET_SRC_EXISTS:-0}" = "1" ]; then
        exit 0
      elif [ -n "${STUB_TARGET_SRC_ERROR:-}" ]; then
        printf '%s\\n' "${STUB_TARGET_SRC_ERROR}" >&2
        exit 1
      else
        echo "gh: Not Found (HTTP 404)" >&2
        exit 1
      fi
    fi
    if [ "$TAG_PART" = "${STUB_PREV_SRC_TAG:-__unset_prev__}" ]; then
      if [ "${STUB_PREV_SRC_EXISTS:-0}" = "1" ]; then exit 0; else exit 1; fi
    fi
    echo "stub gh: unrecognized src tag check: $TAG_PART" >&2
    exit 1
    ;;
  *)
    echo "stub gh: unhandled invocation: $ALL" >&2
    exit 1
    ;;
esac
"""

# Transparent `git` passthrough -- logs argv, then execs the *real* git
# binary (resolved once at import time, before any PATH shadowing). This
# lets `preflight-src-tags.sh` run its own legitimate `git rev-parse HEAD`
# unimpeded while letting the test assert it never *also* shells out to a
# local `git ... refs/tags/...` shortcut to check tag existence (all such
# checks must go through `gh api`, matching release.yml:64's existing
# pattern and staying correct regardless of how complete the checkout's tag
# fetch happened to be).
STUB_GIT_PASSTHROUGH = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >> "${STUB_GIT_ARGV_LOG:?STUB_GIT_ARGV_LOG not set}"
exec "${STUB_GIT_REAL_PATH:?STUB_GIT_REAL_PATH not set}" "$@"
"""


@pytest.fixture()
def preflight_repo(tmp_path):
    """A throwaway git repo with one commit, so `git rev-parse HEAD` inside
    the script under test has something real to compare against."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo_dir, head_sha


def _run_preflight(
    repo_dir: Path,
    tmp_path: Path,
    tag: str,
    *,
    main_sha: str,
    repo: str = "Seretos/agent-worktree",
    github_ref: str = "refs/heads/main",
    target_src_exists: bool = False,
    prev_src_tag: str = "",
    prev_src_exists: bool = True,
    candidates: str = "",
    candidates_file: Path | None = None,
    extra_env: dict | None = None,
):
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    if not (bin_dir / "gh").exists():
        _make_executable(bin_dir / "gh", STUB_GH_PREFLIGHT)
    if not (bin_dir / "git").exists():
        _make_executable(bin_dir / "git", STUB_GIT_PASSTHROUGH)

    argv_log = tmp_path / "gh_argv.log"
    git_argv_log = tmp_path / "git_argv.log"
    output_file = tmp_path / "github_output.txt"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        {
            "REPO": repo,
            "GITHUB_REF": github_ref,
            "GITHUB_OUTPUT": str(output_file),
            "STUB_GH_ARGV_LOG": str(argv_log),
            "STUB_GIT_ARGV_LOG": str(git_argv_log),
            "STUB_GIT_REAL_PATH": GIT_REAL or "",
            "STUB_MAIN_SHA": main_sha,
            "STUB_TARGET_SRC_TAG": tag,
            "STUB_TARGET_SRC_EXISTS": "1" if target_src_exists else "0",
            "STUB_PREV_SRC_TAG": prev_src_tag or "__none__",
            "STUB_PREV_SRC_EXISTS": "1" if prev_src_exists else "0",
            "STUB_CANDIDATES": candidates,
        }
    )
    if candidates_file is not None:
        env["STUB_CANDIDATES_FILE"] = str(candidates_file)
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [GIT_BASH, str(PREFLIGHT_SCRIPT), tag],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, argv_log, git_argv_log, output_file


@requires_git_bash_and_jq
def test_preflight_fails_when_head_is_not_mains_tip(preflight_repo, tmp_path):
    """Driving test (R2 check a) -- HEAD must equal main's tip (read via
    `gh api repos/$REPO/git/refs/heads/main --jq .object.sha`), never a
    locally-cached ref.

    RED today: `.github/scripts/preflight-src-tags.sh` does not exist.
    """
    repo_dir, head_sha = preflight_repo
    other_sha = "f" * 40
    tag = "agent-worktree--v1.2.3"
    proc, *_ = _run_preflight(
        repo_dir, tmp_path, tag, main_sha=other_sha, github_ref="refs/heads/some-branch"
    )
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "Re-run release.yml from main's current tip." in combined, combined
    assert head_sha in combined, combined
    assert other_sha in combined, combined


@requires_git_bash_and_jq
def test_preflight_fails_when_src_tag_for_new_version_already_exists(preflight_repo, tmp_path):
    """Driving test (R2 check b) -- `src/<TAG>` must not already exist; the
    error must never suggest deleting or moving it (versions are immutable;
    a failed build gets a new version number).

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    proc, *_ = _run_preflight(repo_dir, tmp_path, tag, main_sha=head_sha, target_src_exists=True)
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "already exists; version" in combined and "is burned" in combined, combined
    assert "Do not delete or move the marker tag" in combined, combined
    for forbidden in ("git tag -d", "git push --delete", ":refs/tags/"):
        assert forbidden not in combined, (
            f"must never print a delete/move command for the marker tag (found {forbidden!r})"
        )


@requires_git_bash_and_jq
def test_preflight_fails_closed_when_src_tag_check_errors_non_404(preflight_repo, tmp_path):
    """Driving test (R2 check b, round-2 review fix) -- a non-404 `gh api`
    failure (rate-limit, transient 5xx, network blip) while checking whether
    `src/<TAG>` already exists must NOT be treated as "tag absent, proceed".
    Only a genuine 404 means "absent"; anything else is ambiguous and this
    pre-flight exists specifically to hard-stop before any side effect
    rather than gamble on an assumption. Mirrors the fail-closed posture
    check (c) already has for the predecessor-marker lookup.

    RED (round 2, pre-fix): check (b) only special-cased the "tag already
    exists" (`gh api` exit 0) branch and treated every other `gh api`
    outcome -- 404 or otherwise -- as "absent, proceed", so a stubbed 500
    here would incorrectly reach the `prev_tag=` success line.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    proc, *_ = _run_preflight(
        repo_dir,
        tmp_path,
        tag,
        main_sha=head_sha,
        target_src_exists=False,
        extra_env={"STUB_TARGET_SRC_ERROR": "gh: Internal Server Error (HTTP 500)"},
    )
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "HTTP 500" in combined, combined
    assert "prev_tag=" not in proc.stdout, (
        f"must not proceed past the src/<TAG> existence check on an ambiguous "
        f"gh api failure: {combined}"
    )


@requires_git_bash_and_jq
def test_preflight_fails_with_exact_bootstrap_instructions_when_predecessor_marker_missing(
    preflight_repo, tmp_path
):
    """Driving test (R2 check c) -- when a predecessor tag resolves but its
    `src/<PREV_TAG>` marker is missing, the error must print the exact
    two-space-indented bootstrap commands with the literal `<head_sha>`
    placeholder -- never a resolved SHA value.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    prev_tag = "agent-worktree--v1.2.2"
    proc, *_ = _run_preflight(
        repo_dir,
        tmp_path,
        tag,
        main_sha=head_sha,
        target_src_exists=False,
        candidates=f"refs/tags/{prev_tag}\n",
        prev_src_tag=prev_tag,
        prev_src_exists=False,
    )
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    expected_block = (
        f"::error::Missing marker tag src/{prev_tag} for the previous release {prev_tag}.\n"
        f"Read the head SHA from the Actions run that published {prev_tag} and run:\n"
        f"  git tag src/{prev_tag} <head_sha>\n"
        f"  git push origin src/{prev_tag}"
    )
    assert expected_block in combined, f"expected block not found verbatim in:\n{combined}"


@requires_git_bash_and_jq
def test_preflight_checks_main_tip_before_src_tag_existence(preflight_repo, tmp_path):
    """Driving test (R2 ordering) -- check (a) runs before check (b): a run
    that fails both must report only the main-tip error.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    proc, *_ = _run_preflight(
        repo_dir, tmp_path, tag, main_sha="f" * 40, target_src_exists=True
    )
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "Re-run release.yml from main's current tip." in combined, combined
    assert "already exists; version" not in combined, (
        f"check (a) must fail before check (b) is ever evaluated, got:\n{combined}"
    )


@requires_git_bash_and_jq
def test_preflight_checks_src_tag_existence_before_predecessor_marker(preflight_repo, tmp_path):
    """Driving test (R2 ordering) -- check (b) runs before check (c): a run
    that fails both must report only the burned-version error.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    prev_tag = "agent-worktree--v1.2.2"
    proc, *_ = _run_preflight(
        repo_dir,
        tmp_path,
        tag,
        main_sha=head_sha,
        target_src_exists=True,
        candidates=f"refs/tags/{prev_tag}\n",
        prev_src_tag=prev_tag,
        prev_src_exists=False,
    )
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "already exists; version" in combined, combined
    assert "Missing marker tag" not in combined, (
        f"check (b) must fail before check (c) is ever evaluated, got:\n{combined}"
    )


@requires_git_bash_and_jq
def test_preflight_succeeds_and_emits_prev_tag_when_marker_present(preflight_repo, tmp_path):
    """Driving test (R2 success path) -- all checks pass: exit 0, print
    `prev_tag=<PREV_TAG>` to stdout, and append it to $GITHUB_OUTPUT.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    prev_tag = "agent-worktree--v1.2.2"
    proc, _, _, output_file = _run_preflight(
        repo_dir,
        tmp_path,
        tag,
        main_sha=head_sha,
        target_src_exists=False,
        candidates=f"refs/tags/{prev_tag}\n",
        prev_src_tag=prev_tag,
        prev_src_exists=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert f"prev_tag={prev_tag}" in proc.stdout, proc.stdout
    output_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
    assert f"prev_tag={prev_tag}" in output_text, output_text


@requires_git_bash_and_jq
def test_preflight_succeeds_with_empty_prev_tag_on_first_ever_release(preflight_repo, tmp_path):
    """Driving test (R2 success path, first release) -- no predecessor tag
    at all is allowed; `prev_tag=` (empty) is still emitted, and check (c)
    never fires.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v0.0.1"
    proc, *_ = _run_preflight(
        repo_dir, tmp_path, tag, main_sha=head_sha, target_src_exists=False, candidates=""
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "prev_tag=" in proc.stdout, proc.stdout
    assert f"prev_tag={tag}" not in proc.stdout, "must never resolve the tag being created as its own predecessor"


@requires_git_bash_and_jq
def test_preflight_paginates_beyond_30_candidate_tags(preflight_repo, tmp_path):
    """Driving test (R2, forwarded plan-critic note 1) -- the predecessor
    lookup endpoint (`git/matching-refs/tags/...`) pages at 30 items by
    default; with >30 candidates the script must still resolve the true
    highest (not just whatever fits on the first page) and must pass
    `--paginate` on the `gh api` invocation.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v2.0.0"
    lines = [f"refs/tags/agent-worktree--v0.0.{n}" for n in range(1, 36)]
    lines.append("refs/tags/agent-worktree--v1.9.0")  # the true highest, appended last
    candidates_file = tmp_path / "candidates.txt"
    candidates_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    proc, argv_log, _, _ = _run_preflight(
        repo_dir,
        tmp_path,
        tag,
        main_sha=head_sha,
        target_src_exists=False,
        candidates_file=candidates_file,
        prev_src_tag="agent-worktree--v1.9.0",
        prev_src_exists=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "prev_tag=agent-worktree--v1.9.0" in proc.stdout, proc.stdout

    log_text = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    assert "matching-refs/tags" in log_text, log_text
    assert "--paginate" in log_text, (
        f"the matching-refs lookup must pass --paginate (that endpoint pages at "
        f"30 items by default) so a >30-candidate list still resolves correctly, "
        f"recorded gh invocations:\n{log_text}"
    )


@requires_git_bash_and_jq
def test_preflight_never_shortcuts_tag_existence_via_local_git_refs(preflight_repo, tmp_path):
    """Driving test (R2) -- every existence check must go through `gh api`,
    matching the existing check at release.yml:64 and never depending on how
    complete the checkout's own tag fetch happened to be. Asserted by
    recording every local `git` invocation and confirming none of them
    touch `refs/tags`.

    RED today: script does not exist.
    """
    repo_dir, head_sha = preflight_repo
    tag = "agent-worktree--v1.2.3"
    proc, _, git_argv_log, _ = _run_preflight(
        repo_dir, tmp_path, tag, main_sha=head_sha, target_src_exists=False, candidates=""
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    git_log_text = git_argv_log.read_text(encoding="utf-8") if git_argv_log.exists() else ""
    for line in git_log_text.splitlines():
        assert "refs/tags" not in line, (
            f"preflight must resolve tag existence entirely through `gh api`, never a "
            f"local `git ... refs/tags/...` shortcut -- found: {line!r}"
        )


# ===========================================================================
# R3 -- marketplace-payload.sh
# ===========================================================================

STUB_GH_RELEASE_VIEW = """#!/usr/bin/env bash
# Stub `gh` for marketplace-payload.sh tests -- handles only
# `gh release view "$TAG" --repo "$REPO" --json body --jq .body`.
set -u
if [ "${1:-}" = "release" ] && [ "${2:-}" = "view" ]; then
  if [ "${STUB_GH_FAIL:-0}" = "1" ]; then
    echo "stub gh: simulated release view failure" >&2
    exit 1
  fi
  if [ -n "${STUB_GH_BODY_FILE:-}" ]; then
    cat "${STUB_GH_BODY_FILE}"
  else
    printf '%s' "${STUB_GH_BODY:-}"
  fi
  exit 0
fi
echo "stub gh: unhandled invocation: $*" >&2
exit 1
"""


@pytest.fixture()
def payload_harness(tmp_path):
    plugin_json = tmp_path / "plugin.json"
    plugin_json.write_text(
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
    _make_executable(bin_dir / "gh", STUB_GH_RELEASE_VIEW)

    def run(
        *,
        gh_fail=False,
        gh_body=None,
        gh_body_file=None,
        max_len="30000",
        plugin_json_path=None,
        extra_env=None,
        remove_env=None,
    ):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            {
                "GH_TOKEN": "stub-token",
                "REPO": "Seretos/agent-worktree",
                "TAG": "agent-worktree--v1.2.3",
                "VERSION": "1.2.3",
                "PLUGIN_JSON": str(plugin_json_path or plugin_json),
                "MAX_CHANGELOG_LEN": max_len,
                "STUB_GH_FAIL": "1" if gh_fail else "0",
            }
        )
        if gh_body is not None:
            env["STUB_GH_BODY"] = gh_body
        if gh_body_file is not None:
            env["STUB_GH_BODY_FILE"] = str(gh_body_file)
        if extra_env:
            env.update(extra_env)
        if remove_env:
            for key in remove_env:
                env.pop(key, None)

        proc = subprocess.run(
            [GIT_BASH, str(MARKETPLACE_PAYLOAD_SCRIPT)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc

    return run


@requires_git_bash_and_jq
def test_marketplace_payload_hostile_changelog_round_trips_byte_for_byte(payload_harness):
    """Driving test (R3) -- every value must be JSON-escaped via jq, never
    raw-spliced; a hostile body with quotes/backticks/$()/newlines must
    round-trip exactly.

    RED today: `.github/scripts/marketplace-payload.sh` does not exist.
    """
    hostile = 'Notes with "quotes", `backticks`, $(rm -rf /tmp/should-not-run), and\nreal\nnewlines.'
    proc = payload_harness(gh_body=hostile)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["client_payload"]["changelog"] == hostile


@requires_git_bash_and_jq
def test_marketplace_payload_source_is_release_view_never_generate_notes(payload_harness):
    """Driving test (R3) -- the changelog must come from the *published*
    release body (`gh release view ... --json body`), never a fresh
    `releases/generate-notes` call against the orphan tag (which reproduces
    the empty-notes bug this ticket fixes).

    RED today: script does not exist, so this is really asserting the
    absence of the bug, which cannot be demonstrated without the script.
    """
    body = "Real published notes."
    proc = payload_harness(gh_body=body)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["client_payload"]["changelog"] == body
    script_text = MARKETPLACE_PAYLOAD_SCRIPT.read_text(encoding="utf-8") if MARKETPLACE_PAYLOAD_SCRIPT.exists() else ""
    assert "generate-notes" not in script_text, (
        "marketplace-payload.sh must never call releases/generate-notes -- the "
        "orphan tag has no merge-base, so that call reproduces the empty-notes bug"
    )


@requires_git_bash_and_jq
def test_marketplace_payload_gh_release_view_failure_is_fatal(payload_harness):
    """Driving test (R3) -- a failing `gh release view` must abort the
    script non-zero with no JSON on stdout (fatal, unlike the
    empty/whitespace/null cases below, which warn and continue).

    RED today: script does not exist.
    """
    proc = payload_harness(gh_fail=True)
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    with pytest.raises(json.JSONDecodeError):
        json.loads(proc.stdout)


@requires_git_bash_and_jq
@pytest.mark.parametrize(
    "case_name,gh_body",
    [
        ("empty_body", ""),
        ("whitespace_only_body", "   \n\t  "),
        ("literal_null_body", "null"),
    ],
)
def test_marketplace_payload_empty_body_warns_and_omits_key(payload_harness, case_name, gh_body):
    """Driving test (R3) -- an empty/whitespace/literal-"null" body must
    warn and omit the `changelog` key, exit 0 (never abort the dispatch).

    RED today: script does not exist.
    """
    proc = payload_harness(gh_body=gh_body)
    assert proc.returncode == 0, f"[{case_name}] stdout={proc.stdout}\nstderr={proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "::warning::" in combined, f"[{case_name}] {combined}"
    payload = json.loads(proc.stdout)
    assert "changelog" not in payload["client_payload"], f"[{case_name}] {payload}"


@requires_git_bash_and_jq
def test_marketplace_payload_truncates_above_30000_chars(payload_harness, tmp_path):
    """Driving test (R3) -- an oversized body is truncated to the first
    MAX_CHANGELOG_LEN chars plus a truncation suffix naming the release URL
    (preserving today's release.yml/dispatch.yml semantics exactly).

    RED today: script does not exist.
    """
    big_body_file = tmp_path / "big_body.txt"
    big_body_file.write_text("x" * 80000, encoding="utf-8")
    proc = payload_harness(gh_body_file=big_body_file)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    changelog = payload["client_payload"]["changelog"]
    assert changelog.startswith("x" * 30000)
    assert len(changelog) > 30000
    assert "truncated" in changelog.lower()
    expected_url = "https://github.com/Seretos/agent-worktree/releases/tag/agent-worktree--v1.2.3"
    assert expected_url in changelog, changelog[-200:]


@requires_git_bash_and_jq
def test_marketplace_payload_oversized_multibyte_changelog_stays_valid_utf8_json(payload_harness, tmp_path):
    """Additional coverage (R3 edge case) -- truncation must be Unicode-
    codepoint-safe: an oversized multi-byte body must still produce valid
    UTF-8/JSON output, not a body cut mid-codepoint.

    Migrated from tests/test_release_dispatch_payload.py's retired
    `test_oversized_multibyte_changelog_stays_valid_utf8_json` (ticket #184
    moved the payload-building logic that test drove into
    marketplace-payload.sh; this is the same case, adapted to the new
    script-level harness, closing the one coverage gap that retirement
    would otherwise have left).
    """
    multibyte_body_file = tmp_path / "multibyte_body.txt"
    multibyte_body_file.write_text("é中\U0001f600" * 20000, encoding="utf-8")

    proc = payload_harness(gh_body_file=multibyte_body_file)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    # json.loads itself proves the bytes are valid UTF-8/JSON; a mid-codepoint
    # cut would raise UnicodeDecodeError or json.JSONDecodeError here.
    payload = json.loads(proc.stdout)
    changelog = payload["client_payload"]["changelog"]
    assert len(changelog) >= 30000
    assert "truncated" in changelog.lower()


@requires_git_bash_and_jq
def test_marketplace_payload_boundary_exact_length_not_truncated(payload_harness, tmp_path):
    """Additional coverage (R3 boundary) -- a body of exactly 30000 chars
    passes through untouched."""
    boundary_body_file = tmp_path / "boundary_body.txt"
    boundary_body_file.write_text("y" * 30000, encoding="utf-8")
    proc = payload_harness(gh_body_file=boundary_body_file)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["client_payload"]["changelog"] == "y" * 30000


@requires_git_bash_and_jq
def test_marketplace_payload_honors_custom_max_changelog_len(payload_harness):
    """Driving test (R3) -- MAX_CHANGELOG_LEN must be read from the
    environment, not hardcoded: a small custom threshold truncates a short
    body that the default 30000 would not.

    RED today: script does not exist.
    """
    proc = payload_harness(gh_body="0123456789ABCDEF", max_len="10")
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    changelog = payload["client_payload"]["changelog"]
    assert changelog.startswith("0123456789")
    assert len(changelog) > 10
    assert "truncated" in changelog.lower()


@requires_git_bash_and_jq
def test_marketplace_payload_strips_exactly_one_trailing_newline(payload_harness):
    """Driving test (R3, spec Amendment §5) -- exactly one trailing newline
    (jq's own) is stripped, never more.

    RED today: script does not exist.
    """
    proc = payload_harness(gh_body="line1\nline2\n\n")
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["client_payload"]["changelog"] == "line1\nline2\n"


@requires_git_bash_and_jq
def test_marketplace_payload_client_payload_has_at_most_10_keys(payload_harness):
    """Additional coverage (R3) -- GitHub's repository_dispatch max."""
    proc = payload_harness(gh_body="notes")
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    client_payload = payload["client_payload"]
    assert len(client_payload) <= 10, sorted(client_payload)


@requires_git_bash_and_jq
def test_marketplace_payload_fields_sourced_from_plugin_json_and_env(payload_harness, tmp_path):
    """Driving test (R3) -- name/description come from $PLUGIN_JSON; repo,
    version, ref come from $REPO/$VERSION/$TAG.

    RED today: script does not exist.
    """
    custom_plugin_json = tmp_path / "custom-plugin.json"
    custom_plugin_json.write_text(
        json.dumps({"name": "custom-plugin", "description": "Custom desc.", "version": "0.0.0"}),
        encoding="utf-8",
    )
    proc = payload_harness(gh_body="notes", plugin_json_path=custom_plugin_json)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    client_payload = payload["client_payload"]
    assert client_payload["name"] == "custom-plugin"
    assert client_payload["description"] == "Custom desc."
    assert client_payload["repo"] == "Seretos/agent-worktree"
    assert client_payload["version"] == "1.2.3"
    assert client_payload["ref"] == "agent-worktree--v1.2.3"


@requires_git_bash_and_jq
@pytest.mark.parametrize("missing_var", ["REPO", "TAG", "VERSION", "PLUGIN_JSON", "MAX_CHANGELOG_LEN"])
def test_marketplace_payload_fails_loudly_when_required_env_var_missing(payload_harness, missing_var):
    """Driving test (R3, forwarded plan-critic note 3) -- all 5 required env
    vars (REPO/TAG/VERSION/PLUGIN_JSON/MAX_CHANGELOG_LEN -- GH_TOKEN is
    consumed by the `gh` binary itself, not read as a script variable) must
    matter: today's `set -euo pipefail` in the equivalent inline code means
    an unset var used unguarded aborts the script (bash's `set -u`) rather
    than silently proceeding with an empty value.

    RED today: script does not exist (exit 127 either way, so this can't
    yet distinguish "fails loudly for the right reason" from "fails because
    it's missing" -- disclosed honestly; it will discriminate correctly once
    the script exists).
    """
    proc = payload_harness(gh_body="notes", remove_env=[missing_var])
    assert proc.returncode != 0, (
        f"missing {missing_var} should cause a loud failure, got exit 0\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
