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
    WorktreeDirLockedError,
    WorktreeError,
    WorktreeManager,
    WorktreeNotFoundError,
    WorktreeRecord,
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


def _derive_setup_status(status: str) -> str:
    """Map a WorktreeRecord status to a coarse setup-health signal.

    ``"ready"``   -- no managed process; worktree is usable (no-op start).
    ``"running"`` -- managed process is alive.
    ``"failed"``  -- setup: steps ran and at least one step exited non-zero;
                     the worktree directory is left intact for inspection.
    ``"unknown"`` -- process not yet started or has been stopped.
    """
    if status == "ready":
        return "ready"
    if status == "running":
        return "running"
    if status == "setup_failed":
        return "failed"
    return "unknown"


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


def _entry_to_dict(entry: EnvironmentEntry) -> Dict[str, Any]:
    """Shape one ``EnvironmentEntry`` (from ``WorktreeManager.list_repo``)
    into the flat dict returned by ``environment_list``.

    Merges the record's fields with the entry-level ``is_current``/
    ``tracked`` flags and the derived ``setup_status`` signal. Untracked
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
    result["setup_status"] = _derive_setup_status(entry.record.status)
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
        Omit ``base`` when ``branch`` already exists.

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
        always read the ``repo_root`` original.
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
        (a mismatch raises ``ValueError``, from the engine's
        ``CheckoutTargetError``); passing neither also raises ``ValueError``.
        This wrapper performs no validation of the ``(environment_id,
        checkout_path)`` pair itself -- every combination is forwarded
        straight through to the engine.

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
            When ``True``, attempts to terminate foreign processes whose
            current working directory is inside the worktree directory before
            removal. This is an opt-in safety valve, primarily relevant on
            Windows where open handles prevent directory deletion. Defaults
            to ``False`` (no-op when nothing is blocking).

        Returns the removed worktree record on success. The ``ports`` field is
        a dict mapping port name to host port number; empty dict ``{}`` for
        ``isolation: none`` worktrees or before setup runs. Agents read it to
        discover which host ports the worktree's services are bound to.

        The response includes a ``killed_pids`` list (may be empty). Each entry
        is a dict with ``pid`` (int), ``name`` (str), and ``cmdline`` (list of
        str) describing a process that was terminated to unblock removal.

        If the target is not found, returns ``{"error": "..."}`` instead of
        raising, so callers can treat not-found as a soft/idempotent
        condition. When ``environment_id`` looks like a synthesised untracked
        id, the error text names ``checkout_path`` as the remedy.

        Raises ``ValueError`` (mapped from ``WorktreeDirLockedError``) when the
        worktree directory is still locked after attempting to kill blocking
        processes.

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
            return {"error": error_text}
        except WorktreeDirLockedError as exc:
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
        - ``setup_status``: the same coarse setup-health signal documented on
          ``environment_start`` -- ``"ready"``, ``"running"``, ``"failed"``, or
          ``"unknown"``, derived from the record's ``status``.

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
        ``<repo_root>/.seretos/worktree-setup.yml``) produces a silent
        ``{"status": "ready", "pids": {}}`` no-op -- indistinguishable from "no
        contract configured" -- with no error to indicate the misplacement.

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

        Neither is schema-required, but the engine (not this wrapper) enforces
        the resolution: passing both is fine only when they agree (a mismatch
        raises ``ValueError``, from the engine's ``CheckoutTargetError``);
        passing neither also raises ``ValueError``. This wrapper performs no
        validation of the ``(environment_id, checkout_path)`` pair itself --
        every combination is forwarded straight through to the engine.

        (Deliberate, documented deviation from ticket #99's originally
        id-only signature -- see this module's docstring for why an id-only
        surface cannot satisfy the ticket's own AC1, and why ``checkout_path``
        is a strict superset that keeps every existing id-only call working
        unchanged.)

        Multiple named ``start:`` steps are supported; ``variant`` selects the
        step by its ``name``. A single **unnamed** ``start:`` step is
        implicitly the ``"default"`` variant, for back-compat, so
        ``variant="default"`` (the parameter's own default) resolves to it
        without needing a ``name:`` key at all. An unknown variant surfaces
        as a ``ValueError`` listing the available names.

        Step schema: each ``start:`` entry is a YAML mapping with a required
        ``run:`` key (the shell command to execute) and an optional ``name:``
        key (used by ``variant`` to select that step). A single unnamed step
        is the ``"default"`` variant, for back-compat.

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
            roles.
        cwd:
            Working directory for the spawned process. When omitted the
            environment's checkout path is used by the underlying engine.
        variant:
            Selects which named ``start:`` step to run. Defaults to
            ``"default"``, which resolves to the lone unnamed step for
            back-compat. When multiple named steps exist, pass the step's
            ``name`` here. An unknown variant raises ``ValueError`` listing
            the available names.
        env:
            Optional dict of extra environment variables merged into the process
            environment by the engine. Omit (or pass ``None``) to inherit the
            current environment unchanged.

        The operation is idempotent in the sense that if a process is already
        running under the given ``role``, this tool returns a soft error dict
        ``{"error": "..."}`` rather than raising, so callers can treat the
        already-running case gracefully.

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
          ``"ready"`` start where nothing was spawned.

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

        If the target is not found, returns ``{"error": "..."}`` instead of
        raising, so callers can treat not-found as a soft/idempotent
        condition. The message names whichever target identifier was
        supplied (``environment_id`` if given, else ``checkout_path``).
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
            return {"error": f"environment '{target_name}' not found"}
        except ProcessAlreadyRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return {**_record_to_dict(record), **_contract_diagnostics(record, role)}

    @mcp.tool()
    def environment_stop(
        environment_id: Optional[str] = None,
        checkout_path: Optional[str] = None,
        role: str = "main",
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
        neither, or both when they disagree, raises ``ValueError`` from the
        engine's ``CheckoutTargetError`` -- this wrapper performs no
        validation of the pair itself.

        Unlike ``environment_start``, stopping never materialises a primary
        record: an unstarted primary has nothing to stop, so it returns the
        same soft ``{"error": "..."}`` not-found dict as an unknown
        ``environment_id`` rather than creating a record just to stop it.

        Parameters
        ----------
        environment_id:
            The normal way to address the target -- see "Addressing the
            target" above.
        checkout_path:
            The cold-start/primary way to address the target -- see
            "Addressing the target" above.
        role:
            Logical role name of the process to stop; defaults to ``"main"``.
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
        ``{"error": "..."}`` rather than raising, so callers can treat the
        already-stopped case gracefully.

        On success returns the canonical environment record dict. Fields of
        note:

        - ``status``: ``"stopped"`` after the process has been terminated.
        - ``backing``: ``"primary"`` for the main clone, ``"worktree"`` for a
          linked worktree.
        - ``pids``: a dict mapping role name to PID; the stopped role's entry
          is removed once the process exits.
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` for environments with no port setup.

        If the target is not found, returns ``{"error": "..."}`` instead of
        raising, so callers can treat not-found as a soft/idempotent
        condition. The message names whichever target identifier was
        supplied (``environment_id`` if given, else ``checkout_path``).
        """

        try:
            record = manager.stop(
                environment_id,
                checkout_path=checkout_path,
                role=role,
                timeout=timeout,
                kill_orphans=kill_orphans,
            )
        except WorktreeNotFoundError:
            target_name = (
                environment_id if environment_id is not None else checkout_path
            )
            return {"error": f"environment '{target_name}' not found"}
        except ProcessNotRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)


__all__ = ("register",)
