"""Tests for the plugin manifest's skill registration (ticket #91).

Verifies that ``.claude-plugin/plugin.json`` registers the new ``skills/worktree``
skill directory, and that the skill file itself is well-formed and documents the
worktree contract and its troubleshooting recipes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILL_MD = REPO_ROOT / "skills" / "worktree" / "SKILL.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
WORKTREE_PY = REPO_ROOT / "src" / "worktree_plugin" / "tools" / "worktree.py"
README_MD = REPO_ROOT / "README.md"
SPEC_FILE = REPO_ROOT / "worktree.spec"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _read_frontmatter_and_body(text: str) -> tuple[dict, str]:
    """Split a ``---``-fenced YAML frontmatter block from the Markdown body."""
    assert text.startswith("---"), "SKILL.md must start with a YAML frontmatter fence"
    _, frontmatter_raw, body = text.split("---", 2)
    frontmatter = yaml.safe_load(frontmatter_raw)
    return frontmatter, body


def test_plugin_json_registers_skills_dir():
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["skills"] == "./skills"
    assert SKILL_MD.exists()


def test_skill_frontmatter_wellformed():
    text = SKILL_MD.read_text(encoding="utf-8")
    frontmatter, _ = _read_frontmatter_and_body(text)
    assert isinstance(frontmatter, dict)
    assert frontmatter.get("name")
    assert frontmatter.get("description")


def test_skill_documents_contract_and_recipes():
    text = SKILL_MD.read_text(encoding="utf-8")
    load_bearing_tokens = [
        ".seretos/worktree-setup.yml",
        "isolation: none",
        "isolation: full",
        "setup:",
        "start:",
        "stop:",
        "kill_blocking_processes",
        "force=true",
    ]
    for token in load_bearing_tokens:
        assert token in text, f"SKILL.md is missing expected token: {token!r}"


# ---- Ticket #99: MCP tool-surface split (worktree_* -> checkout/environment) ----


def test_skill_and_agents_teach_two_lifecycle_split():
    """SKILL.md and AGENTS.md must both teach the ticket #99 two-lifecycle
    split (checkout vs. environment tools), including the checkout_path
    addressing deviation, and must contain no trace of the six removed
    tool names/params they replace."""
    required_tokens = [
        "environment_list",
        "environment_start",
        "environment_stop",
        "environment_id",
        "checkout_path",
    ]
    # Any of these phrases is sufficient evidence of a "primary/main clone"
    # mention -- the split's key conceptual addition.
    primary_mentions = ["primary checkout", "primary/main clone", "main clone"]
    forbidden_tokens = [
        "worktree_list",
        "worktree_get",
        "worktree_start",
        "worktree_stop",
        "worktree_id",
    ]

    for path in (SKILL_MD, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        for token in required_tokens:
            assert token in text, f"{path.name} is missing expected token: {token!r}"
        assert any(phrase in text for phrase in primary_mentions), (
            f"{path.name} does not mention the primary/main clone"
        )
        for token in forbidden_tokens:
            assert token not in text, (
                f"{path.name} still references removed token: {token!r}"
            )


# ---- Ticket #113: untracked orphan recovery doc contract ----


def _windows_after(text: str, token: str, window: int = 600) -> list[str]:
    """Return the ``window``-char slice following each occurrence of
    ``token`` in ``text`` -- used to check that a follow-up token (e.g.
    ``checkout_path``) appears near a given mention, without depending on
    exact heading/section structure."""
    return [
        text[match.start() : match.start() + window]
        for match in re.finditer(re.escape(token), text)
    ]


def test_docs_state_untracked_orphan_recovery():
    """AGENTS.md and SKILL.md must document the true untracked-linked-
    worktree id contract (a synthesised ``-untracked-<8hex>`` id, addressed
    for removal via ``checkout_path``) and must no longer claim such an
    id is the empty string ``""`` (the stale claim ticket #113 fixes)."""
    stale_claims = ['is `""`', 'id is ""', "empty string"]

    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")

        assert "-untracked-" in text, (
            f"{path.name} does not document the synthesised -untracked-<8hex> id"
        )

        windows = _windows_after(text, "worktree_remove")
        assert any("checkout_path" in w for w in windows), (
            f"{path.name} does not mention checkout_path near worktree_remove"
        )

        for stale in stale_claims:
            assert stale not in text, (
                f"{path.name} still contains the stale untracked-id claim: {stale!r}"
            )


def test_source_docstrings_state_untracked_orphan_recovery():
    """``worktree.py``'s ``_entry_to_dict`` and ``environment_list``
    docstrings must document the true synthesised-linked-worktree id shape
    (``<checkout-dirname-slug>-untracked-<8-hex>`` -- the checkout
    directory's own basename, slugged, plus an 8-hex SHA-256 hash of its
    resolved path, via ``untracked_id_for()``) and must no longer claim
    such an id is the empty string ``""`` (the stale claim ticket #113's
    review fix removes from the source, not just the docs)."""
    text = WORKTREE_PY.read_text(encoding="utf-8")

    stale_claims = ['id == ""', "is the empty string"]
    for stale in stale_claims:
        assert stale not in text, (
            f"worktree.py still contains the stale untracked-id claim: {stale!r}"
        )

    assert text.count("-untracked-<8-hex>") >= 2, (
        "worktree.py's _entry_to_dict and environment_list docstrings must "
        "both state the true synthesised id shape "
        "<checkout-dirname-slug>-untracked-<8-hex>"
    )
    assert text.count("untracked_id_for") >= 2, (
        "worktree.py's _entry_to_dict and environment_list docstrings must "
        "both attribute the synthesised id to untracked_id_for()"
    )


def test_docs_state_base_default_behaviour():
    """AGENTS.md and SKILL.md must document that omitting `base` for a
    not-yet-existing branch defaults to the branch currently checked out at
    repo_root, and the detached/unborn-HEAD exception -- and AGENTS.md must
    no longer carry the stale phrasing that implied base was mandatory for
    a new branch (ticket #110, Befund 1)."""
    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        text_lower = text.lower()

        assert (
            "currently checked out" in text_lower
            or "currently checked-out" in text_lower
        ), f"{path.name} does not document the base default-to-checked-out-branch behaviour"

        assert "detached" in text_lower and "unborn" in text_lower, (
            f"{path.name} does not document the detached/unborn HEAD exception "
            "to the base default"
        )

    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    assert "Omit when `branch` already exists." not in agents_text, (
        "AGENTS.md still carries the stale base-row phrasing that implied "
        "base was mandatory for a new branch"
    )


# ---- Ticket #112: soft-error `code` values + Windows signal-handling docs ----


def test_docs_document_soft_error_codes_and_signal_guard():
    """AGENTS.md must document the 3 machine-readable soft-error ``code``
    values (``not_found``/``already_running``/``not_running``) and a new
    "Signal handling on Windows" note covering the SIGBREAK guard, the
    deliberate non-change to SIGINT, and the upstream engine hazard
    (recommended, not implemented here). SKILL.md must mirror the 3 code
    values in its existing soft-error prose.

    RED (pre-fix): none of these tokens exist in either doc yet.
    """
    code_values = ["not_found", "already_running", "not_running"]

    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        for code in code_values:
            assert code in text, (
                f"{path.name} is missing the soft-error code value {code!r}"
            )

    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    assert "SIGBREAK" in agents_text or "Ctrl+Break" in agents_text, (
        "AGENTS.md is missing a Ctrl+Break/SIGBREAK signal-handling note"
    )
    assert "SIGINT" in agents_text, (
        "AGENTS.md does not mention SIGINT's deliberately unchanged disposition"
    )
    agents_lower = agents_text.lower()
    assert "unchanged" in agents_lower or "untouched" in agents_lower, (
        "AGENTS.md does not state that SIGINT's disposition is deliberately "
        "left unchanged/untouched"
    )


# ---- Ticket #117: setup_status derived from setup_outcome, not status ----


def test_docs_document_setup_status_derived_from_setup_outcome():
    """AGENTS.md and SKILL.md must document the ticket #117 full decoupling
    of ``setup_status`` from ``setup_outcome`` (never from ``status``, the
    overall run status) and its real vocabulary (``"completed"`` /
    ``"failed"`` / ``"skipped"`` / ``"unknown"``), and must no longer carry
    the stale claim that it is "derived from status" / "derived from
    `status`" (the pre-fix behaviour ticket #117 reports as a bug)."""
    required_tokens = ["setup_outcome", "completed", "failed", "skipped", "unknown"]
    stale_claims = ["derived from status", "derived from `status`"]

    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        for token in required_tokens:
            assert token in text, (
                f"{path.name} is missing expected setup_status vocabulary "
                f"token: {token!r}"
            )
        for stale in stale_claims:
            assert stale not in text, (
                f"{path.name} still contains the stale setup_status claim: "
                f"{stale!r}"
            )


# ---- Ticket #127 ----


def _normalize(text: str) -> str:
    """Strip Markdown emphasis/code markup and collapse whitespace, so
    prose assertions match semantic tokens via regex alternation rather
    than depending on exact wording/formatting surviving a future edit."""
    stripped = text.replace("``", "").replace("`", "").replace("**", "")
    return re.sub(r"\s+", " ", stripped).lower()


def test_docs_document_lone_start_step_default_variant_fallback():
    """Claims under protection:

    1. AGENTS.md and SKILL.md must teach the corrected v0.3.5 / upstream
       lib-python-worktree#112 semantics -- the `variant="default"`
       lone-step fallback fires for a single `start:` step regardless of
       whether it is named or unnamed, not only for a lone *unnamed* step
       (the stale claim both docs previously carried).
    2. Both docs must also document the ticket #139 Part B fix: when the
       fallback resolves a NAMED step, the *engine* records that step's own
       name in `record.variants[role]`, never the literal `"default"` --
       but `environment_stop(variant="default")` afterwards DOES resolve
       against that role anyway, because the wrapper pre-resolves a bare
       `"default"` to the contract's lone named step before ever calling
       the engine. The stale "will not resolve" claim must be gone from
       both docs.
    3. SKILL.md's `## Pitfalls` section specifically (not just its earlier
       contract prose) must gain an entry: a multi-step contract with no
       step named `default` makes the *first* `environment_start` call
       fail unless `variant=` is passed explicitly.
    """
    fallback_pattern = re.compile(
        r"\b(single|lone|exactly one)\b[^.]{0,160}"
        r"\b(regardless|even if|whether it is named|named or unnamed)\b"
    )
    # Ticket #139: the corrected claim is now an AFFIRMATIVE resolve claim
    # (environment_stop(variant="default") DOES resolve), not the old
    # negative one -- see stale_asymmetry_pattern below, asserted absent.
    symmetry_pattern = re.compile(r"\b(does|will|can)\b[^.]{0,120}resolv")
    stale_asymmetry_pattern = re.compile(
        r"\b(will not|does not|won't|cannot|never)\b[^.]{0,120}resolv"
    )
    stale_claims = {
        AGENTS_MD: "resolves to the lone unnamed step for back-compat",
        SKILL_MD: "a single unnamed step is the implicit",
    }

    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        assert fallback_pattern.search(norm), (
            f"{path.name} must document that the default variant fallback "
            "covers a lone NAMED step too, not only a lone unnamed one"
        )
        stale = stale_claims[path]
        assert stale not in norm, (
            f"{path.name} still contains the stale claim: {stale!r}"
        )

        found_symmetry = False
        found_stale_asymmetry = False
        for m in re.finditer(r"default", norm):
            idx = m.start()
            window = norm[max(0, idx - 500) : idx + 500]
            if "variants" not in window:
                continue
            if symmetry_pattern.search(window):
                found_symmetry = True
            if stale_asymmetry_pattern.search(window):
                found_stale_asymmetry = True
        assert found_symmetry, (
            f'{path.name} must document that environment_stop(variant='
            '"default") DOES resolve when the lone-step fallback resolved '
            "a named step (ticket #139 Part B)"
        )
        # Negative assertion: a reworded-but-still-stale paragraph could in
        # principle satisfy found_symmetry above via some other affirmative
        # sentence while the old negative claim survives untouched
        # elsewhere nearby -- guard against that explicitly so a stale copy
        # in either file can't slip through.
        assert not found_stale_asymmetry, (
            f"{path.name} still documents the stale asymmetry claim "
            '(environment_stop(variant="default") will not resolve) near a '
            '"default"/"variants" mention -- this must be fully replaced, '
            "not merely supplemented"
        )

    skill_text = SKILL_MD.read_text(encoding="utf-8")
    pitfalls_idx = skill_text.find("## Pitfalls")
    assert pitfalls_idx != -1, "SKILL.md must have a '## Pitfalls' heading"
    pitfalls_section = _normalize(skill_text[pitfalls_idx:])
    assert "default" in pitfalls_section and "variant=" in pitfalls_section, (
        "SKILL.md's Pitfalls section must gain an entry about a multi-step "
        "contract with no step named default failing the first "
        "environment_start call unless variant= is passed"
    )


# ---- Ticket #130: docstring / SKILL / README sweep ----
#
# Four re-sliced findings originally filed as #124 (misplaced-contract
# CAUTION is a silent no-op), #125 (environment_stop primary-vs-linked /
# environment_start's three addressing outcomes), #126 (kill_blocking_
# processes tracked vs foreign), #128 (start_log_path role-casing
# mismatch). This file covers the Markdown-doc side (SKILL.md/AGENTS.md/
# README.md) plus the shadowed_contract cross-file claim; docstring-only
# claims live in tests/test_environment_tools.py and
# tests/test_worktree_tools.py.


def test_docs_no_longer_claim_misplaced_contract_is_silent():
    """Claim under protection (ticket #130, re-slicing #124): SKILL.md's
    "Critical:" contract block and its Pitfalls section must both describe
    the misplaced-contract case as diagnosable (no_op_reason:
    "contract_misplaced"), not silent/indistinguishable-from-unconfigured."""
    text = SKILL_MD.read_text(encoding="utf-8")
    norm = _normalize(text)

    assert "no_op_reason" in norm
    assert "contract_misplaced" in norm
    # WP #162 test-critic round-1 note 4: pin presence of the remaining
    # underscore literal SKILL.md must also carry (the "vs" contrast in the
    # same sentence, "no_contract" for the genuinely-unconfigured case), so
    # deleting that mention cannot pass this test as a mere rename of
    # "contract_misplaced".
    assert "no_contract" in norm
    assert "with no error" not in norm

    found_silent_far_from_diagnosis = False
    for m in re.finditer(r"(?<!not )\bsilent\b", norm):
        idx = m.start()
        window = norm[max(0, idx - 300) : idx + 300]
        if "contract_misplaced" in window or "misplacement" in window:
            found_silent_far_from_diagnosis = True
    assert not found_silent_far_from_diagnosis, (
        "SKILL.md must not describe the misplaced-contract case as "
        "(unqualified) 'silent' near its diagnosis -- it is diagnosable "
        "via no_op_reason; 'not silent' is fine"
    )


def test_docs_document_engine_shadowed_contract_diagnostic():
    """Claim under protection (ticket #130, requirement 1b): worktree.py,
    SKILL.md, and AGENTS.md must each document the engine-owned
    shadowed_contract diagnostic (lib-python-worktree, upstream #100),
    including both reason values and its non-persistence, and must
    attribute it to the engine layer rather than to this wrapper's own
    ticket-#103 diagnostics."""
    for path in (WORKTREE_PY, SKILL_MD, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"shadowed_contract", norm))
        assert occurrences, f"{path.name} must document shadowed_contract"

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 900) : idx + 900]

            if "used_path" not in window:
                continue
            if "differs" not in window:
                continue
            if "unreadable" not in window:
                continue
            if "lib-python-worktree" not in window and "engine" not in window:
                continue
            if path in (WORKTREE_PY, SKILL_MD) and not (
                "state.yaml" in window
                or "not persisted" in window
                or "transient" in window
            ):
                continue
            found = True
            break

        assert found, (
            f"{path.name} must have at least one shadowed_contract mention "
            "whose surrounding window documents used_path, both reason "
            "values (differs/unreadable), engine attribution, and (for "
            "worktree.py/SKILL.md) non-persistence to state.yaml"
        )


# Matches the standalone word "tracked", but not as the tail of "untracked"
# (e.g. "uncommitted/untracked changes") -- a negative lookbehind excludes
# the "un" prefix so an unrelated "untracked" mention can't satisfy this.
_TRACKED_WORD_RE = re.compile(r"(?<!un)tracked\b")


def test_docs_document_tracked_vs_foreign_blocking_processes():
    """Claim under protection (ticket #130, re-slicing #126): SKILL.md,
    AGENTS.md, and README.md must each document that kill_blocking_processes
    is for foreign holders, not a process started via environment_start,
    which removal stops first as a tracked role."""
    for path in (SKILL_MD, AGENTS_MD, README_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"kill_blocking_processes", norm))
        assert occurrences, f"{path.name} must mention kill_blocking_processes"

        # A coincidental, unrelated "tracked" mention (e.g. an "environment_list"
        # entry's "tracked: false" field, discussed several paragraphs away in
        # an orphan-worktree recipe) can fall inside a generously-sized window
        # without actually being part of the same sentence/paragraph explaining
        # the tracked-vs-foreign distinction. Requiring the standalone "tracked"
        # word and "environment_start" to additionally sit close to each other
        # (same sentence/paragraph, not just the same wide window) rules that
        # out.
        found = False
        for m in occurrences:
            idx = m.start()
            window_start = max(0, idx - 1200)
            window = norm[window_start : idx + 1200]
            tracked_positions = [
                mm.start() + window_start for mm in _TRACKED_WORD_RE.finditer(window)
            ]
            start_positions = [
                mm.start() + window_start
                for mm in re.finditer(r"environment_start", window)
            ]
            if any(
                abs(t - s) <= 400 for t in tracked_positions for s in start_positions
            ):
                found = True
                break

        assert found, (
            f"{path.name} must have at least one kill_blocking_processes "
            "mention whose surrounding window documents the tracked-vs-"
            "foreign distinction (the standalone word 'tracked', not merely "
            "'untracked') and references environment_start"
        )


def test_docs_document_start_log_path_role_casing():
    """Claim under protection (ticket #146, correcting #130/#128): SKILL.md and
    AGENTS.md must document that start_log_path's filename is a
    *case-preserving* slug of role (never lower-cased) while pids/
    record.variants key on role verbatim, citing the fully-qualified
    upstream reference. v0.3.7 fixed the lower-casing bug #111 originally
    reported; the residual caveat is case-insensitive-filesystem
    interleaving, not lower-casing."""
    for path in (SKILL_MD, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"start_log_path", norm))
        assert occurrences, f"{path.name} must mention start_log_path"

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 500) : idx + 900]
            if (
                "seretos/lib-python-worktree#111" in window
                and ("lower" in window or "slug" in window)
                and "preserv" in window
                and "pids" in window
            ):
                found = True
                break

        assert found, (
            f"{path.name} must have at least one start_log_path mention "
            "whose surrounding window fully-qualifies the upstream #111 "
            "reference, names the case-preserving slug behaviour, and "
            "mentions pids"
        )


def test_upstream_issue_111_reference_is_fully_qualified():
    """Guard (ticket #130): every occurrence of '#111' in the four edited
    doc/docstring files must be immediately preceded by
    'Seretos/lib-python-worktree', so it is never confused with this repo's
    own closed #111 (a thread-leak ticket cited by
    tests/test_thread_leak_regression.py, deliberately excluded here)."""
    bare_111 = re.compile(r"(?<!Seretos/lib-python-worktree)#111")
    for path in (WORKTREE_PY, SKILL_MD, AGENTS_MD, README_MD):
        text = path.read_text(encoding="utf-8")
        match = bare_111.search(text)
        assert match is None, (
            f"{path.name} contains a bare '#111' not qualified with "
            f"'Seretos/lib-python-worktree' near: "
            f"{text[max(0, match.start() - 40) if match else 0:(match.end() + 40) if match else 0]!r}"
        )


# ---- Ticket #146, Topic 1: contract copy (committed/live) vs live filesystem read ----


def test_docs_document_contract_copy_vs_live_read():
    """Claim under protection (ticket #146, Topic 1): worktree.py and
    SKILL.md must document that environment_start/environment_stop always
    read the contract live from repo_root on every call, that the
    checkout-local .seretos/ copy is written once at create time and never
    re-read, and that a shadowed_contract reason of "differs" is the
    expected, diagnosed signal for that divergence (not a malfunction)."""
    for path in (WORKTREE_PY, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"shadowed_contract|\.seretos", norm))
        assert occurrences, f"{path.name} must mention shadowed_contract/.seretos"

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 900) : idx + 900]
            if (
                "live" in window
                and "on every" in window
                and ("create-time" in window or "create time" in window)
                and "repo_root" in window
                and "differs" in window
            ):
                found = True
                break

        assert found, (
            f"{path.name} must have at least one shadowed_contract/.seretos "
            "mention whose surrounding window documents the 'live ... on "
            "every' call read, the create-time copy, repo_root, and the "
            "'differs' signal"
        )


# ---- Ticket #146, Topic 2: default per-OS step shell ----


def test_docs_document_default_step_shell_per_os():
    """Claim under protection (ticket #146, Topic 2): worktree.py and
    SKILL.md must document the per-OS default shell used for a step that
    omits shell: (powershell.exe on Windows, bash on POSIX), the accepted
    override set, and the &&-under-PowerShell portability warning."""
    for path in (WORKTREE_PY, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"shell:", norm))
        assert occurrences, f"{path.name} must mention 'shell:'"

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 200) : idx + 1200]
            if (
                "powershell" in window
                and "bash" in window
                and "windows" in window
                and ("posix" in window or "linux" in window or "macos" in window)
            ):
                found = True
                break

        assert found, (
            f"{path.name} must have at least one 'shell:' mention whose "
            "surrounding window names both the powershell and bash defaults "
            "and both platform words (windows + posix/linux/macos)"
        )

        assert "&&" in text, (
            f"{path.name} must warn that '&&' does not parse under the "
            "Windows default powershell.exe"
        )
        for accepted in ("bash", "sh", "pwsh", "powershell"):
            assert accepted in norm, (
                f"{path.name} must list {accepted!r} as an accepted shell: override"
            )


# ---- Ticket #146, Topic 3: kill_orphans vs. unconditional Job Object kill ----


def test_docs_document_kill_orphans_vs_job_object():
    """Claim under protection (ticket #146, Topic 3): worktree.py, SKILL.md
    and AGENTS.md must document that the tree/Job Object kill is
    unconditional and that kill_orphans is a separate path-scoped scan, not
    a deeper containment mechanism."""
    for path in (WORKTREE_PY, SKILL_MD, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"kill_orphans", norm))
        assert occurrences, f"{path.name} must mention kill_orphans"

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 200) : idx + 1400]
            if (
                "job object" in window
                and "unconditional" in window
                and ("path-scoped" in window or "path scoped" in window)
            ):
                found = True
                break

        assert found, (
            f"{path.name} must have at least one kill_orphans mention whose "
            "surrounding window documents the unconditional job-object kill "
            "and the path-scoped nature of kill_orphans"
        )

    worktree_text = _normalize(WORKTREE_PY.read_text(encoding="utf-8"))
    assert "job_member_list_truncated" in worktree_text, (
        "worktree.py must document that kill_orphans does not help a "
        "job_member_list_truncated stop_incomplete outcome"
    )
    assert "kill_orphans_may_help" in worktree_text, (
        "worktree.py must document the stop_detail.kill_orphans_may_help "
        "gating hint"
    )


# ---- Ticket #116: transport-level "Connection closed" read-back docs ----


def test_transport_failure_readback_documented_in_markdown():
    """Claim under protection (ticket #116): SKILL.md and README.md must
    each document the "Connection closed" transport-drop read-back recipe --
    mentioning environment_list as the read-back tool, the
    existing_environment_id token a lost worktree_create retry surfaces, and
    that retrying by environment_id (not checkout_path) is the safe way to
    retry a worktree_remove. AGENTS.md must additionally state, using a
    unique case-sensitive token, that the first-environment_start
    sub-symptom is NOT explained by #112's SIGBREAK fix.

    RED (pre-fix): none of these tokens/mentions exist in any of the three
    docs yet -- 'Connection closed' appears nowhere outside AGENTS.md's
    pre-existing #112 signal-handling paragraph."""
    for path in (SKILL_MD, README_MD):
        text = path.read_text(encoding="utf-8")
        assert "Connection closed" in text, (
            f"{path.name} must mention the literal transport-drop error "
            f"text 'Connection closed'"
        )
        assert "environment_list" in text
        assert "existing_environment_id" in text

        norm = _normalize(text)
        assert re.search(r"environment_id", norm) and re.search(
            r"not.{0,40}checkout_path|checkout_path.{0,40}not", norm
        ), (
            f"{path.name} must state that retrying worktree_remove is safer "
            f"by environment_id than by checkout_path"
        )

    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    assert "Connection closed" in agents_text
    assert "environment_list" in agents_text
    assert "NOT explained by #112" in agents_text, (
        "AGENTS.md must state, using the exact case-sensitive token "
        "'NOT explained by #112', that the first-environment_start "
        "sub-symptom is not accounted for by the #112 SIGBREAK fix"
    )
    assert agents_text.count("NOT explained by #112") == 1, (
        "the 'NOT explained by #112' token must be unique in AGENTS.md"
    )


def test_pyinstaller_lead_is_labelled_unverified_and_contained():
    """Claim under protection (ticket #116): the PyInstaller
    bootloader_ignore_signals lead must be recorded in AGENTS.md as an
    explicitly-labelled UNVERIFIED lead (never acted on), must state that
    the pytest suite cannot verify it, must NOT appear in README.md or
    SKILL.md, and worktree.spec must still carry the literal
    'bootloader_ignore_signals=False' unchanged -- pinning that the lead was
    recorded and NOT acted on.

    A proximity window (not the exact heading string) is asserted around
    the token, so a harmless rewording of the heading doesn't break this
    test -- but dropping the 'unverified' labelling, or the
    cannot-be-verified-by-tests caveat, does."""
    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    norm = _normalize(agents_text)

    match = re.search(r"bootloader_ignore_signals", norm)
    assert match, "AGENTS.md must mention bootloader_ignore_signals"

    window = norm[max(0, match.start() - 400) : match.start() + 400]
    assert "unverified" in window, (
        "AGENTS.md's bootloader_ignore_signals passage must be labelled "
        "'unverified' within 400 chars"
    )

    full_window = norm[max(0, match.start() - 400) : match.start() + 2000]
    assert re.search(
        r"cannot.{0,40}(be )?verif|test suite cannot verify", full_window
    ), (
        "AGENTS.md's bootloader_ignore_signals passage must state that the "
        "pytest suite cannot verify this lead"
    )

    for path in (README_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        assert "bootloader_ignore_signals" not in text, (
            f"{path.name} must not mention bootloader_ignore_signals -- the "
            f"lead is deliberately contained to AGENTS.md only"
        )

    spec_text = SPEC_FILE.read_text(encoding="utf-8")
    assert "bootloader_ignore_signals=False" in spec_text, (
        "worktree.spec must still contain the literal "
        "'bootloader_ignore_signals=False' -- the lead was recorded, not "
        "acted on, so this flag must stay unchanged"
    )


def test_build_provenance_archaeology_recorded_as_unreleased():
    """Claim under protection (correction 2, follow-up on ticket #116):
    AGENTS.md must record which build the #116 sweep actually ran against
    (v0.1.16 / commit 00adeab6cfd3) AND must not let that turn into a claim
    that #116 itself is fixed/resolved -- the #112 fix that would be
    relevant to #116's symptom has never shipped in a released build.

    Requires BOTH a build-identity cue (v0.1.16 or the pinned commit hash)
    AND an unreleased/unverified qualifier to appear together, so a future
    edit that keeps the build identity but drops the "not fixed" caveat --
    or vice versa -- fails this test. Also forbids an outright
    "#116 is fixed/resolved" claim anywhere in the file, and keeps the
    archaeology contained to AGENTS.md (not user-facing README.md/SKILL.md),
    mirroring the PyInstaller unverified-leads containment test above.
    """
    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    norm = _normalize(agents_text)

    build_cue = re.compile(r"00adeab6cfd3|v0\.1\.16")
    match = build_cue.search(norm)
    assert match, (
        "AGENTS.md must name the actual installed build (v0.1.16 / "
        "00adeab6cfd3) that the #116 sweep ran against"
    )

    window = norm[max(0, match.start() - 800) : match.start() + 2000]
    assert re.search(r"\bunreleased\b|\bunverified\b", window), (
        "AGENTS.md's build-provenance passage must state that the #112 fix "
        "is unreleased/unverified in the field -- it must not silently "
        "imply the fix shipped"
    )

    # A bare "#116 ... fixed/resolved" claim is forbidden UNLESS it is
    # itself negated nearby (e.g. "does not mean #116 is fixed or
    # resolved") -- the correct text asserts the negation, so a plain
    # substring/regex ban would flag its own correct wording as a
    # violation. Require an explicit negation cue in the 40 chars
    # preceding any such match instead of banning the phrase outright.
    forbidden = re.compile(
        r"#116.{0,60}\b(is|was)\s+(now\s+)?(fixed|resolved)\b"
        r"|\b(fixed|resolved)\b.{0,60}#116"
    )
    negation_cue = re.compile(r"\bnot\b|\bnever\b|\bn't\b|does not")
    for m in forbidden.finditer(norm):
        preceding = norm[max(0, m.start() - 40) : m.start()]
        assert negation_cue.search(preceding), (
            "AGENTS.md must not describe #116 as fixed/resolved without an "
            "explicit negation nearby -- the #112 fix has never shipped in "
            f"a released build: found {norm[max(0, m.start() - 60): m.end() + 20]!r}"
        )

    for path in (README_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        assert "00adeab6cfd3" not in text and "df0d8eb" not in text, (
            f"{path.name} must not carry the #116 build-archaeology detail "
            f"-- it is repo archaeology, contained to AGENTS.md only"
        )


# ---- Ticket #116 fix cycle (review round 2): best-effort caveat + ----
# ---- environment_stop exhaustiveness one-sidedness -------------------


def test_docs_state_existing_id_tokens_are_best_effort():
    """Claim under protection (review finding 1, ticket #116): README.md,
    SKILL.md, and AGENTS.md all state the worktree_create duplicate-retry
    token-naming (existing_environment_id / existing_path) as if it always
    happens. But the handler in worktree.py is explicitly best-effort: when
    the record lookup misses or raises, it falls back to the engine's bare
    message with no tokens at all -- see
    test_duplicate_create_lookup_miss_falls_back_to_bare_message and
    test_duplicate_create_lookup_raise_falls_back_to_bare_message in
    tests/test_transport_failure_readback.py. Each summary doc must carry a
    best-effort/fallback cue near its existing_environment_id mention so a
    caller is never told to unconditionally expect a token that may be
    absent."""
    for path in (README_MD, SKILL_MD, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"existing_environment_id", norm))
        assert occurrences, f"{path.name} must mention existing_environment_id"

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 400) : idx + 400]
            if re.search(r"best.effort", window):
                found = True
                break
        assert found, (
            f"{path.name} must have at least one existing_environment_id "
            f"mention whose surrounding window carries a best-effort/"
            f"fallback cue -- the token-naming is not unconditional"
        )


def test_skill_environment_stop_recipe_documents_raise_path():
    """Claim under protection (review finding 2, ticket #116): SKILL.md's
    environment_stop recipe (item 4) used to read, in substance, as an
    exhaustive disjunction -- 'a blind retry is safe: code: not_running, or
    a graceful no-op' -- directly contradicting the environment_stop
    docstring in worktree.py, which was already corrected this round to
    document a third, RAISING path: CheckoutTargetError,
    VariantResolutionError, InvalidRepoError, and the generic
    (WorktreeError, ProcessLifecycleError) tail all raise ValueError rather
    than return a soft dict. SKILL.md's recipe must name that raising path
    too, mirroring the docstring's own corrected assertion (see
    test_environment_stop_transport_block_documents_the_raise_path in
    tests/test_transport_failure_readback.py)."""
    text = SKILL_MD.read_text(encoding="utf-8")
    norm = _normalize(text)

    idx = norm.find("4. environment_stop")
    assert idx != -1, "SKILL.md must have an environment_stop recipe item (4.)"
    end_idx = norm.find("fields that can never serve", idx)
    window = norm[idx : end_idx if end_idx != -1 else idx + 1200]

    assert re.search(r"\bvalueerror\b", window), (
        "SKILL.md's environment_stop recipe must name ValueError as a "
        "possible outcome of a blind retry, not only the soft `code` "
        "outcomes"
    )
    assert re.search(r"\braises?\b", window), (
        "SKILL.md's environment_stop recipe must state that a blind retry "
        "can RAISE, not only return a soft code -- branching on `code` is "
        "only meaningful for a call that returned"
    )


# ---- Ticket #148: untracked/orphan synthesised id docstring correction ----
#
# Ground truth (lib_python_worktree v0.3.7 core/checkout.py::untracked_id_for):
# the id is <checkout-dirname-slug>-untracked-<8-hex> -- derived from the
# checkout directory's OWN basename (slugged) plus an 8-hex SHA-256 hash of
# its resolved path. It never derives from a repo slug or a branch slug, even
# though a hand-made orphan whose directory happens to be named like a
# tool-created checkout can visually look like it does.

_UNTRACKED_ID_DOC_SITES = (WORKTREE_PY, AGENTS_MD, SKILL_MD, README_MD)


def test_docs_no_longer_claim_repo_and_branch_slug_untracked_id():
    """None of the four corrected doc sites may still claim the untracked/
    orphan synthesised id is built from a repo slug and a branch slug
    (``<repo-slug>-<branch-slug>-untracked-<8-hex>``) -- that formula is
    wrong; the id derives solely from the checkout directory's own
    basename plus a path hash."""
    for path in _UNTRACKED_ID_DOC_SITES:
        text = path.read_text(encoding="utf-8")
        assert "<branch-slug>-untracked-" not in text, (
            f"{path.name} still claims the untracked id is built from "
            "<repo-slug>-<branch-slug>-untracked-<8-hex> -- it is not, see "
            "untracked_id_for() in lib_python_worktree/core/checkout.py"
        )


def test_docs_state_untracked_id_derives_from_checkout_dirname():
    """Each of the four corrected doc sites must, at least once near an
    ``-untracked-`` mention, explain the TRUE derivation: the checkout
    directory's own basename/dirname (slugged), plus a SHA-256-derived
    hash -- not merely drop the wrong formula without stating the right
    one."""
    directory_cue = re.compile(
        r"basename|dirname|directory name|directory's own name"
    )
    hash_cue = re.compile(r"sha-?256|hash")

    for path in _UNTRACKED_ID_DOC_SITES:
        text = path.read_text(encoding="utf-8")
        norm = _normalize(text)

        occurrences = list(re.finditer(r"-untracked-", norm))
        assert occurrences, (
            f"{path.name} must still document the -untracked- id suffix"
        )

        found = False
        for m in occurrences:
            idx = m.start()
            window = norm[max(0, idx - 400) : idx + 400]
            if directory_cue.search(window) and hash_cue.search(window):
                found = True
                break
        assert found, (
            f"{path.name} must explain, near an -untracked- mention, that "
            "the id derives from the checkout directory's own basename "
            "plus a SHA-256 path hash"
        )


def test_docs_still_state_tracked_create_id_formula():
    """Regression fence: the ticket #148 fix must not have swept away the
    UNRELATED, already-correct tracked/tool-created worktree create-id
    formula (<repo-slug>-<branch-slug>-<8-hex>, no -untracked- infix) --
    that one genuinely does derive from the repo slug and branch slug, via
    a different code path (lib_python_worktree/core/manager.py)."""
    for path in (WORKTREE_PY, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        assert "<repo-slug>-<branch-slug>-<8-hex>" in text, (
            f"{path.name} must still document the tracked create-id "
            "formula <repo-slug>-<branch-slug>-<8-hex>"
        )


# ---- Ticket #150: environment_list's repos allow-list must be documented ----


def test_docs_document_environment_list_repos_filter():
    """AGENTS.md and SKILL.md must document the new ``repos`` allow-list
    parameter narrowing ``environment_list(scope="all")``'s fan-out:
    AGENTS.md's `environment_list(` signature line must include `repos`,
    both docs must mention `repos`, both must describe the containment/
    "at or under" matching rule, and both must state that `repos` is only
    valid with `scope='all'` (raising otherwise)."""
    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")
        assert "repos" in text, f"{path.name} does not mention `repos`"

        text_lower = text.lower()
        assert "at or under" in text_lower, (
            f"{path.name} does not describe the containment ('at or under') "
            "matching rule for the repos allow-list"
        )
        assert "only valid with" in text_lower and "scope" in text_lower, (
            f"{path.name} does not state that repos is only valid with "
            "scope='all'"
        )

    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    signature_line = next(
        line
        for line in agents_text.splitlines()
        if line.strip().startswith("environment_list(")
    )
    assert "repos" in signature_line, (
        f"AGENTS.md's environment_list(...) signature line must include "
        f"`repos`: {signature_line!r}"
    )


# ---- Ticket #153: killed_pids[].cmdline must be agent-readable ----


def test_docs_document_decoded_cmdline_and_cmdline_raw():
    """Claim under protection (ticket #153): worktree.py, SKILL.md,
    AGENTS.md, and README.md must each document that killed_pids[].cmdline
    is decoded/human-readable (not an opaque base64 -EncodedCommand blob)
    and that the original raw argv is recoverable via cmdline_raw. The
    stale phrase 'cmdline (list of str)' -- describing cmdline as a bare,
    undecoded argv list, true only before the v0.3.9 engine bump -- must no
    longer appear anywhere in these files."""
    decoded_claim_re = re.compile(r"decoded|human-readable|agent-readable")
    # The stale wording always wraps ``cmdline`` in backticks (single in the
    # Markdown docs, double in the RST-style worktree.py docstring, itself
    # sometimes line-wrapped between "of" and "str") and, pre-fix, ran
    # straight into "describing" with nothing in between. Post-fix, that
    # same "(list of str)" type annotation on ``cmdline`` is still present
    # (the type didn't change) but is now followed by "and `cmdline_raw`
    # ..." before "describing" -- so anchoring on immediate adjacency to
    # "describing" is what actually distinguishes stale from current text.
    stale_phrase_re = re.compile(
        r"`{1,2}cmdline`{1,2}\s*\(\s*list\s+of\s+str\s*\)\s*describing"
    )
    for path in (WORKTREE_PY, SKILL_MD, AGENTS_MD, README_MD):
        text = path.read_text(encoding="utf-8")

        assert "cmdline_raw" in text, f"{path.name} must mention cmdline_raw"
        assert decoded_claim_re.search(text), (
            f"{path.name} must claim killed_pids[].cmdline is decoded/"
            "human-readable/agent-readable"
        )
        assert not stale_phrase_re.search(text), (
            f"{path.name} still contains the stale 'cmdline (list of str) "
            "describing' phrasing that predates the v0.3.9 decode (ticket #153)"
        )


# ---- Ticket #166: "Running the suite" measured chunk table + agent rule ----


def _running_suite_section(text: str) -> str:
    """Return the ``## Running the suite`` section's text, from its heading
    up to (but not including) the next top-level ``## `` heading or EOF.
    Returns ``""`` if the heading is absent."""
    match = re.search(r"^##\s+Running the suite", text, flags=re.MULTILINE)
    if not match:
        return ""
    rest = text[match.start() :]
    next_heading = re.search(r"\n## ", rest)
    if next_heading:
        return rest[: next_heading.start()]
    return rest


def _chunk_table(section: str) -> str:
    """Return only the markdown table lines of ``section`` -- every line
    whose ``strip()`` starts with ``|``, rejoined with ``\\n``. Prose lines
    (the rule paragraph, the CI note, the local-vs-CI caution) are excluded
    by construction."""
    lines = [line for line in section.splitlines() if line.strip().startswith("|")]
    return "\n".join(lines)


def _missing_from_chunk_table(table: str, names: list[str]) -> list[str]:
    """Return the filenames from ``names`` not contained verbatim in
    ``table``. Safe as plain substring containment: no one of the 13
    ``tests/test_*.py`` filenames is a substring of another."""
    return [name for name in names if name not in table]


def _measured_figures(table: str) -> list[str]:
    """Return every measured-seconds figure (e.g. ``12s``, ``7 s``) found
    in ``table``."""
    return re.findall(r"\b\d{1,4}\s*s\b", table)


def test_agents_documents_running_the_suite_chunks():
    """AGENTS.md must gain a '## Running the suite' section containing a
    measured per-chunk timing table covering every tests/test_*.py file,
    the sum-of-measured-chunks disclosure, the never-background/synchronous
    rule, and the CI-runs-whole-suite note.

    RED (pre-fix): AGENTS.md has no '## Running the suite' heading at all,
    so _running_suite_section returns "" and assertion 1 fails.
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    section = _running_suite_section(text)
    assert section, "AGENTS.md must have a '## Running the suite' section"

    table = _chunk_table(section)
    table_lines = [line for line in table.splitlines() if line.strip()]
    assert len(table_lines) >= 3, (
        "the Running the suite section must contain a markdown table with "
        "a header, separator, and at least one data row"
    )

    names = sorted(p.name for p in (REPO_ROOT / "tests").glob("test_*.py"))
    assert len(names) >= 13, (
        "expected at least 13 tests/test_*.py files -- an accidental empty "
        "glob must not vacuously pass this test"
    )
    missing = _missing_from_chunk_table(table, names)
    assert missing == [], (
        f"the chunk table in AGENTS.md's Running the suite section is "
        f"missing these tests/test_*.py filenames: {missing!r}"
    )

    figures = _measured_figures(table)
    assert len(figures) >= 2, (
        "the chunk table must contain at least two measured second-figures "
        f"(e.g. '12s'), found: {figures!r}"
    )

    section_lower = section.lower()
    assert "sum of the measured chunks" in section_lower
    assert "not a single" in section_lower

    assert "background" in section_lower
    assert "synchronous" in section_lower or "synchronously" in section_lower
    assert "single turn" in section_lower

    assert "CI" in section and "whole suite" in section_lower


def test_running_suite_guard_detects_a_missing_test_file():
    table = (
        "| file | seconds |\n"
        "| --- | --- |\n"
        "| tests/test_config.py | 3s |\n"
        "| tests/test_contract.py | 4s |\n"
    )
    names = ["tests/test_config.py", "tests/test_contract.py", "tests/test_setup_runner.py"]
    assert _missing_from_chunk_table(table, names) == ["tests/test_setup_runner.py"]


def test_running_suite_guard_ignores_prose_outside_the_table():
    section = (
        "## Running the suite\n\n"
        "CI runs 301 s / 406 s; most of the time is in "
        "tests/test_environment_tools.py.\n\n"
        "| file | seconds |\n"
        "| --- | --- |\n"
        "| placeholder | - |\n"
    )
    table = _chunk_table(section)
    missing = _missing_from_chunk_table(table, ["tests/test_environment_tools.py"])
    assert missing == ["tests/test_environment_tools.py"]
    assert _measured_figures(table) == []


# ---- Ticket #168: CI trigger must be pull_request-only, not push ----


def test_test_workflow_triggers_on_pull_request_only():
    """Claim under protection (ticket #168): `.github/workflows/test.yml`
    must fire only on `pull_request`, never on `push` -- gatekeeper-mandated
    so branch protection on `main` (an out-of-band repo setting, not part of
    this diff) is the sole gate, and CI doesn't double-run on every push to
    an open PR's branch.

    YAML 1.1 gotcha: `yaml.safe_load` parses a bare top-level `on:` key as
    the boolean `True`, not the string `"on"` -- resolved below by checking
    both.

    RED (pre-fix): the current `on:` block has both `push: {branches:
    ["**"]}` and `pull_request:`, and the header comment (lines 3-9) claims
    "Runs pytest on every push to any branch and on every PR" -- so
    `"push" not in triggers` and the header-comment assertion both fail RED
    for that reason, not for an unrelated reason (e.g. FileNotFoundError).
    """
    raw_text = TEST_WORKFLOW.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    triggers = data[True] if True in data else data["on"]

    assert "pull_request" in triggers, (
        "test.yml's `on:` block must still trigger on pull_request"
    )
    assert "push" not in triggers, (
        "test.yml's `on:` block must no longer trigger on push -- branch "
        "protection on main is the sole gate (ticket #168)"
    )

    # yaml parsing drops comments -- assert against the raw text directly.
    assert "every push" not in raw_text, (
        "test.yml's header comment must no longer claim push-triggering"
    )
    assert re.search(r"\bpull[- ]request\b", raw_text, flags=re.IGNORECASE), (
        "test.yml's header comment must state PR-only triggering"
    )

    # Guard the out-of-scope release.yml -- its workflow_dispatch trigger
    # must be untouched by this change.
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" in release_text


def test_test_workflow_pytest_job_has_headroom_for_windows():
    """Claim under protection (ticket #169/PR #173): the `pytest` job's
    `timeout-minutes` budget must have enough headroom for post-v0.3.11
    Windows runtimes, without drifting to something effectively unbounded.

    Evidence: pre-bump CI durations were 301s / 315s / 397s / 406s. After
    bumping lib-python-worktree to v0.3.11 (new orphan-scan overhead), the
    `windows-latest` leg of this job was cancelled twice at ~10m17s -- a
    cancellation wall-clock forced by the old `timeout-minutes: 10` budget,
    not a completion time, so the true post-bump Windows duration is unknown
    but at least that long. Sizing hypothesis: ~2x overhead on the 406s
    pre-bump worst case is ~812s (~13.5 min); the fix bumps the budget to 20
    minutes, comfortably covering that estimate plus margin while still
    catching a runaway/hung job.

    RED (pre-fix): `.github/workflows/test.yml` still has
    `timeout-minutes: 10`, so the `15 <= value` floor fails on a plain
    value comparison (10 is not >= 15) -- not a KeyError, not a YAML parse
    error, not a missing-file error.
    """
    raw_text = TEST_WORKFLOW.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    timeout_minutes = data["jobs"]["pytest"]["timeout-minutes"]

    assert isinstance(timeout_minutes, int), (
        "timeout-minutes must be a real integer, not a string like '20'"
    )
    assert 15 <= timeout_minutes <= 30, (
        "timeout-minutes must be bumped enough to cover post-v0.3.11 "
        "Windows runtimes (floor) without becoming effectively unbounded "
        f"(ceiling); got {timeout_minutes}"
    )


# ---- Ticket #175: SKILL.md missing 3 of 5 real no_op_reason values ----
#
# `_contract_diagnostics` in worktree.py assigns exactly five `no_op_reason`
# literals: isolation_none, no_start_steps (two assignment sites),
# contract_misplaced, no_contract, contract_unreadable. SKILL.md currently
# only mentions contract_misplaced/no_contract (twice each, in the
# "Critical:" block and in Pitfall 1); the other three are entirely absent.
# This is documentation-only -- no production code changes in this ticket.

NO_OP_REASON_VALUES = {
    "isolation_none",
    "no_start_steps",
    "contract_misplaced",
    "no_contract",
    "contract_unreadable",
}


def _table_rows(text: str) -> list[list[str]]:
    """Return every Markdown table row in ``text`` as a list of stripped
    cell strings, for lines that look like a table row (start with ``|``
    and split into >=3 cells once the leading/trailing empty strings
    produced by the outer pipes are dropped). Used to find a dedicated
    ``no_op_reason`` value-reference table without depending on exact
    heading text or column widths."""
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) >= 3:
            rows.append(parts)
    return rows


def test_skill_documents_full_no_op_reason_value_enum():
    """Driving test (ticket #175). SKILL.md must document all five real
    no_op_reason values as substantive Markdown-table rows, not bare
    token mentions.

    RED (pre-fix): isolation_none, no_start_steps, contract_unreadable have
    zero occurrences in SKILL.md at all, and none of the five values appear
    as the first cell of any existing Markdown table row (the pre-fix
    contract_misplaced/no_contract mentions are inline prose, not table
    rows) -- so every value is reported missing below.
    """
    source_text = WORKTREE_PY.read_text(encoding="utf-8")
    found_values = set(re.findall(r'no_op_reason = "(\w+)"', source_text))
    assert found_values == NO_OP_REASON_VALUES, (
        "expected worktree.py's _contract_diagnostics to assign exactly "
        f"these 5 no_op_reason literals, got: {found_values!r}"
    )

    skill_text = SKILL_MD.read_text(encoding="utf-8")
    rows = _table_rows(skill_text)

    missing_rows = []
    for value in sorted(NO_OP_REASON_VALUES):
        row = next(
            (cells for cells in rows if value in _normalize(cells[0])), None
        )
        if row is None:
            missing_rows.append(value)
            continue

        remaining = " ".join(row[1:])
        assert len(remaining) >= 40, (
            f"SKILL.md's {value!r} table row must carry real documentation, "
            f"not a bare token -- remaining cells only had "
            f"{len(remaining)} chars: {row!r}"
        )
        assert len(row[-1]) >= 15, (
            f"SKILL.md's {value!r} table row's 'what to do' (last) cell "
            f"must be substantive (>=15 chars), got {row[-1]!r}"
        )

        norm_row = _normalize(" | ".join(row))
        if value == "isolation_none":
            assert "isolation" in norm_row and "none" in norm_row, (
                f"SKILL.md's isolation_none row must mention both "
                f"'isolation' and 'none': {row!r}"
            )
        elif value == "no_start_steps":
            assert "start" in norm_row and "role" in norm_row, (
                f"SKILL.md's no_start_steps row must mention both 'start' "
                f"and 'role': {row!r}"
            )
        elif value == "contract_misplaced":
            assert "checkout" in norm_row and "repo_root" in norm_row, (
                f"SKILL.md's contract_misplaced row must mention both "
                f"'checkout' and 'repo_root': {row!r}"
            )
        elif value == "no_contract":
            assert "repo_root" in norm_row, (
                f"SKILL.md's no_contract row must mention 'repo_root': "
                f"{row!r}"
            )
        elif value == "contract_unreadable":
            assert "read" in norm_row or "parse" in norm_row, (
                f"SKILL.md's contract_unreadable row must mention 'read' "
                f"or 'parse': {row!r}"
            )

    assert missing_rows == [], (
        "SKILL.md must document every no_op_reason value as a Markdown "
        "table row (>=3 pipe-delimited cells, the value as the first "
        f"cell); missing a row for: {missing_rows!r}"
    )


def test_skill_no_op_reason_values_are_colocated_in_one_reference_block():
    """Claim under protection (ticket #175): the full five-value
    no_op_reason reference must live in a single dedicated block, not be
    scattered across the document, so a caller can find every value in one
    place.

    RED (pre-fix): the anchor heading text 'no_op_reason values' occurs
    zero times in SKILL.md today, so no window can be found at all.
    """
    norm = _normalize(SKILL_MD.read_text(encoding="utf-8"))
    heading = "no_op_reason values"
    # 900 chars is generous enough to span the heading, its leading
    # sentence, and a 5-row value table, while still being narrow enough
    # that two unrelated mentions elsewhere in the ~15k-char document can't
    # coincidentally both fall inside it.
    window_chars = 900

    found = False
    for m in re.finditer(re.escape(heading), norm):
        idx = m.start()
        window = norm[max(0, idx - 100) : idx + window_chars]
        if all(value in window for value in NO_OP_REASON_VALUES):
            found = True
            break

    assert found, (
        "SKILL.md must have a single contiguous ~900-char block containing "
        "the heading 'no_op_reason values' together with all five "
        f"no_op_reason value tokens: {sorted(NO_OP_REASON_VALUES)!r}"
    )


def test_skill_two_value_sites_cross_reference_the_full_enum_block():
    """Claim under protection (ticket #175): the two existing two-value
    spots (the "Critical:" contract block before ## Troubleshooting, and
    Pitfall 1) must each point readers at the new full five-value
    reference block, without losing their existing contract_misplaced/
    no_contract contrast.

    RED (pre-fix): the anchor phrase 'no_op_reason values' occurs zero
    times in SKILL.md today, so neither site's cross-reference can be
    found.
    """
    raw = SKILL_MD.read_text(encoding="utf-8")

    troubleshooting_idx = raw.find("## Troubleshooting")
    pitfalls_idx = raw.find("## Pitfalls")
    assert troubleshooting_idx != -1, (
        "SKILL.md must have a '## Troubleshooting' heading"
    )
    assert pitfalls_idx != -1, "SKILL.md must have a '## Pitfalls' heading"

    # Site A: the "Critical:" contract block, which sits before
    # ## Troubleshooting.
    before_troubleshooting = _normalize(raw[:troubleshooting_idx])
    found_site_a = False
    for m in re.finditer("contract_misplaced", before_troubleshooting):
        idx = m.start()
        window = before_troubleshooting[max(0, idx - 400) : idx + 400]
        if "no_op_reason values" in window and "troubleshooting" in window:
            found_site_a = True
            break
    assert found_site_a, (
        "SKILL.md's pre-Troubleshooting 'Critical:' block must "
        "cross-reference 'no_op_reason values' (mentioning "
        "'Troubleshooting') within ~400 chars of its 'contract_misplaced' "
        "mention"
    )

    # Site B: Pitfall 1 specifically, isolated from the rest of
    # ## Pitfalls by slicing up to the next numbered item ("2. ").
    pitfalls_raw = raw[pitfalls_idx:]
    next_item_match = re.search(r"^2\. ", pitfalls_raw, flags=re.MULTILINE)
    assert next_item_match, "SKILL.md's '## Pitfalls' section must have an item '2.'"
    pitfall_1_norm = _normalize(pitfalls_raw[: next_item_match.start()])

    assert "no_op_reason values" in pitfall_1_norm, (
        "SKILL.md's Pitfall 1 must cross-reference 'no_op_reason values'"
    )
    assert "troubleshooting" in pitfall_1_norm, (
        "SKILL.md's Pitfall 1 cross-reference must mention 'Troubleshooting'"
    )
    assert "contract_misplaced" in pitfall_1_norm, (
        "SKILL.md's Pitfall 1 must still mention 'contract_misplaced' "
        "alongside the new cross-reference"
    )


def test_skill_keeps_existing_two_value_misplaced_contrast():
    """Guard/regression test (ticket #175, additive not replacement): the
    pre-existing contract_misplaced-vs-no_contract contrast in both the
    "Critical:" block and Pitfall 1 must survive the new full-enum
    reference block being added alongside it.

    RED (pre-fix): contract_misplaced occurs only 2 times today (the two
    pre-existing sites); the >=3 bound (2 pre-existing + 1 new table row)
    is not yet met.
    """
    raw = SKILL_MD.read_text(encoding="utf-8")
    norm_full = _normalize(raw)

    assert norm_full.count("contract_misplaced") >= 3, (
        "SKILL.md must retain both pre-existing 'contract_misplaced' "
        "mentions (Critical: block + Pitfall 1) and gain a third "
        "occurrence from the new no_op_reason values reference table"
    )

    troubleshooting_idx = raw.find("## Troubleshooting")
    pitfalls_idx = raw.find("## Pitfalls")
    assert troubleshooting_idx != -1 and pitfalls_idx != -1

    before_troubleshooting = _normalize(raw[:troubleshooting_idx])
    pitfalls_section = _normalize(raw[pitfalls_idx:])

    assert "no_contract" in before_troubleshooting, (
        "SKILL.md's pre-Troubleshooting 'Critical:' block must still "
        "mention 'no_contract'"
    )
    assert "no_contract" in pitfalls_section, (
        "SKILL.md's Pitfalls section must still mention 'no_contract'"
    )
