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
- `start_variants` (always present, unlike `warning`) — the raw list of *named* `start:` step names declared by the contract (unnamed steps excluded); `None` when there is no contract file to read or it exists but couldn't be read/parsed, `[]` when the contract was read successfully but declares no named `start:` steps. This is purely the contract's declared names — contrast with `environment_list`'s own `start_variants` key, which is engine-populated and may include the synthesised `"default"` entry when a fallback tier is reachable; `worktree_create`'s value here never does.

**Errors:** raises `ValueError` (surfaces to the caller as a tool error) for any `WorktreeError` — e.g. branch conflicts or filesystem failures. Retrying a create whose response was lost raises a duplicate error that names the landed environment inline: `(existing_environment_id: "<id>", existing_path: "<path>")` (ticket #116) — best-effort only: when the landed record's lookup misses or raises, only the engine's own bare "already exists" text is raised and no id is invented.

---

#### worktree_remove

```
worktree_remove(environment_id: Optional[str] = None, checkout_path: Optional[str] = None, force: bool = False, kill_blocking_processes: bool = False) -> dict
```

Addressed by `environment_id` and/or `checkout_path`, mirroring the environment-lifecycle tools below — with one important difference in *why* `checkout_path` matters here: it is the **only** way to remove an untracked/orphan checkout. A linked worktree that exists on disk (`git worktree list --porcelain` reports it, and `environment_list` shows it with `tracked: false`) but was never created through this tool has a synthesised, display-only id of the form `<checkout-dirname-slug>-untracked-<8-hex>`: the checkout directory's own basename, slugged (lower-case ASCII, non-alphanumeric runs collapsed to `-`, truncated to 40 chars), plus the first 8 hex characters of a SHA-256 hash of its resolved path — no repo slug and no branch slug are involved, and it is not a state-store key. Such an id can never resolve via `environment_id` alone; pass the checkout's `path` (as shown by `environment_list`) as `checkout_path` instead. Removing an untracked target this way tears down the checkout but never touches the state store (there was nothing there to remove) and never deletes its branch, even with `force=True`, since the checkout was never recorded as owning one.

Removing the primary/main clone is never allowed regardless of how it is addressed (by `environment_id` or by `checkout_path`) — see the primary refusal below.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `environment_id` | `str` | No* | The id of a *tracked* checkout to remove (as returned by `worktree_create` or `environment_list`). |
| `checkout_path` | `str` | No* | The path of the checkout to remove — the only way to address an untracked/orphan checkout. |
| `force` | `bool` | No | When `True`, removes the worktree even if it contains uncommitted changes. Defaults to `False`. |
| `kill_blocking_processes` | `bool` | No | When `True`, attempts to terminate **foreign** processes whose cwd is inside the worktree directory before removal. Opt-in; primarily a Windows concern. Defaults to `False` (no-op when nothing is blocking). Not needed for a process you started via `environment_start` — removal stops every tracked role first; best-effort, so a tracked process that refuses to die still blocks. |

\* At least one of `environment_id`/`checkout_path` is required; passing neither raises `ValueError`. Passing both is fine only when they agree — a mismatch also raises `ValueError`. Resolution is entirely the engine's job (via its `CheckoutTargetError`), and the wrapper performs no validation of the pair itself — but it re-words that error's text before raising `ValueError`, replacing the engine's internal parameter name and engine-API vocabulary (`start()`/`stop()`/`remove()`) with a `worktree_remove`-specific message naming `environment_id` and `checkout_path`.

**Returns** the removed worktree record dict on success. The `ports` field is a dict mapping port name to host port number; `{}` for `isolation: none` worktrees or before setup runs. The response also includes a `killed_pids` list (may be empty); each entry is a dict with `pid` (int), `name` (str), `cmdline` (list of str) and `cmdline_raw` (list of str, or `None`) describing a process that was terminated to unblock removal. `cmdline` is agent-readable: a PowerShell/pwsh `-EncodedCommand` base64 blob argv is decoded in place to the human-readable script text (ticket #153; decoding lives in the pinned engine, ticket #132), with the original, untouched argv preserved in `cmdline_raw` (`None` when no such decoding happened).

**Soft error:** if the target is not found, returns `{"error": "...", "code": "not_found"}` instead of raising, so callers can treat not-found as an idempotent condition and branch on `code` rather than parsing the error text. When `environment_id` looks like a synthesised untracked id, the error text names `checkout_path` as the remedy (`code` is `"not_found"` either way).

**Errors:** raises `ValueError` for other `WorktreeError` conditions (e.g. uncommitted changes when `force=False`). Also raises `ValueError` (mapped from `WorktreeDirLockedError`) when the worktree directory remains locked even after killing blocking processes. An unusable `checkout_path` (does not exist, is not a directory, or is not a git repository) raises `ValueError` whose message names `checkout_path` — the engine's `InvalidRepoError` text is re-worded (ticket #123) to replace its internal `repo_root` parameter name with `checkout_path`, with the full diagnostic reason preserved.

**Compound blocking, reported in one shot (ticket #120):** when the directory lock AND uncommitted/untracked changes are BOTH blocking removal at once, the engine raises `WorktreeRemovalBlockedError` instead of the single-condition exceptions above. The wrapper catches it explicitly and raises one `ValueError` naming every currently-blocking condition and the flag needed to clear each — `(blocked_by: "dir_locked", "uncommitted_changes"; required_flags: kill_blocking_processes=True, force=True)` — so a single informed retry (passing both flags at once) suffices, instead of a caller discovering each condition sequentially across up to three separate failed attempts. Filesystem paths are never included in this message.

**Primary refusal (hard, non-`force`-able):** attempting to remove the primary/main clone's environment — whether addressed by `environment_id` or by `checkout_path`, and even with `force=True` — raises `ValueError`. This is checked before any teardown work runs and can never be bypassed: a primary checkout IS the repo, so deleting it would be catastrophic. The raised message includes the engine's own text plus an explicit `backing: "primary"` token.

---

### Environment lifecycle

`environment_list`, `environment_start`, and `environment_stop` operate against **any** checkout — a linked worktree or the repo's own primary/main clone. The primary is an environment like any other; it is simply never created or deleted by this plugin (it already exists before the plugin runs, and `worktree_remove` refuses to delete it — see above).

#### Addressing an environment

Every environment is addressed by one or both of:

- **`environment_id`** — the normal way. Use the id returned by `worktree_create` (a linked worktree) or by `environment_list` / a prior `environment_start` call (the primary, once materialised).
- **`checkout_path`** — the cold-start/primary way. This is the *only* way to start the primary/main clone's environment before it has ever been started. A primary's id, `primary_id_for(repo_root)`, is a one-way SHA-256 hash of the repo root — before the first successful `environment_start()` call, nothing persisted maps that hash back to a path, so id-only addressing cannot cold-start it. Pass the repo root (or any path inside it) as `checkout_path` and the engine resolves and, if needed, materialises the primary's record — this is the **only** place a primary record is ever written.

`environment_start` and `environment_stop` both accept `environment_id: Optional[str] = None` and `checkout_path: Optional[str] = None`; neither is schema-required, but the *engine* (not the MCP wrapper) enforces the resolution: passing both is fine only when they agree — a mismatch raises `ValueError` — and passing neither also raises `ValueError`. Resolution is entirely the engine's job, via its `CheckoutTargetError`, and the wrapper performs no validation of the `(environment_id, checkout_path)` pair itself — but each tool re-words that error's text before raising `ValueError`, replacing the engine's internal parameter name and engine-API vocabulary (`start()`/`stop()`/`remove()`) with a message naming `environment_id`, `checkout_path`, and the calling tool itself (`environment_start` or `environment_stop`). The same re-wording also applies to the engine's `InvalidRepoError` (ticket #123), raised when `checkout_path` is given but isn't a usable git repository — its internal `repo_root` parameter name is replaced with `checkout_path`, with the full diagnostic reason preserved.

> **Deliberate, documented deviation from ticket #99.** The ticket specifies id-only `environment_start`/`environment_stop` signatures. That cannot satisfy the ticket's own AC1: cold-starting a primary that has never been started is structurally impossible with an id-only signature, for the one-way-hash reason above. `checkout_path` is a strict *superset* of the id-only surface — every existing id-only call keeps working byte-for-byte, and it is the only way to address a never-started primary.

#### `role` vs `variant`

These two parameters are independent and easy to conflate:

- **`role`** is the *tracking/addressing key* a process's pid is filed under (`record.pids[role]`). It defaults to `"main"` **regardless of which `variant` was requested** — starting `variant="gui"` with no explicit `role` still records its pid under `role="main"`, exactly like starting the default variant would. `record.pids`/`record.variants` key on this **verbatim** `role` string, which is *nearly* how the start-log filename is derived (sanitised, but case-preserved) — see `start_log_path` below; the origin reference is `Seretos/lib-python-worktree#111` — not this repository's own already-closed issue of the same number, an unrelated thread-leak ticket.
- **`variant`** only selects *which* contract `start:` step is run (by its `name`). It has no effect on where the resulting pid is filed.

Because the two are independent, two variants started concurrently against the same environment need two *distinct* `role`s — reusing the same (default) role on the second call returns/errors with an `already_running` condition, even though a different `variant` was requested. Whichever `variant` actually started a given `role` is remembered in `record.variants[role]`, so a later `environment_stop(variant=...)` call can resolve and stop that role without the caller separately tracking which role it used: with `role` omitted, `variant` alone resolves the role to stop (raising `ValueError` if the variant matches zero or more than one currently-running role, or if an explicitly-given `role` disagrees with what `variant` resolves to). Neither given stops `role="main"`, as before this parameter existed.

**Resolving `variant="default"`.** Three tiers, tried in order: (1) an exact `name:` match against `variant`; (2) exactly one **unnamed** `start:` step — implicitly the `"default"` variant, for back-compat; (3) exactly one `start:` step overall — even if that single step is named rather than unnamed (upstream lib-python-worktree#112, shipped in the pinned v0.3.5) — so a contract whose sole step carries a `name:` other than `"default"` still resolves without the caller passing `variant` explicitly. Two or more `start:` steps with none of them named `"default"` still raise `ValueError` listing the available names, even under tier 3.

**Symmetry with environment_stop (ticket #139).** When tier 3 (the lone-step fallback) resolves a *named* step from a bare `variant="default"` call, the *engine* records that step's own name in `record.variants[role]` (e.g. `"main"`) — never the literal string `"default"` — because the engine itself records `variant=step.name or variant`. This wrapper compensates: `environment_stop(variant="default")` **does resolve** against that role — before calling the engine, `environment_stop` pre-resolves a bare `"default"` to the contract's single named `start:` step (mirroring `environment_start`'s own tier-3 rule), so the call that started a lone named step can stop it the same way, with no need to track or pass the step's actual name. Passing the step's actual name as `variant`, or omitting `variant` and relying on `role="main"` (the default), both keep working exactly as before.

#### Injected env vars

Every `setup:`/`start:`/`stop:`/`teardown:` step's shell process automatically receives `WORKTREE_ID`, `WORKTREE_PATH`, and `WORKTREE_BRANCH` identifying the environment it runs against, plus one `WORKTREE_PORT_<NAME>` per allocated `ports:` slot — `<NAME>` is the slot's `name:` upper-cased (e.g. a `ports:` slot named `app` becomes `WORKTREE_PORT_APP`). `environment_start`'s `env=` parameter is merged in last and can override any of these injected values.

#### environment_list

```
environment_list(path: str, scope: str = "repo", repos: Optional[list[str]] = None) -> list[dict]
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | `str` | Yes | Any path inside a git repository — the repo root, a linked worktree checkout, or a subdirectory of either. There is no "list everything, everywhere" call; every environment this tool can return is reachable from a `path` you already have. |
| `scope` | `str` | No | `"repo"` (default) — only the repo containing `path`. `"all"` — every distinct repo this server has ever tracked an environment for, fanned out with the identical entry shape (no second shape, no repo-grouping wrapper); the repo containing `path` is always listed first. Unknown values raise `ValueError`. |
| `repos` | `list[str]` | No | Ticket #150. Allow-list narrowing the `scope="all"` fan-out to specific repos or everything under a parent directory — is only valid with scope='all', raising `ValueError` if combined with `scope="repo"`. Each entry is a repo root or parent directory; a tracked repo root is included only if its resolved path is at or under one entry's resolved path (containment match). The repo containing `path` is always included and always listed first regardless of `repos`. `repos=[]` means no additional repos (same result set as `scope="repo"`). An entry matching no tracked repo is silently ignored. `repos=None` (the default) disables filtering — identical to pre-#150 behaviour. |

This tool replaces the old unfiltered discovery listing (no `repo_root` filter meant every worktree, everywhere) and the old single-record-by-id lookup. Each entry mirrors a `WorktreeRecord` plus:

- `is_current` (bool) — this entry's checkout contains the queried `path`. At most one entry has this set across the whole result, even under `scope="all"` (entries fanned out from another repo always have it forced to `False`).
- `tracked` (bool) — `False` marks a *synthesised* entry (on disk but no persisted record yet — the case for the primary before its first `environment_start()`, and for any un-adopted/orphan linked worktree). **Always branch on `tracked`, never on `id`**, to tell a synthesised entry from a persisted one — a synthesised primary's `id` is the deterministic `primary_id_for(repo_root)` (round-trips once materialised); a synthesised linked worktree's `id` is `<checkout-dirname-slug>-untracked-<8-hex>`: the checkout directory's own basename, slugged (lower-case ASCII, non-alphanumeric runs collapsed to `-`, truncated to 40 chars), plus the first 8 hex characters of a SHA-256 hash of its resolved path — no repo slug and no branch slug are involved, and it is **not** a state-store key. It cannot be looked up by `worktree_remove(environment_id=...)`; address it by `checkout_path` instead (see `worktree_remove` above). Look-alike caveat: a worktree this tool created and later lost the record for has a directory basename that already looks like `<repo-slug>-<branch-slug>-<8-hex>` (the tracked create-id shape), so its untracked id can visually appear to contain a repo slug and a branch slug even though it does not derive from either — a hand-made orphan's prefix is simply whatever the directory happens to be named.
- `setup_status` — a coarse setup-health signal derived SOLELY from the record's `setup_outcome` (never from `status`, the overall run status — full decoupling, ticket #117): `"unknown"` when `setup_outcome` is `None` (the `setup:` hook was never reached — a legacy/adopted/synthesised record); otherwise the verbatim `setup_outcome.status` — `"completed"` / `"failed"` / `"skipped"`. This value survives later rewrites of `status` by `start`/`stop`/`reconcile`. Each entry's full `setup_outcome` dict (`message`, `completed_at`, `steps_run`, `failed_step_index`, `failed_step_name`, `log_path`, `returncode`, `timed_out`) is also present for detail.

This call **never writes state** — listing the primary before it has ever started does not create a record for it.

**Errors:** raises `ValueError` for an unknown `scope`, when `path` itself is not a valid, existing git repository (mapped from `InvalidRepoError`, re-worded per ticket #123's pattern to replace the engine's internal `repo_root` parameter name with `path`, with the full diagnostic reason preserved), or when `repos` is combined with `scope="repo"` (ticket #150 — `repos` is only valid with scope='all'). Under `scope="all"`, a *different*, previously tracked repo whose clone has since vanished from disk is skipped gracefully; only a bad `path` argument raises.

---

#### environment_start

```
environment_start(environment_id: Optional[str] = None, checkout_path: Optional[str] = None, role: str = "main", cwd: Optional[str] = None, variant: str = "default", env: Optional[Dict[str, str]] = None) -> dict
```

See "Addressing an environment" above for `environment_id`/`checkout_path`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `role` | `str` | No | Logical role name for the process. Defaults to `"main"`. Multiple processes can be attached to one environment under different roles. See "`role` vs `variant`" above. |
| `cwd` | `str` | No | Working directory for the spawned process. When omitted, the environment's checkout path is used by the underlying engine. |
| `variant` | `str` | No | Selects which named `start:` step to run. Defaults to `"default"`, which resolves via the three-tier rule under "`role` vs `variant`" above (exact `name:` match; else the lone unnamed step; else the lone step overall, named or not, if the contract declares exactly one). Two or more steps with none named `"default"` still raise `ValueError` listing the available names. |
| `env` | `dict` | No | Optional dict of extra environment variables merged into the process environment by the engine. Omit (or pass `null`) to inherit the current environment unchanged. |

**The command to run is NOT supplied by the caller — it is read from the setup step(s) defined in `.seretos/worktree-setup.yml` at `repo_root`.** Multiple named `start:` steps are supported; `variant` selects the step by its `name`. A missing step or unknown variant surfaces as a `ValueError`.

**Returns** the canonical environment record dict on success. Fields of note:

- `status` — `"running"` when the process started successfully; `"ready"` for a no-op start (no `start:` step configured).
- `backing` — `"primary"` for the main clone, `"worktree"` for a linked worktree.
- `pids` — dict mapping role name to PID (e.g. `{"main": 12345}`).
- `ports` — dict mapping port name to host port number; `{}` before port setup runs.
- `start_log_path` — filesystem path to the captured startup log. **Casing caveat:** the filename is `start-<slug(role)>.log`, a **case-preserving** slug (never lower-cased) with non-alphanumeric runs collapsed to `-`, truncated to 40 chars, and falling back to `_` for a role with no alphanumeric characters — unlike `pids`/`record.variants`, which key on the verbatim `role`. Two roles differing only in case produce two distinct filenames, but on a case-insensitive filesystem (Windows, default macOS) those names collide and their append-mode output interleaves. This is an accepted, documented upstream limitation, tracked at its origin as `Seretos/lib-python-worktree#111` (the lower-casing bug it originally reported was fixed upstream in the pinned v0.3.7).
- Contract diagnostics (ticket #103) — five additive keys computed by this wrapper: `contract_found`, `contract_path`, `contract_isolation`, `steps_run`, `no_op_reason`.
- `shadowed_contract` — a **separate, engine-produced** diagnostic (`lib-python-worktree`, upstream #100), not derived by this wrapper: `None` or `{path, used_path, reason, message}` with `reason` ∈ `{"differs", "unreadable"}`. Transient — never written to `state.yaml`. `None` for a primary, for `checkout == repo_root`, and for the identical copy `worktree_create` writes.

**Soft errors:** if the target is not found, returns `{"error": "...", "code": "not_found"}`; if a process is already running under the given `role`, returns `{"error": "...", "code": "already_running"}` — both instead of raising, so callers can branch on `code` rather than parsing the error text. The not-found message names whichever target identifier was supplied (`environment_id` if given, else `checkout_path`).

**Errors:** raises `ValueError` for `WorktreeError` (including the engine's `CheckoutTargetError` — re-worded to a wrapper-native `environment_start` message, see "Addressing an environment" above — and `UnknownVariantError`) or `ProcessLifecycleError` conditions.

---

#### environment_stop

```
environment_stop(environment_id: Optional[str] = None, checkout_path: Optional[str] = None, role: Optional[str] = None, variant: Optional[str] = None, timeout: float = 10.0, kill_orphans: bool = False) -> dict
```

See "Addressing an environment" above for `environment_id`/`checkout_path`. Unlike `environment_start`, stopping never materialises a primary record — an unstarted primary has nothing to stop, so it returns the same soft not-found dict as an unknown `environment_id`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `role` | `Optional[str]` | No | Logical role name of the process to stop. Defaults to `None`, meaning "use `main`" *unless* `variant` is also given, in which case `variant` alone resolves the role. See "`role` vs `variant`" above. |
| `variant` | `Optional[str]` | No | Resolves to the role that was started with this variant (via `record.variants`), so a process can be stopped without knowing which role it was started under. Defaults to `None`. See "`role` vs `variant`" above for the full resolution contract, including the three ways it can raise `ValueError`. |
| `timeout` | `float` | No | Seconds to wait for graceful shutdown (SIGTERM/CtrlBreak) before the process is forcibly killed (SIGKILL/TerminateProcess). Defaults to `10.0`. |
| `kill_orphans` | `bool` | No | The process-tree/Job Object kill is **unconditional** — always runs regardless of this flag, and on Windows a `Start-Process`/`ShellExecuteEx`-delegated grandchild is already killed via the Job Object without `kill_orphans` (breakaway is refused). `kill_orphans=True` instead runs a **path-scoped** scan (cwd/cmdline/open-file/Windows handle table) under the checkout path, needed only for: a POSIX `setsid()` escape; a Windows job that failed to create/assign or whose handle is unavailable at stop time; a `setup:`-step process never entered in `pids`; or the sub-millisecond `Popen`-to-assignment window. It does not help a `job_member_list_truncated` outcome. Do not pass it defensively — let `stop_detail.kill_orphans_may_help` on a `stop_incomplete` response tell you when to re-call with it. Defaults to `False` (backward-compatible). |

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

Ticket #112 ("Connection closed (intermittent)"): before the v0.3.12 pin, the `lib-python-worktree` engine's `_send_graceful_signal` (`process_lifecycle.py:819-836`, verified against v0.3.11) sent `CTRL_BREAK_EVENT` via `os.kill(pid, signal.CTRL_BREAK_EVENT)` to non-group-leader pids from two call sites — `_kill_process_tree` (`process_lifecycle.py:1579`, verified against v0.3.11; reached from the redesigned teardown module's `_phase_stop_processes`) and the `environment_stop(kill_orphans=True)` orphan scan inside `_kill_blocking_processes` (`process_lifecycle.py:3165`, verified against v0.3.11). On Windows, `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)` — what that `os.kill` call maps to — takes a *process group id*, not an arbitrary pid; sent to a non-group-leader, the OS was free to deliver it elsewhere on the same console, including back to this MCP server itself. `_spawn_detached` (`process_lifecycle.py:441`, comment at `469-479`, verified against v0.3.11) deliberately kept the spawned child attached to the server's own console (no `DETACHED_PROCESS`, so ctrl-break delivery to the *intended* child stayed possible at all) — the tradeoff that made the stray-delivery-back-to-the-server failure mode reachable. POSIX already guarded this exact class of mistake in `_signal_process_group` (`process_lifecycle.py:1416-1453`, verified against v0.3.11; refuses to signal a non-leader or the caller's own group). **As of the pinned v0.3.12, upstream PR #151 closed the Windows gap too:** `_send_graceful_signal` gained a `group_leader` keyword-only parameter and now refuses/skips issuing `CTRL_BREAK_EVENT` at all unless the caller has confirmed process-group leadership, bringing Windows to parity with the POSIX guard above.

**This plugin's backstop (defence-in-depth):** `worktree_plugin.server` installs a `SIGBREAK` handler (`_install_signal_guards()`, called from `main()` before `mcp.run()`) that logs and swallows a received `CTRL_BREAK_EVENT` instead of letting Python's default disposition terminate the process. It is a no-op on POSIX (no `SIGBREAK` there). As of v0.3.12 this handler is a defence-in-depth layer on top of the engine's own group-leader guard, not the only mitigation in play. **`SIGINT` is deliberately left completely unchanged/untouched** — this guard never calls `signal.signal` for it, and Ctrl+C keeps working exactly as before.

**Tradeoff, by design:** the guard swallows *every* `SIGBREAK`/`CTRL_BREAK_EVENT` it receives, including a hypothetical legitimate one aimed at this process itself (an operator's own Ctrl+Break, or a launcher/supervisor that might use it for graceful teardown). Windows carries no metadata on the signal that distinguishes "stray, meant for a child sharing our console" from "intentional, meant for us" — there is no way to swallow only the former, so this remains an unavoidable consequence of the chosen approach, not a bug to code around, even now that the engine's own guard makes the stray case rarer. It is accepted because it loses no legitimate capability: the supported ways to stop this server are (a) the MCP host closing stdin or killing the process, and (b) `SIGINT` (Ctrl+C) for interactive use. `CTRL_BREAK_EVENT` is never a supported shutdown signal for this server.

**Upstream fix landed in v0.3.12 (PR #151).** `_send_graceful_signal` now refuses (skips) sending `CTRL_BREAK_EVENT` to a pid that is not confirmed to be the leader of its own process group, mirroring the POSIX guard already in `_signal_process_group` — exactly the fix this document previously described as unimplemented. See `tests/test_signal_resilience.py`'s module docstring in this repo for the full executable evidence and exact source citations, including its own v0.3.12 status update.

**History.** This repo's own already-closed thread-leak ticket (unrelated to upstream's numbering -- see `tests/test_thread_leak_regression.py`) was mitigated by pinning v0.3.3 → #112 (this stray-`CTRL_BREAK_EVENT` defect, mitigated in-repo by the SIGBREAK guard above and, at the source-dependency level, by upstream PR #151) → #116 ("Connection closed" / lost responses, a partially-overlapping symptom, see below -- the relevant fix has not shipped in any released build, see the build-provenance note below) → #159 and #169 (follow-up investigation and hardening of this plugin's signal-handling story) → #176 (this pin bump to v0.3.12, which brings in upstream PR #151's group-leader guard at the source-dependency level and retires the "upstream recommendation, not implemented" framing above).

## Transport-level failures ("Connection closed")

A tool call can die with `Connection closed` / `MCP error -32000` before its
JSON-RPC response is written. The response is lost; the operation may well have
landed. Two sub-symptoms have been reported (ticket #116):

**Masked success on a stop/remove.** `environment_stop` and `worktree_remove`
both traverse `_send_graceful_signal` (via `_kill_process_tree` and teardown) --
the exact call sites ticket #112 pinned (before v0.3.12). If the server died
after the state mutation but before the response was written, the caller
would see a transport error for an operation that fully succeeded. This was
*consistent with* the pre-v0.3.12 #112 mechanism above (it is Windows-only:
`CTRL_BREAK_EVENT` has no POSIX analogue on this path); it was not per-incident
proof for any individual report, and as of v0.3.12 the underlying group-leader
gap that made it possible is closed upstream (see above) -- though a lost
response from *some* transport-level cause remains generically possible, which
is why the read-back recipe below still applies regardless of root cause.

**A first `environment_start` invocation that drops. NOT explained by #112.**
`environment_start()` called with neither `environment_id` nor `checkout_path`
raises `CheckoutTargetError` inside `WorktreeManager._resolve_target`, before
any process or signal code runs at all -- there is no `os.kill` and no
`CTRL_BREAK_EVENT` anywhere on that path. The SIGBREAK guard therefore cannot
account for this sub-symptom. It is recorded here as a symptom only; no cause
is claimed, and nothing in this repo currently addresses it.

**The mitigation shipped for #116 is recovery, not prevention.** The transport
itself is outside this repo; the #112 guard is the only in-repo lever and is
already in place. What #116 adds is (a) a read-back recipe in every mutating
tool's docstring, and (b) one production change: `worktree_create` now catches
`DuplicateWorktreeError` explicitly and appends `(existing_environment_id:
"<id>", existing_path: "<path>")` to the raised `ValueError`. That is the only
tool whose lost response destroys unrecoverable information -- a record's 8-hex
id suffix is random and cannot be re-derived -- so it is the only tool that got
a production hint. `worktree_remove`'s misleading retry error (`invalid
checkout_path '<p>': checkout_path does not exist: ...`, indistinguishable from
a typo, see #123's `_invalid_path_error_text`) was deliberately left alone: a
hint there would also fire on ordinary typos.

**Read-back rules (canonical long form lives in `skills/worktree/SKILL.md`):**

| Lost call | Read back with | Landed if |
| --- | --- | --- |
| `worktree_create` | `environment_list(path=<repo_root>)` | an entry has your `branch` and `tracked: true` |
| `worktree_remove` | `environment_list(path=<repo_root>)`, never the removed path | the entry is absent (`status: "orphaned"` = partially landed) |
| `environment_start` | `environment_list(...)` | your `role` is a key in `pids` |
| `environment_stop` | `environment_list(...)` | your `role` is absent from `pids` |

`environment_list` never writes state, so retrying *it* is always safe. Retry a
`worktree_remove` **by `environment_id`, not `checkout_path`** -- the id form is
self-diagnosing (soft `{"code": "not_found"}`), the path form is not.

**Structural constraint on any future recipe.** `yaml_store._record_to_dict`
does not persist `stop_attempt`, `killed_pids`, `shadowed_contract` or
`orphan_scan`, and `environment_list` rebuilds every entry from `state.yaml`.
Those four keys are present in the output but always `null`/`[]` there. Never
write a recipe that reads them back.

### Build provenance of the #116 sweep (verified 2026-08-19)

Which binary generated the reports that opened #116, and whether the #112
fix could have prevented them, is settled below by commit-ancestry
evidence — not inferred from timing alone.

- The cluster-testers who filed #116 do not exercise this git working
  tree; they run a prebuilt `worktree.exe` cached at
  `C:/Users/arnev/.claude/plugins/cache/agent-marketplace/agent-worktree/`.
- The newest build ever installed in that cache is `0.1.16-00adeab6cfd3`
  (installed 2026-08-11 22:08; no directory in that cache carries a later
  mtime, and no version above `0.1.16` exists there). Nothing was
  installed on 2026-08-17.
- `00adeab6cfd3` is commit `00adeab6cfd31a5a9cb0b85081d9d58057218ab7`,
  `release: v0.1.16`, committed 2026-07-23T23:37:37Z.
- `git merge-base --is-ancestor df0d8eb 00adeab6` returns **false**: the
  #112 SIGBREAK fix, commit `df0d8ebc93b9e12a7153513fae8104bcf39b85dd`
  ("server: harden against stray Windows console ctrl-break; add
  machine-readable soft-error codes (#112)", committed
  2026-08-17T08:22:36Z UTC), is **not an ancestor** of the installed
  build.
- `git tag --contains df0d8eb` returns nothing: no release tag contains
  the #112 fix. The newest release tag in the repo remains
  `agent-worktree--v0.1.16`.

**Conclusions, and the line between them:**

1. #116's sweep (2026-08-17) ran against a binary built roughly 25 days
   before the #112 fix landed on `main`. Its observations therefore
   **predate #112** — established by commit ancestry, not merely
   inferred from the calendar gap.
2. **This does not mean #116 is fixed or resolved.** The #112 fix is
   merged to `main` but has **never shipped in a released build** — no
   release tag contains it, and no cache install postdates it. The
   transport-drop symptom remains live for anyone running the currently
   released plugin, and the fix's effectiveness against the incidents
   reported in #116 is **unverified in the field**. Do not describe #116
   as fixed/resolved on the strength of #112 alone; that requires a new
   release build and a repro run against it.

A `strings`-based scan of the installed binary for fix markers was
attempted and was **inconclusive** (the PyInstaller payload is
compressed; a sanity-control string also returned zero matches, so the
negative result carries no evidential weight). It is not cited as
evidence above — the commit-ancestry check is the only load-bearing
evidence for this section.

### Unverified leads (NOT investigated, NOT implemented)

Everything under this heading is an untested hypothesis recorded so it is not
lost. None of it has been confirmed, and none of it is acted on in this repo.

- **UNVERIFIED lead -- PyInstaller `bootloader_ignore_signals=False`
  (`worktree.spec`).** In a
  frozen build the PyInstaller bootloader sits between the OS and the Python
  process and has its own console-control-event handling; in principle a
  console event could terminate the bootloader before Python's `SIGBREAK`
  handler from `_install_signal_guards()` ever runs, which would make that
  guard ineffective in the packaged binary while remaining effective when
  running from source. **What was NOT done:** PyInstaller's actual Windows
  console-control behaviour was not confirmed against its source or docs, no
  frozen build was tested, and no correlation with any reported incident was
  established. `worktree.spec` is deliberately left unchanged -- flipping this
  flag blind could break Ctrl+C or clean shutdown. **This lead is UNVERIFIED
  and the pytest suite cannot verify it**: the tests exercise the source
  package, never a frozen binary, so no test in `tests/` can confirm or refute
  it. Investigating it requires building the binary and sending real console
  control events to it.

## Running the suite (agent sessions: read this before you run pytest)

The full suite is slow enough on a local Windows checkout that a single
foreground `pytest` invocation risks running past a tool call's timeout. Do
not run the whole suite in one call. Instead, run it in the four chunks
below, one after another, each as its own foreground `pytest` call.

| Chunk | Files / selector | Tests | Measured (local Windows) |
| --- | --- | --- | --- |
| 1 | `tests/test_environment_tools.py` | 120 | 266 s |
| 2 | `tests/test_worktree_tools.py` | 130 | 214 s |
| 3 | `tests/test_setup_runner.py`, `tests/test_signal_resilience.py`, `tests/test_thread_leak_regression.py`, `tests/test_transport_failure_readback.py`, `tests/test_wrapper_script_args.py`, `tests/test_pytest_timeout_config.py` | 58 passed + 2 xfailed | 290 s |
| 4 | `tests/test_config.py`, `tests/test_contract.py`, `tests/test_docstring_contract_alignment.py`, `tests/test_plugin_manifest.py`, `tests/test_dependency_pin.py`, `tests/test_release_dispatch_payload.py` | 146 | 7 s |
| **Total** | all 14 `tests/test_*.py` files | 454 passed + 2 xfailed | **777 s** |

**Chunk 4 dependency note.** `tests/test_release_dispatch_payload.py` has a
`requires_bash_and_jq`-gated "layer (b)" of 18 tests (of its 34 total) that
drive the real `bash`+`jq` interpreters; they silently `skip` (not fail) when
`bash` or `jq` is not on `PATH`, so a local run without `jq` reports 97
passed/18 skipped for chunk 4 (16 passed/18 skipped for the file alone), not
the 115-passed figure above. Both `windows-latest` and `ubuntu-22.04`
GitHub-hosted runners ship `jq` preinstalled, so CI always runs the full 34;
this caveat is local-dev only.

The Total row is the **sum of the measured chunks**, **not a single**
end-to-end measured run of the whole suite in one `pytest` invocation — the
four chunks were timed separately, in separate `pytest` processes, and their
wall-clock times added together. Nobody has run the whole suite as one
uninterrupted local measurement; a single-run total could differ (lower, from
avoided per-process pytest-collection overhead paid four times over here;
or higher, from shared-resource contention across a longer single process)
from this sum.

**Operational rule.** Run the chunks one after another, synchronously, inside
a single turn. Never start the suite as a background task and end the turn
waiting for it to finish — a headless agent process dies when its turn ends,
and a backgrounded suite dies with it, silently, with no result ever
delivered. Commit at each chunk boundary, and commit and push before any turn
that might end, so a lost turn never loses already-measured or already-passing
work.

**CI note.** This chunking is an agent-session constraint only. CI still runs
the whole test suite in one go, in a single job step, via
`.github/workflows/test.yml`. This document does not change, and is not
proposing to change, that chunking-avoidance behaviour — the one thing that
has changed is the job's `timeout-minutes` budget, raised 10 → 20 (see the
next paragraph); the single-job-step structure and chunking guidance above
are unaffected.

**Local vs. CI caution.** Do not assume local wall-clock numbers generalize
to CI, in either direction. This repo's own CI runs of the same suite have
been observed at 301 s, 315 s, 397 s, and 406 s — sometimes faster than the
675 s summed-local figure above, sometimes not, because CI runners and a
local Windows workstation have different CPU counts, disk speed, and
antivirus/filesystem-filter overhead. The sibling `lib-python-worktree`
project shows the same local/CI mismatch even more starkly: 245-508 s in CI
versus 567 s measured locally. Treat both this table's numbers and any CI
number as approximate, machine-dependent data points, not a portable
benchmark.

After bumping the `lib-python-worktree` pin to v0.3.11 (new orphan-scan
overhead), the `windows-latest` leg of the `pytest` job was observed
cancelled twice at ~10m17s — a cancellation wall-clock forced by the (then)
`timeout-minutes: 10` budget, not a completion time, so the true post-bump
Windows duration is unknown but at least that long. `timeout-minutes` has
been raised 10 → 20, sized from a ~2x-overhead hypothesis on the 406 s
pre-bump worst case (~13.5 min) plus margin. The per-test `timeout=60`
configured in `pyproject.toml` (plus explicit longer marks on the
thread-leak tests) is what actually catches an individual wedged test; this
job-level `timeout-minutes` is only the outer backstop for the whole run.
Follow-up: once the Windows leg completes green in CI under the new budget,
replace the "~10m17s cancellation" datum above with its real measured
duration.

**Slow-test clustering.** Durations were not flat within every chunk.
Chunk 1 (`tests/test_environment_tools.py`) clusters ten
`test_worktree_remove_*` tests at roughly 21-23 s each (real worktree
create/teardown subprocess and git operations dominate); the rest of that
file's 110 tests run in a combined ~4 s. Chunk 2
(`tests/test_worktree_tools.py`) similarly clusters five `test_create_*`
tests at roughly 21 s each, with the remaining 120 tests in well under a
second combined. Chunk 3 is the most skewed: two tests in
`tests/test_thread_leak_regression.py` alone
(`test_create_remove_cycles_do_not_leak_threads` at ~171 s and
`test_create_remove_cycles_with_kill_blocking_processes_do_not_leak_threads`
at ~106 s — status per `tests/test_thread_leak_regression.py`'s own module
docstring, not repeated here) account for ~277 s of
that chunk's ~290 s total; the other five files in chunk 3 are fast. Chunk 4
is flat and fast throughout, with no cluster.

## Security

Setup scripts run with the user's own OS privileges — see `SECURITY.md` for the full threat model.
