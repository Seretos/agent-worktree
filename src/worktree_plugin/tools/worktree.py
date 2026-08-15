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

import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    CONTRACT_FILENAME,
    CheckoutTargetError,
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
    primary_id_for,
)


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


def _entry_to_dict(entry: EnvironmentEntry) -> Dict[str, Any]:
    """Shape one ``EnvironmentEntry`` (from ``WorktreeManager.list_repo``)
    into the flat dict returned by ``environment_list``.

    Merges the record's fields with the entry-level ``is_current``/
    ``tracked`` flags and the derived ``setup_status`` signal. Untracked
    (synthesised) entries -- ``tracked=False``, and for a synthesised linked
    worktree ``id == ""`` -- pass through unchanged; callers must use
    ``tracked``, never the id, as the "is this persisted" discriminator.
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
        environment_id: str,
        force: bool = False,
        kill_blocking_processes: bool = False,
    ) -> Dict[str, Any]:
        """Remove a tracked worktree checkout by id.

        This tool is deliberately **id-only** -- unlike ``environment_start``/
        ``environment_stop`` it has no ``checkout_path`` parameter. Deleting
        the primary/main clone is never allowed (see below) regardless of how
        it might be addressed, so there is no cold-start case to support here.

        Parameters
        ----------
        environment_id:
            The id of the checkout to remove (as returned by
            ``worktree_create`` or ``environment_list``).
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

        If ``environment_id`` is not found, returns ``{"error": "..."}``
        instead of raising, so callers can treat not-found as a soft/idempotent
        condition.

        Raises ``ValueError`` (mapped from ``WorktreeDirLockedError``) when the
        worktree directory is still locked after attempting to kill blocking
        processes.

        **Primary checkouts are never removed.** Attempting to remove the
        primary/main clone's environment -- even with ``force=True`` -- raises
        ``ValueError``. This refusal is structural, checked before any
        teardown work runs, and cannot be bypassed: a primary checkout IS the
        repo, so deleting it would be catastrophic. The raised message
        includes the engine's own text plus an explicit ``backing: "primary"``
        token so callers can react programmatically without parsing prose.
        """

        try:
            record = manager.remove(
                environment_id,
                force=force,
                kill_blocking_processes=kill_blocking_processes,
            )
        except PrimaryCheckoutError as exc:
            raise ValueError(f'{exc} (backing: "primary")') from exc
        except WorktreeNotFoundError:
            return {"error": f"environment '{environment_id}' not found"}
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
          a synthesised linked worktree's ``id`` is the empty string ``""``.
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
        step by its ``name`` (default ``"default"`` resolves to the lone unnamed
        step for back-compat). An unknown variant surfaces as a ``ValueError``.

        Step schema: each ``start:`` entry is a YAML mapping with a required
        ``run:`` key (the shell command to execute) and an optional ``name:``
        key (used by ``variant`` to select that step). A single unnamed step
        is the ``"default"`` variant, for back-compat.

        The contract file also requires two top-level keys: ``version`` (an
        int) and ``isolation`` (one of ``full``, ``partial``, or ``none``).
        When ``isolation: none`` is set, ``start:``, ``stop:``, and ``ports:``
        are forbidden in the contract. Example::

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
            return {
                "error": f"environment '{environment_id or checkout_path}' not found"
            }
        except ProcessAlreadyRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

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
            return {
                "error": f"environment '{environment_id or checkout_path}' not found"
            }
        except ProcessNotRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)


__all__ = ("register",)
