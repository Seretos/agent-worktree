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

import inspect
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "skills" / "worktree" / "SKILL.md"


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
