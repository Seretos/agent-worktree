"""FastMCP tools for the worktree lifecycle (W2).

These wrap ``WorktreeManager`` and shape its outputs into MCP-friendly
plain-dict payloads.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from lib_python_worktree import (
    CONTRACT_FILENAME,
    KilledProcessInfo,
    ProcessAlreadyRunningError,
    ProcessLifecycleError,
    ProcessNotRunningError,
    SetupFailedError,
    WorktreeDirLockedError,
    WorktreeError,
    WorktreeManager,
    WorktreeNotFoundError,
    WorktreeRecord,
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


def register(mcp: FastMCP, manager: WorktreeManager) -> None:
    """Register the W2 tools against the given FastMCP server."""

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

        Returns the canonical worktree record. Fields of note:

        - ``id``: follows the pattern ``<repo-slug>-<branch-slug>-<8-hex>``
          where slugs are lower-case ASCII with non-alphanumeric runs collapsed
          to ``-``; ids are not stable across remove/re-create cycles.
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
                    f" contract directory '{src_contract_dir}' into it: {exc}"
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
    def worktree_list(repo_root: Optional[str] = None) -> List[Dict[str, Any]]:
        """List worktrees reflected in the persistent, disk-backed worktree state
        (survives server restarts; reconciled on startup).

        Parameters
        ----------
        repo_root:
            Optional path to a git repository root. When provided, only
            worktrees whose ``repo_root`` resolves to the same directory are
            returned (subdirectory paths are resolved via ``Path.resolve()``
            before comparison). Omit to return all worktrees across all repos.

        Each entry mirrors a ``WorktreeRecord``. The ``ports`` field is a dict
        mapping port name to host port number; empty dict ``{}`` for
        ``isolation: none`` worktrees or before setup runs. Agents read it to
        discover which host ports the worktree's services are bound to.
        """

        records = manager.list()

        if repo_root is not None:
            resolved_filter = Path(repo_root).expanduser().resolve()
            records = [
                r for r in records
                if Path(r.repo_root).resolve() == resolved_filter
            ]

        return [_record_to_dict(r) for r in records]

    @mcp.tool()
    def worktree_remove(
        worktree_id: str,
        force: bool = False,
        kill_blocking_processes: bool = False,
    ) -> Dict[str, Any]:
        """Remove a tracked worktree by id.

        Passes through to the manager's teardown hook (W8 will extend this
        with full teardown semantics).

        Parameters
        ----------
        worktree_id:
            The id of the worktree to remove (as returned by
            ``worktree_create`` or ``worktree_list``).
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

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.

        Raises ``ValueError`` (mapped from ``WorktreeDirLockedError``) when the
        worktree directory is still locked after attempting to kill blocking
        processes.
        """

        try:
            record = manager.remove(
                worktree_id,
                force=force,
                kill_blocking_processes=kill_blocking_processes,
            )
        except WorktreeNotFoundError:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        except WorktreeDirLockedError as exc:
            raise ValueError(str(exc)) from exc
        except WorktreeError as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_start(
        worktree_id: str,
        role: str = "main",
        cwd: Optional[str] = None,
        variant: str = "default",
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Start a detached process for a tracked worktree.

        The command to run is **not** supplied by the caller — it is read from
        the worktree contract's ``start:`` steps in ``.seretos/worktree-setup.yml``
        inside the worktree. Multiple named ``start:`` steps are supported;
        ``variant`` selects the step by its ``name`` (default ``"default"``
        resolves to the lone unnamed step for back-compat). An unknown variant
        surfaces as a ``ValueError``.

        Parameters
        ----------
        worktree_id:
            The id of the worktree (as returned by ``worktree_create`` or
            ``worktree_list``).
        role:
            Logical role name for the process; defaults to ``"main"``. Multiple
            processes can be attached to one worktree under different roles.
        cwd:
            Working directory for the spawned process. When omitted the worktree
            path is used by the underlying engine.
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

        On success returns the canonical worktree record dict. Fields of note:

        - ``status``: ``"running"`` when the process started successfully.
        - ``pids``: a dict mapping role name to PID (e.g. ``{"main": 12345}``).
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` before port setup runs.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        try:
            record = manager.start(worktree_id, role=role, env=env, cwd=cwd, variant=variant)
        except WorktreeNotFoundError:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        except ProcessAlreadyRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_stop(
        worktree_id: str,
        role: str = "main",
        timeout: float = 10.0,
        kill_orphans: bool = False,
    ) -> Dict[str, Any]:
        """Stop the process running under a given role for a tracked worktree.

        Parameters
        ----------
        worktree_id:
            The id of the worktree (as returned by ``worktree_create`` or
            ``worktree_list``).
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
        from being stopped.

        The operation is idempotent in the sense that if no process is running
        under the given ``role``, this tool returns a soft error dict
        ``{"error": "..."}`` rather than raising, so callers can treat the
        already-stopped case gracefully.

        On success returns the canonical worktree record dict. Fields of note:

        - ``status``: ``"stopped"`` after the process has been terminated.
        - ``pids``: a dict mapping role name to PID; the stopped role's entry
          is removed once the process exits.
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` for worktrees with no port setup.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        try:
            record = manager.stop(worktree_id, role=role, timeout=timeout, kill_orphans=kill_orphans)
        except WorktreeNotFoundError:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        except ProcessNotRunningError as exc:
            return {"error": str(exc)}
        except (WorktreeError, ProcessLifecycleError) as exc:
            raise ValueError(str(exc)) from exc
        return _record_to_dict(record)

    @mcp.tool()
    def worktree_get(worktree_id: str) -> Dict[str, Any]:
        """Retrieve a single tracked worktree by id without removing it.

        Returns the canonical worktree record on success. Fields of note:

        - ``id``: follows the pattern ``<repo-slug>-<branch-slug>-<8-hex>``
          where slugs are lower-case ASCII with non-alphanumeric runs collapsed
          to ``-``; ids are not stable across remove/re-create cycles.
        - ``path``: absolute checkout location under
          ``<store_root>/<repo_slug>/<id>/`` where ``store_root`` defaults to
          ``~/agent-worktree-store`` or the value of ``$WORKTREE_STORE_ROOT``.
        - ``ports``: a dict mapping port name to host port number; empty dict
          ``{}`` for ``isolation: none`` worktrees or before setup runs.
        - ``setup_status``: coarse setup-health signal derived from the
          worktree's ``status`` field. One of:

          - ``"ready"`` -- no managed process; worktree is usable (no-op start).
          - ``"running"`` -- managed process is alive.
          - ``"failed"`` -- setup: steps ran and at least one step exited
            non-zero; the worktree directory is left intact for inspection.
          - ``"unknown"`` -- process not yet started or has been stopped.

        If ``worktree_id`` is not found, returns ``{"error": "..."}`` instead
        of raising, so callers can treat not-found as a soft/idempotent
        condition.
        """

        record = manager.state.get(worktree_id)
        if record is None:
            return {"error": f"worktree_id '{worktree_id}' not found"}
        result = _record_to_dict(record)
        result["setup_status"] = _derive_setup_status(record.status)
        return result


__all__ = ("register",)
