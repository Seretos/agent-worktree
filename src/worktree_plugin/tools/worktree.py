"""FastMCP tools for the worktree/environment lifecycle (ticket #99).

Ticket #99 splits the six conflated ``worktree_*`` tools into a five-tool
surface along the two real lifecycles a checkout goes through:

- **Checkout lifecycle** (``worktree_create`` / ``worktree_remove``) --
  creating or deleting a *directory*: a git worktree checkout on disk.
- **Environment lifecycle** (``environment_list`` / ``environment_start`` /
  ``environment_stop``) -- managing the *process* running against a
  checkout. Critically, this now covers **any** checkout, including the
  repo's own primary/main clone, which is an environment like any other --
  it just never gets created or removed by this plugin (it already exists
  before the plugin ever runs, and it is never deleted by it).

This is a **hard, non-backward-compatible break**: ``worktree_list``,
``worktree_get``, ``worktree_start``, and ``worktree_stop`` no longer exist.
There are no aliases and no deprecation window. ``worktree_create`` and
``worktree_remove`` keep their names because they still own exactly the
checkout lifecycle. ``.seretos/*.yml`` and the ``WORKTREE_*`` environment
variables injected into spawned processes are entirely unaffected by this
split.

**Deliberate, documented deviation from ticket #99's "final" signatures.**
The ticket specifies id-only ``environment_start``/``environment_stop``
signatures. That cannot satisfy the ticket's own AC1: a primary checkout's
id is ``primary_id_for(repo_root)`` -- a one-way SHA-256 hash of the repo
root -- and before the first ``environment_start()`` call ever
materialises a record for it, nothing maps that hash back to a path, so an
id-only cold start of a primary is structurally impossible. Both lifecycle
tools therefore also accept ``checkout_path``, a strict *superset* of the
id-only surface: every existing id-only call keeps working byte-for-byte,
and ``checkout_path`` is the only way to address a primary that has never
been started. See each tool's docstring, ``AGENTS.md``, and
``skills/worktree/SKILL.md`` for the full addressing contract.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    CONTRACT_FILENAME,
    CheckoutTargetError,
    ContractError,
    DuplicateWorktreeError,
    EnvironmentEntry,
    InvalidRepoError,
    KilledProcessInfo,
    PrimaryCheckoutError,
    ProcessAlreadyRunningError,
    ProcessLifecycleError,
    ProcessNotRunningError,
    SetupFailedError,
    SetupOutcome,
    VariantResolutionError,
    WorktreeDirLockedError,
    WorktreeError,
    WorktreeManager,
    WorktreeNotFoundError,
    WorktreeRecord,
    WorktreeRemovalBlockedError,
    classify_checkout,
    load as load_contract,
    primary_id_for,
)


# Matches the synthesised-id suffix minted by ``untracked_id_for()`` --
# mirrors ``lib_python_worktree.core.manager._UNTRACKED_ID_RE`` (not
# re-exported from the package, so duplicated here). Used to decide, in the
# ``worktree_remove`` not-found soft-error path, whether the engine's
# exception text is actually informative (it names ``checkout_path`` as the
# remedy only when the looked-up id is untracked-shaped) or just restates
# the id back (an ordinary unknown id), in which case it is dropped to keep
# the original bare error format (ticket #113 review fix).
#
# Known, accepted, inherited edge case: this regex is a byte-identical copy
# of the pinned engine's own private id-shape heuristic, not an
# independently invented one. In the extremely unlikely case a
# legitimately-tracked id happens to have this exact shape (e.g. a branch
# slug ending in "-untracked-" followed by 8 lowercase hex characters),
# this wrapper's decision to append {exc} will simply mirror whatever the
# pinned lib_python_worktree engine itself already does for that same id --
# the engine raises its more informative "not found" message for exactly
# the ids this pattern matches. That can't be "fixed" here without making
# the wrapper's error-format decision disagree with the engine's own
# exception text for the same id, so it is intentionally left as-is.
_UNTRACKED_ID_RE = re.compile(r"-untracked-[0-9a-f]{8}$")


def _record_to_dict(record: WorktreeRecord) -> Dict[str, Any]:
    return asdict(record)


def _addressing_error_text(
    exc: CheckoutTargetError, *, tool_name: str, hint: str
) -> str:
    """Re-word the engine's ``CheckoutTargetError`` into a wrapper-native
    addressing-error message for ``tool_name``.

    The engine's own message names its internal ``worktree_id`` parameter
    and describes the contract in engine-API vocabulary (``start()``/
    ``stop()``/``remove()``), neither of which matches this wrapper's actual
    ``environment_id`` parameter or the calling tool's own name. This
    re-words the message to name ``environment_id`` and ``tool_name``
    instead -- addressing BEHAVIOUR is entirely unchanged (resolution is
    still the engine's job; this wrapper still performs no validation of the
    pair itself), only the text presented to callers changes.

    ``exc.reason`` is one of ``"missing"`` (neither ``environment_id`` nor
    ``checkout_path`` was given) or ``"id_mismatch"`` (both were given but
    disagree). Any other/future ``reason`` value falls through to a generic,
    wrapper-native message -- deliberately never ``str(exc)``, which would
    re-leak the engine's ``worktree_id`` wording straight through.
    """
    if exc.reason == "missing":
        return (
            f"{tool_name} requires either environment_id or checkout_path "
            f"to address a target; neither was given. {hint}"
        )
    if exc.reason == "id_mismatch":
        return (
            f"checkout_path '{exc.checkout_path}' resolved to id "
            f"'{exc.resolved_id}', which does not match the given "
            f"environment_id '{exc.worktree_id}'."
        )
    return (
        f"{tool_name} could not resolve environment_id/checkout_path to a "
        f"single target. {hint}"
    )


def _invalid_path_error_text(exc: InvalidRepoError, *, param_name: str) -> str:
    """Re-word the engine's ``InvalidRepoError`` into a wrapper-native
    invalid-path message naming ``param_name`` (e.g. ``checkout_path``)
    instead of the engine-internal ``repo_root`` (ticket #123).

    Deliberate divergence from ``_addressing_error_text``'s allow-list
    policy above. ``_addressing_error_text`` can safely special-case (and
    drop) unknown ``CheckoutTargetError`` reason text because that
    exception carries *structured* reason codes (``"missing"`` /
    ``"id_mismatch"``) plus separate ``worktree_id``/``checkout_path``/
    ``resolved_id`` attributes it can rebuild a full message from.
    ``InvalidRepoError`` has no such structure -- its ``reason`` string
    *is* the entire diagnostic (e.g. ``"not a git repository: ..."``,
    ``"unexpected 'git rev-parse' output: ..."``), so dropping or
    generically replacing it would weaken the error, which this ticket
    forbids. Instead this performs a mechanical, ``\\b``-anchored token
    substitution applied to *every* reason -- known or unknown/future --
    which satisfies "do not weaken the error" by construction (no detail
    is ever lost) and keeps working for engine reasons that don't exist
    yet. The f-string otherwise mirrors the engine's own
    ``InvalidRepoError.__init__`` construction byte-for-byte apart from
    the substituted token.
    """
    reason = re.sub(r"\brepo_root\b", param_name, exc.reason)
    return f"invalid {param_name} {exc.repo_root!r}: {reason}"


def _ensure_contract_copy_ignored(contract_dir: Path) -> None:
    """Make ``contract_dir`` (the ``.seretos/`` copy ``worktree_create``
    just wrote into a *new worktree checkout*) invisible to git.

    Ticket #110 (Befund 2): the create-time convenience copy of
    ``.seretos/`` (see the "Contract file" section of ``worktree_create``'s
    docstring) is untracked by definition -- that is exactly why the copy
    was needed in the first place. An untracked directory left behind makes
    ``git status`` report the checkout as dirty, which in turn makes ``git
    worktree remove`` refuse to run without ``force=True`` -- so a freshly
    created, otherwise-untouched worktree could never be removed with the
    default ``force=False``.

    The fix: write a self-ignoring ``.gitignore`` into the copied
    directory. A bare ``*`` pattern in a directory's own ``.gitignore``
    matches every entry in that directory *including the ``.gitignore``
    file itself* (gitignore glob matching applies to dotfiles too), so git
    reports nothing for ``.seretos/`` at all -- neither tracked nor
    untracked -- and the worktree stays clean.

    This must never touch anything under ``repo_root``: it only ever
    receives the *destination* (in-worktree) contract directory, never the
    source. Writing to the shared ``.git/info/exclude`` was considered and
    rejected -- for a linked worktree that path maps to the *common* git
    dir, so it would silently mutate the user's main clone and every
    sibling worktree.

    Idempotent: this helper's own marker comment (``header`` below) is the
    only signal used to detect "already ran" -- if ``<contract_dir>/.gitignore``
    already contains it, the file is left untouched. A bare ``*`` line is
    *not* treated as sufficient by itself: gitignore matching is
    order-sensitive (the last matching pattern wins), so a source
    ``.seretos/.gitignore`` that happens to contain ``*`` followed by a
    later negation (e.g. ``!worktree-setup.yml``) would leave that file
    un-ignored, and detecting "already self-ignoring" from the bare ``*``
    alone would then wrongly skip appending the override. When the marker
    is absent -- whether the file doesn't exist yet, or it exists without
    ever having been touched by this helper -- the self-ignoring block is
    always appended at the end (never clobbering existing content). Since
    the last matching pattern wins, appending ``*`` last is precisely what
    makes it override any earlier negation in the pre-existing content.
    """
    gitignore_path = contract_dir / ".gitignore"
    header = "# Ticket #110: keep this create-time copy out of git status.\n"
    if gitignore_path.exists():
        # Read tolerantly, for detection only. A pre-existing .gitignore that
        # is not valid UTF-8 (e.g. cp1252/Latin-1, plausible on a Windows
        # box) must not crash worktree_create with an unwrapped
        # UnicodeDecodeError -- and UnicodeDecodeError is a ValueError
        # subclass, not an OSError, so it would slip past the `except
        # OSError` around this helper's call site. This decoded text is used
        # only to check the idempotency marker and the trailing-newline
        # separator below; it is never written back, so a lossy decode here
        # can never corrupt the file's original bytes.
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
        if header in existing:
            return
        separator = "" if existing.endswith("\n") or existing == "" else "\n"
        # Append rather than rewrite: this leaves every pre-existing byte in
        # the file untouched, so a non-UTF-8 (or otherwise unusual) existing
        # .gitignore is never round-tripped through the tolerant decode
        # above and cannot be corrupted by it.
        with open(gitignore_path, "a", encoding="utf-8") as f:
            f.write(f"{separator}\n{header}*\n")
    else:
        gitignore_path.write_text(f"{header}*\n", encoding="utf-8")


def _derive_setup_status(setup_outcome: Optional[SetupOutcome]) -> str:
    """Map a ``WorktreeRecord.setup_outcome`` to a coarse setup-health
    signal, fully decoupled from ``record.status`` (ticket #117).

    ``record.status`` is continuously rewritten by ``create``/``start``/
    ``stop``/``reconcile`` for entirely different purposes and does not
    answer "how did the ``setup:`` hook itself end?" once later calls have
    moved ``status`` on -- so this deliberately never reads ``status``, not
    even as a fallback for legacy records.

    - ``None`` -- the ``setup:`` hook was never reached (a record predating
      ``setup_outcome``, an adopted record, or a synthesised entry) --
      ``"unknown"``.
    - otherwise -- ``setup_outcome.status`` verbatim (``"completed"``,
      ``"failed"``, ``"skipped"``, or any forward-compatible future engine
      value) -- passed through as-is rather than mapped through an
      if/elif chain, so an unrecognised future status is preserved rather
      than rejected.
    """
    if setup_outcome is None:
        return "unknown"
    return setup_outcome.status


def _contract_diagnostics(record: WorktreeRecord, role: str) -> Dict[str, Any]:
    """Diagnose whether ``environment_start`` actually read a contract, and
    from where, so "nothing ran" outcomes are distinguishable from a real
    start (ticket #103): no contract at all, a contract found but
    ``isolation: none``, a contract present but with no ``start:`` steps, or
    a contract misplaced in the worktree checkout instead of ``repo_root``.

    Two accepted, deliberate caveats:

    - This is a **second read** of the same contract file
      ``manager.start()`` already read internally -- a negligible, accepted
      TOCTOU window (the file could in principle change between the two
      reads), not a shared/cached read.
    - This function **re-derives** its own no-op verdict from the contract
      and the final record state; it does not observe the engine's actual
      internal decision (the engine has no first-class "why" signal to
      surface -- see the plan's "deferred to the engine repo" notes).

    Never raises: any ``OSError``/``ContractError`` degrades to
    ``no_op_reason == "contract-unreadable"`` rather than propagating.
    """
    contract_path = Path(record.repo_root) / CONTRACT_FILENAME
    contract_found = False
    contract_isolation: Optional[str] = None
    steps_run = 0
    no_op_reason: Optional[str] = None

    try:
        contract_found = contract_path.exists()
        if contract_found:
            contract = load_contract(contract_path)
            contract_isolation = contract.isolation
            if contract.isolation == "none":
                no_op_reason = "isolation-none"
            elif not contract.start:
                no_op_reason = "no-start-steps"
            elif role in record.pids:
                # By this point `contract.start` is non-empty and no
                # exception was raised, so the engine's `_lifecycle_start`
                # already ran unconditionally and set `record.pids[role]`
                # (ticket #103 regression: `record.status` only reaches
                # "running" if the process survives the engine's early-exit
                # wait -- a fast-exiting process is "exited" but is still a
                # real start, not a no-op). Key purely on `role in
                # record.pids`, not on `status == "running"`.
                steps_run = 1
            else:
                no_op_reason = "no-start-steps"
        else:
            checkout_dir = Path(record.path)
            same_dir = checkout_dir.resolve() == Path(record.repo_root).resolve()
            checkout_contract_path = checkout_dir / CONTRACT_FILENAME
            if not same_dir and checkout_contract_path.exists():
                no_op_reason = "contract-misplaced"
            else:
                no_op_reason = "no-contract"
    except (OSError, ContractError):
        # `contract_found` is deliberately left as whatever it was already
        # set to above: if `contract_path.exists()` returned True before
        # this exception was raised (a contract that exists but failed to
        # parse/read), it stays True -- "found but unreadable" is a
        # different state than "nothing there at all". It only stays at its
        # initial False if `.exists()` itself is what raised.
        contract_isolation = None
        # `manager.start()` already performed its own, successful read of
        # this contract before this helper's second (redundant) read ever
        # ran -- only the second read failed (the documented TOCTOU
        # window). `record.pids` was populated by that first, real read and
        # needs no further I/O, so it is the authority here: if `role` is
        # already in it, a real start genuinely happened and must not be
        # misreported as a no-op just because the diagnostics re-read failed.
        if role in record.pids:
            steps_run = 1
            no_op_reason = None
        else:
            steps_run = 0
            no_op_reason = "contract-unreadable"

    return {
        "contract_found": contract_found,
        "contract_path": contract_path.as_posix(),
        "contract_isolation": contract_isolation,
        "steps_run": steps_run,
        "no_op_reason": no_op_reason,
    }


def _start_step_names(repo_root: str) -> Optional[List[str]]:
    """Return the ``name:`` of every *named* ``start:`` step declared by the
    contract at ``<repo_root>/.seretos/worktree-setup.yml`` (ticket #127),
    so a caller of ``worktree_create`` can see up front which
    ``environment_start(variant=...)`` values are valid, instead of only
    discovering them from an ``UnknownVariantError`` on the first failed
    call.

    This is a byte-for-byte mirror of the engine's own ``available``
    computation in ``UnknownVariantError`` (``[s.name for s in
    contract.start if s.name]``) -- unnamed steps are deliberately excluded,
    exactly as the engine excludes them from its own error message.

    Sentinel semantics -- the two ``None``/``[]`` return values are NOT
    interchangeable:

    - ``None`` -- there is no contract file to read, or the contract exists
      but could not be read/parsed (``OSError``/``ContractError``). Callers
      cannot distinguish "no contract" from "unreadable contract" from this
      return value alone (mirrors ``_contract_diagnostics``'s no-raise
      posture) -- if that distinction matters, cross-reference
      ``contract_found``/``no_op_reason`` from a prior ``environment_start``
      call instead.
    - ``[]`` -- the contract was read successfully but declares no *named*
      ``start:`` steps. Covers ``isolation: none`` (which forbids ``start:``
      entirely), an empty ``start:`` list, and a ``start:`` list whose
      entries are all unnamed.

    Note the explicit ``.exists()`` guard below is required: unlike this
    helper, ``load_contract`` on a **missing** file returns an implicit
    ``isolation: none`` contract rather than raising, which would otherwise
    make a genuinely absent contract indistinguishable from a validly-read
    one with nothing to offer -- collapsing the ``None``-vs-``[]``
    distinction above.

    Never raises: any ``OSError``/``ContractError`` degrades to ``None``,
    mirroring ``_contract_diagnostics``'s never-raise posture.
    """
    contract_path = Path(repo_root) / CONTRACT_FILENAME
    try:
        if not contract_path.exists():
            return None
        contract = load_contract(contract_path)
        return [s.name for s in contract.start if s.name]
    except (OSError, ContractError):
        return None


def _default_stop_variant(
    manager: WorktreeManager,
    environment_id: Optional[str],
    checkout_path: Optional[str],
) -> str:
    """Pre-resolve ``environment_stop``'s ``variant="default"`` so it can
    stop the same lone-named-step environment ``environment_start`` starts
    by default (ticket #139 Part B).

    Root cause and why this is fixed here, not upstream
    -----------------------------------------------------
    The engine's ``start()`` tier-3 fallback (upstream lib-python-worktree
    #112, shipped in the pinned v0.3.5) lets a bare ``variant="default"``
    call resolve a contract's lone ``start:`` step even when that step
    carries its own ``name:`` -- but it then records ``variant=step.name or
    variant`` (``manager.py``), i.e. the step's own name, never the literal
    ``"default"``. The engine's own docstring calls this "a deliberate,
    documented asymmetry": ``stop(variant="default")`` -- the same literal
    the caller passed to ``start()`` -- will not resolve against that role,
    because no role is ever recorded under the variant ``"default"`` once
    the fallback substitutes the step's own name. Changing that upstream
    would be a behaviour/contract change to a documented-as-deliberate
    engine decision, and is not implementable from this repo. But the
    engine's own docstring for ``stop()`` explicitly assigns exactly this
    translation job to this layer: any dict-shaped soft-error contract "is
    owned by the MCP wrapper layer in the separate agent-worktree plugin
    repo, which translates this engine's return values/exceptions into
    whatever shape its tool surface promises callers". Mirroring ``start()``
    's own tier-3 fallback here, wrapper-side, against pure record/contract
    data the wrapper already has read access to, satisfies that contract
    without touching the engine.

    Never raises. Strict precedence, in order:

    1. Locate the target record -- ``manager.state.get(environment_id)`` if
       given, else via ``checkout_path``: resolve it through
       ``classify_checkout()``, then branch on ``info.backing`` exactly as
       ``WorktreeManager._resolve_target()`` does -- a primary id lookup
       only when ``backing == "primary"``, else a resolved-path match over
       this repo's non-primary tracked records. ``info.repo_root`` is
       always the main clone's root regardless of which checkout
       ``checkout_path`` itself belongs to, so the primary id lookup must
       never be attempted unconditionally -- doing so would resolve a
       linked-worktree ``checkout_path`` to its repo's *primary* record
       instead. Any failure at this step (invalid ``checkout_path``,
       unknown id, no match at all) falls through to step 4 below -- the
       original engine error text for whatever is actually wrong with the
       addressing pair must survive verbatim, this helper is never the
       thing that raises for it.
    2. If ``"default"`` is already present in ``record.variants.values()``,
       return ``"default"`` unchanged. This keeps every existing exact-match
       call byte-for-byte unaffected, and makes this helper forward-
       compatible with a future engine bump that starts recording the
       caller's literal itself (e.g. as part of resolving the upstream
       ticket this fix's plan recommends filing): the plain path resolves
       first and this fallback simply never engages.
    3. Else, load ``<record.repo_root>/.seretos/worktree-setup.yml``. If it
       declares exactly one ``start:`` step and that step has a non-empty
       ``name:`` other than ``"default"``, return that name -- mirroring
       ``start()``'s own tier-3 rule byte-for-byte (single step, named or
       not, resolves ``variant="default"``).
    4. Any miss -- no record, no contract, an unreadable contract, more
       than one ``start:`` step, or a lone step that is *unnamed* (which the
       engine already records under the literal ``"default"``, so step 2
       already covers it) -- returns ``"default"`` unchanged, so today's
       ``VariantResolutionError`` failure path for every one of those cases
       is byte-for-byte what it was before this helper existed.

    Only engages when the caller's ``variant`` is exactly the string
    ``"default"``; ``environment_stop``'s call site only invokes this
    helper in that case, so ``variant=None`` (the default -- no resolution
    at all) and every other literal are entirely untouched by this helper's
    existence.
    """
    try:
        record: Optional[WorktreeRecord] = None
        if environment_id is not None:
            record = manager.state.get(environment_id)
        if record is None and checkout_path is not None:
            info = classify_checkout(checkout_path)
            if info.backing == "primary":
                # Mirrors WorktreeManager._resolve_target()'s primary branch
                # (manager.py:1698-1700): classify_checkout() documents that
                # info.repo_root is always the main clone's root regardless
                # of which checkout checkout_path itself belongs to, so this
                # lookup must only be attempted when checkout_path actually
                # IS the primary -- otherwise it returns the primary's own
                # record for a linked-worktree checkout_path too (ticket
                # #139 fix-cycle finding).
                record = manager.state.get(primary_id_for(info.repo_root))
            else:
                # checkout_path resolved to a linked worktree: go straight
                # to the path/containment match, scoped to this repo's
                # non-primary records, mirroring _resolve_target()'s
                # "worktree" branch (manager.py:1701-1719).
                target = Path(info.checkout_path).resolve()
                repo_root_str = info.repo_root.as_posix()
                for rec in manager.state.list():
                    if rec.backing == "primary" or rec.repo_root != repo_root_str:
                        continue
                    if Path(rec.path).resolve() == target:
                        record = rec
                        break
        if record is None:
            return "default"

        if any(v == "default" for v in record.variants.values()):
            return "default"

        contract_path = Path(record.repo_root) / CONTRACT_FILENAME
        if not contract_path.exists():
            return "default"
        contract = load_contract(contract_path)
        if len(contract.start) == 1:
            name = contract.start[0].name
            if isinstance(name, str) and name and name != "default":
                return name
        return "default"
    except Exception:  # noqa: BLE001 -- best-effort resolution must never
        # raise; any failure here must leave environment_stop's existing
        # behaviour (and error text) completely unaffected. Mirrors the
        # blanket-except posture of worktree_create's DuplicateWorktreeError
        # enrichment (worktree.py, ticket #116).
        return "default"


def _entry_to_dict(entry: EnvironmentEntry) -> Dict[str, Any]:
    """Shape one ``EnvironmentEntry`` (from ``WorktreeManager.list_repo``)
    into the flat dict returned by ``environment_list``.

    Merges the record's fields with the entry-level ``is_current``/
    ``tracked`` flags and the ``setup_status`` signal derived from
    ``record.setup_outcome`` (never from ``record.status`` -- see
    ``_derive_setup_status``). Untracked
    (synthesised) entries -- ``tracked=False`` -- pass through unchanged;
    callers must use ``tracked``, never the id, as the "is this persisted"
    discriminator. A synthesised linked worktree's ``id`` is
    ``<repo-slug>-<branch-slug>-untracked-<8-hex>`` (minted by
    ``untracked_id_for()``), a one-way derivation of its checkout path --
    NOT a state-store key. It cannot be passed as ``environment_id`` to
    ``worktree_remove``; address it via ``checkout_path`` instead.
    """
    result = {
        **asdict(entry.record),
        "is_current": entry.is_current,
        "tracked": entry.tracked,
    }
    result["setup_status"] = _derive_setup_status(entry.record.setup_outcome)
    return result


def _repo_roots_for_scope_all(manager: WorktreeManager, path: str) -> List[str]:
    """Return every distinct repo root ``environment_list(scope="all")``
    should fan out over, current-repo first.

    ``path`` is the already-resolved ``repo_root`` of the repo containing
    the caller's queried path (i.e. ``RepoListing.repo_root`` from the
    ``manager.list_repo(path)`` call ``environment_list`` already makes for
    ``scope="repo"``) -- resolving it a second time here is a cheap,
    git-free ``Path.resolve()`` call, not a second git subprocess.

    Additional roots come from every ``repo_root`` known to the state store
    (``manager.state.list()``), deduplicated on the *resolved* path so a
    non-normalised spelling can never produce a duplicate entry, and with
    the current repo's root excluded. The remainder is sorted by resolved
    POSIX string for a deterministic, reproducible order.
    """
    current_resolved = Path(path).resolve()
    seen = {current_resolved}
    others: List[str] = []
    for rec in manager.state.list():
        resolved = Path(rec.repo_root).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        others.append(rec.repo_root)
    others.sort(key=lambda r: Path(r).resolve().as_posix())
    return [path, *others]


def register(mcp: FastMCP, manager: WorktreeManager) -> None:
    """Register the split five-tool surface against the given FastMCP server."""

    # ------------------------------------------------------------------
    # Checkout lifecycle: create/delete the directory.
    # ------------------------------------------------------------------

    @mcp.tool()
    def worktree_create(
        repo_root: str,
        branch: str,
        base: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a git worktree for ``branch`` rooted at ``repo_root``.

        ``base`` is the name of a local branch to base the new worktree on;
        the tool fetches the latest commits from ``origin`` automatically so
        the new worktree always starts from an up-to-date remote state.
        Omit ``base`` when ``branch`` already exists. When ``branch`` does
        not yet exist and ``base`` is omitted, it defaults to whatever
        branch is currently checked out at ``repo_root`` -- but this still
        raises when ``repo_root``'s HEAD is detached or unborn (no commits
        yet), since there is then no checked-out branch to default to.

        The ``ports`` field is a dict mapping port name to host port number;
        empty dict ``{}`` for ``isolation: none`` worktrees or before setup
        runs. Agents read it to discover which host ports the worktree's
        services are bound to.

        Returns the canonical worktree record.

        CAUTION: A worktree's ``id`` is NOT stable across a remove + re-create
        cycle. Do not cache an ``id`` and reuse it after the worktree has been
        removed and re-created -- always re-fetch the current id via
        ``environment_list``. A stale id will not resolve to the new
        worktree.

        Fields of note:

        - ``id``: follows the pattern ``<repo-slug>-<branch-slug>-<8-hex>``
          where slugs are lower-case ASCII with non-alphanumeric runs collapsed
          to ``-``.
        - ``path``: absolute checkout location under
          ``<store_root>/<repo_slug>/<id>/`` where ``store_root`` defaults to
          ``~/agent-worktree-store`` or the value of ``$WORKTREE_STORE_ROOT``.
        - ``warning`` (optional): present when ``repo_root`` was silently
          re-rooted to the actual git repository root (e.g. when a subdirectory
          was passed). The field contains the original and resolved paths.
        - ``start_variants`` (always present, unlike ``warning``): the raw
          list of *named* ``start:`` step names declared by the contract
          (unnamed steps excluded), so an agent can see up front which
          ``environment_start(variant=...)`` values are valid instead of
          only discovering a mismatch from an ``UnknownVariantError`` on
          the first failed call. ``None`` when there is no contract file to
          read (or it exists but could not be read/parsed); an empty list
          ``[]`` when the contract was read successfully but declares no
          *named* ``start:`` steps (e.g. ``isolation: none``, an empty
          ``start:`` list, or a ``start:`` list whose entries are all
          unnamed) -- these two states are deliberately distinct and must
          not be conflated. This is purely the contract's declared names,
          **not** a prediction of which step a bare
          ``variant="default"`` call to ``environment_start`` will
          actually select -- see that tool's docstring for the three-tier
          resolution rule.

        Contract file (``.seretos/worktree-setup.yml``)
        -------------------------------------------------
        ``create()`` runs the contract's ``setup:`` steps as part of
        creating this worktree. The contract lives at
        ``<repo_root>/.seretos/worktree-setup.yml`` -- the engine always
        reads it from ``repo_root`` (the original repository clone), never
        from the new checkout. Top-level keys: ``version`` (int, required),
        ``isolation`` (required; one of ``full``, ``partial``, or ``none``),
        ``setup:``, ``start:``, ``stop:``, ``teardown:`` (each an ordered
        list of steps), and ``ports:`` (a list of named port slots).
        ``isolation: none`` forbids all of ``setup:``/``start:``/``stop:``/
        ``teardown:``/``ports:`` -- combining them is a hard schema-
        validation error, not a silent no-op. Each ``setup:`` step is a YAML
        mapping with a required ``run:`` key (the shell command) and
        optional ``name:``/``shell:`` keys. Example::

            version: 1
            isolation: full
            setup:
              - name: install
                run: npm install

        As a create-time convenience, this tool also *copies*
        ``<repo_root>/.seretos/`` into the new worktree checkout when it
        would not otherwise be tracked there (see below) -- but that copy is
        never what ``environment_start``/``environment_stop`` read; they
        always read the ``repo_root`` original. If you suspect the two have
        drifted, which signal to check depends on whether ``repo_root``'s own
        contract exists: if it is missing entirely while the checkout-local
        copy is present, ``environment_start``'s response carries
        ``no_op_reason: "contract-misplaced"``. If both exist but disagree,
        ``no_op_reason`` stays ``None`` (the start proceeds normally against
        ``repo_root``'s contract) -- check that response's
        ``shadowed_contract`` field (``reason: "differs"``) instead; see
        ``environment_start``'s docstring for its shape. The copy is marked ignored
        via a self-ignoring ``.gitignore`` written inside it, so it stays
        invisible to ``git status`` and the worktree remains removable with
        ``worktree_remove``'s default ``force=False`` (ticket #110).

        Transport-level failure ("Connection closed"): confirm before retrying
        ---------------------------------------------------------------------
        If this call dies with a transport error ("Connection closed",
        "MCP error -32000"), you do NOT know whether it landed: the
        worktree may exist and its record may be persisted even though no
        response ever reached you. **Read back before retrying.** Call
        ``environment_list(path=<the same repo_root>)`` and look for the
        entry whose ``branch`` equals the ``branch`` you passed AND whose
        ``tracked`` is ``true``. If it is there the create landed: that
        entry's ``id`` is exactly what the lost response carried -- and
        since the id's 8-hex suffix is random, read-back is the ONLY way
        to recover it -- while ``setup_status`` reports how the contract's
        ``setup:`` steps ended.

        A blind retry is non-destructive: the duplicate-branch guard fires
        before any worktree-creating git command runs -- only a read-only
        ``git rev-parse`` (repo classification via ``_validate_repo()`` /
        ``classify_checkout()``) has executed by that point -- so no second
        worktree is ever created. It does report as a failure -- ``ValueError("A worktree
        for branch '...' already exists in ...")`` -- but that message
        also carries the landed environment's identity as machine-readable
        tokens, ``(existing_environment_id: "<id>", existing_path:
        "<path>")``, so a blind retry is self-diagnosing; parse those
        tokens instead of treating the error as fatal. The tokens are
        best-effort: when the existing record cannot be looked up, only
        the engine's own text is raised and no id is invented.

        Honest limit: this tells you the worktree exists and what the
        persisted ``setup_status`` says; it cannot tell you whether a
        setup step was interrupted mid-command.
        """

        try:
            record = manager.create(repo_root=repo_root, branch=branch, base=base)
        except SetupFailedError as exc:
            raise ValueError(
                f"Setup failed for worktree (left intact at path for inspection): {exc}"
            ) from exc
        except DuplicateWorktreeError as exc:
            # DuplicateWorktreeError subclasses WorktreeError, so this catch
            # must come before the generic `except WorktreeError` tail below
            # -- same MRO-ordering concern as CheckoutTargetError (#119),
            # WorktreeRemovalBlockedError (#120) and InvalidRepoError (#123)
            # in worktree_remove.
            #
            # Ticket #116: when a create's JSON-RPC response is lost to a
            # transport drop ("Connection closed"), the caller loses the
            # record's *random* 8-hex id suffix, which is not re-derivable
            # from anything it holds. Naming the landed environment inline
            # makes the blind retry self-diagnosing. Best-effort only: the
            # lookup mirrors the engine's own key derivation
            # (`_validate_repo` == classify_checkout(resolved path).repo_root),
            # and any failure falls back to the engine's bare text -- an id
            # is never invented.
            existing = None
            try:
                resolved_root = classify_checkout(
                    Path(repo_root).expanduser().resolve()
                ).repo_root.as_posix()
                existing = manager.state.find_by_branch(resolved_root, branch)
            except Exception:  # noqa: BLE001 -- diagnostics must never re-fail
                existing = None
            if existing is not None:
                raise ValueError(
                    f'{exc} (existing_environment_id: "{existing.id}",'
                    f' existing_path: "{existing.path}")'
                ) from exc
            raise ValueError(str(exc)) from exc
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc

        result = _record_to_dict(record)

        # If .seretos/ exists in the repo root but was not copied into the new
        # worktree by git (common when the directory is excluded from tracking
        # via .git/info/exclude), copy it now so setup can find the contract.
        contract_dir_name = Path(CONTRACT_FILENAME).parts[0]  # ".seretos"
        src_contract_dir = Path(record.repo_root) / contract_dir_name
        dst_contract_dir = Path(record.path) / contract_dir_name
        if src_contract_dir.is_dir() and not dst_contract_dir.exists():
            try:
                shutil.copytree(src_contract_dir, dst_contract_dir)
                _ensure_contract_copy_ignored(dst_contract_dir)
            except OSError as exc:
                raise ValueError(
                    f"Worktree created at '{record.path}' but failed to copy"
                    f" contract directory '{src_contract_dir.as_posix()}' into it: {exc}"
                ) from exc

        # Emit a warning when the caller's repo_root was silently re-rooted to
        # the actual git repository root (e.g. a subdirectory was passed).
        resolved_input = Path(repo_root).expanduser().resolve()
        resolved_record = Path(record.repo_root).resolve()
        if resolved_input != resolved_record:
            result["warning"] = (
                f"repo_root was re-rooted from '{repo_root}' to '{record.repo_root}'"
            )

        # Ticket #127: surface the contract's declared *named* start: steps
        # up front, so a contract author naming their sole step something
        # other than "default" discovers the available variant names here
        # instead of only from an UnknownVariantError on the first
        # environment_start call. Read from record.repo_root (never the
        # caller's repo_root argument) -- the engine may have re-rooted it,
        # per the warning block above.
        result["start_variants"] = _start_step_names(record.repo_root)

        return result

    @mcp.tool()
    def worktree_remove(
        environment_id: Optional[str] = None,
        checkout_path: Optional[str] = None,
        force: bool = False,
        kill_blocking_processes: bool = False,
    ) -> Dict[str, Any]:
        """Remove a worktree checkout, addressed by ``environment_id``
        and/or ``checkout_path``.

        Addressing the target
        ----------------------
        - ``environment_id`` -- **the normal way** for a *tracked* checkout
          (one ``worktree_create`` persisted a record for). Use the id
          returned by ``worktree_create`` or ``environment_list``.
        - ``checkout_path`` -- **the only way to remove an untracked/orphan
          checkout.** A linked worktree that exists on disk (``git worktree
          list --porcelain`` reports it, and ``environment_list`` shows it
          with ``tracked: false``) but was never created through this tool
          has a synthesised, display-only id
          (``<repo-slug>-<branch-slug>-untracked-<8-hex>``) that is a
          one-way derivation of its checkout path, not a state-store key --
          it can never resolve via ``environment_id`` alone. Pass the
          checkout's path (as shown in ``environment_list``'s ``path``
          field) as ``checkout_path`` instead. Removing an untracked target
          this way tears down the checkout but never touches the state
          store (there was nothing there to remove) and never deletes its
          branch, even with ``force=True``, since the checkout was never
          recorded as owning one.

        Neither is schema-required, but the engine (not this wrapper)
        enforces the resolution: passing both is fine only when they agree
        (a mismatch raises ``ValueError``); passing neither also raises
        ``ValueError``. This wrapper performs no validation of the
        ``(environment_id, checkout_path)`` pair itself -- resolution is
        entirely the engine's job, via its ``CheckoutTargetError`` -- but it
        re-words that error's text before raising ``ValueError``: the
        engine's own message names its internal ``worktree_id`` parameter
        and engine-API vocabulary (``start()``/``stop()``/``remove()``),
        so this wrapper replaces it with a ``worktree_remove``-specific
        message naming ``environment_id`` and ``checkout_path`` instead.

        Similarly, when ``checkout_path`` is given but isn't a usable git
        repository (e.g. it doesn't exist, isn't a directory, or isn't a
        git repo at all), resolution raises the engine's ``InvalidRepoError``
        (ticket #123). This wrapper re-words that message too, replacing the
        engine-internal ``repo_root`` parameter name -- which this tool
        doesn't have -- with ``checkout_path``, while preserving every byte
        of the underlying diagnostic reason.

        (Deliberate, documented deviation from ticket #99's originally
        id-only signature -- see this module's docstring for why an id-only
        surface cannot satisfy the ticket's own AC1, and why ``checkout_path``
        is a strict superset that keeps every existing id-only call working
        unchanged.)

        Parameters
        ----------
        environment_id:
            The normal way to address a tracked checkout -- see "Addressing
            the target" above.
        checkout_path:
            The only way to address an untracked/orphan checkout -- see
            "Addressing the target" above.
        force:
            When ``True``, removes the worktree even if it contains
            uncommitted changes. Defaults to ``False``.
        kill_blocking_processes:
            When ``True``, attempts to terminate **foreign** processes whose
            current working directory is inside the worktree directory before
            removal. This is an opt-in safety valve, primarily relevant on
            Windows where open handles prevent directory deletion. Defaults
            to ``False`` (no-op when nothing is blocking).

            **Tracked vs. foreign.** Removal stops every process *tracked* in
            the environment's ``pids`` (each role started via
            ``environment_start``) as its first step, before any contract
            ``stop:``/``teardown:`` steps and before any filesystem delete --
            so a tracked process is normally already gone by the time the
            directory lock is evaluated, and never needs this flag. The flag
            exists for a genuinely foreign holder instead: an editor, a
            shell sitting in the checkout, a build/indexing tool, or an
            orphaned grandchild reparented away from the tracked shell
            wrapper (none of which are ever in ``pids``). Two caveats: (1)
            the tracked stop is best-effort, so a tracked process that
            refuses to die degrades into exactly the same blocking condition
            and *does* then need this flag; and (2) the underlying scan
            filters only the host process and its OS-level ancestors -- it
            has no tracked-pid allow-list, so the exclusion in the normal
            case is a matter of ordering, not filtering.

        Returns the removed worktree record on success. The ``ports`` field is
        a dict mapping port name to host port number; empty dict ``{}`` for
        ``isolation: none`` worktrees or before setup runs. Agents read it to
        discover which host ports the worktree's services are bound to.

        The response includes a ``killed_pids`` list (may be empty). Each entry
        is a dict with ``pid`` (int), ``name`` (str), and ``cmdline`` (list of
        str) describing a process that was terminated to unblock removal.

        If the target is not found, returns ``{"error": "...", "code":
        "not_found"}`` instead of raising, so callers can treat not-found as
        a soft/idempotent condition, and can branch on ``code`` rather than
        parsing the error text. When ``environment_id`` looks like a
        synthesised untracked id, the error text names ``checkout_path`` as
        the remedy (``code`` is ``"not_found"`` either way).

        Raises ``ValueError`` (mapped from ``WorktreeDirLockedError``) when the
        worktree directory is still locked after attempting to kill blocking
        processes.

        **Compound blocking is reported in one shot (ticket #120).** When
        BOTH the directory lock AND uncommitted/untracked changes are
        blocking removal at once, the engine raises
        ``WorktreeRemovalBlockedError`` instead of the single-condition
        exceptions above. This wrapper catches it explicitly and raises a
        single ``ValueError`` naming every currently-blocking condition and
        the flag needed to clear each -- ``(blocked_by: "dir_locked",
        "uncommitted_changes"; required_flags: kill_blocking_processes=True,
        force=True)`` -- so one informed retry (passing both flags at once)
        suffices, instead of a caller discovering each condition
        sequentially across up to three separate failed attempts. Filesystem
        paths are deliberately never included in this message.

        **Primary checkouts are never removed.** Attempting to remove the
        primary/main clone's environment -- whether addressed by
        ``environment_id`` or by ``checkout_path``, and even with
        ``force=True`` -- raises ``ValueError``. This refusal is structural,
        checked before any teardown work runs, and cannot be bypassed: a
        primary checkout IS the repo, so deleting it would be catastrophic.
        The raised message includes the engine's own text plus an explicit
        ``backing: "primary"`` token so callers can react programmatically
        without parsing prose.

        Transport-level failure ("Connection closed"): confirm before retrying
        ---------------------------------------------------------------------
        Read back with ``environment_list(path=<the REPO ROOT>)`` -- never
        with the removed checkout's own path. If the removal landed that
        path is gone, and passing it back to any tool yields ``ValueError``
        text of the form ``invalid checkout_path '<p>': checkout_path does
        not exist: ...``, which is byte-for-byte what a simple typo
        produces. That error is therefore NOT evidence that the removal
        succeeded. From the repo root the reading is unambiguous:

        - entry absent -> the removal landed; you are done.
        - entry present with ``status: "orphaned"`` -> partially landed
          (the directory is gone, the record survives). Finish it with
          ``worktree_remove(environment_id=<that entry's id>)``.
        - entry present and unchanged -> the removal did not land; retry.

        A blind retry addressed by ``environment_id`` is self-diagnosing:
        an already-removed target comes back as the soft ``{"error":
        "...", "code": "not_found"}`` instead of raising. A blind retry
        addressed by ``checkout_path`` is not -- it raises the misleading
        "does not exist" text above. **Prefer ``environment_id`` for any
        retry after a transport failure.**
        """

        try:
            record = manager.remove(
                environment_id,
                force=force,
                kill_blocking_processes=kill_blocking_processes,
                checkout_path=checkout_path,
            )
        except PrimaryCheckoutError as exc:
            raise ValueError(f'{exc} (backing: "primary")') from exc
        except WorktreeNotFoundError as exc:
            target_name = (
                environment_id if environment_id is not None else checkout_path
            )
            error_text = f"environment '{target_name}' not found"
            # Only append the engine's own exception text when the looked-up
            # id is untracked-shaped: that's the one case where {exc} is
            # actually informative (it names checkout_path as the remedy).
            # For an ordinary unknown id, the engine's text just restates the
            # id ("No worktree tracked with id '...'"), so appending it would
            # only change the error format without adding information --
            # restore the original bare format there instead (ticket #113
            # review fix; preserves backward compatibility for callers who
            # may depend on the exact bare-error string shape).
            if environment_id is not None and _UNTRACKED_ID_RE.search(
                environment_id
            ):
                error_text = f"{error_text}: {exc}"
            return {"error": error_text, "code": "not_found"}
        except WorktreeRemovalBlockedError as exc:
            # Ticket #120: WorktreeRemovalBlockedError subclasses BOTH
            # WorktreeDirLockedError and DirtyWorktreeError, so this clause
            # must come before the plain `except WorktreeDirLockedError`
            # below -- otherwise that clause would silently swallow the
            # compound case and only ever report the lock half of the
            # picture. Surface both blocking conditions and both required
            # flags in one message so a single informed retry suffices.
            raise ValueError(
                f'{exc} (blocked_by: "dir_locked", "uncommitted_changes"; '
                f"required_flags: kill_blocking_processes=True, force=True)"
            ) from exc
        except WorktreeDirLockedError as exc:
            raise ValueError(str(exc)) from exc
        except CheckoutTargetError as exc:
            raise ValueError(
                _addressing_error_text(
                    exc,
                    tool_name="worktree_remove",
                    hint=(
                        "Pass environment_id for a tracked checkout, or "
                        "checkout_path for an untracked/orphan checkout."
                    ),
                )
            ) from exc
        except InvalidRepoError as exc:
            # InvalidRepoError subclasses WorktreeError, so this catch must
            # come before the generic `except WorktreeError` tail below --
            # same MRO-ordering concern as CheckoutTargetError's catch above
            # (ticket #119) and WorktreeRemovalBlockedError's (ticket #120).
            # Ticket #123: the engine's message names its internal
            # `repo_root` parameter, which this tool doesn't have -- rename
            # it to `checkout_path` only when the rejected path is the one
            # this wrapper actually received (identity guard), so an
            # InvalidRepoError from some other internally-resolved path is
            # never mislabelled.
            if checkout_path is not None and exc.repo_root == checkout_path:
                raise ValueError(
                    _invalid_path_error_text(exc, param_name="checkout_path")
                ) from exc
            raise ValueError(str(exc)) from exc
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    # ------------------------------------------------------------------
    # Environment lifecycle: the process running against any checkout,
    # primary clone included.
    # ------------------------------------------------------------------

    @mcp.tool()
    def environment_list(path: str, scope: str = "repo") -> List[Dict[str, Any]]:
        """List the environments (primary clone + linked worktrees) for the
        repo containing ``path``, joined against persistent, disk-backed
        state (survives server restarts; reconciled on startup).

        ``path`` is **required** -- there is no "list everything, everywhere"
        call. This deliberately replaces the old ``worktree_list()``
        unfiltered-discovery hole and the single-record ``worktree_get()``
        lookup: every entry this tool can return -- tracked or not, primary
        or linked -- is reachable from a ``path`` you already have (a repo
        root, a worktree checkout, or any subdirectory of either).

        Parameters
        ----------
        path:
            Any path inside a git repository -- the repo root itself, a
            linked worktree checkout, or a subdirectory of either. Resolves
            to that repo's canonical root internally; the same repo returns
            the same listing regardless of which path inside it you pass.
        scope:
            ``"repo"`` (default) -- only the repo containing ``path``.
            ``"all"`` -- every distinct repo this server has ever tracked an
            environment for, fanned out with the identical entry shape as
            ``"repo"`` (no second shape, no repo-grouping wrapper). The repo
            containing ``path`` is always listed first. Costs one extra
            ``git worktree list --porcelain`` subprocess call per
            *additional* repo. Any unknown value raises ``ValueError``.

        Each entry mirrors a ``WorktreeRecord`` plus three extra keys:

        - ``is_current`` (bool): this entry's checkout contains the queried
          ``path``. Under ``scope="repo"`` at most one entry has this set. Under
          ``scope="all"`` this invariant holds across the *entire* result --
          only an entry from the repo containing ``path`` can ever be
          ``True``; every entry fanned out from another repo is forced to
          ``False`` even though, from that other repo's own vantage point, one
          of its own entries would otherwise also look "current".
        - ``tracked`` (bool): ``False`` marks a *synthesised* entry -- a
          checkout that exists on disk but has no persisted record yet. This
          is the case for the primary/main clone before its first
          ``environment_start()`` call, and for any linked worktree created
          outside this tool and not yet adopted. **Always branch on
          ``tracked``, never on ``id``, to tell a synthesised entry from a
          persisted one** -- a synthesised primary's ``id`` is the
          deterministic ``primary_id_for(repo_root)`` (so it round-trips
          correctly once later materialised by ``environment_start``), while
          a synthesised linked worktree's ``id`` is
          ``<repo-slug>-<branch-slug>-untracked-<8-hex>`` (minted by
          ``untracked_id_for()``) -- a one-way derivation of its checkout
          path, NOT a state-store key. It cannot be passed as
          ``environment_id`` to ``worktree_remove``; address it via
          ``checkout_path`` instead.
        - ``setup_status``: a coarse setup-health signal derived SOLELY from
          the record's ``setup_outcome`` (an ``Optional[SetupOutcome]``),
          never from ``status`` (the overall run status) -- full decoupling
          (ticket #117). ``"unknown"`` when ``setup_outcome`` is ``None``
          (the ``setup:`` hook was never reached -- a record predating this
          field, an adopted record, or a synthesised entry); otherwise the
          verbatim ``setup_outcome.status``: ``"completed"``, ``"failed"``,
          or ``"skipped"``. This value survives later rewrites of ``status``
          by ``start``/``stop``/``reconcile`` -- it reflects only what
          happened when ``create()`` ran the ``setup:`` hook, once, and is
          never touched again. Each entry's full ``setup_outcome`` dict
          (via ``asdict``) is also present for detail: ``message``,
          ``completed_at``, ``steps_run``, ``failed_step_index``,
          ``failed_step_name``, ``log_path``, ``returncode``, and
          ``timed_out``.

        This call **never writes state** -- listing the primary before it has
        ever been started does not create a record for it; only
        ``environment_start`` does that.

        Raises ``ValueError`` for an unknown ``scope``, or when ``path`` itself
        is not a valid, existing git repository (mapped from the engine's
        ``InvalidRepoError``, re-worded per ticket #123's pattern to name
        ``path`` instead of the engine-internal ``repo_root`` parameter,
        with the full diagnostic reason preserved). Under ``scope="all"``, a
        *different*, previously tracked repo whose on-disk clone has since
        vanished is skipped gracefully rather than failing the whole call --
        only a bad ``path`` argument raises.

        **Retrying this call is always safe.** It never writes state, so a
        transport-level failure ("Connection closed", "MCP error -32000")
        can be retried unconditionally. That is precisely what makes it
        the read-back tool for deciding whether a lost *mutating* call
        landed -- see the "Transport-level failure" block in
        ``worktree_create``, ``worktree_remove``, ``environment_start``
        and ``environment_stop``.

        **Fields this call never populates.** Each entry carries every
        ``WorktreeRecord`` key, but three of them are transient by design
        and are never persisted to ``state.yaml``: ``stop_attempt``,
        ``killed_pids`` and ``shadowed_contract``. Because this call
        rebuilds every entry from persisted state, those three are always
        ``null``/``[]`` here regardless of what actually happened. They
        are readable ONLY on the response of the call that produced them
        (``environment_stop``, ``worktree_remove``,
        ``environment_start``). Never use them as read-back evidence.
        """
        if scope not in ("repo", "all"):
            raise ValueError(
                f"unknown scope {scope!r}; expected 'repo' or 'all'"
            )

        try:
            listing = manager.list_repo(path)
        except InvalidRepoError as exc:
            # InvalidRepoError subclasses WorktreeError. environment_list
            # deliberately has no generic `except WorktreeError` tail today
            # (unlike worktree_remove/environment_start/environment_stop) --
            # this comment documents that invariant for whoever adds one
            # later, so its ordering relative to this catch gets the same
            # MRO-ordering scrutiny given to the sibling catches at #119/
            # #120/#123.
            # Ticket #123/#139: the engine's message names its internal
            # `repo_root` parameter, which this tool doesn't have -- rename
            # it to `path` only when the rejected path is the one this
            # wrapper actually received (identity guard), so an
            # InvalidRepoError from some other internally-resolved path
            # (e.g. the scope="all" fan-out over other tracked repo roots,
            # which already skips stale roots gracefully rather than
            # raising) is never mislabelled.
            if path is not None and exc.repo_root == path:
                raise ValueError(
                    _invalid_path_error_text(exc, param_name="path")
                ) from exc
            raise ValueError(str(exc)) from exc

        entries = [_entry_to_dict(e) for e in listing.entries]

        if scope == "all":
            for root in _repo_roots_for_scope_all(manager, listing.repo_root):
                if Path(root).resolve() == Path(listing.repo_root).resolve():
                    continue
                try:
                    extra_listing = manager.list_repo(root)
                except InvalidRepoError:
                    # Stale root: the clone this repo used to live at no
                    # longer exists on disk. Skip it gracefully rather than
                    # failing the whole scope="all" call.
                    continue
                for entry in extra_listing.entries:
                    entry_dict = _entry_to_dict(entry)
                    entry_dict["is_current"] = False
                    entries.append(entry_dict)

        return entries

    @mcp.tool()
    def environment_start(
        environment_id: Optional[str] = None,
        checkout_path: Optional[str] = None,
        role: str = "main",
        cwd: Optional[str] = None,
        variant: str = "default",
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Start a detached process for a target environment -- a linked
        worktree or the repo's own primary/main clone.

        The command to run is **not** supplied by the caller -- it is read
        from the environment's contract's ``start:`` steps in
        ``.seretos/worktree-setup.yml``. The engine reads this file from
        ``repo_root`` -- the original repository clone -- **not** from a
        linked worktree checkout itself. ``worktree_create`` copies
        ``.seretos/`` into a new worktree as a create-time convenience, but
        that copy is not what the engine reads.

        CAUTION: placing the contract only in a worktree checkout (and not at
        ``<repo_root>/.seretos/worktree-setup.yml``) still produces a
        ``{"status": "ready", "pids": {}}`` no-op -- but it is **not silent**
        and **not** indistinguishable from "no contract configured": the same
        response carries ``contract_found: false``, ``steps_run: 0``, and
        ``no_op_reason: "contract-misplaced"`` (vs ``"no-contract"`` for the
        genuinely-unconfigured case). Callers should branch on ``no_op_reason``
        rather than inferring the cause from ``status``/``pids`` alone -- see
        the "Contract diagnostics" block below for the full five-key set. The
        engine may additionally set ``shadowed_contract`` on the response in
        this case -- see the sixth diagnostic bullet below.

        Addressing the target
        ----------------------
        Every environment -- the primary/main clone included -- is addressed
        by one or both of:

        - ``environment_id`` -- **the normal way.** Use the id returned by
          ``worktree_create`` (a linked worktree) or by ``environment_list``/
          a prior ``environment_start`` (the primary, once materialised).
        - ``checkout_path`` -- **the cold-start/primary path.** This is the
          *only* way to start the primary/main clone's environment before it
          has ever been started. A primary's id,
          ``primary_id_for(repo_root)``, is a one-way SHA-256 hash of the
          repo root; before this call's first success, nothing persisted maps
          that hash back to a path, so id-only addressing cannot cold-start
          it. Pass the repo root (or any path inside it) as ``checkout_path``
          and the engine resolves and, if needed, materialises the primary's
          record here -- this is the **only** place a primary
          ``WorktreeRecord`` is ever written.

        Neither is schema-required, but the engine (not this wrapper)
        enforces the resolution: passing both is fine only when they agree
        (a mismatch raises ``ValueError``); passing neither also raises
        ``ValueError``. This wrapper performs no validation of the
        ``(environment_id, checkout_path)`` pair itself -- resolution is
        entirely the engine's job, via its ``CheckoutTargetError`` -- but it
        re-words that error's text before raising ``ValueError``: the
        engine's own message names its internal ``worktree_id`` parameter
        and engine-API vocabulary (``start()``/``stop()``/``remove()``),
        so this wrapper replaces it with an ``environment_start``-specific
        message naming ``environment_id`` and ``checkout_path`` instead.

        Similarly, when ``checkout_path`` is given but isn't a usable git
        repository, resolution raises the engine's ``InvalidRepoError``
        (ticket #123). This wrapper re-words that message too, replacing
        the engine-internal ``repo_root`` parameter name -- which this tool
        doesn't have -- with ``checkout_path``, while preserving every byte
        of the underlying diagnostic reason.

        (Deliberate, documented deviation from ticket #99's originally
        id-only signature -- see this module's docstring for why an id-only
        surface cannot satisfy the ticket's own AC1, and why ``checkout_path``
        is a strict superset that keeps every existing id-only call working
        unchanged.)

        All three addressing outcomes in one place: neither given raises
        ``ValueError``; both given but disagreeing raises ``ValueError``; and
        -- when the ``(environment_id, checkout_path)`` pair is well-formed
        but resolves to nothing the engine knows about -- the *target* is
        soft-not-found, returning ``{"error": "...", "code": "not_found"}``
        rather than raising (see "If the target is not found" below for the
        full contract). Not every addressing failure raises: only a
        malformed pair does; an unresolvable-but-well-formed pair does not.

        Multiple named ``start:`` steps are supported; ``variant`` selects the
        step by its ``name``. Resolving ``variant="default"`` (the
        parameter's own default) works in three tiers, tried in order:

        1. An exact ``name:`` match against ``variant``.
        2. Exactly one **unnamed** ``start:`` step -- implicitly the
           ``"default"`` variant, for back-compat.
        3. Exactly one ``start:`` step overall -- **even if that single
           step is named rather than unnamed** (upstream
           lib-python-worktree #112, shipped in the pinned v0.3.5) -- so a
           contract whose sole step carries a ``name:`` other than
           ``"default"`` still resolves without the caller needing to pass
           ``variant`` explicitly.

        Two or more ``start:`` steps with none of them named ``"default"``
        still raise ``ValueError`` even under tier 3 -- the lone-step
        fallback only ever fires when the contract declares exactly one
        step. An unknown variant surfaces as a ``ValueError`` listing the
        available *named* steps (unnamed steps are never listed, since they
        have no name to list).

        Step schema: each ``start:`` entry is a YAML mapping with a required
        ``run:`` key (the shell command to execute) and an optional ``name:``
        key (used by ``variant`` to select that step). See the three-tier
        resolution above for how a contract with only one ``start:`` step
        total -- named or not -- is matched by ``variant="default"``.

        Contract file schema (``<repo_root>/.seretos/worktree-setup.yml``)
        ----------------------------------------------------------------
        Top-level keys: ``version`` (int, required), ``isolation`` (required;
        one of ``full``, ``partial``, or ``none``), ``setup:``, ``start:``,
        ``stop:``, ``teardown:`` (each an ordered list of steps), and
        ``ports:`` (a list of named port slots).

        Isolation rule: any of the ``setup:``/``start:``/``stop:``/
        ``teardown:``/``ports:`` blocks requires a non-``none`` isolation --
        use ``isolation: full`` (``partial`` also validates); ``isolation:
        none`` **forbids** those blocks, and combining them is not a silent
        no-op -- it is a hard schema-validation error (``ContractError``)
        raised when the contract is loaded. Example::

            version: 1
            isolation: full
            start:
              - name: web
                run: start-web.sh
              - name: worker
                run: start-worker.sh

        ``role`` vs ``variant``
        -----------------------
        These two parameters are independent and are easy to conflate:

        - ``role`` is the *tracking/addressing key* under which the spawned
          process's pid is recorded (``record.pids[role]``). It defaults to
          ``"main"`` **regardless of which** ``variant`` was requested --
          starting ``variant="worker"`` with no explicit ``role`` still
          records its pid under ``role="main"``, exactly like starting
          ``variant="default"`` would. ``record.pids[role]`` and
          ``record.variants[role]`` are both keyed by this **verbatim**
          ``role`` string -- which is *not* how the start-log filename is
          derived; see the ``start_log_path`` casing caveat below.
        - ``variant`` only selects *which* contract ``start:`` step is run
          (by its ``name``). It has no effect on where the resulting pid is
          filed.

        Because the two are independent, two variants started concurrently
        against the same environment need two *distinct* ``role``s -- if the
        second call reuses the same (default) role, it returns/errors with
        an ``already_running`` condition (that role already has a live pid),
        even though a different ``variant`` was requested. Whichever
        ``variant`` actually started a given ``role`` is remembered in
        ``record.variants[role]``, so a later ``environment_stop(variant=...)``
        call can address that role without the caller having to separately
        track which role it used -- see ``environment_stop``'s docstring.

        **Symmetry with environment_stop (ticket #139):** when tier 3 of the
        variant resolution above (the lone-step fallback) resolves a *named*
        step from a bare ``variant="default"`` call, the value the *engine*
        records in ``record.variants[role]`` is that step's own name (e.g.
        ``"main"``) -- never the literal string ``"default"`` -- because the
        engine itself records ``variant=step.name or variant``. Despite
        that, a later ``environment_stop(variant="default")`` call **does**
        resolve a lone-named-step environment: this wrapper compensates by
        pre-resolving a bare ``"default"`` to the same lone-step contract
        rule before ever calling the engine -- see ``environment_stop``'s
        docstring for the mechanism. Passing the step's actual name, e.g.
        ``environment_stop(variant="main")``, keeps working too.

        Parameters
        ----------
        environment_id:
            The normal way to address the target -- see "Addressing the
            target" above.
        checkout_path:
            The cold-start/primary way to address the target -- see
            "Addressing the target" above.
        role:
            Logical role name for the process; defaults to ``"main"``. Multiple
            processes can be attached to one environment under different
            roles. See "``role`` vs ``variant``" above for how this relates
            to the ``variant`` parameter.
        cwd:
            Working directory for the spawned process. When omitted the
            environment's checkout path is used by the underlying engine.
        variant:
            Selects which named ``start:`` step to run. Defaults to
            ``"default"``, which resolves via the three-tier rule above: an
            exact ``name:`` match; else the lone unnamed step, if there is
            exactly one; else the lone step overall -- regardless of
            whether it is named or unnamed -- if the contract declares
            exactly one ``start:`` step total. Two or more steps with none
            named ``"default"`` still raise ``ValueError`` listing the
            available names. See "``role`` vs ``variant``" above for how
            this relates to the ``role`` parameter, including the note on
            ``environment_stop(variant="default")``'s symmetry with this
            resolution when the lone-step fallback resolves a named step.
        env:
            Optional dict of extra environment variables merged into the process
            environment by the engine. Omit (or pass ``None``) to inherit the
            current environment unchanged.

        The operation is idempotent in the sense that if a process is already
        running under the given ``role``, this tool returns a soft error dict
        ``{"error": "...", "code": "already_running"}`` rather than raising,
        so callers can treat the already-running case gracefully and branch
        on ``code`` rather than parsing the error text.

        On success returns the canonical environment record dict. Fields of
        note:

        - ``status``: ``"running"`` when the process started successfully;
          ``"ready"`` for a no-op start (no ``start:`` step configured).
        - ``backing``: ``"primary"`` for the main clone, ``"worktree"`` for a
          linked worktree.
        - ``pids``: a dict mapping role name to PID (e.g. ``{"main": 12345}``).
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` before port setup runs.
        - ``start_log_path``: filesystem path to the engine's captured
          startup log for the spawned process; useful for diagnosing a
          process that exits immediately. May be absent/``null`` on a no-op
          ``"ready"`` start where nothing was spawned. **Role-casing
          caveat:** the filename is ``start-<slug(role)>.log``, where the
          slug is the ``role`` **lower-cased**, with non-alphanumeric runs
          collapsed to ``-`` and truncated to 40 characters -- unlike
          ``pids``/``record.variants``, which key on the **verbatim**
          ``role`` string. Example: ``role="API Server"`` files its pid
          under ``pids["API Server"]`` but logs to
          ``start-api-server.log``. Two roles differing only in case (e.g.
          ``"API"`` and ``"api"``) become two distinct ``pids`` keys that
          share one append-mode log file, interleaving their output. Never
          derive the log path by slugging a ``pids``/``variants`` key
          yourself -- always read ``start_log_path`` from the response.
          Documented here, not fixed: this is an upstream engine defect,
          tracked as ``Seretos/lib-python-worktree#111`` -- not to be
          confused with this repository's own already-closed issue of the
          same number, an unrelated thread-leak ticket.

        Contract diagnostics (ticket #103) -- five additive keys that make a
        real start distinguishable from every "nothing ran" flavour,
        including the misplaced-contract case warned about above:

        - ``contract_found`` (bool): whether ``<repo_root>/.seretos/worktree-
          setup.yml`` actually existed and was read.
        - ``contract_path`` (str): the absolute path the engine reads. When
          ``contract_found`` is ``False``, this is where to create the
          contract.
        - ``contract_isolation`` (str or ``None``): the contract's
          ``isolation`` value (``"full"``, ``"partial"``, or ``"none"``), or
          ``None`` when no contract was read.
        - ``steps_run`` (int): ``1`` when a ``start:`` step was actually
          spawned for ``role``; ``0`` for every no-op flavour.
        - ``no_op_reason`` (str or ``None``): ``None`` on a real start;
          otherwise exactly one of ``"no-contract"`` (nothing at
          ``repo_root``), ``"contract-misplaced"`` (found only in the
          worktree checkout, not ``repo_root``), ``"isolation-none"``
          (contract read but ``isolation: none``), ``"no-start-steps"``
          (contract read, isolation allows it, but no ``start:`` step ran),
          or ``"contract-unreadable"`` (the contract exists but could not be
          read/parsed).

        The record additionally carries one diagnostic produced by the
        **engine itself** (``lib-python-worktree``, upstream #100) rather
        than re-derived by this tool; it is passed through verbatim from the
        engine's ``WorktreeRecord``:

        - ``shadowed_contract`` (dict or ``None``): non-``None`` when a
          checkout-local ``.seretos/worktree-setup.yml`` exists that is
          **not** the file the engine actually read. Shape: ``{"path": <the
          shadowing checkout-local copy>, "used_path": <the repo_root
          contract path compared against -- the file the engine read there
          when one exists; when none exists, this is merely the path that
          would hold it, standing in for the engine's implicit
          ``isolation: none`` fallback contract, since nothing is literally
          read from a missing file>, "reason": <"differs" | "unreadable">,
          "message": <human-readable text naming both paths>}``.
          ``"differs"`` means the checkout copy parsed to something other
          than what was used; ``"unreadable"`` means it exists but could not
          be read/parsed. It is **transient**: computed fresh on every
          ``environment_start`` call and never persisted to ``state.yaml``.
          It is ``None`` for a primary checkout, ``None`` when the checkout
          *is* ``repo_root``, and ``None`` for the byte-identical
          convenience copy ``worktree_create`` writes -- so a non-``None``
          value always means a genuine divergence.

          This is complementary to, not redundant with, ``no_op_reason``: it
          fires whenever the contract actually used for comparison -- a real
          ``repo_root`` file, or, when none exists, the engine's implicit
          ``isolation: none`` fallback -- differs from a non-trivial
          checkout-local copy. That includes the case where a repo-root
          contract exists and starts *normally* while the checkout-local
          copy was separately edited to differ (``no_op_reason`` is
          ``null`` there, and the five wrapper-derived keys above see
          nothing wrong) -- but it fires just as readily alongside a
          non-``null`` ``no_op_reason``, notably ``"contract-misplaced"``:
          no file exists at ``repo_root``, the implicit fallback contract is
          what gets compared, and a checkout-local copy that diverges from
          that fallback still shadows it.

        If the target is not found, returns ``{"error": "...", "code":
        "not_found"}`` instead of raising, so callers can treat not-found as
        a soft/idempotent condition, and can branch on ``code`` rather than
        parsing the error text. The message names whichever target
        identifier was supplied (``environment_id`` if given, else
        ``checkout_path``).

        Transport-level failure ("Connection closed"): confirm before retrying
        ---------------------------------------------------------------------
        A transport error tells you nothing about whether the start
        landed. Read back with ``environment_list(path=<checkout path or
        repo root>)`` and inspect the entry's ``pids``.

        The unambiguous case first: if your ``role`` (default ``"main"``)
        is a key in ``pids``, the start landed AND the process is still
        alive -- ``variants[<role>]`` names the variant that was selected,
        and there is nothing further to do.

        If the role is absent the reading is ambiguous: either the start
        never happened, or it happened and the process has since exited
        (this listing reconciles dead pids away, so the two look
        identical). A heuristic can sometimes break the tie, but only for
        a role that was never started before -- for such a role a
        non-``null`` ``returncode``/``start_log_path`` proves a spawn
        occurred, whereas for a role that HAS been started at some earlier
        point both fields are leftovers from that earlier run, are not
        cleared by reconciliation, and therefore decide nothing at all; in
        that case the only reliable evidence is the content and mtime of
        the file at ``start_log_path``.

        A blind retry is protected by ``{"error": "...", "code":
        "already_running"}`` only while the previously started pid is
        ALIVE. A start that landed and whose process then exited is not
        protected: the blind retry starts a second process.
        """

        try:
            record = manager.start(
                environment_id,
                checkout_path=checkout_path,
                role=role,
                env=env,
                cwd=cwd,
                variant=variant,
            )
        except WorktreeNotFoundError:
            target_name = (
                environment_id if environment_id is not None else checkout_path
            )
            return {
                "error": f"environment '{target_name}' not found",
                "code": "not_found",
            }
        except ProcessAlreadyRunningError as exc:
            return {"error": str(exc), "code": "already_running"}
        except CheckoutTargetError as exc:
            raise ValueError(
                _addressing_error_text(
                    exc,
                    tool_name="environment_start",
                    hint=(
                        "Pass environment_id for a known environment, or "
                        "checkout_path to cold-start the primary/main clone."
                    ),
                )
            ) from exc
        except InvalidRepoError as exc:
            # InvalidRepoError subclasses WorktreeError, so this catch must
            # come before the generic `except (WorktreeError,
            # ProcessLifecycleError)` tail below -- same MRO-ordering
            # concern as CheckoutTargetError's catch above (ticket #119).
            # Ticket #123: the engine's message names its internal
            # `repo_root` parameter, which this tool doesn't have -- rename
            # it to `checkout_path` only when the rejected path is the one
            # this wrapper actually received (identity guard), so an
            # InvalidRepoError from some other internally-resolved path is
            # never mislabelled.
            if checkout_path is not None and exc.repo_root == checkout_path:
                raise ValueError(
                    _invalid_path_error_text(exc, param_name="checkout_path")
                ) from exc
            raise ValueError(str(exc)) from exc
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return {**_record_to_dict(record), **_contract_diagnostics(record, role)}

    @mcp.tool()
    def environment_stop(
        environment_id: Optional[str] = None,
        checkout_path: Optional[str] = None,
        role: Optional[str] = None,
        variant: Optional[str] = None,
        timeout: float = 10.0,
        kill_orphans: bool = False,
    ) -> Dict[str, Any]:
        """Stop the process running under a given role for a target
        environment -- a linked worktree or the repo's own primary/main
        clone.

        Addressing the target
        ----------------------
        Same two ways as ``environment_start`` -- ``environment_id`` (the
        normal way) or ``checkout_path`` (the cold-start/primary way; see
        ``environment_start``'s docstring for the full rationale). Passing
        neither, or both when they disagree, raises ``ValueError``.
        Resolution is entirely the engine's job, via its
        ``CheckoutTargetError`` -- this wrapper performs no validation of
        the pair itself -- but it re-words that error's text before
        raising ``ValueError``: the engine's own message names its
        internal ``worktree_id`` parameter and engine-API vocabulary
        (``start()``/``stop()``/``remove()``), so this wrapper replaces it
        with an ``environment_stop``-specific message naming
        ``environment_id`` and ``checkout_path`` instead.

        Similarly, when ``checkout_path`` is given but isn't a usable git
        repository, resolution raises the engine's ``InvalidRepoError``
        (ticket #123). This wrapper re-words that message too, replacing
        the engine-internal ``repo_root`` parameter name -- which this tool
        doesn't have -- with ``checkout_path``, while preserving every byte
        of the underlying diagnostic reason.

        Unlike ``environment_start``, stopping never materialises a primary
        record: an unstarted primary has nothing to stop, so it returns the
        same soft not-found dict (``{"error": "...", "code": "not_found"}``
        -- see "If the target is not found" below) as an unknown
        ``environment_id`` rather than creating a record just to stop it.

        A *linked* worktree differs: ``worktree_create`` already persisted
        its record, so stopping a role that was never started there is not a
        not-found condition. The engine takes its graceful no-op path
        instead -- any contract ``stop:`` steps still run best-effort and no
        signal is sent -- and this tool returns a normal environment record
        whose ``stop_attempt`` is always ``{"outcome": "no_process_recorded",
        ...}``, but whose ``status`` depends on what else is tracked:
        ``"stopped"`` only if popping this role leaves ``pids`` empty *and*
        the record wasn't already ``"stop_incomplete"``/``"orphaned"`` (those
        two are sticky and are never overwritten back to ``"stopped"`` by
        this no-op path); otherwise ``status`` is left unchanged -- e.g.
        still ``"running"`` when another role's process is still tracked.
        Only the *primary* (no record until its first ``environment_start``)
        and a genuinely unknown ``environment_id``/``checkout_path`` yield
        the soft not-found dict.

        ``role`` vs ``variant``
        -----------------------
        See ``environment_start``'s "``role`` vs ``variant``" section for the
        full explanation of why these are independent (``role`` is the
        addressing key a pid is tracked under; ``variant`` only selects which
        contract step ran to start it). Here, ``variant`` lets you stop a
        process **without knowing which role it was started under**:

        - Neither ``role`` nor ``variant`` given: stops ``role="main"``,
          exactly as before this parameter existed.
        - ``variant`` given, ``role`` omitted: resolved against
          ``record.variants`` (populated by a prior
          ``environment_start(variant=...)`` call) to whichever currently-
          running role was started with that variant, and that role is
          stopped.
        - Both given: they must agree -- ``variant`` must resolve to exactly
          the role named by ``role``, or a ``ValueError`` is raised.

        **Symmetry with environment_start (ticket #139):**
        ``environment_start``'s three-tier ``variant="default"`` resolution
        includes a lone-step fallback that can fire for a *named* step (see
        ``environment_start``'s "``role`` vs ``variant``" section). When it
        does, the *engine* records that step's own name in
        ``record.variants[role]`` -- never the literal string ``"default"``.
        This tool compensates for that: a bare ``environment_stop(variant=
        "default")`` call **does resolve** a lone-named-step environment --
        before ever calling the engine, this wrapper pre-resolves
        ``"default"`` to whatever single named ``start:`` step the
        contract declares (mirroring ``environment_start``'s own tier-3
        rule), so the same call that started it can stop it too. This only
        engages when ``record.variants`` does not already contain the
        literal ``"default"`` (an exact-match ``environment_start(variant=
        "default")`` call, or a step literally named ``"default"``, both
        keep resolving via the plain match as before). Passing the step's
        actual name, e.g. ``environment_stop(variant="main")``, or omitting
        ``variant`` and relying on ``role="main"`` (the default), both keep
        working exactly as before.

        Resolution can fail three ways, all surfaced as ``ValueError`` (never
        a soft error dict): the variant matches no currently-running role
        (e.g. a typo, or a role started before this parameter existed and so
        has no recorded variant), the variant matches more than one
        currently-running role (ambiguous -- pass ``role=`` to disambiguate),
        or an explicitly-given ``role`` disagrees with the role ``variant``
        resolves to.

        Parameters
        ----------
        environment_id:
            The normal way to address the target -- see "Addressing the
            target" above.
        checkout_path:
            The cold-start/primary way to address the target -- see
            "Addressing the target" above.
        role:
            Logical role name of the process to stop. Omitting it (the
            default, ``None``) means "use ``main``" *unless* ``variant`` is
            also given, in which case ``variant`` alone resolves the role --
            see "``role`` vs ``variant``" above.
        variant:
            Resolves to the role that was started with this variant (via
            ``record.variants``), so a process started with
            ``environment_start(variant=...)`` can be stopped without
            separately tracking which role it used. Defaults to ``None``
            (no resolution; ``role`` alone selects the target). See
            "``role`` vs ``variant``" above for the full contract, including
            its three ``ValueError`` failure modes.
        timeout:
            Seconds to wait for graceful shutdown (SIGTERM/CtrlBreak) before
            the process is forcibly killed (SIGKILL/TerminateProcess). Defaults
            to ``10.0``.
        kill_orphans:
            When ``True``, after the primary stop signal a cwd/open-file scan
            terminates orphaned grandchild processes that were reparented away
            from the tracked shell wrapper (e.g. a detached GUI started via
            ``Start-Process -PassThru``). Defaults to ``False``
            (backward-compatible).

        Any contract ``stop:`` steps defined in ``.seretos/worktree-setup.yml``
        are executed best-effort before the graceful SIGTERM/CtrlBreak signal is
        sent; failures in those steps are logged but do not prevent the process
        from being stopped. As with ``environment_start``, the engine reads this
        file from ``repo_root`` -- the original repository clone -- not from a
        linked worktree checkout itself; a contract placed only in the
        checkout is silently ignored.

        Step schema: ``stop:`` steps share the same per-step shape as
        ``start:`` steps -- each entry is a YAML mapping with a required
        ``run:`` key (the shell command to execute) and an optional ``name:``
        key. The contract also requires top-level ``version`` (int) and
        ``isolation`` (``full``/``partial``/``none``) keys; ``isolation: none``
        forbids ``start:``, ``stop:``, and ``ports:``. Example::

            version: 1
            isolation: full
            stop:
              - name: web
                run: stop-web.sh

        The operation is idempotent in the sense that if no process is running
        under the given ``role``, this tool returns a soft error dict
        ``{"error": "...", "code": "not_running"}`` rather than raising, so
        callers can treat the already-stopped case gracefully and branch on
        ``code`` rather than parsing the error text. ``code: "not_running"``
        maps the engine's ``ProcessNotRunningError``; it is *not* what a
        tracked-but-never-started role on a linked worktree returns -- that
        case takes the graceful no-op path described above
        (``stop_attempt.outcome: "no_process_recorded"`` always;
        ``status: "stopped"`` only when no other role is still tracked in
        ``pids``), not this one.

        ``not_running`` reachability (ticket #139, version-scoped to the
        pinned engine v0.3.5): this is a genuinely live branch, not a
        documentation-only relic kept "just in case". It fires when the pid
        entry for the resolved role disappears between the engine's own
        ``WorktreeManager.stop()`` snapshotting the record early on and its
        delegated ``process_lifecycle.stop()`` performing its own,
        independent, fresh re-read of the same state-store entry immediately
        before deciding whether to raise ``ProcessNotRunningError`` -- a
        window a **concurrent** writer can close: another ``environment_stop``
        call racing for the same role, or an ``environment_list`` call whose
        ``reconcile()`` pass prunes a dead pid out from under it. This is
        distinct from -- and must not be confused with -- the tracked-but-
        never-started ``no_process_recorded`` no-op path described just
        above, which is reached without any concurrent actor at all.

        On success returns the canonical environment record dict. Fields of
        note:

        - ``status``: ``"stopped"`` after the process has been terminated (see
          the tracked-but-never-started no-op case above for when a linked
          worktree's ``status`` does not unconditionally end up ``"stopped"``).
        - ``backing``: ``"primary"`` for the main clone, ``"worktree"`` for a
          linked worktree.
        - ``pids``: a dict mapping role name to PID; the stopped role's entry
          is removed once the process exits.
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` for environments with no port setup.

        If the target is not found, returns ``{"error": "...", "code":
        "not_found"}`` instead of raising, so callers can treat not-found as
        a soft/idempotent condition, and can branch on ``code`` rather than
        parsing the error text. The message names whichever target
        identifier was supplied (``environment_id`` if given, else
        ``checkout_path``).

        Transport-level failure ("Connection closed"): confirm before retrying
        ---------------------------------------------------------------------
        Read back with ``environment_list(path=<checkout path or repo
        root>)`` and inspect ``pids``: your ``role`` absent means no live
        tracked process remains under it (the stop landed, or the process
        was already gone); the role still present means the stop did not
        land.

        Honest limits: the listing cannot tell you whether the contract's
        ``stop:`` steps ran. The persisted ``stop_detail`` (and a sticky
        ``status: "stop_incomplete"``) is the only stop diagnostic that
        survives into ``environment_list``; the richer ``stop_attempt``
        exists ONLY on this call's own response and is never readable from
        the listing -- so it can never serve as read-back evidence.

        A blind retry that *returns* is safe: it comes back as one of three
        soft outcomes -- ``{"error": "...", "code": "not_found"}`` (mapping
        ``WorktreeNotFoundError``), ``{"error": "...", "code":
        "not_running"}`` (mapping ``ProcessNotRunningError``), or a
        graceful no-op reported as ``stop_attempt.outcome:
        "no_process_recorded"``. But those three are not the only possible
        outcomes -- this is not an exhaustive disjunction. The same call can
        instead **raise ``ValueError``**
        when the target itself fails to resolve: an invalid
        ``checkout_path`` (``InvalidRepoError``, ticket #123), a missing or
        mutually-disagreeing ``environment_id``/``checkout_path`` pair
        (``CheckoutTargetError``), or a ``variant`` that fails to resolve to
        a role (``VariantResolutionError`` -- see "``role`` vs ``variant``"
        above for its three failure modes). Branching on ``code`` is only
        meaningful for a call that *returned*; a raise is a separate path
        the caller must handle independently, not a third value of ``code``.
        """

        # Ticket #139 Part B: pre-resolve a bare variant="default" before
        # ever calling the engine -- not a retry, no nested try/except. See
        # _default_stop_variant's docstring for the full rationale and
        # precedence rule; it never raises, and only ever changes behaviour
        # when variant is exactly "default".
        if variant == "default":
            variant = _default_stop_variant(manager, environment_id, checkout_path)

        try:
            record = manager.stop(
                environment_id,
                checkout_path=checkout_path,
                role=role,
                variant=variant,
                timeout=timeout,
                kill_orphans=kill_orphans,
            )
        except WorktreeNotFoundError:
            target_name = (
                environment_id if environment_id is not None else checkout_path
            )
            return {
                "error": f"environment '{target_name}' not found",
                "code": "not_found",
            }
        except ProcessNotRunningError as exc:
            # Ticket #139 Part C: confirmed reachable, not dead code, for the
            # pinned engine v0.3.5 -- `manager.stop()` (manager.py ~:1510)
            # snapshots the record via `_resolve_target()` and only checks
            # `effective_role not in record.pids` (manager.py ~:1556)
            # against THAT snapshot, after running the best-effort contract
            # `stop:` steps in between. The delegated
            # `process_lifecycle.stop()` then performs its OWN, independent,
            # fresh `store.get(worktree_id)` re-read (process_lifecycle.py
            # ~:2802) immediately before raising this exception if the role
            # is still absent (~:2809). A concurrent writer -- another
            # `environment_stop` call for the same role, or an
            # `environment_list` call whose `reconcile()` pass prunes a dead
            # pid -- can close that pid entry in the gap between the two
            # reads, so this branch is live. Do not "clean this up" as
            # unreachable without re-verifying against whichever
            # lib-python-worktree version is pinned at the time.
            return {"error": str(exc), "code": "not_running"}
        except CheckoutTargetError as exc:
            raise ValueError(
                _addressing_error_text(
                    exc,
                    tool_name="environment_stop",
                    hint=(
                        "Pass environment_id for a known environment, or "
                        "checkout_path to address it directly."
                    ),
                )
            ) from exc
        except VariantResolutionError as exc:
            # VariantResolutionError subclasses WorktreeError, so this catch
            # must come before the generic (WorktreeError, ProcessLifecycleError)
            # tail below -- same MRO-ordering concern as CheckoutTargetError's
            # catch above (ticket #119). The engine's own message already
            # uses role=/variant= vocabulary, so it is passed through
            # verbatim, with a short hint appended pointing at the fix.
            raise ValueError(
                f"{exc} (hint: pass role=<role> explicitly, or see "
                f"environment_start's role-vs-variant docs)"
            ) from exc
        except InvalidRepoError as exc:
            # InvalidRepoError subclasses WorktreeError, so this catch must
            # come before the generic `except (WorktreeError,
            # ProcessLifecycleError)` tail below -- same MRO-ordering
            # concern as CheckoutTargetError's and VariantResolutionError's
            # catches above (tickets #119 / this module's own precedent).
            # Ticket #123: the engine's message names its internal
            # `repo_root` parameter, which this tool doesn't have -- rename
            # it to `checkout_path` only when the rejected path is the one
            # this wrapper actually received (identity guard), so an
            # InvalidRepoError from some other internally-resolved path is
            # never mislabelled.
            if checkout_path is not None and exc.repo_root == checkout_path:
                raise ValueError(
                    _invalid_path_error_text(exc, param_name="checkout_path")
                ) from exc
            raise ValueError(str(exc)) from exc
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)


__all__ = ("register",)
