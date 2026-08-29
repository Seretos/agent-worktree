"""Executable evidence for ticket #116 ("Connection closed" transport errors).

A tool call can die with a transport-level error ("Connection closed", "MCP
error -32000") *before* its JSON-RPC response reaches the caller. The
response is lost; the operation may have fully landed anyway. Ticket #112
already fixed one concrete server-death mechanism (a stray Windows
CTRL_BREAK_EVENT reaching this server itself -- see
``tests/test_signal_resilience.py``) but that is not, and cannot be, a fix
for the transport drop in general: the transport itself is outside this
repo's reach, and a first ``environment_start()`` call with neither
``environment_id`` nor ``checkout_path`` drops before any signal-adjacent
code runs at all.

This module proves the ticket #116 mitigation actually shipped:

- **R1** -- ``worktree_create``'s new ``DuplicateWorktreeError`` handler
  names the landed environment inline (``existing_environment_id`` /
  ``existing_path`` tokens), so a blind retry of a lost create response is
  self-diagnosing instead of a bare "already exists" message.
- **R2** -- every mutating tool's docstring (``worktree_create``,
  ``worktree_remove``, ``environment_start``, ``environment_stop``) carries
  a "confirm before retrying" read-back recipe naming ``environment_list``.
- **R3** -- ``environment_list``'s docstring documents both its own
  retry-safety (it never writes state) and the three transient fields
  (``stop_attempt``, ``killed_pids``, ``shadowed_contract``) that can never
  serve as read-back evidence because they are never persisted to
  ``state.yaml``.

Local helpers mirror ``tests/test_signal_resilience.py``'s
``_make_tool_fixtures``/``_make_repo`` precedent -- kept local to this file
rather than imported across test modules, per the approved plan.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from lib_python_worktree import InMemoryStateStore, ManagerConfig, WorktreeManager


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(base: Path, name: str = "src-repo") -> Path:
    """Build a real temp git repo with a committed README.

    Local copy of the equivalent helper in test_environment_tools.py /
    test_signal_resilience.py -- kept local per the approved plan.
    """
    repo = base / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def _make_tool_fixtures(tmp_path: Path):
    """Return (mgr, fn_map) for tool-layer tests.

    Local copy of the helper in tests/test_worktree_tools.py:426-439 /
    tests/test_signal_resilience.py:147-166 -- kept local per the approved
    plan.
    """
    from mcp.server.fastmcp import FastMCP
    from worktree_plugin.tools.worktree import register

    store_root = tmp_path / "store"
    mgr = WorktreeManager(
        config=ManagerConfig(store_root=store_root),
        state=InMemoryStateStore(),
    )
    mcp = FastMCP("test")
    register(mcp, mgr)
    fns = {name: t.fn for name, t in mcp._tool_manager._tools.items()}
    return mgr, fns


# ---------------------------------------------------------------------------
# R1: worktree_create's DuplicateWorktreeError handler names the landed
# environment inline.
# ---------------------------------------------------------------------------


def test_duplicate_create_names_existing_environment_id(tmp_path: Path):
    """Driving test (RED before the production change, GREEN after).

    A blind retry of a worktree_create whose response was lost to a
    transport drop must be self-diagnosing: the raised ValueError must
    carry the landed environment's id and path as machine-readable tokens,
    not just the engine's bare "already exists" text.

    Deliberately asserts the FULL quoted token
    (``existing_environment_id: "<id>"``), not a bare substring check for
    the id alone -- the id embeds the branch slug ("feat-x"), which already
    appears unavoidably in the engine's own bare message, so a bare-id
    assertion would pass even if the wrapper never added the new token at
    all (the "tracked"/"untracked" incidental-match trap called out in the
    ticket's environment notes).
    """
    repo = _make_repo(tmp_path)
    mgr, fns = _make_tool_fixtures(tmp_path)

    record = fns["worktree_create"](repo_root=str(repo), branch="feat/x")
    assert "error" not in record, f"first create failed: {record}"

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root=str(repo), branch="feat/x")

    message = str(exc_info.value)
    assert f'existing_environment_id: "{record["id"]}"' in message, (
        f"expected the full quoted existing_environment_id token in the "
        f"duplicate-create error message, got: {message!r}"
    )
    assert f'existing_path: "{record["path"]}"' in message, (
        f"expected the full quoted existing_path token in the duplicate-"
        f"create error message, got: {message!r}"
    )


def test_duplicate_create_lookup_miss_falls_back_to_bare_message(
    tmp_path: Path, monkeypatch
):
    """Edge (a): when the wrapper's own read-back lookup (after catching
    DuplicateWorktreeError) finds nothing, it must fall back to the
    engine's bare message -- never invent an id, never fabricate tokens."""
    repo = _make_repo(tmp_path)
    mgr, fns = _make_tool_fixtures(tmp_path)

    created = fns["worktree_create"](repo_root=str(repo), branch="feat/miss")
    assert "error" not in created

    original_find_by_branch = mgr.state.find_by_branch
    calls = {"n": 0}

    def _patched(repo_root: str, branch: str):
        calls["n"] += 1
        if calls["n"] == 1:
            # This first call is the ENGINE's own duplicate-branch check
            # inside manager.create() -- must behave normally so
            # DuplicateWorktreeError is actually raised.
            return original_find_by_branch(repo_root, branch)
        # The second call is the wrapper's own best-effort read-back lookup
        # in its new `except DuplicateWorktreeError` handler -- simulate it
        # missing.
        return None

    monkeypatch.setattr(mgr.state, "find_by_branch", _patched)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root=str(repo), branch="feat/miss")

    message = str(exc_info.value)
    assert "existing_environment_id" not in message
    assert "existing_path" not in message
    assert "already exists" in message


def test_duplicate_create_lookup_raise_falls_back_to_bare_message(
    tmp_path: Path, monkeypatch
):
    """Edge (b): when the wrapper's own read-back lookup raises, the
    handler must swallow it and fall back to the bare engine message --
    never let a new (unrelated) exception type escape."""
    repo = _make_repo(tmp_path)
    mgr, fns = _make_tool_fixtures(tmp_path)

    created = fns["worktree_create"](repo_root=str(repo), branch="feat/boom")
    assert "error" not in created

    original_find_by_branch = mgr.state.find_by_branch
    calls = {"n": 0}

    def _patched(repo_root: str, branch: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return original_find_by_branch(repo_root, branch)
        raise RuntimeError("boom -- diagnostics lookup exploded")

    monkeypatch.setattr(mgr.state, "find_by_branch", _patched)

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root=str(repo), branch="feat/boom")

    message = str(exc_info.value)
    assert "existing_environment_id" not in message
    assert "existing_path" not in message
    assert "already exists" in message


def test_duplicate_create_untracked_conflict_stays_token_free(tmp_path: Path):
    """Edge (c): an *untracked* on-disk worktree already holding the branch
    (created via raw git, bypassing the state store) must NOT be found by
    find_by_branch -- so the create attempt proceeds past the duplicate
    guard, git itself refuses (branch already checked out elsewhere), and
    the engine raises BranchAlreadyCheckedOutError -- a structurally
    different exception that the new `except DuplicateWorktreeError` clause
    must not intercept. The resulting message stays token-free."""
    repo = _make_repo(tmp_path)
    mgr, fns = _make_tool_fixtures(tmp_path)

    # Create an untracked linked worktree directly via git, bypassing the
    # state store entirely, so `find_by_branch` cannot find it.
    other_checkout = tmp_path / "manual-checkout"
    _git(
        "worktree", "add", "-b", "feat/untracked", str(other_checkout), "main",
        cwd=repo,
    )

    with pytest.raises(ValueError) as exc_info:
        fns["worktree_create"](repo_root=str(repo), branch="feat/untracked")

    message = str(exc_info.value)
    assert "existing_environment_id" not in message
    assert "existing_path" not in message


def test_duplicate_create_different_branch_still_succeeds(tmp_path: Path):
    """Edge (d): the new exception handler must not interfere with the
    ordinary, non-duplicate path -- creating a second, differently-named
    worktree in the same repo still succeeds normally."""
    repo = _make_repo(tmp_path)
    mgr, fns = _make_tool_fixtures(tmp_path)

    first = fns["worktree_create"](repo_root=str(repo), branch="feat/one")
    assert "error" not in first

    second = fns["worktree_create"](repo_root=str(repo), branch="feat/two")
    assert "error" not in second
    assert second["id"] != first["id"]


# ---------------------------------------------------------------------------
# R2: every mutating tool's docstring carries a "confirm before retrying"
# read-back recipe.
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Strip Markdown/RST emphasis markup and collapse whitespace -- mirrors
    tests/test_plugin_manifest.py's `_normalize` helper so prose assertions
    match semantic tokens via regex, not exact formatting."""
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
    return doc


_MUTATING_TOOLS = ("worktree_create", "worktree_remove", "environment_start", "environment_stop")


@pytest.mark.parametrize("tool_name", _MUTATING_TOOLS)
def test_transport_failure_readback_is_documented(tool_name: str):
    """Driving test (RED before the docstring edits, GREEN after).

    Every mutating tool's docstring must, within a bounded window around a
    transport-drop cue ("connection closed" / "transport"), also mention
    `environment_list` (the read-back tool) and a retry cue -- proving the
    recipe actually connects "this call can drop silently" to "here is how
    you find out" rather than merely mentioning the two ideas somewhere
    unrelated in a long docstring.
    """
    doc = _normalize(_get_tool_docstring(tool_name))

    transport_cue = re.compile(r"connection closed|transport")
    match = transport_cue.search(doc)
    assert match, (
        f"{tool_name}'s docstring does not mention a transport-drop cue "
        f"(connection closed / transport) at all"
    )

    window_start = max(0, match.start() - 500)
    window = doc[window_start : match.end() + 500]

    assert "environment_list" in window, (
        f"{tool_name}'s transport-failure block does not name "
        f"environment_list as the read-back tool nearby"
    )
    assert re.search(r"retry|blind", window), (
        f"{tool_name}'s transport-failure block does not mention retrying "
        f"nearby"
    )


@pytest.mark.parametrize(
    "tool_name,required_fields",
    [
        ("worktree_create", ("tracked", "branch")),
        ("worktree_remove", ("not_found", "orphaned")),
        ("environment_start", ("pids", "already_running")),
        ("environment_stop", ("pids", "not_running")),
    ],
)
def test_transport_failure_readback_names_the_deciding_fields(
    tool_name: str, required_fields: tuple[str, str]
):
    """Edge coverage: each tool's read-back recipe must name the specific
    field(s) that decide "landed or not" for that tool -- not just gesture
    vaguely at environment_list.

    Uses a word-boundary regex for every required token so a short token
    like "tracked" cannot pass by incidentally matching inside "untracked"
    (the exact trap called out in the ticket's environment notes) -- and
    "orphaned"/"not_found"/etc. are all long enough to be unambiguous, but
    are still matched with \\b for consistency.
    """
    doc = _normalize(_get_tool_docstring(tool_name))
    for field in required_fields:
        pattern = re.compile(r"\b" + re.escape(field) + r"\b")
        assert pattern.search(doc), (
            f"{tool_name}'s docstring must name {field!r} (word-boundary "
            f"match, not an incidental substring) as part of its read-back "
            f"recipe"
        )


def test_environment_stop_transport_block_documents_the_raise_path():
    """Driving test (RED before the docstring fix, GREEN after) -- fix for a
    blocking defect found in review of the #116 work.

    environment_stop's transport-failure block used to close with "a blind
    retry is safe: it either returns {code: not_running} or performs a
    graceful no-op" -- phrased as an exhaustive disjunction. It is not: the
    same call can instead RAISE ``ValueError`` when the target fails to
    resolve (``CheckoutTargetError``, ``VariantResolutionError``,
    ``InvalidRepoError``, or the generic ``WorktreeError``/
    ``ProcessLifecycleError`` tail all raise rather than return a soft
    dict). A caller who branches on ``code`` without knowing the call can
    raise instead is exactly the failure mode ticket #116 exists to
    prevent.

    The window is anchored at the "transport-level failure" section header
    and runs to the end of the docstring (that section is always the last
    one in every mutating tool's docstring) -- NOT a whole-docstring
    substring search -- because "raise"/"ValueError" already appear earlier
    in this same docstring for unrelated reasons (the "Addressing the
    target" and "role vs variant" sections both discuss raising
    ValueError). A whole-docstring check would pass for the wrong reason;
    anchoring the window at the header proves the raise path is documented
    specifically as part of the retry recipe, not merely mentioned
    somewhere else in a long docstring.
    """
    doc = _normalize(_get_tool_docstring("environment_stop"))

    header = re.search(r"transport-level failure", doc)
    assert header, (
        "environment_stop's docstring must have a "
        "'Transport-level failure' section"
    )
    window = doc[header.start() :]

    assert re.search(r"\bvalueerror\b", window), (
        "environment_stop's transport-failure block must name ValueError "
        "as a possible outcome of a blind retry -- not only the soft "
        "`code` outcomes"
    )
    assert re.search(r"\braises?\b", window), (
        "environment_stop's transport-failure block must state that a "
        "blind retry can RAISE, not only return a soft error dict"
    )
    assert not re.search(r"it either returns.{0,120}or performs", window), (
        "environment_stop's transport-failure block must not present the "
        "two soft-return outcomes as an exhaustive disjunction "
        "('it either ... or ...') now that a raise is documented as a "
        "third, separate outcome"
    )


def test_worktree_remove_readback_warns_against_the_removed_path():
    """Negative guard: worktree_remove's transport-failure block must warn
    that reading back with the REMOVED checkout_path itself is misleading
    (it raises the same "does not exist" text a typo would), not just
    silently omit the caveat."""
    doc = _normalize(_get_tool_docstring("worktree_remove"))
    assert "does not exist" in doc
    assert re.search(r"typo|not evidence", doc), (
        "worktree_remove's docstring must explicitly warn that a "
        "'does not exist' error from retrying with the removed "
        "checkout_path is indistinguishable from an ordinary typo, and is "
        "therefore not evidence of anything"
    )


def test_environment_start_staleness_caveat_is_inside_the_heuristic_window():
    """Negative guard: the start_log_path/returncode heuristic's staleness
    limit must sit INSIDE the same discussion as the heuristic itself (per
    the plan's explicit ordering requirement), not trail off as a
    disconnected footnote elsewhere in the docstring.

    Checked by requiring a staleness cue (stale|leftover) to appear within
    350 chars AFTER *some* `start_log_path` mention -- iterated over every
    occurrence (the docstring mentions start_log_path more than once, e.g.
    also in its unrelated "Fields of note" section), rather than just the
    first, since only the occurrence inside the heuristic discussion is
    expected to satisfy this."""
    doc = _normalize(_get_tool_docstring("environment_start"))
    matches = list(re.finditer(r"start_log_path", doc))
    assert matches, "environment_start's docstring must mention start_log_path"

    found = False
    for match in matches:
        window = doc[match.end() : match.end() + 350]
        if re.search(r"stale|leftover", window):
            found = True
            break
    assert found, (
        f"environment_start's docstring must mention staleness/leftover "
        f"within 350 chars after some start_log_path mention, so the "
        f"caveat cannot be demoted to a trailing footnote"
    )


# ---------------------------------------------------------------------------
# R3: environment_list documents what it never populates, and that it is
# always safe to retry.
# ---------------------------------------------------------------------------


def test_environment_list_documents_transient_fields():
    """Driving test (RED before the docstring edit, GREEN after).

    environment_list's docstring must name all three fields that are
    structurally never persisted to state.yaml (stop_attempt, killed_pids,
    shadowed_contract) together with a transience cue, and must separately
    state that retrying environment_list itself is always safe.

    Ticket #181: the pinned engine's v0.3.13 bump removed
    ``WorktreeRecord.orphan_scan`` entirely (it existed from the v0.3.11
    bump, ticket #169, through v0.3.12) -- a breaking upstream change, so
    the docstring must no longer name it, and this test asserts the
    negative explicitly rather than just dropping it from the positive
    list above."""
    doc = _normalize(_get_tool_docstring("environment_list"))

    for field in ("stop_attempt", "killed_pids", "shadowed_contract"):
        assert field in doc, (
            f"environment_list's docstring must name {field!r} as a field "
            f"it never populates"
        )
    assert "orphan_scan" not in doc, (
        "environment_list's docstring must not mention orphan_scan -- "
        "ticket #181's v0.3.13 bump removed WorktreeRecord.orphan_scan "
        "entirely, so the field no longer exists to document"
    )
    assert re.search(r"transient|never|not persisted", doc), (
        "environment_list's docstring must carry a transience cue "
        "(transient/never/not persisted) near the never-populated fields"
    )
    assert "never writes state" in doc, (
        "environment_list's docstring must state that it never writes "
        "state, which is what makes retrying it always safe"
    )


def test_environment_list_entry_never_fabricates_transient_fields(tmp_path: Path):
    """Narrow behavioural companion (InMemoryStateStore-safe): a freshly
    created record's environment_list entry must show the three transient
    fields at their empty/None defaults, never a fabricated value -- kept
    deliberately narrow (no yaml round-trip assertion, since these fixtures
    are in-memory and reconcile() is a no-op there)."""
    repo = _make_repo(tmp_path)
    mgr, fns = _make_tool_fixtures(tmp_path)

    created = fns["worktree_create"](repo_root=str(repo), branch="feat/entry")
    assert "error" not in created

    listing = fns["environment_list"](path=str(repo))
    entry = next(e for e in listing if e["id"] == created["id"])

    assert entry["killed_pids"] == []
    assert entry["stop_attempt"] is None
    assert entry["shadowed_contract"] is None
