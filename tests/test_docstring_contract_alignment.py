"""Guard tests for WP #162 (child tickets #156, #157): docstring/no_op_reason
alignment with the contract.

This module covers:

- **R1(a)** -- a repo-wide (explicit six-file allow-list, not a repo walk)
  guard that zero hyphenated ``no_op_reason`` literals remain anywhere.
- **R3** -- both ``ports`` docstring paragraphs (``worktree_create``,
  ``worktree_remove``) disambiguate the response dict from the contract's
  list-shaped ``ports:`` input block.
- **R4** -- ``environment_list`` explains ``status: "orphaned"`` as a
  *tracked* record whose state-store entry outlived git's own worktree
  registration, distinct from an unmanaged on-disk checkout
  (``status: "created"``, ``tracked: false``).
- **R5** -- ``worktree_create``'s ``start_variants`` bullet states that
  omitting ``variant`` (or passing ``variant="default"``) still resolves
  the contract's single unnamed ``start:`` step.
- **R6** -- ``worktree_remove``'s untracked-removal recipe warns that
  ``force=True`` may be required for a checkout holding uncommitted/
  untracked local changes.

Local ``_normalize``/``_get_tool_docstring`` helpers duplicate the shape
already used by ``tests/test_transport_failure_readback.py`` (kept local
per that file's own documented precedent, rather than imported across test
modules).
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "skills" / "worktree" / "SKILL.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
WORKTREE_PY = REPO_ROOT / "src" / "worktree_plugin" / "tools" / "worktree.py"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"
THREAD_LEAK_TEST = REPO_ROOT / "tests" / "test_thread_leak_regression.py"
TIMEOUT_CONFIG_TEST = REPO_ROOT / "tests" / "test_pytest_timeout_config.py"


def _normalize(text: str) -> str:
    """Strip Markdown/RST emphasis markup and collapse whitespace -- mirrors
    tests/test_plugin_manifest.py's / tests/test_transport_failure_readback.py's
    `_normalize` helper so prose assertions match semantic tokens via regex,
    not exact formatting. Lower-cases, so every caller below uses lower-case
    tokens (`force=true`, `tracked: false`, ...)."""
    stripped = text.replace("``", "").replace("`", "").replace("**", "")
    return re.sub(r"\s+", " ", stripped).lower()


def _get_tool_docstring(tool_name: str) -> str:
    from mcp.server.fastmcp import FastMCP
    from lib_python_worktree import InMemoryStateStore as _Mem
    from lib_python_worktree import ManagerConfig as _Cfg
    from lib_python_worktree import WorktreeManager as _Mgr
    from worktree_plugin.tools.worktree import register

    mgr = _Mgr(config=_Cfg(store_root=Path("unused")), state=_Mem())
    mcp = FastMCP("test")
    register(mcp, mgr)
    fn = mcp._tool_manager._tools[tool_name].fn
    doc = fn.__doc__
    assert doc, f"{tool_name} has no docstring at all"
    # `fn.__doc__` is the *raw* compiled docstring constant. Since CPython
    # 3.13 (gh-81283) the compiler itself strips each docstring's common
    # leading whitespace at compile time, matching `inspect.cleandoc()` --
    # but on 3.11/3.12 (CI pins 3.12, see .github/workflows/test.yml) the
    # docstring's original source indentation (this file's tool functions
    # are nested one level inside `register()`, so every continuation line
    # after the first carries 8+ literal leading spaces) survives verbatim
    # in `__doc__`. A dev box on 3.13+ therefore sees already-dedented text
    # while CI's 3.12 runner sees the raw indented text for the exact same
    # commit -- silently, since nothing in FastMCP or this plugin dedents
    # it (confirmed: no `cleandoc`/`dedent` call anywhere in `mcp` or in
    # `worktree_plugin`). Column-0-anchored regexes/assertions below
    # (e.g. the R4 structural bullet count) would find 0 matches under the
    # un-dedented 3.12 text while passing locally on 3.13+ -- exactly the
    # "found 0" CI failure this normalization fixes. `inspect.cleandoc` is
    # idempotent on already-dedented text, so this is a no-op on 3.13+.
    return inspect.cleandoc(doc)


def _sentence_containing(region: str, needle: str) -> str:
    """Return the ``". "``-delimited sentence of `region` that contains the
    first occurrence of `needle`. Used throughout this module so assertions
    key on a claim's own sentence (polarity/qualifier), not merely on the
    bare presence of a token somewhere in a whole paragraph."""
    idx = region.index(needle)
    start = region.rfind(". ", 0, idx)
    start = 0 if start == -1 else start + 2
    end = region.find(". ", idx)
    end = len(region) if end == -1 else end + 1
    return region[start:end]


# ---------------------------------------------------------------------------
# R1(a): repo-wide guard -- zero hyphenated no_op_reason literals anywhere.
# ---------------------------------------------------------------------------

# Explicit file list (not a repo walk), so this guard cannot trip on
# .adev/162-*/plan.md|spec.md, .venv/, or its own regex literal -- this
# module is deliberately not in its own scan list. Orchestrator-verified
# ground truth (plan-critic round 1, major misread::F1 resolution): exactly
# 39 occurrences across exactly these 4 of the 6 listed files; AGENTS.md and
# tests/test_contract.py are zero both before and after.
_R1_SCAN_FILES = (
    "src/worktree_plugin/tools/worktree.py",
    "skills/worktree/SKILL.md",
    "AGENTS.md",
    "tests/test_environment_tools.py",
    "tests/test_plugin_manifest.py",
    "tests/test_contract.py",
)

_R1_HYPHENATED_PATTERN = re.compile(
    r"isolation-none|no-start-steps|contract-misplaced|no-contract|contract-unreadable"
)


def test_no_op_reason_values_use_underscores_repo_wide():
    """R1 edge (a): every wrapper-derived no_op_reason literal -- in source,
    the skill doc, and test assertions alike -- must use the underscore
    spelling, not the hyphenated one.

    Expected RED reason (tests phase): src/worktree_plugin/tools/worktree.py
    and skills/worktree/SKILL.md still emit/document the hyphenated forms --
    renaming them is production-code/doc work deferred to the implement
    phase. tests/test_environment_tools.py and tests/test_plugin_manifest.py
    were already flipped to underscores as this phase's R1 driving-test
    edit, and AGENTS.md / tests/test_contract.py have zero occurrences
    either way (verified, not assumed).
    """
    offenders = {}
    for rel in _R1_SCAN_FILES:
        path = REPO_ROOT / rel
        text = path.read_text(encoding="utf-8")
        matches = _R1_HYPHENATED_PATTERN.findall(text)
        if matches:
            offenders[rel] = len(matches)
    assert offenders == {}, f"hyphenated no_op_reason literals remain: {offenders}"


# ---------------------------------------------------------------------------
# R3: `ports` response dict vs. the contract's list-shaped `ports:` block.
# ---------------------------------------------------------------------------


def test_worktree_create_ports_paragraph_disambiguates_response_from_contract():
    """R3 driving test (worktree_create call site, worktree.py:623-626):
    the ports paragraph must distinguish the response's dict-shaped
    `ports` from the contract's own list-shaped `ports:` input block.

    Expected RED reason: this region contains none of "response",
    "contract", "list" today.
    """
    doc = _normalize(_get_tool_docstring("worktree_create"))
    start = doc.index("ports field is a dict mapping port name to host port number")
    end = doc.index("returns the canonical worktree record")
    region = doc[start:end]

    assert "response" in region
    assert "contract" in region
    assert "list" in region

    contract_sentence = _sentence_containing(region, "contract")
    assert "list" in contract_sentence
    # Test-critic round-1 note 5: bind "list" to the contract side and
    # "dict" to the response side (polarity, not mere token presence) --
    # a sentence stating the relationship backwards (e.g. "the contract's
    # ports block is a dict, not a list") would satisfy the bare-presence
    # checks above but must fail here.
    assert "dict" not in contract_sentence
    response_sentence = _sentence_containing(region, "response")
    assert "dict" in response_sentence
    assert "list" not in response_sentence


def test_worktree_create_ports_empty_dict_is_reframed_as_response_state():
    """R3 edge (a): the ticket's literal repro trigger -- the `{}` example
    must survive (it is legitimate, describing the response's empty state),
    re-framed by its own sentence naming "response".

    Expected RED reason: line 624's `{}` sentence has no "response" today.
    """
    doc = _normalize(_get_tool_docstring("worktree_create"))
    start = doc.index("ports field is a dict mapping port name to host port number")
    end = doc.index("returns the canonical worktree record")
    region = doc[start:end]

    assert "{}" in region
    empty_dict_sentence = _sentence_containing(region, "{}")
    assert "response" in empty_dict_sentence


def test_worktree_remove_ports_paragraph_disambiguates_response_from_contract():
    """R3 driving test (second call site, worktree_remove, worktree.py:
    944-947) -- same claim, same region shape as worktree_create's. Start
    anchor is "returns the removed worktree record on success." (no
    "response" token of its own) and end anchor is "the response includes a
    killed_pids list" (excluded by the slice, so its own "response" token
    cannot inflate the count).

    Expected RED reason: this region also contains none of "response",
    "contract", "list" today.
    """
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    start = doc.index("returns the removed worktree record on success")
    end = doc.index("the response includes a killed_pids list")
    region = doc[start:end]

    assert "response" in region
    assert "contract" in region
    assert "list" in region

    contract_sentence = _sentence_containing(region, "contract")
    assert "list" in contract_sentence
    # Test-critic round-1 note 5, mirrored at the second call site.
    assert "dict" not in contract_sentence
    response_sentence = _sentence_containing(region, "response")
    assert "dict" in response_sentence
    assert "list" not in response_sentence


def test_worktree_remove_ports_empty_dict_is_reframed_as_response_state():
    """R3 edge (a) mirrored at the second call site.

    Expected RED reason: same as worktree_create's mirror -- the `{}`
    sentence has no "response" today.
    """
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    start = doc.index("returns the removed worktree record on success")
    end = doc.index("the response includes a killed_pids list")
    region = doc[start:end]

    assert "{}" in region
    empty_dict_sentence = _sentence_containing(region, "{}")
    assert "response" in empty_dict_sentence


# ---------------------------------------------------------------------------
# R4: `orphaned` is a tracked-but-stale record, not an unmanaged checkout.
# ---------------------------------------------------------------------------

_R4_REGION_START = "each entry mirrors a worktreerecord plus"
_R4_REGION_END = "setup_status: a coarse setup-health signal"


def test_environment_list_orphaned_is_a_tracked_stale_record():
    """R4 driving test: environment_list's docstring must explain
    `status: "orphaned"` as a *tracked* record whose state-store entry
    outlived git's own worktree registration -- distinct from an unmanaged
    on-disk checkout, which is `status: "created"` + `tracked: false`.

    Expected RED reason: `\\borphaned\\b` has zero matches in this region
    today (verified -- the only "orphan" in the whole docstring is
    "orphan's prefix" at worktree.py:1178, which `\\borphaned\\b` does not
    match), so assertion (1) below fails immediately.
    """
    doc = _normalize(_get_tool_docstring("environment_list"))
    start = doc.index(_R4_REGION_START)
    end = doc.index(_R4_REGION_END)
    region = doc[start:end]

    match = re.search(r"\borphaned\b", region)
    assert match is not None, 'region must document status: "orphaned"'

    window = region[match.start() : match.start() + 300]
    for token in ("state store", "tracked: true", "status: created", "tracked: false"):
        assert token in window, f"{token!r} missing from the orphaned-explanation window"

    assert window.index("tracked: true") < window.index("tracked: false"), (
        "orphaned must be bound to the tracked case first, with the "
        "untracked case presented as the contrast"
    )


def test_environment_list_and_worktree_remove_agree_orphaned_is_a_record():
    """R4 edge (a): cross-tool consistency -- neither tool describes
    `status: "orphaned"` as anything other than a surviving record.
    worktree_remove already documents this in its readback recipe; once
    environment_list's own continuation paragraph lands, both windows must
    agree that "record" is present nearby.

    Expected RED reason: environment_list's half fails today for the same
    underlying reason as the driving test above -- no "orphaned" mention to
    build a window around yet.
    """
    remove_doc = _normalize(_get_tool_docstring("worktree_remove"))
    idx_remove = remove_doc.find('status: "orphaned"')
    assert idx_remove != -1, 'worktree_remove must already document status: "orphaned"'
    window_remove = remove_doc[idx_remove : idx_remove + 300]
    assert "record" in window_remove

    list_doc = _normalize(_get_tool_docstring("environment_list"))
    match = re.search(r"\borphaned\b", list_doc)
    assert match is not None, (
        "environment_list must document orphaned before this cross-tool "
        "consistency check can run -- see the R4 driving test"
    )
    window_list = list_doc[match.start() : match.start() + 300]
    assert "record" in window_list


def test_worktree_remove_orphaned_readback_still_says_record_survives():
    """R4 edge (b): pinning -- worktree_remove's existing normalized phrase
    must survive verbatim; this is why R4's new text may not claim the
    checkout is still present (it would contradict this line). Expected to
    already pass -- untouched by R4."""
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    assert "the directory is gone, the record survives" in doc


def test_environment_list_still_documents_exactly_three_extra_keys():
    """R4 edge (c): count pin -- proves the new text lands as an indented
    continuation of the existing `tracked` bullet, not as a fourth
    top-level key bullet (which would falsify this count).

    Test-critic round-1 note 3 (minor): the phrase "three extra keys"
    surviving is not enough -- a fourth top-level bullet could be added
    alongside it without breaking that phrase. Made structural: counts the
    actual top-level ``- ``key```` bullet lines in the docstring (as
    returned by ``fn.__doc__``, already dedented by FastMCP's tool
    registration -- top-level bullets sit at column 0, ``- ``key```; a
    continuation paragraph is indented and has no leading ``- ```), and
    asserts the count is exactly 3. R4's text landing as an indented
    continuation (per the plan) cannot trip this count, while a genuine
    fourth top-level bullet would.

    Expected to already pass -- untouched by R4, provided the new orphaned
    text lands as an indented continuation as the plan specifies."""
    raw_doc = _get_tool_docstring("environment_list")
    raw_start = raw_doc.index(
        "Each entry mirrors a ``WorktreeRecord`` plus three extra keys:"
    )
    raw_end = raw_doc.index("This call **never writes state**")
    raw_region = raw_doc[raw_start:raw_end]

    top_level_bullets = re.findall(r"(?m)^- ``\w+``", raw_region)
    assert len(top_level_bullets) == 3, (
        f"expected exactly 3 top-level key bullets, found "
        f"{len(top_level_bullets)}: {top_level_bullets}"
    )

    doc = _normalize(raw_doc)
    start = doc.index(_R4_REGION_START)
    end = doc.index(_R4_REGION_END)
    region = doc[start:end]
    assert "three extra keys" in region


# ---------------------------------------------------------------------------
# R5: omitting `variant` still resolves the unnamed default start step.
# ---------------------------------------------------------------------------

_R5_REGION_START = "start_variants (always present, unlike warning)"
_R5_REGION_END = "contract file (.seretos/worktree-setup.yml)"


def test_worktree_create_start_variants_bullet_says_omitting_variant_still_resolves():
    """R5 driving test: the `start_variants` bullet must state that
    omitting `variant` entirely (or passing `variant="default"`) still
    resolves the contract's single unnamed `start:` step.

    Expected RED reason: the region already contains "default" and
    "unnamed" (exactly why those tokens are not the assertion here), but no
    form of "omit" occurs in it -- the only "Omit" in the whole docstring is
    "Omit ``base``" at worktree.py:617, outside this region.
    """
    doc = _normalize(_get_tool_docstring("worktree_create"))
    start = doc.index(_R5_REGION_START)
    end = doc.index(_R5_REGION_END)
    region = doc[start:end]

    match = re.search(r"omit\w*\s+(?:the\s+)?variant", region)
    assert match is not None, "region must state that omitting variant is still valid"

    sentence = _sentence_containing(region, match.group(0))
    assert re.search(r"\b(still|remains)\b", sentence), sentence
    assert re.search(r"\b(valid|resolves?|callable|invokes?|selects?|starts?)\b", sentence), sentence
    assert not re.search(r"\b(never|cannot|can't|no longer)\b", sentence), sentence


def test_worktree_create_start_variants_bullet_keeps_none_vs_empty_list_distinction():
    """R5 edge case: the region must still preserve the `None`-vs-`[]`
    contract-ambiguity distinction and the `variant="default"` mention, so
    the new sentence displaces nothing. Expected to already pass --
    untouched by R5."""
    doc = _normalize(_get_tool_docstring("worktree_create"))
    start = doc.index(_R5_REGION_START)
    end = doc.index(_R5_REGION_END)
    region = doc[start:end]

    assert "none when there is no contract file to read" in region
    assert "an empty list [] when the contract was read successfully" in region
    assert 'variant="default"' in region


# ---------------------------------------------------------------------------
# R6: `force=True` may be required for an untracked checkout with local
# changes.
# ---------------------------------------------------------------------------

_R6_REGION_START = "checkout_path -- the only way to remove an untracked/orphan checkout."
_R6_REGION_END = "neither is schema-required"


def test_worktree_remove_untracked_recipe_warns_force_for_uncommitted_changes():
    """R6 driving test: the untracked-removal recipe (the `checkout_path`
    bullet) must warn that `force=True` may be required when the checkout
    holds uncommitted/untracked local changes.

    Expected RED reason: the region already contains one `force=true`
    mention ("never deletes its branch, even with ``force=True``") --
    exactly why this test keys on "uncommitted", a token that occurs
    nowhere in the region today. The pre-existing bare-token mention cannot
    satisfy this assertion.
    """
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    start = doc.index(_R6_REGION_START)
    end = doc.index(_R6_REGION_END)
    region = doc[start:end]

    assert "uncommitted" in region, "region must mention uncommitted/untracked local changes"

    sentence = _sentence_containing(region, "uncommitted")
    assert "force=true" in sentence
    assert re.search(r"\b(may|must|need)\b", sentence)


# ---------------------------------------------------------------------------
# Ticket #176 (Q2 spillover, R3): worktree_create's own start_variants may
# include a synthesised "default" entry (the v0.3.12 engine's
# available_variants fallback-tier projection) that a subsequent
# environment_list-sourced record does not necessarily carry the same way --
# this create-vs-record divergence must be documented on three surfaces.
# ---------------------------------------------------------------------------

_R176_REQUIRED_TOKENS = ("start_variants", "environment_list", '"default"')
_R176_ENGINE_TOKENS = ("engine", "upstream")
_R176_PHRASING = re.compile(r"may include|can include|includes")


def test_start_variants_create_vs_record_divergence_documented_on_three_surfaces():
    """R3 driving test (ticket #176, Q2 spillover): the create-vs-record
    `start_variants` divergence must be documented in all three of:
    `worktree.py`'s `start_variants` bullet (inside the existing R5 region,
    756-789), `AGENTS.md` (either the `worktree_create` or `environment_list`
    section), and `skills/worktree/SKILL.md` (near its `start_variants`
    mention around 583-591).

    Expected RED reason: none of the three sources currently mentions this
    divergence -- `environment_list` is required token, and none of the two
    Markdown sources currently pairs `environment_list` with `start_variants`
    in the same breath (the worktree.py region doesn't mention
    `environment_list` at all, and neither AGENTS.md nor SKILL.md's
    `start_variants` mentions pair it with `"default"` plus an
    engine/upstream attribution and `may include`/`can include`/`includes`
    phrasing).
    """
    worktree_doc = _normalize(_get_tool_docstring("worktree_create"))
    ws_start = worktree_doc.index(_R5_REGION_START)
    ws_end = worktree_doc.index(_R5_REGION_END)
    worktree_region = worktree_doc[ws_start:ws_end]

    agents_text = _normalize(AGENTS_MD.read_text(encoding="utf-8"))
    skill_text = _normalize(SKILL_MD.read_text(encoding="utf-8"))

    # The worktree_region slice starts at the literal marker string
    # "start_variants (always present, unlike warning)" (_R5_REGION_START),
    # so a bare `"start_variants" in worktree_region` check would be
    # tautologically true regardless of content -- the slice always begins
    # with that substring. Instead, require a SECOND `start_variants`
    # occurrence (the divergence note re-mentioning the key by name, beyond
    # the marker itself) with `environment_list` nearby, proving the note
    # genuinely links the two keys rather than the two tokens merely
    # appearing somewhere, unrelated, in the same region.
    second_start_variants_idx = worktree_region.find(
        "start_variants", len(_R5_REGION_START)
    )
    assert second_start_variants_idx != -1, (
        "worktree.py start_variants bullet (R5 region) must mention "
        "start_variants a second time (beyond the region marker) as part "
        "of the divergence note"
    )
    proximity_window = worktree_region[
        max(0, second_start_variants_idx - 300) : second_start_variants_idx + 300
    ]
    assert "environment_list" in proximity_window, (
        "worktree.py start_variants bullet (R5 region) must re-mention "
        "start_variants near environment_list to link the two keys"
    )

    for label, text in (
        ("worktree.py start_variants bullet (R5 region)", worktree_region),
        ("AGENTS.md", agents_text),
        ("skills/worktree/SKILL.md", skill_text),
    ):
        tokens = _R176_REQUIRED_TOKENS
        if label == "worktree.py start_variants bullet (R5 region)":
            # "start_variants" is excluded here: it is checked above via the
            # proximity assertion instead of a bare membership check, since
            # membership alone is tautological for this particular slice.
            tokens = tuple(t for t in tokens if t != "start_variants")
        for token in tokens:
            assert token in text, f"{label} must mention {token!r}"
        assert any(tok in text for tok in _R176_ENGINE_TOKENS), (
            f"{label} must attribute the synthesised \"default\" entry to "
            f"the engine/upstream"
        )
        assert _R176_PHRASING.search(text), (
            f"{label} must use 'may include'/'can include'/'includes' phrasing"
        )


# ---------------------------------------------------------------------------
# Ticket #176 (Q1, R4): the falsified "live upstream defect" narrative for
# the Windows CTRL_BREAK guard is gone -- upstream PR #151 (v0.3.12) added
# the engine's own group-leader guard, so this plugin's SIGBREAK handler is
# now defence-in-depth, not the only mitigation.
# ---------------------------------------------------------------------------

_R176_SIGNAL_FORBIDDEN = (
    "calls this on non-group-leader pids from two call sites",
    "no check",
    "no equivalent guard",
    "is the fix",
)


def test_signal_guard_docstrings_describe_v0_3_12_engine_guard():
    """R4 driving test #1 (ticket #176, Q1): `server._ignore_and_log` and
    `server._install_signal_guards`'s docstrings must stop describing the
    engine as having "no check"/"no equivalent guard" against non-group-
    leader `CTRL_BREAK_EVENT` delivery -- upstream PR #151 (shipped in the
    pinned v0.3.12) added exactly that guard. The docstrings must instead
    describe this plugin's own SIGBREAK handler as a backstop/defence-in-
    depth layered on top of the engine's own fix, while the untouched
    `SIGINT` and `Tradeoff` paragraphs survive verbatim.

    Expected RED reason: today's docstrings still contain the falsified
    forbidden phrases (verified directly: `_ignore_and_log.__doc__` contains
    "calls this on non-group-leader pids from two call sites") and mention
    neither `v0.3.12` nor `_send_graceful_signal` nor any
    backstop/defence-in-depth framing.
    """
    from worktree_plugin.server import _ignore_and_log, _install_signal_guards

    ignore_doc = _normalize(_ignore_and_log.__doc__ or "")
    install_doc = _normalize(_install_signal_guards.__doc__ or "")
    combined = f"{ignore_doc} {install_doc}"

    for forbidden in _R176_SIGNAL_FORBIDDEN:
        assert forbidden not in combined, (
            f"falsified upstream-defect phrase must be removed: {forbidden!r}"
        )

    assert "v0.3.12" in combined
    assert "_send_graceful_signal" in combined

    sentence = _sentence_containing(combined, "_send_graceful_signal")
    assert re.search(r"refus\w+|reject\w+|declin\w+|skips?\b", sentence), sentence

    assert re.search(r"backstop|defence-in-depth", combined)

    # Untouched survivors (Q1(b): SIGBREAK guard logic/framing paragraphs
    # stay byte-for-byte; only a lead-in sentence is added).
    assert "sigint" in install_doc
    assert "tradeoff" in install_doc


def test_markdown_signal_narrative_is_not_falsified():
    """R4 driving test #2 (ticket #176, Q1/Q3): `AGENTS.md` and
    `skills/worktree/SKILL.md` must stop asserting the engine has "no
    equivalent guard" and stop citing an "Upstream recommendation (not
    implemented in this repo)" that upstream PR #151 (v0.3.12) has since
    shipped. `AGENTS.md` must also still cite the thread-leak regression
    test file by its literal path, and must no longer point readers at a
    "thread-leak note above" that Q3's condensed-history rewrite removes.
    `SKILL.md`'s "what this does and does not fix" region must gain the
    v0.3.12/`_send_graceful_signal`/backstop framing while its two verbatim
    survivor sentences stay intact.

    Expected RED reason: `AGENTS.md` still contains both falsified phrases
    verbatim today and does not mention `v0.3.12` anywhere; `SKILL.md`'s
    region does not mention `v0.3.12`, `_send_graceful_signal`, or any
    backstop/defence-in-depth framing.
    """
    agents_raw = AGENTS_MD.read_text(encoding="utf-8")
    agents_norm = _normalize(agents_raw)

    assert "no equivalent guard in the engine today" not in agents_norm
    assert (
        "upstream recommendation (not implemented in this repo)" not in agents_norm
    )
    assert "v0.3.12" in agents_norm
    assert "see the thread-leak note above" not in agents_norm
    assert "tests/test_thread_leak_regression.py" in agents_raw

    skill_norm = _normalize(SKILL_MD.read_text(encoding="utf-8"))
    start = skill_norm.index("what this does and does not fix")
    end = skill_norm.index("orphan worktree recovery")
    region = skill_norm[start:end]

    assert "v0.3.12" in region
    assert "_send_graceful_signal" in region
    assert re.search(r"backstop|defence-in-depth", region)
    assert "it does not eliminate transport drops" in region
    assert (
        "fails during argument resolution before any signal code runs at all"
        in region
    )


def test_worktree_remove_untracked_recipe_new_sentence_does_not_alter_branch_pin():
    """R6 edge (a): anti-false-positive pin -- the pre-existing "never
    deletes its branch, even with force=true" sentence must survive intact
    and stay free of "uncommitted", forcing the new claim to be its own
    separate sentence rather than a one-word edit to this one. Expected to
    already pass -- untouched by R6."""
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    start = doc.index(_R6_REGION_START)
    end = doc.index(_R6_REGION_END)
    region = doc[start:end]

    assert "never deletes its branch, even with force=true" in region
    sentence = _sentence_containing(region, "even with force=true")
    assert "uncommitted" not in sentence


def test_skill_md_orphan_recovery_recipe_keeps_force_true_with_uncommitted_rationale():
    """R6 edge (b): SKILL.md pinning (orchestrator addendum, "minor
    untestable::F1" resolution -- folded into implementation as a concrete
    rule). On the raw text of skills/worktree/SKILL.md's "Orphan worktree
    recovery" recipe, the `force=true` occurrence and the token
    "uncommitted" must fall within the same 300-character window, mirroring
    R4's window technique. Expected to already pass -- SKILL.md:493-495
    already ties `force=true` to the uncommitted-changes rationale; no
    R6 edit lands there."""
    text = SKILL_MD.read_text(encoding="utf-8")
    norm = _normalize(text)

    start = norm.index("an orphan is a linked worktree that exists on disk")
    end = norm.index("never recorded as owning one", start)
    recipe = norm[start:end]

    force_idx = recipe.index("force=true")
    window = recipe[max(0, force_idx - 300) : force_idx + 300]
    assert "uncommitted" in window


# ---------------------------------------------------------------------------
# WP #165 R1/R2: "Contract authoring quick reference" block (gap 1),
# front-loaded onto worktree_create (and a shorter one onto
# environment_start) so a truncating MCP client still delivers the
# load-bearing facts. Region delimiters are content-independent and
# identical for both docstrings (plan rev.3):
#   heading:  "Contract authoring quick reference"
#   sentinel: "Details for every point above follow below." (fixed sentinel,
#             last line of the block, byte-identical in both docstrings).
#
# Every test below is a driving test for WP #165's phase=tests dispatch:
# neither the heading nor the sentinel exists in any docstring yet (verified
# absent repo-wide at plan time), so `_quick_reference_region` raises
# `ValueError` ("substring not found") for every test that calls it -- the
# expected RED reason, attributable to the missing block content, not to an
# import/fixture/environment problem.
# ---------------------------------------------------------------------------

_QR_HEADING = "contract authoring quick reference"
_QR_SENTINEL = "details for every point above follow below."

# Per-item bullet cap for this dispatch (plan: "pick a concrete, exact
# number <=150" -- previously left as "~150 chars"). Checked against the
# *raw* (cleandoc'd, not whitespace-collapsed) docstring text, one physical
# source line at a time -- the normalized region collapses newlines so it
# cannot be used for a per-line check.
_QR_MAX_LINE_LEN = 150

# R3's "proximity window" around each file's WORKTREE_PORT_ mention (plan:
# "pick a fixed, exact window size, e.g. +/-500 chars"). Mirrors this file's
# existing R4 +/-300-char window convention, widened because R3 additionally
# requires three more tokens (ports:, an uppercase cue, WORKTREE_ID) to fall
# in the same window, not just one.
_QR_ENV_VAR_WINDOW = 500


def _quick_reference_region(doc: str) -> Tuple[int, int, str]:
    """Compute ``(start, end, block_text)`` for the "Contract authoring
    quick reference" block within a *normalized* (lower-cased,
    backtick/emphasis-stripped, whitespace-collapsed) docstring ``doc`` --
    mirrors this file's existing ``_normalize`` convention, matching WP
    #165 plan rev.3's region computation::

        start = norm_doc.index("contract authoring quick reference")
        end   = norm_doc.index("details for every point above follow below.") + len(sentinel)

    Raises ``ValueError`` (via ``str.index``) when either delimiter is
    absent -- the expected RED signal for every test below before the block
    is authored (WP #165 phase=implement work, not this dispatch's).
    """
    start = doc.index(_QR_HEADING)
    end = doc.index(_QR_SENTINEL) + len(_QR_SENTINEL)
    return start, end, doc[start:end]


def _raw_quick_reference_block(raw_doc: str) -> str:
    """Locate the quick-reference block in the *raw* (``inspect.cleandoc``'d
    but not whitespace-collapsed) docstring text, case-insensitively --
    needed for the per-line length budget (R1a edge case), since
    ``_quick_reference_region``'s normalized text has no newlines left to
    split on."""
    lower = raw_doc.lower()
    start = lower.index(_QR_HEADING)
    end = lower.index(_QR_SENTINEL, start) + len(_QR_SENTINEL)
    return raw_doc[start:end]


def test_worktree_create_quick_reference_fits_truncation_budget():
    """R1a driving test: worktree_create's quick-reference block must start
    near the top of the docstring (well before the plan's measured
    ~2,240-char truncation cut) and fit the plan's budget -- block content
    (heading through sentinel, inclusive) <= 1,000 chars, absolute block END
    <= 1,100.

    Expected RED reason: neither the heading nor the sentinel exists yet in
    worktree_create's docstring -- ``_quick_reference_region`` raises
    ``ValueError`` (substring not found).
    """
    raw_doc = _get_tool_docstring("worktree_create")
    doc = _normalize(raw_doc)
    start, end, _block = _quick_reference_region(doc)

    assert start < 200, f"heading starts too late (offset {start})"
    assert end <= 1100, f"block end exceeds budget (offset {end})"
    assert end - start <= 1000, f"block content exceeds 1000 chars ({end - start})"

    raw_block = _raw_quick_reference_block(raw_doc)
    for line in raw_block.splitlines():
        assert len(line) <= _QR_MAX_LINE_LEN, (
            f"quick-reference line exceeds {_QR_MAX_LINE_LEN} chars "
            f"({len(line)}): {line!r}"
        )


def test_environment_start_quick_reference_fits_truncation_budget():
    """R1a driving test, environment_start's shorter block: block content
    <= 500 chars, absolute block END <= 700 (the plan's own measured
    ~710-char cut for this docstring's CAUTION block).

    Expected RED reason: same as worktree_create's -- heading/sentinel
    absent today.
    """
    raw_doc = _get_tool_docstring("environment_start")
    doc = _normalize(raw_doc)
    start, end, _block = _quick_reference_region(doc)

    assert start < 200, f"heading starts too late (offset {start})"
    assert end <= 700, f"block end exceeds budget (offset {end})"
    assert end - start <= 500, f"block content exceeds 500 chars ({end - start})"

    raw_block = _raw_quick_reference_block(raw_doc)
    for line in raw_block.splitlines():
        assert len(line) <= _QR_MAX_LINE_LEN, (
            f"quick-reference line exceeds {_QR_MAX_LINE_LEN} chars "
            f"({len(line)}): {line!r}"
        )


def test_quick_reference_sentinel_is_unique_and_terminates_block():
    """R1h driving test: the sentinel line must appear exactly once per
    docstring, strictly after the heading, and exactly twice total in
    worktree.py's source (once per docstring). Edge case: the sentinel must
    never leak into SKILL.md/AGENTS.md.

    Expected RED reason: the sentinel string doesn't exist anywhere yet --
    ``count() == 1`` fails as ``0 == 1`` for the first tool checked.
    """
    for tool_name in ("worktree_create", "environment_start"):
        doc = _normalize(_get_tool_docstring(tool_name))
        count = doc.count(_QR_SENTINEL)
        assert count == 1, f"{tool_name}: sentinel must appear exactly once, found {count}"
        assert doc.index(_QR_SENTINEL) > doc.index(_QR_HEADING), (
            f"{tool_name}: sentinel must come after the heading"
        )

    source_lower = WORKTREE_PY.read_text(encoding="utf-8").lower()
    source_count = source_lower.count(_QR_SENTINEL)
    assert source_count == 2, (
        f"expected the sentinel exactly twice in worktree.py's source (once "
        f"per docstring), found {source_count}"
    )

    skill_text = SKILL_MD.read_text(encoding="utf-8").lower()
    agents_text = AGENTS_MD.read_text(encoding="utf-8").lower()
    assert _QR_SENTINEL not in skill_text, "sentinel must not leak into SKILL.md"
    assert _QR_SENTINEL not in agents_text, "sentinel must not leak into AGENTS.md"


def test_worktree_create_quick_reference_does_not_displace_existing_prose():
    """R1b: regression pin, not a driving test -- must PASS both before and
    after the quick-reference block lands. Six sentinel substrings from
    worktree_create's existing tail (including the R5 driving-test anchors
    already defined in this file) must survive, in their original relative
    order, once the new block is inserted at the very top of the docstring
    (right after the one-line summary) -- a pure prefix insertion preserves
    every existing substring's *relative* order even though every absolute
    offset shifts upward by the same amount.

    Expected to already PASS -- untouched by WP #165's test phase (which
    writes no production content), and the plan's insertion point (a
    prefix, not a mid-document splice) cannot reorder anything that already
    exists.
    """
    doc = _normalize(_get_tool_docstring("worktree_create"))

    anchors = [
        "returns the canonical worktree record",
        "caution: a worktree",
        _R5_REGION_START,
        _R5_REGION_END,
        'transport-level failure ("connection closed"): confirm before retrying',
        "honest limit: this tells you the worktree exists",
    ]
    indices = [doc.index(a) for a in anchors]
    assert indices == sorted(indices), (
        f"existing tail anchors out of order: {list(zip(anchors, indices))}"
    )


@pytest.mark.parametrize("tool_name", ["worktree_create", "environment_start"])
def test_quick_reference_states_isolation_none_forbids_blocks(tool_name):
    """R1c driving test: within the quick-reference block, the contract's
    full key set and the ``isolation: none`` prohibition must both be
    stated (plan item 3). The forbidden-key set was verified against the
    real installed validator rather than trusted blindly (the plan-critic
    flagged this as previously unverified): ``load_text("version: 1\\n"
    "isolation: none\\nseed_postprocess:\\n  - run: echo hi\\n")`` raises
    ``ContractValidationError`` with message "isolation: none forbids
    fields: seed_postprocess" against the installed ``lib_python_worktree``
    package, confirming ``seed_postprocess`` belongs in this forbidden set
    alongside ``setup:``/``start:``/``stop:``/``teardown:``/``ports:``.

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``
    -- neither the heading nor the sentinel exists in either docstring yet.
    """
    doc = _normalize(_get_tool_docstring(tool_name))
    start, end, region = _quick_reference_region(doc)

    assert "isolation: none" in region
    for token in ("setup:", "start:", "stop:", "teardown:", "ports:", "seed_postprocess:"):
        assert token in region, f"{tool_name}: quick reference must mention {token!r}"
    assert re.search(r"forbid|reject|invalid|schema error", region), (
        f"{tool_name}: quick reference must state a prohibition cue"
    )

    # Edge case: the tail's own pre-existing "isolation: none forbids ..."
    # sentence must survive, distinct from (and strictly after) the new
    # block -- not conflated with or displaced by it.
    tail_idx = doc.index("isolation: none forbids", end)
    assert tail_idx >= end


@pytest.mark.parametrize("tool_name", ["worktree_create", "environment_start"])
def test_quick_reference_states_named_start_steps_are_variants(tool_name):
    """R1d driving test: the block must state both the mechanism (named
    ``start:`` steps are selected via ``environment_start(variant=...)``)
    AND the plan-critic-flagged nuance that named steps do NOT run by
    default (plan item 4). Deliberately paraphrases the existing
    ``start_variants`` bullet's own wording (per the plan's fragility
    rules) rather than cloning ``"start_variants (always present, unlike
    warning)"`` verbatim.

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``.
    """
    doc = _normalize(_get_tool_docstring(tool_name))
    start, end, region = _quick_reference_region(doc)

    assert "environment_start(variant=" in region
    assert "start:" in region
    assert re.search(r"named|name:", region), (
        f"{tool_name}: quick reference must use a naming cue"
    )
    assert "variant" in region
    assert re.search(r"not run by default|do not run by default|not run unless", region), (
        f"{tool_name}: quick reference must state named steps don't run by default"
    )

    # Edge case: must not clone the existing start_variants bullet's own
    # anchor sentence verbatim -- paraphrase only (fragility rule).
    assert _R5_REGION_START not in region


@pytest.mark.parametrize("tool_name", ["worktree_create", "environment_start"])
def test_quick_reference_states_injected_env_vars(tool_name):
    """R1e driving test: the block must name all four injected env vars and
    the uppercase-derivation rule (plan item 5).

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``.
    """
    doc = _normalize(_get_tool_docstring(tool_name))
    start, end, region = _quick_reference_region(doc)

    for token in ("worktree_id", "worktree_path", "worktree_branch", "worktree_port_"):
        assert token in region, f"{tool_name}: quick reference must mention {token!r}"
    assert re.search(r"upper|uppercas", region), (
        f"{tool_name}: quick reference must state the uppercase-derivation rule"
    )

    if tool_name == "worktree_create":
        # Edge case: the R3 tail paragraph (documenting the same vars in
        # prose, added elsewhere by this same ticket) occurs at an index
        # greater than the block end -- the quick reference states it once,
        # up front; the tail paragraph is the detailed follow-up, not a
        # duplicate inside the block itself.
        tail_idx = doc.index("worktree_id", end)
        assert tail_idx >= end


def test_quick_reference_states_teardown_force_true():
    """R1f driving test: worktree_create's block must state that teardown
    normally needs ``worktree_remove(force=True)`` and frame that as the
    expected happy-path end state, not an emergency override (plan item 6).

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``.
    """
    doc = _normalize(_get_tool_docstring("worktree_create"))
    start, end, region = _quick_reference_region(doc)

    assert "worktree_remove" in region
    assert "force=true" in region
    assert re.search(r"expected|normal|routine", region), (
        "worktree_create quick reference must frame force=True as expected/normal"
    )


def test_quick_reference_teardown_force_true_absent_from_environment_start():
    """R1f edge case: environment_start's shorter block (items 3/4/5 only,
    per the plan) must NOT carry the teardown item -- that item is specific
    to worktree_create's checkout lifecycle.

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``
    (the block doesn't exist yet, so the absence check below is unreached
    until the driving test above turns green -- listed here as a
    forward-looking regression guard, matching this file's existing
    edge-case convention).
    """
    doc = _normalize(_get_tool_docstring("environment_start"))
    start, end, region = _quick_reference_region(doc)

    assert "worktree_remove" not in region


def test_quick_reference_states_required_version_and_isolation():
    """R1g driving test: worktree_create's block must state the required
    keys -- ``version: 1`` (the only accepted value) and ``isolation:``
    (``full``/``partial``/``none``) -- plan item 1.

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``.
    """
    doc = _normalize(_get_tool_docstring("worktree_create"))
    start, end, region = _quick_reference_region(doc)

    assert "version: 1" in region
    assert "isolation:" in region
    for value in ("full", "partial", "none"):
        assert value in region, f"quick reference must mention isolation value {value!r}"
    assert re.search(r"required|must", region), (
        "quick reference must state version/isolation are required"
    )

    # Edge case: no other version value is claimed acceptable.
    assert not re.search(r"version:\s*(?!1\b)\d", region), (
        "quick reference must not claim any version value other than 1"
    )


def test_quick_reference_lists_every_contract_top_level_key():
    """R2 driving test: every ``WorktreeContract.model_fields`` name (as
    installed) must appear inside worktree_create's quick-reference block --
    pinned to the real schema rather than a hand-maintained list, so a
    future contract-schema field addition trips this test instead of
    silently drifting out of sync with the docstring.

    Expected RED reason: ``_quick_reference_region`` raises ``ValueError``.
    """
    from lib_python_worktree import WorktreeContract

    field_names = list(WorktreeContract.model_fields.keys())
    assert "seed_postprocess" in field_names, (
        "sanity check: the installed schema must still declare "
        "seed_postprocess for this test's premise to hold"
    )

    doc = _normalize(_get_tool_docstring("worktree_create"))
    start, end, region = _quick_reference_region(doc)

    for field in field_names:
        if field in ("version", "isolation"):
            # Covered by R1g as required keys, not part of the "full
            # top-level key set" bullet (item 2) this test targets.
            continue
        assert field in region, f"quick reference must list contract key {field!r}"

    # Edge case: seed_postprocess carries a "not run by any tool" cue,
    # since it is schema-valid but no tool in this MCP surface ever runs it
    # (verified by grepping every manager.*() call site at plan time).
    idx = region.index("seed_postprocess")
    window = region[idx : idx + 200]
    assert re.search(r"not run by (any|this)|never run|not executed", window), (
        "seed_postprocess mention must note it is not run by any tool in "
        "this surface"
    )


def test_quick_reference_key_set_matches_environment_starts_existing_schema_list():
    """R2 edge case: cross-check worktree_create's new quick-reference key
    set against environment_start's own pre-existing "Contract file schema"
    paragraph (``"Top-level keys: version ... setup:, start:, stop:,
    teardown: ... and ports:"``), so the two documented key sets can never
    silently disagree.

    Expected RED reason: same as the driving test -- the quick-reference
    region doesn't exist in worktree_create yet.
    """
    doc_create = _normalize(_get_tool_docstring("worktree_create"))
    start, end, region = _quick_reference_region(doc_create)

    doc_start = _normalize(_get_tool_docstring("environment_start"))
    schema_start = doc_start.index("top-level keys: version")
    schema_end = doc_start.index("isolation rule:", schema_start)
    schema_region = doc_start[schema_start:schema_end]

    for token in ("setup:", "start:", "stop:", "teardown:", "ports:"):
        assert token in schema_region, (
            f"pre-condition failed: environment_start's existing schema "
            f"list must already mention {token!r}"
        )
        assert token in region, (
            f"worktree_create's quick reference must also mention {token!r}, "
            f"matching environment_start's existing schema list"
        )


# ---------------------------------------------------------------------------
# WP #165 R3: the four WORKTREE_* env vars the engine injects into contract
# steps (gap 3), documented outside the quick-reference block too -- in
# worktree.py's docstrings (prose, not just the block), SKILL.md, and
# AGENTS.md. Token checks are deliberately case-SENSITIVE against the *raw*
# (non-lower-cased) text: ``worktree_id`` (lower-case) is a pervasive,
# unrelated Python parameter/variable name throughout worktree.py, and also
# appears in existing docstring prose naming the engine's internal
# ``worktree_id`` parameter (see ``_addressing_error_text``'s docstring) --
# a case-insensitive check would find that pre-existing, unrelated mention
# and never go RED. ``WORKTREE_ID`` (upper-case, the actual injected env var
# spelling) has zero case-sensitive occurrences repo-wide today (verified).
# ---------------------------------------------------------------------------

_ENV_VAR_TOKENS = ("WORKTREE_ID", "WORKTREE_PATH", "WORKTREE_BRANCH", "WORKTREE_PORT_")

_DOC_FILES_FOR_ENV_VARS = {
    "worktree.py": WORKTREE_PY,
    "SKILL.md": SKILL_MD,
    "AGENTS.md": AGENTS_MD,
}


@pytest.mark.parametrize("file_key", ["worktree.py", "SKILL.md", "AGENTS.md"])
def test_docs_document_injected_worktree_env_vars(file_key):
    """R3 driving test: all four ``WORKTREE_*`` env vars injected by the
    engine must be documented (gap 3) in worktree.py's docstrings,
    SKILL.md, and AGENTS.md alike. The ``WORKTREE_PORT_`` mention's
    +/-500-char window must also name ``ports:``, an uppercase-derivation
    cue, and ``WORKTREE_ID`` -- so the port-slot-name-to-env-var-suffix
    derivation rule is stated together with the other vars, not just four
    bare tokens scattered anywhere in the file.

    Expected RED reason: none of these four tokens exist (case-sensitively)
    in any of the three files today (verified absent repo-wide at plan
    time).
    """
    path = _DOC_FILES_FOR_ENV_VARS[file_key]
    text = path.read_text(encoding="utf-8")

    for token in _ENV_VAR_TOKENS:
        assert token in text, f"{file_key} must document {token!r}"

    port_idx = text.index("WORKTREE_PORT_")
    window = text[max(0, port_idx - _QR_ENV_VAR_WINDOW) : port_idx + _QR_ENV_VAR_WINDOW]
    window_lower = window.lower()
    assert "ports:" in window_lower, f"{file_key}: WORKTREE_PORT_ window must name ports:"
    assert re.search(r"upper|uppercas", window_lower), (
        f"{file_key}: WORKTREE_PORT_ window must state the uppercase-derivation rule"
    )
    assert "WORKTREE_ID" in window, (
        f"{file_key}: WORKTREE_PORT_ window must also name WORKTREE_ID nearby"
    )


def test_environment_stop_cross_references_injected_env_vars():
    """R3 edge (a): environment_stop's docstring must cross-reference the
    injected ``WORKTREE_*`` vars (plan Approach/R3: "a cross-reference
    sentence in environment_stop's docstring"), even though
    environment_stop itself never spawns a process and so never injects
    them directly.

    Case-sensitive, same rationale as the driving test above:
    environment_stop's docstring already contains the lower-case substring
    "worktree_id" today (the engine-internal-parameter prose, unrelated to
    the env var), so a case-insensitive check on that one token alone would
    false-pass without this test ever going RED.

    Expected RED reason: none of the four upper-case tokens are present.
    """
    doc = _get_tool_docstring("environment_stop")
    for token in _ENV_VAR_TOKENS:
        assert token in doc, f"environment_stop must cross-reference {token!r}"


def test_environment_start_docs_pin_env_param_overrides_injected_values():
    """R3 edge (b): environment_start's docs must state that its ``env=``
    parameter can override an injected ``WORKTREE_*`` value -- mirrors the
    engine's own merge order in ``_build_worktree_env`` (identity/port vars
    first, then ``caller_env`` merged in last).

    Expected RED reason: no such pin exists in the docstring today
    (verified: no "env"..."overrid"/"overrid"..."env" proximity match).
    """
    doc = _normalize(_get_tool_docstring("environment_start"))
    assert re.search(r"env(?:=| parameter)[^.]{0,200}overrid", doc) or re.search(
        r"overrid[^.]{0,200}env(?:=| parameter)", doc
    ), "environment_start docs must state env= overrides injected WORKTREE_* values"


# ---------------------------------------------------------------------------
# WP #165 R5: worktree_remove's `force=True` reframed as the expected
# happy-path teardown flag (gap 4), not an emergency override.
# ---------------------------------------------------------------------------

# Boundaries for the `force:` parameter's own entry, chosen for this
# dispatch (plan: "define its region precisely") -- from the `force:`
# parameter label to the next parameter's label
# (`kill_blocking_processes:`), both unique substrings in the normalized
# docstring.
_FORCE_PARAM_REGION_START = "force: when true, removes the worktree even if it contains"
_FORCE_PARAM_REGION_END = "kill_blocking_processes: when true, attempts to terminate"


def test_worktree_remove_force_param_frames_dirty_checkout_as_expected():
    """R5 driving test (gap 4): worktree_remove's ``force:`` parameter
    entry must frame a dirty checkout needing ``force=True`` as the
    *expected* happy-path teardown state, not an emergency override.

    Expected RED reason: today this region is the bare mechanical
    description only ("removes the worktree even if it contains
    uncommitted changes. Defaults to False.") -- no "force=true" restated
    with an expectation-framing cue.
    """
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    start = doc.index(_FORCE_PARAM_REGION_START)
    end = doc.index(_FORCE_PARAM_REGION_END)
    region = doc[start:end]

    assert "force=true" in region, (
        "worktree_remove's force: parameter region must restate force=true "
        "explicitly, alongside the new expectation framing"
    )
    sentence = _sentence_containing(region, "force=true")
    assert re.search(r"expected|normal|routine|not an emergency", sentence), (
        f"expectation-framing cue missing from: {sentence!r}"
    )


def test_skill_md_states_force_true_is_normal_teardown_not_emergency():
    """R5 edge (b): SKILL.md must also reframe force=True as the routine
    happy-path teardown flag -- not scoped to the orphan-recovery recipe
    alone (that recipe's own force=true/uncommitted pairing is pinned
    separately by
    ``test_skill_md_orphan_recovery_recipe_keeps_force_true_with_uncommitted_rationale``
    above and must not be disturbed).

    Expected RED reason: no SKILL.md force=true mention today sits near an
    expectation-framing cue (verified: every existing force=true window in
    SKILL.md is False for this cue).
    """
    norm = _normalize(SKILL_MD.read_text(encoding="utf-8"))

    match = None
    for m in re.finditer(r"force=true", norm):
        window = norm[max(0, m.start() - 200) : m.start() + 200]
        if re.search(r"expected|normal|routine|not an emergency", window):
            match = m
            break
    assert match is not None, (
        "SKILL.md must state, near some force=true mention, that a dirty "
        "checkout needing force=True is expected/normal/routine, not an "
        "emergency override"
    )


# ---------------------------------------------------------------------------
# Ticket #176 (Q3, #111 half): the daemon-thread-leak narrative in
# tests/test_thread_leak_regression.py, pyproject.toml's timeout rationale
# comment, tests/test_pytest_timeout_config.py's module docstring, and
# AGENTS.md's chunk table all still describe the leak as a live, unbounded
# defect measured against v0.3.11 -- falsified by upstream ticket #148
# (shipped in the now-pinned v0.3.12), which introduces a persistent bounded
# query worker (_persistent_query_worker/_handle_scan_lock, capped by
# _handle_scan_max_live_workers) closing the residual per-scan leak. This
# block's four driving tests gate that rewrite; mirrors this file's existing
# _R176_SIGNAL_FORBIDDEN block's shape and reuses _normalize/
# _sentence_containing.
# ---------------------------------------------------------------------------


def _module_docstring(path: Path) -> str:
    """Return the raw (``ast.get_docstring``, default ``clean=True``) module
    docstring of the Python source file at *path* -- preserves blank-line
    paragraph breaks (unlike this file's whitespace-collapsing
    ``_normalize``), which the History-confinement checks below need."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree)
    assert doc, f"{path} has no module docstring"
    return doc


def test_thread_leak_tests_are_no_longer_xfail():
    """Behavioural requirement 1 (plan): the leak tests must stop being
    ``xfail`` once the v0.3.12 measurement plateaus. Scoped deliberately to
    the marker/decorator usage (``@pytest.mark.xfail`` and the
    ``_XFAIL_REASON`` constant), not a bare ``"xfail" not in source"`` --
    a legitimately labelled historical mention of the word inside the
    rewritten History paragraph must not be forbidden (the same pattern
    already accepted in ``tests/test_signal_resilience.py``). The survivor
    assertions prove the flip cannot be achieved by deleting or weakening
    the tests themselves.

    Expected RED reason: the file today contains
    ``@pytest.mark.xfail(strict=False, reason=_XFAIL_REASON)`` (line 306), a
    second ``@pytest.mark.xfail(`` decorator (line 473), and
    ``_XFAIL_REASON = (`` (line 257) -- all verified present at plan/test
    time.
    """
    source = THREAD_LEAK_TEST.read_text(encoding="utf-8")

    assert "@pytest.mark.xfail" not in source
    assert "_XFAIL_REASON" not in source

    # Survivors: the flip must not be achieved by deleting or weakening the
    # tests themselves.
    assert "skipif(" in source
    assert "timeout(300)" in source
    assert "growth <= 2" in source


_THREAD_LEAK_FORBIDDEN = (
    "still xfail (never xpass",
    "both xfail, matching v0.3.3's originally measured shape",
    "today's leak is linear and unbounded from the very first call",
)

_THREAD_LEAK_REQUIRED_ANY = ("_persistent_query_worker", "_handle_scan_lock")

# "v0.3.1" is deliberately checked with a "not followed by another digit"
# guard (see `_history_token_pattern`) rather than plain substring
# containment -- "v0.3.1" is itself a substring of "v0.3.10"/"v0.3.11"/
# "v0.3.12", so a naive `"v0.3.1" not in block` check would misfire against
# the v0.3.12 lead paragraph that every GREEN rewrite must contain.
_THREAD_LEAK_HISTORY_TOKENS = (
    "v0.3.1",
    "v0.3.3",
    "v0.3.10",
    "v0.3.11",
    "teardown.py:666",
    "teardown.py:693",
)

_THREAD_LEAK_HISTORY_TICKET_LINKS = ("#114", "#159", "#169")


def _history_token_pattern(token: str) -> "re.Pattern[str]":
    return re.compile(re.escape(token) + r"(?!\d)")


def test_thread_leak_module_docstring_describes_v0_3_12_state():
    """Behavioural requirement 2 (plan): the module docstring must lead with
    the v0.3.12/#148 state (citing the upstream ticket, the new bounded-
    worker internals, and the re-measured ``growth_series=``) and confine
    the v0.3.1..v0.3.11 narrative to a single labelled History block that
    keeps its ticket links. Also repairs the #112 cross-reference paragraph
    to cite upstream PR #151.

    Expected RED reason: the docstring today contains every forbidden phrase
    verbatim (the v0.3.11 doubled-rate paragraph, the v0.3.10 paragraph, and
    the "Known limitation" paragraph) and zero occurrences of "v0.3.12"
    (grep-verified: 0 hits repo-wide in this file) -- the first forbidden-
    phrase assertion below fails immediately. Separately, "history" occurs
    zero times in the docstring today (grep-verified), so the
    exactly-one-History-block check would also fail (0 != 1) once reached.
    """
    raw_doc = _module_docstring(THREAD_LEAK_TEST)
    norm = _normalize(raw_doc)

    for forbidden in _THREAD_LEAK_FORBIDDEN:
        assert forbidden not in norm, (
            f"falsified upstream-defect phrase must be removed: {forbidden!r}"
        )

    assert "v0.3.12" in norm
    assert "seretos/lib-python-worktree#148" in norm
    assert any(tok in norm for tok in _THREAD_LEAK_REQUIRED_ANY), (
        f"docstring must mention one of {_THREAD_LEAK_REQUIRED_ANY}"
    )
    assert "_handle_scan_max_live_workers" in norm
    assert "teardown.py:732" in norm

    v0312_idx = norm.index("v0.3.12")
    assert v0312_idx < 800, f"v0.3.12 must lead the docstring (found at {v0312_idx})"
    for older in ("v0.3.3", "v0.3.10", "v0.3.11"):
        if older in norm:
            assert v0312_idx < norm.index(older), (
                f"v0.3.12 must precede {older!r} in the docstring"
            )

    # History-confinement: split the *raw* (blank-line-preserving) docstring
    # into paragraphs -- _normalize collapses "\n\n" into a single space, so
    # this check deliberately does not use it.
    blocks = [b for b in raw_doc.split("\n\n") if b.strip()]
    history_blocks = [b for b in blocks if "history" in b.lower()]
    assert len(history_blocks) == 1, (
        f"expected exactly one History block, found {len(history_blocks)}"
    )
    history_block = history_blocks[0]
    assert len(history_block) <= 900, (
        f"History block too long ({len(history_block)} chars)"
    )

    non_history_blocks = [b for b in blocks if b is not history_block]
    for token in _THREAD_LEAK_HISTORY_TOKENS:
        pattern = _history_token_pattern(token)
        for block in non_history_blocks:
            assert not pattern.search(block), (
                f"{token!r} must be confined to the History block, found "
                f"elsewhere: {block[:80]!r}..."
            )

    assert any(link in history_block for link in _THREAD_LEAK_HISTORY_TICKET_LINKS), (
        "History block must keep at least one prior-bump ticket link "
        f"({_THREAD_LEAK_HISTORY_TICKET_LINKS})"
    )

    growth_match = re.search(r"growth_series=\[([0-9,\s]*)\]", raw_doc)
    assert growth_match is not None, (
        "the v0.3.12 section must cite a freshly measured growth_series=[...]"
    )
    growth_values = [int(v) for v in growth_match.group(1).split(",") if v.strip()]
    assert growth_values, "growth_series literal must not be empty"
    assert max(growth_values) <= 2

    send_sentence = _sentence_containing(norm, "_send_graceful_signal")
    assert "#151" in send_sentence, (
        "the sentence mentioning _send_graceful_signal must also cite #151"
    )


def test_timeout_rationale_is_not_falsified():
    """Behavioural requirement 3 (plan): the pytest-timeout rationale stops
    citing a live, untracked-down upstream leak. Checks two *scoped*
    regions, not two whole files -- a whole-file check on pyproject.toml
    would trivially pass because the v0.3.12 pin already sits on line 15,
    which would make the test worthless against an untouched rationale
    comment.

    Expected RED reason: ``pyproject.toml``'s comment block (between
    ``addopts = `` and ``timeout = 60``) contains "...useful diagnostics for
    tracking down the upstream leak." and no "v0.3.12"/"#148" inside that
    slice; ``tests/test_pytest_timeout_config.py``'s module docstring opens
    with "The suite has a load-dependent daemon-thread leak" -- both
    verified present verbatim at plan/test time.
    """
    pyproject_raw = PYPROJECT_TOML.read_text(encoding="utf-8")
    start = pyproject_raw.index("addopts = ")
    end = pyproject_raw.index("timeout = 60")
    rationale_slice = pyproject_raw[start:end]

    assert "tracking down the upstream leak" not in rationale_slice
    assert "v0.3.12" in rationale_slice
    assert "#148" in rationale_slice
    assert "backstop" in rationale_slice or "defence-in-depth" in rationale_slice

    # Survivors, checked against the whole file (unambiguous, not scope-
    # sensitive the way the forbidden/required tokens above are).
    assert "timeout = 60" in pyproject_raw
    assert "#105" in pyproject_raw

    timeout_doc = _normalize(_module_docstring(TIMEOUT_CONFIG_TEST))

    assert "the suite has a load-dependent daemon-thread leak" not in timeout_doc
    assert "v0.3.12" in timeout_doc
    assert "#148" in timeout_doc
    assert "backstop" in timeout_doc or "defence-in-depth" in timeout_doc
    assert "#105" in timeout_doc


def test_agents_md_suite_counts_carry_no_xfail_status():
    """Behavioural requirement 4 (plan): ``AGENTS.md``'s chunk table must
    carry no stale XFAIL/XPASS status once the markers are flipped --
    scanned across the *entire* file (safe because ``AGENTS.md`` already
    delegates all leak-test status to the module docstring elsewhere, so any
    xfail/xpass token left in this file is by construction a re-divergence).
    Also asserts the leak-test file citation survives, so the fix cannot be
    achieved by deleting the table row that names it.

    Expected RED reason: ``AGENTS.md:362`` reads "58 passed + 2 xfailed" and
    ``:364`` reads "454 passed + 2 xfailed" (grep-verified: these are the
    only two ``xfail``/``xpass`` hits in the whole file today).
    """
    agents_raw = AGENTS_MD.read_text(encoding="utf-8")

    assert not re.search(r"x(?:fail|pass)", agents_raw, re.IGNORECASE)
    assert "tests/test_thread_leak_regression.py" in agents_raw
