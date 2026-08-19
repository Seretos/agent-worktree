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
    (``<repo-slug>-<branch-slug>-untracked-<8-hex>``, via
    ``untracked_id_for()``) and must no longer claim such an id is the
    empty string ``""`` (the stale claim ticket #113's review fix removes
    from the source, not just the docs)."""
    text = WORKTREE_PY.read_text(encoding="utf-8")

    stale_claims = ['id == ""', "is the empty string"]
    for stale in stale_claims:
        assert stale not in text, (
            f"worktree.py still contains the stale untracked-id claim: {stale!r}"
        )

    assert text.count("-untracked-<8-hex>") >= 2, (
        "worktree.py's _entry_to_dict and environment_list docstrings must "
        "both state the true synthesised id shape "
        "<repo-slug>-<branch-slug>-untracked-<8-hex>"
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
    2. Both docs must also document the `environment_stop(variant=
       "default")` asymmetry: when the fallback resolves a NAMED step,
       `record.variants[role]` stores that step's own name, never the
       literal `"default"`, so a later `environment_stop(variant=
       "default")` will not resolve.
    3. SKILL.md's `## Pitfalls` section specifically (not just its earlier
       contract prose) must gain an entry: a multi-step contract with no
       step named `default` makes the *first* `environment_start` call
       fail unless `variant=` is passed explicitly.
    """
    fallback_pattern = re.compile(
        r"\b(single|lone|exactly one)\b[^.]{0,160}"
        r"\b(regardless|even if|whether it is named|named or unnamed)\b"
    )
    asymmetry_pattern = re.compile(
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

        found_asymmetry = False
        for m in re.finditer(r"default", norm):
            idx = m.start()
            window = norm[max(0, idx - 500) : idx + 500]
            if "variants" in window and asymmetry_pattern.search(window):
                found_asymmetry = True
                break
        assert found_asymmetry, (
            f'{path.name} must document that environment_stop(variant='
            '"default") does not resolve when the lone-step fallback '
            "resolved a named step"
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
    "contract-misplaced"), not silent/indistinguishable-from-unconfigured."""
    text = SKILL_MD.read_text(encoding="utf-8")
    norm = _normalize(text)

    assert "no_op_reason" in norm
    assert "contract-misplaced" in norm
    assert "with no error" not in norm

    found_silent_far_from_diagnosis = False
    for m in re.finditer(r"(?<!not )\bsilent\b", norm):
        idx = m.start()
        window = norm[max(0, idx - 300) : idx + 300]
        if "contract-misplaced" in window or "misplacement" in window:
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
    """Claim under protection (ticket #130, re-slicing #128): SKILL.md and
    AGENTS.md must document that start_log_path's filename is a lower-cased
    slug of role while pids/record.variants key on role verbatim, citing
    the fully-qualified upstream defect."""
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
                and "pids" in window
            ):
                found = True
                break

        assert found, (
            f"{path.name} must have at least one start_log_path mention "
            "whose surrounding window fully-qualifies the upstream #111 "
            "reference, names the lower-case/slug behaviour, and mentions "
            "pids"
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
