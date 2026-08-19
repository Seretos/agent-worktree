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
        """

        try:
            record = manager.create(repo_root=repo_root, branch=branch, base=base)
        except SetupFailedError as exc:
            raise ValueError(
                f"Setup failed for worktree (left intact at path for inspection): {exc}"
            ) from exc
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
        ``InvalidRepoError``). Under ``scope="all"``, a *different*, previously
        tracked repo whose on-disk clone has since vanished is skipped
        gracefully rather than failing the whole call -- only a bad ``path``
        argument raises.
        """
        if scope not in ("repo", "all"):
            raise ValueError(
                f"unknown scope {scope!r}; expected 'repo' or 'all'"
            )

        try:
            listing = manager.list_repo(path)
        except InvalidRepoError as exc:
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

        **Asymmetry warning:** when tier 3 of the variant resolution above
        (the lone-step fallback) resolves a *named* step from a bare
        ``variant="default"`` call, the value recorded in
        ``record.variants[role]`` is that step's own name (e.g.
        ``"main"``) -- never the literal string ``"default"`` -- because
        the engine records ``variant=step.name or variant``. A later
        ``environment_stop(variant="default")`` will not resolve against
        that role, since ``record.variants[role]`` is never ``"default"``
        in that case. To stop it, either omit ``variant`` entirely
        (``role`` alone defaults to ``"main"``) or pass the step's actual
        name, e.g. ``environment_stop(variant="main")``.

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
            this relates to the ``role`` parameter, including the
            asymmetry warning about ``environment_stop(variant="default")``
            when the lone-step fallback resolves a named step.
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

        **Asymmetry warning:** ``environment_start``'s three-tier
        ``variant="default"`` resolution includes a lone-step fallback that
        can fire for a *named* step (see ``environment_start``'s "``role``
        vs ``variant``" section). When it does, ``record.variants[role]``
        stores that step's own name -- never the literal string
        ``"default"``. As a result, calling
        ``environment_stop(variant="default")`` afterwards **will not
        resolve** against that role: ``record.variants`` never contains
        ``"default"`` in that case. Use ``role="main"`` (the default) or
        pass the step's actual name as ``variant`` instead.

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
        """

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
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)


__all__ = ("register",)
