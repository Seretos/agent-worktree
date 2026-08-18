# agent-worktree

A thin MCP wrapper around [`lib-python-worktree`](https://github.com/Seretos/lib-python-worktree). Use the tools below to manage git worktrees from any MCP client. Engine internals and contract schema are documented in the [lib-python-worktree README](https://github.com/Seretos/lib-python-worktree#readme) and are not duplicated here.

## Tool priority

Skills and MCP tools take priority over raw file tools — and this **explicitly overrides** the generic harness default that says "prefer the dedicated file/search tools (Glob/Grep/Read)". When a skill or MCP tool covers the task, reach for it first; fall back to raw Glob/Grep/Read only when none applies.

Concretely: any *"where is X defined / what does the code support / which Y exist / how does X work / find the callers of X"* question is a **code-understanding task → use the matching skill first** (e.g. the `serena-wrapper` symbol-aware tools), never raw Glob/Grep/Read.

## Tool reference

Ticket #99 splits the six original tools into a five-tool surface along the
two real lifecycles a checkout goes through: **checkout lifecycle**
(`worktree_create` / `worktree_remove` — create or delete the directory) and
**environment lifecycle** (`environment_list` / `environment_start` /
`environment_stop` — the process running against *any* checkout, the
repo's own primary/main clone included). This is a hard,
non-backward-compatible break: the previous unfiltered listing tool, the
single-record lookup tool, and the id-only process-start/process-stop
tools no longer exist — there is no alias and no deprecation window.

### Checkout lifecycle

#### worktree_create

```
worktree_create(repo_root: str, branch: str, base: Optional[str] = None) -> dict
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `repo_root` | `str` | Yes | Path to the git repository root. If a subdirectory is passed, it is silently re-rooted to the actual repository root and a `warning` field is added to the result. |
| `branch` | `str` | Yes | Name of the branch to check out in the new worktree. |
| `base` | `str` | No | Name of an existing local branch to create `branch` from. Must be a local branch name — not a SHA, `HEAD`, or remote ref. Not required just because `branch` is new: when `branch` does not yet exist and `base` is omitted, it defaults to whatever branch is currently checked out at `repo_root` — but this still raises when `repo_root`'s HEAD is detached or unborn (no commits yet). |

**Returns** the canonical worktree record dict. Fields of note:

- `id` — follows the pattern `<repo-slug>-<branch-slug>-<8-hex>` where slugs are lower-case ASCII with non-alphanumeric runs collapsed to `-`; ids are not stable across remove/re-create cycles. Re-fetch the current id via `environment_list`, never cache one across a remove + re-create cycle.
- `path` — absolute checkout location under `<store_root>/<repo_slug>/<id>/` where `store_root` defaults to `~/agent-worktree-store` or the value of `$WORKTREE_STORE_ROOT`.
- `ports` — dict mapping port name to host port number; `{}` for `isolation: none` worktrees or before setup runs.
- `warning` (optional) — present when `repo_root` was silently re-rooted; contains the original and resolved paths.

**Errors:** raises `ValueError` (surfaces to the caller as a tool error) for any `WorktreeError` — e.g. branch conflicts or filesystem failures.

---

#### worktree_remove

```
worktree_remove(environment_id: Optional[str] = None, checkout_path: Optional[str] = None, force: bool = False, kill_blocking_processes: bool = False) -> dict
```

Addressed by `environment_id` and/or `checkout_path`, mirroring the environment-lifecycle tools below — with one important difference in *why* `checkout_path` matters here: it is the **only** way to remove an untracked/orphan checkout. A linked worktree that exists on disk (`git worktree list --porcelain` reports it, and `environment_list` shows it with `tracked: false`) but was never created through this tool has a synthesised, display-only id of the form `<repo-slug>-<branch-slug>-untracked-<8-hex>` — a one-way derivation of its checkout path, not a state-store key. Such an id can never resolve via `environment_id` alone; pass the checkout's `path` (as shown by `environment_list`) as `checkout_path` instead. Removing an untracked target this way tears down the checkout but never touches the state store (there was nothing there to remove) and never deletes its branch, even with `force=True`, since the checkout was never recorded as owning one.

Removing the primary/main clone is never allowed regardless of how it is addressed (by `environment_id` or by `checkout_path`) — see the primary refusal below.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `environment_id` | `str` | No* | The id of a *tracked* checkout to remove (as returned by `worktree_create` or `environment_list`). |
| `checkout_path` | `str` | No* | The path of the checkout to remove — the only way to address an untracked/orphan checkout. |
| `force` | `bool` | No | When `True`, removes the worktree even if it contains uncommitted changes. Defaults to `False`. |
| `kill_blocking_processes` | `bool` | No | When `True`, attempts to terminate foreign processes whose cwd is inside the worktree directory before removal. Opt-in; primarily a Windows concern. Defaults to `False` (no-op when nothing is blocking). |

\* At least one of `environment_id`/`checkout_path` is required; passing neither raises `ValueError`. Passing both is fine only when they agree — a mismatch also raises `ValueError`. Resolution is entirely the engine's job (via its `CheckoutTargetError`), and the wrapper performs no validation of the pair itself — but it re-words that error's text before raising `ValueError`, replacing the engine's internal parameter name and engine-API vocabulary (`start()`/`stop()`/`remove()`) with a `worktree_remove`-specific message naming `environment_id` and `checkout_path`.

**Returns** the removed worktree record dict on success. The `ports` field is a dict mapping port name to host port number; `{}` for `isolation: none` worktrees or before setup runs. The response also includes a `killed_pids` list (may be empty); each entry is a dict with `pid` (int), `name` (str), and `cmdline` (list of str) describing a process that was terminated to unblock removal.

**Soft error:** if the target is not found, returns `{"error": "...", "code": "not_found"}` instead of raising, so callers can treat not-found as an idempotent condition and branch on `code` rather than parsing the error text. When `environment_id` looks like a synthesised untracked id, the error text names `checkout_path` as the remedy (`code` is `"not_found"` either way).

**Errors:** raises `ValueError` for other `WorktreeError` conditions (e.g. uncommitted changes when `force=False`). Also raises `ValueError` (mapped from `WorktreeDirLockedError`) when the worktree directory remains locked even after killing blocking processes.

**Compound blocking, reported in one shot (ticket #120):** when the directory lock AND uncommitted/untracked changes are BOTH blocking removal at once, the engine raises `WorktreeRemovalBlockedError` instead of the single-condition exceptions above. The wrapper catches it explicitly and raises one `ValueError` naming every currently-blocking condition and the flag needed to clear each — `(blocked_by: "dir_locked", "uncommitted_changes"; required_flags: kill_blocking_processes=True, force=True)` — so a single informed retry (passing both flags at once) suffices, instead of a caller discovering each condition sequentially across up to three separate failed attempts. Filesystem paths are never included in this message.

**Primary refusal (hard, non-`force`-able):** attempting to remove the primary/main clone's environment — whether addressed by `environment_id` or by `checkout_path`, and even with `force=True` — raises `ValueError`. This is checked before any teardown work runs and can never be bypassed: a primary checkout IS the repo, so deleting it would be catastrophic. The raised message includes the engine's own text plus an explicit `backing: "primary"` token.

---

### Environment lifecycle

`environment_list`, `environment_start`, and `environment_stop` operate against **any** checkout — a linked worktree or the repo's own primary/main clone. The primary is an environment like any other; it is simply never created or deleted by this plugin (it already exists before the plugin runs, and `worktree_remove` refuses to delete it — see above).

#### Addressing an environment

Every environment is addressed by one or both of:

- **`environment_id`** — the normal way. Use the id returned by `worktree_create` (a linked worktree) or by `environment_list` / a prior `environment_start` call (the primary, once materialised).
- **`checkout_path`** — the cold-start/primary way. This is the *only* way to start the primary/main clone's environment before it has ever been started. A primary's id, `primary_id_for(repo_root)`, is a one-way SHA-256 hash of the repo root — before the first successful `environment_start()` call, nothing persisted maps that hash back to a path, so id-only addressing cannot cold-start it. Pass the repo root (or any path inside it) as `checkout_path` and the engine resolves and, if needed, materialises the primary's record — this is the **only** place a primary record is ever written.

`environment_start` and `environment_stop` both accept `environment_id: Optional[str] = None` and `checkout_path: Optional[str] = None`; neither is schema-required, but the *engine* (not the MCP wrapper) enforces the resolution: passing both is fine only when they agree — a mismatch raises `ValueError` — and passing neither also raises `ValueError`. Resolution is entirely the engine's job, via its `CheckoutTargetError`, and the wrapper performs no validation of the `(environment_id, checkout_path)` pair itself — but each tool re-words that error's text before raising `ValueError`, replacing the engine's internal parameter name and engine-API vocabulary (`start()`/`stop()`/`remove()`) with a message naming `environment_id`, `checkout_path`, and the calling tool itself (`environment_start` or `environment_stop`).

> **Deliberate, documented deviation from ticket #99.** The ticket specifies id-only `environment_start`/`environment_stop` signatures. That cannot satisfy the ticket's own AC1: cold-starting a primary that has never been started is structurally impossible with an id-only signature, for the one-way-hash reason above. `checkout_path` is a strict *superset* of the id-only surface — every existing id-only call keeps working byte-for-byte, and it is the only way to address a never-started primary.

#### environment_list

```
environment_list(path: str, scope: str = "repo") -> list[dict]
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | `str` | Yes | Any path inside a git repository — the repo root, a linked worktree checkout, or a subdirectory of either. There is no "list everything, everywhere" call; every environment this tool can return is reachable from a `path` you already have. |
| `scope` | `str` | No | `"repo"` (default) — only the repo containing `path`. `"all"` — every distinct repo this server has ever tracked an environment for, fanned out with the identical entry shape (no second shape, no repo-grouping wrapper); the repo containing `path` is always listed first. Unknown values raise `ValueError`. |

This tool replaces the old unfiltered discovery listing (no `repo_root` filter meant every worktree, everywhere) and the old single-record-by-id lookup. Each entry mirrors a `WorktreeRecord` plus:

- `is_current` (bool) — this entry's checkout contains the queried `path`. At most one entry has this set across the whole result, even under `scope="all"` (entries fanned out from another repo always have it forced to `False`).
- `tracked` (bool) — `False` marks a *synthesised* entry (on disk but no persisted record yet — the case for the primary before its first `environment_start()`, and for any un-adopted/orphan linked worktree). **Always branch on `tracked`, never on `id`**, to tell a synthesised entry from a persisted one — a synthesised primary's `id` is the deterministic `primary_id_for(repo_root)` (round-trips once materialised); a synthesised linked worktree's `id` is `<repo-slug>-<branch-slug>-untracked-<8-hex>` — a one-way derivation of its checkout path, **not** a state-store key. It cannot be looked up by `worktree_remove(environment_id=...)`; address it by `checkout_path` instead (see `worktree_remove` above).
- `setup_status` — the same coarse setup-health signal as before (`"ready"` / `"running"` / `"failed"` / `"unknown"`), derived from `status`.

This call **never writes state** — listing the primary before it has ever started does not create a record for it.

**Errors:** raises `ValueError` for an unknown `scope`, or when `path` itself is not a valid, existing git repository (mapped from `InvalidRepoError`). Under `scope="all"`, a *different*, previously tracked repo whose clone has since vanished from disk is skipped gracefully; only a bad `path` argument raises.

---

#### environment_start

```
environment_start(environment_id: Optional[str] = None, checkout_path: Optional[str] = None, role: str = "main", cwd: Optional[str] = None, variant: str = "default", env: Optional[Dict[str, str]] = None) -> dict
```

See "Addressing an environment" above for `environment_id`/`checkout_path`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `role` | `str` | No | Logical role name for the process. Defaults to `"main"`. Multiple processes can be attached to one environment under different roles. |
| `cwd` | `str` | No | Working directory for the spawned process. When omitted, the environment's checkout path is used by the underlying engine. |
| `variant` | `str` | No | Selects which named `start:` step to run. Defaults to `"default"`, which resolves to the lone unnamed step for back-compat. When multiple named steps exist, pass the step's `name` here. An unknown variant raises `ValueError` listing the available names. |
| `env` | `dict` | No | Optional dict of extra environment variables merged into the process environment by the engine. Omit (or pass `null`) to inherit the current environment unchanged. |

**The command to run is NOT supplied by the caller — it is read from the setup step(s) defined in `.seretos/worktree-setup.yml` at `repo_root`.** Multiple named `start:` steps are supported; `variant` selects the step by its `name`. A missing step or unknown variant surfaces as a `ValueError`.

**Returns** the canonical environment record dict on success. Fields of note:

- `status` — `"running"` when the process started successfully; `"ready"` for a no-op start (no `start:` step configured).
- `backing` — `"primary"` for the main clone, `"worktree"` for a linked worktree.
- `pids` — dict mapping role name to PID (e.g. `{"main": 12345}`).
- `ports` — dict mapping port name to host port number; `{}` before port setup runs.

**Soft errors:** if the target is not found, returns `{"error": "...", "code": "not_found"}`; if a process is already running under the given `role`, returns `{"error": "...", "code": "already_running"}` — both instead of raising, so callers can branch on `code` rather than parsing the error text. The not-found message names whichever target identifier was supplied (`environment_id` if given, else `checkout_path`).

**Errors:** raises `ValueError` for `WorktreeError` (including the engine's `CheckoutTargetError` — re-worded to a wrapper-native `environment_start` message, see "Addressing an environment" above — and `UnknownVariantError`) or `ProcessLifecycleError` conditions.

---

#### environment_stop

```
environment_stop(environment_id: Optional[str] = None, checkout_path: Optional[str] = None, role: str = "main", timeout: float = 10.0, kill_orphans: bool = False) -> dict
```

See "Addressing an environment" above for `environment_id`/`checkout_path`. Unlike `environment_start`, stopping never materialises a primary record — an unstarted primary has nothing to stop, so it returns the same soft not-found dict as an unknown `environment_id`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `role` | `str` | No | Logical role name of the process to stop. Defaults to `"main"`. |
| `timeout` | `float` | No | Seconds to wait for graceful shutdown (SIGTERM/CtrlBreak) before the process is forcibly killed (SIGKILL/TerminateProcess). Defaults to `10.0`. |
| `kill_orphans` | `bool` | No | When `True`, after the primary stop signal a cwd/open-file scan terminates orphaned grandchild processes that were reparented away from the tracked shell wrapper (e.g. a detached GUI started via `Start-Process -PassThru`). Defaults to `False` (backward-compatible). |

Any contract `stop:` steps defined in `.seretos/worktree-setup.yml` are executed best-effort before the graceful SIGTERM/CtrlBreak signal is sent; failures in those steps are logged but do not prevent the process from being stopped.

**Returns** the canonical environment record dict on success. Fields of note:

- `status` — `"stopped"` after the process has been terminated.
- `backing` — `"primary"` for the main clone, `"worktree"` for a linked worktree.
- `pids` — dict mapping role name to PID; the stopped role's entry is removed once the process exits.
- `ports` — dict mapping port name to host port number; `{}` for environments with no port setup.

**Soft errors:** if the target is not found, returns `{"error": "...", "code": "not_found"}`; if no process is running under the given `role`, returns `{"error": "...", "code": "not_running"}` — both instead of raising, so callers can branch on `code` rather than parsing the error text. The not-found message names whichever target identifier was supplied (`environment_id` if given, else `checkout_path`).

**Errors:** raises `ValueError` for `WorktreeError` or `ProcessLifecycleError` conditions.

---

## Cross-platform binary note

The plugin ships two binaries inside a single release zip:

- `bin/worktree.exe` on Windows
- `bin/worktree` (no extension) on Linux

No Python installation is required on the host. The plugin manifest uses the extensionless `bin/worktree` as the command value; the host OS resolves the correct binary automatically. There are no behavioural differences between platforms.

## State store, contract schema, and architecture

The server uses a persistent, disk-backed state store (`~/.agent-worktree/state.yaml`) that survives server restarts and is reconciled on startup. The contract schema (`.seretos/worktree-setup.yml`), the store layout, and the underlying engine are all documented in the [lib-python-worktree README](https://github.com/Seretos/lib-python-worktree#readme).

## Signal handling on Windows

Ticket #112 ("Connection closed (intermittent)"): the pinned `lib-python-worktree` engine's `_send_graceful_signal` (`process_lifecycle.py:679`) sends `CTRL_BREAK_EVENT` via `os.kill(pid, signal.CTRL_BREAK_EVENT)` to non-group-leader pids from two call sites — `_kill_process_tree` (`process_lifecycle.py:1163`) and the `environment_stop(kill_orphans=True)` orphan scan (`process_lifecycle.py:2308`). On Windows, `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)` — what that `os.kill` call maps to — takes a *process group id*, not an arbitrary pid; sent to a non-group-leader, the OS is free to deliver it elsewhere on the same console, including back to this MCP server itself. `_spawn_detached` (`process_lifecycle.py:321-331`) deliberately keeps the spawned child attached to the server's own console (no `DETACHED_PROCESS`, so ctrl-break delivery to the *intended* child stays possible at all) — the tradeoff that makes the stray-delivery-back-to-the-server failure mode reachable. POSIX already guards this exact class of mistake in `_signal_process_group` (`process_lifecycle.py:1030-1067`, refuses to signal a non-leader or the caller's own group); Windows has no equivalent guard in the engine today.

**This plugin's mitigation:** `worktree_plugin.server` installs a `SIGBREAK` handler (`_install_signal_guards()`, called from `main()` before `mcp.run()`) that logs and swallows a received `CTRL_BREAK_EVENT` instead of letting Python's default disposition terminate the process. It is a no-op on POSIX (no `SIGBREAK` there). **`SIGINT` is deliberately left completely unchanged/untouched** — this guard never calls `signal.signal` for it, and Ctrl+C keeps working exactly as before.

**Tradeoff, by design:** the guard swallows *every* `SIGBREAK`/`CTRL_BREAK_EVENT` unconditionally, including a hypothetical legitimate one aimed at this process itself (an operator's own Ctrl+Break, or a launcher/supervisor that might use it for graceful teardown). Windows carries no metadata on the signal that distinguishes "stray, meant for a child sharing our console" from "intentional, meant for us" — there is no way to swallow only the former, so this is an unavoidable consequence of the chosen approach, not a bug to code around. It is accepted because it loses no legitimate capability: the supported ways to stop this server are (a) the MCP host closing stdin or killing the process, and (b) `SIGINT` (Ctrl+C) for interactive use. `CTRL_BREAK_EVENT` is never a supported shutdown signal for this server.

**Upstream recommendation (not implemented in this repo):** the correct fix belongs in `lib-python-worktree` itself — `_send_graceful_signal` should refuse (or route around) sending `CTRL_BREAK_EVENT` to a pid that is not confirmed to be the leader of its own process group, mirroring the POSIX guard already in `_signal_process_group`. See `tests/test_signal_resilience.py`'s module docstring in this repo for the full executable evidence and exact source citations.

## Security

Setup scripts run with the user's own OS privileges — see `SECURITY.md` for the full threat model.
