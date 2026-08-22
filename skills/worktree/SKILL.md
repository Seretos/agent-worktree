---
name: worktree
description: Teaches the agent-worktree MCP contract (.seretos/worktree-setup.yml) and the create → setup → start → work → stop → remove lifecycle for isolated git worktrees. Use when authoring or debugging a worktree-setup.yml, choosing isolation none vs full, running parallel or N-simultaneous-instance feature work, or troubleshooting worktree setup-script, port-leak, Windows directory-lock, or orphan-worktree failures.
---

# worktree

## What this skill is for

Reach for this skill when the task involves **git worktree lifecycle management** via
the `agent-worktree` MCP server — creating an isolated checkout for parallel work,
authoring or debugging a `.seretos/worktree-setup.yml` contract, deciding between
`isolation: none` and `isolation: full`, starting/stopping a per-environment process, or
troubleshooting a failed setup script, a leaked port, a Windows directory lock, or an
orphaned worktree left on disk.

## Mental model

Ticket #99 split this server's tool surface along the two real lifecycles a checkout
goes through, and everything below is organized around that split:

- **Checkout lifecycle** — a *checkout* is a directory: a git worktree on disk (or the
  repo's own primary/main clone). `worktree_create`/`worktree_remove` create and delete
  that directory.
- **Environment lifecycle** — an *environment* is a process-bearing checkout: the same
  directory, plus whatever `start:`-launched process is (or isn't) currently running
  against it. `environment_list`/`environment_start`/`environment_stop` manage that
  process, against **any** checkout.

**The primary/main clone is an environment like any other.** It is never created or
deleted by this plugin (it exists before the plugin ever runs, and `worktree_remove`
structurally refuses to delete it — see Pitfall 5 below), but its process lifecycle is
managed exactly the same way as a linked worktree's: `environment_start`/
`environment_stop` work against it too. This matters for anything that runs a
long-lived process directly in the main checkout (a dev server, a watcher) — you no
longer need a disposable worktree just to get MCP-managed start/stop for it.

Every managed checkout is driven by a single contract file:

```
<repo_root>/.seretos/worktree-setup.yml
```

**Critical:** the engine reads this file from `repo_root` — the original repository
clone every checkout (primary or linked) traces back to — **not** from a linked
worktree's own copy, and it does so **live from disk on every
`environment_start`/`environment_stop` call**: an uncommitted edit to
`<repo_root>/.seretos/worktree-setup.yml` takes effect immediately, no commit
and no worktree re-create needed. `worktree_create` copies `.seretos/` into a
new worktree as a create-time convenience (so it is visible from inside the
checkout), but that copy is written **once, at create time, and is never
re-read** — `environment_start`/`environment_stop` always read the live
`repo_root` original, never this copy. Which state the checkout-local copy
holds is conditional: if `.seretos/` is **tracked** in git, `git worktree add`
already checked out the **committed** tree at the branch tip before
`worktree_create`'s own copy step runs, so that step is skipped — an
uncommitted edit at `repo_root` then leaves this checkout-local copy pinned to
the last commit while start/stop already see the edit. If `.seretos/` is
**untracked/excluded** (the case the copy exists for), the copy step runs and
copies the **live** working-tree bytes, once. Placing the contract
only in a worktree checkout, and never at `<repo_root>/.seretos/worktree-setup.yml`,
still produces a no-op — `environment_start` returns `{"status": "ready", "pids": {}}`
— but it is **not silent** and **not** indistinguishable from "no contract configured"
(issue #87): the same response carries `contract_found: false`, `steps_run: 0`, and
`no_op_reason: "contract-misplaced"` (vs `"no-contract"` for the genuinely-unconfigured
case) — ticket #103's contract diagnostics. The engine (`lib-python-worktree`, upstream
#100) additionally sets `shadowed_contract` on the response — `None`, or `{path,
used_path, reason, message}` with `reason` either `"differs"` or `"unreadable"` —
whenever a checkout-local copy exists that is not the file it read. It is transient
(never written to `state.yaml`) and is `None` for a primary, for `checkout ==
repo_root`, and for the identical copy `worktree_create` writes. A `"differs"`
reason is the **expected, diagnosed signal** for the tracked-and-edited-
uncommitted case above — not a malfunction; commit the contract (or re-create
the worktree) to re-converge.

The contract declares up to five lifecycle hooks, each fired by a distinct MCP tool:

| Hook | Fired by | When |
|---|---|---|
| `setup:` | `worktree_create` | Once, right after the checkout is created |
| `start:` | `environment_start` | On demand, to launch a long-running process |
| `stop:` | `environment_stop` | On demand, before the process is signalled to exit |
| `teardown:` | `worktree_remove` | Before the worktree directory is deleted |
| `ports:` | (reservation only) | Declares named ports the environment's services bind to |

## The contract: `.seretos/worktree-setup.yml`

Two top-level keys are always required:

- `version` — an int (currently `1`).
- `isolation` — one of `full`, `partial`, or `none` (see below).

Each step under `setup:`, `start:`, `stop:`, or `teardown:` is a YAML mapping with:

- `run:` — **required**, the shell command to execute.
- `name:` — optional; for `start:`/`stop:` steps this selects the step via
  `environment_start`'s `variant` parameter (see the three-tier
  `variant="default"` resolution below for when an unnamed — or even a
  named — step becomes the implicit default).
- `shell:` — optional override of the shell used to run the step. **When
  omitted, the default is per-OS:** on **Windows** the step runs under
  `powershell.exe -NoProfile -NonInteractive` (Windows PowerShell 5.1, not
  `pwsh`); on **POSIX** (Linux/macOS) it runs under `bash -c`. This is the
  same default for `setup:`, `start:`, `stop:`, and `teardown:` steps
  alike. Accepted override values are exactly `bash`, `sh`, `pwsh`, and
  `powershell` — anything else raises `ValueError: unknown step shell:
  '<value>'` when the step runs. `-NonInteractive` is always passed to
  both PowerShell variants, so a step that would prompt fails loudly
  instead of hanging. **Portability warning:** a `run:` line using `&&`,
  `||`, `$VAR`, backticks, or `2>&1` is bash/cmd syntax and will not parse
  under the Windows default `powershell.exe` (Windows PowerShell 5.1 has
  no `&&` operator, so this is a shell *parse* error, not a step failure
  with a useful message) — write `shell: bash` explicitly on such a step
  (or use PowerShell's `;` / `-and`) rather than relying on the default.

`environment_start` supports multiple **named** `start:` variants — pass the step's
`name` as `variant` to select it (e.g. `variant="gui"` vs. the default headless launch).
An unknown variant raises a `ValueError` listing the available names.

**Resolving `variant="default"`.** Three tiers, tried in order: (1) an exact
`name:` match against `variant`; (2) exactly one **unnamed** `start:` step —
implicitly the `"default"` variant, for back-compat; (3) exactly one `start:`
step overall — even if that single step is named rather than unnamed
(upstream lib-python-worktree#112, shipped in the pinned v0.3.5) — so a
contract whose sole step carries a `name:` other than `"default"` still
resolves without passing `variant` explicitly. Two or more `start:` steps
with none of them named `"default"` still raise `ValueError` listing the
available names, even under tier 3.

**`role` vs `variant`.** These are independent parameters, easy to conflate: `role` is
the tracking key a process's pid is filed under (`pids[role]`), and it defaults to
`"main"` **regardless of which `variant` was requested** — starting `variant="gui"`
with no explicit `role` still records its pid under `role="main"`. `variant` only
selects which `start:` step runs. Because they're independent, two variants started
concurrently need two distinct `role`s, or the second call returns/errors with an
`already_running` condition. Whichever `variant` started a given `role` is remembered
(`record.variants`), so `environment_stop(variant=...)` can later stop that role
without the caller separately tracking which role it used — see `environment_stop`'s
own `variant` parameter below.

**Symmetry with environment_stop.** When tier 3 above (the lone-step
fallback) resolves a *named* step from a bare `variant="default"` call, the
*engine* records that step's own name in `record.variants[role]` (e.g.
`"main"`) — never the literal string `"default"`. `environment_stop`
compensates for this: `environment_stop(variant="default")` **does
resolve** against that role — it pre-resolves a bare `"default"` to the
contract's single named `start:` step (mirroring this same tier-3 rule)
before ever calling the engine, so the same lone-step contract that started
under its own name can be stopped without the caller tracking or passing
that name. Passing the step's actual name as `variant`, or omitting
`variant` and relying on `role="main"` (the default), both keep working
exactly as before.

**Log-file naming caveat.** `pids`/`record.variants` key on the **verbatim**
`role` string. `start_log_path` (and `start_log_paths[role]`) is *nearly*
the same, but sanitised: the filename is `start-<slug(role)>.log`, where
the slug is **case-preserving** — never lower-cased, and case-preserving is
not upper-casing either; casing is simply untouched — tracked at its
origin as `Seretos/lib-python-worktree#111` (the lower-casing bug it
originally reported was fixed upstream in the pinned v0.3.7). Non-alphanumeric
runs collapse to `-`, leading/trailing `-` are stripped, the result is truncated
to 40 characters (and re-stripped), and a role with no alphanumeric
characters at all falls back to `_`. Example: `role="API Server"` files its
pid under `pids["API Server"]` and logs to `start-API-Server.log`. Two
roles differing only in case (e.g. `"API"` vs `"api"`) produce two distinct
`pids` keys and two distinct filename strings, but on a case-insensitive
filesystem (Windows, default macOS) those strings name the same physical
file and their append-mode output interleaves. Only the *sanitisation* is
lossy: `"role a"`, `"role-a"` and `"role_a"` all slug to `role-a`. Always
read the log path from `environment_start`'s `start_log_path` field rather
than deriving it yourself. This is an accepted, documented upstream
limitation — not to be confused with this repository's own already-closed issue of the
same number, an unrelated thread-leak ticket.

Concrete example (mirrors the multi-step, multi-variant shape used in this repo's own
`.seretos/worktree-setup.yml`):

```yaml
version: 1
isolation: full
setup:
  - name: install deps
    run: pnpm install
  - run: pnpm prisma migrate dev
start:
  - name: web
    run: start-web.sh
  - name: worker
    run: start-worker.sh
stop:
  - name: web
    run: stop-web.sh
teardown:
  - run: docker compose down
ports:
  - name: app
  - name: db
```

## `isolation: none` vs `full`

- `isolation: full` is **required** whenever any `setup:`, `start:`, `stop:`,
  `teardown:`, or `ports:` block is present.
- `isolation: none` is the bare minimum contract — just `version: 1` plus
  `isolation: none`, with **no** other blocks. The parser rejects `setup:`/`start:`/
  `stop:`/`ports:` under `isolation: none` (`ContractValidationError`).
- The underlying engine also accepts `isolation: partial` as a third value; this repo's
  README and the ticket vocabulary only distinguish `none`/`full`, but `partial` is
  valid input if you encounter it.

A missing contract file, or an empty one, is treated as an implicit
`isolation: none` — no error, just no hooks configured.

## Tool inventory

Five MCP tools, all under the `worktree` server, split by lifecycle:

**Checkout lifecycle** (create/delete the directory):

| Tool | Best for |
|---|---|
| `worktree_create` | Create a new worktree for a branch (runs `setup:` steps); copies `.seretos/` into the checkout as a convenience. `base` is optional — for a not-yet-existing `branch`, omitting it defaults to whatever branch is currently checked out at `repo_root` (still raises on a detached/unborn HEAD) |
| `worktree_remove` | Run `teardown:` steps, then delete the worktree checkout; addressed by `environment_id` and/or `checkout_path` (see "Addressing an environment" below — `checkout_path` is the only way to remove an untracked/orphan checkout); supports `force` and `kill_blocking_processes`. Structurally refuses to delete a primary checkout, even with `force=True` |

**Environment lifecycle** (the process running against any checkout, primary included):

| Tool | Best for |
|---|---|
| `environment_list` | Enumerate the environments (primary + linked worktrees) for the repo containing a given path, including `setup_status` (`"completed"` / `"failed"` / `"skipped"` / `"unknown"`, derived solely from the record's `setup_outcome`, never from `status`); `scope="all"` fans out across every tracked repo, optionally narrowed to specific repos or a parent directory with `repos=[...]` (a repo root is kept only if it resolves at or under one entry; only valid with `scope="all"`, else `ValueError`) |
| `environment_start` | Launch a named `start:` variant as a tracked, detached process, against any checkout |
| `environment_stop` | Run `stop:` steps best-effort, then gracefully (and if needed forcibly) terminate the tracked process, against any checkout; accepts an optional `variant` to resolve the target `role` from `record.variants` instead of naming `role` directly (see "`role` vs `variant`" above) |

## Addressing an environment

`environment_start`, `environment_stop`, and `worktree_remove` each accept two ways to
name their target — pass one or the other (or both, if they agree):

- **`environment_id`** — the normal way. Use the id `worktree_create` returned for a
  linked worktree, or the id `environment_list`/a prior `environment_start` call
  returned for the primary (once materialised).
- **`checkout_path`** — the cold-start/primary way for `environment_start`/
  `environment_stop`, and the *only* way to remove an untracked/orphan checkout via
  `worktree_remove`. For `environment_start`, this is the *only* way to start the
  primary/main clone's environment before it has ever been started: a primary's id is
  `primary_id_for(repo_root)` — a one-way SHA-256 hash of the repo root — so nothing
  persisted maps that hash back to a path until the first successful
  `environment_start()` call writes the record. Pass the repo root (or any path inside
  it) as `checkout_path` instead, and the engine resolves (and, for the primary,
  materialises) the target. For `worktree_remove`, the same "one-way hash, not a
  lookup key" problem applies to an **untracked linked worktree**: `environment_list`
  displays it with a synthesised id of the form
  `<checkout-dirname-slug>-untracked-<8-hex>` — the checkout directory's own basename,
  slugged (lower-case ASCII, non-alphanumeric runs collapsed to `-`, truncated to 40
  chars), plus the first 8 hex characters of a SHA-256 hash of its resolved path (no
  repo slug and no branch slug are involved) — and that id can never resolve via
  `environment_id` — pass the checkout's `path` (from `environment_list`) as
  `checkout_path` instead. See "Orphan worktree recovery" below for the full recipe.

Passing both is fine only when they agree — a mismatch raises `ValueError`. Passing
neither also raises `ValueError`. This resolution is entirely the *engine's* job, not
the MCP wrapper's — the wrapper performs no validation of the pair itself — but each
tool (`worktree_remove`, `environment_start`, `environment_stop`) re-words the engine's
raw `CheckoutTargetError` text before raising `ValueError`: the engine's own message
names its internal parameter and describes the contract in engine-API vocabulary
(`start()`/`stop()`/`remove()`), so the wrapper replaces it with a message naming
`environment_id`, `checkout_path`, and the calling tool itself. The same three tools
also re-word the engine's `InvalidRepoError` (ticket #123), raised when `checkout_path`
is given but isn't a usable git repository — its internal `repo_root` parameter name is
replaced with `checkout_path`, with the full diagnostic reason preserved byte-for-byte.

> **Spec-gap note (ticket #99).** The ticket's originally-specified surface is
> id-only. That cannot satisfy the ticket's own AC1 — cold-starting a primary that has
> never been started is structurally impossible with an id-only signature, for the
> one-way-hash reason above. `checkout_path` is a strict superset: every id-only call
> keeps working unchanged, and it is the only way to reach a never-started primary.
> This deviation is intentional and documented here, in `AGENTS.md`, and in the tool
> docstrings themselves — the ticket calls the id-only surface final, so the deviation
> is called out explicitly rather than left silent.

## Lifecycle

```
worktree_create     →  setup: runs automatically
      │
      ▼
environment_start   →  start: <variant> launches a tracked process (optional; skip if no long-running process is needed)
      │
      ▼
   ...work...
      │
      ▼
environment_stop    →  stop: steps run best-effort, then the process is terminated
      │
      ▼
worktree_remove     →  teardown: steps run, then the checkout is deleted
```

`environment_start`/`environment_stop` are optional — many tickets only need
`worktree_create` → work → `worktree_remove`, with no long-running process involved.
The primary/main clone skips the top and bottom of this diagram entirely (it is never
created or removed by this plugin) but can still be driven through the middle via
`environment_start(checkout_path=...)` / `environment_stop(...)`.

## When to reach for a worktree (the gate)

Use a worktree when:

- Doing **parallel feature work** — multiple tickets/branches need independent
  checkouts so they don't collide on file state.
- **Isolating risky changes** — testing a change without disturbing the main checkout's
  working tree.
- Running **N simultaneous instances** of a service for integration-style or
  multiplayer-style testing, where each instance needs its own process and (if
  declared) its own reserved ports.

Skip it for a **single-branch, quick edit** on the checkout you already have open —
spinning up a worktree adds create/teardown overhead with no isolation benefit when
there is no concurrent work to isolate from. If you just need a managed process against
the checkout you already have (no isolation needed), `environment_start` against the
primary via `checkout_path` gets you that without a worktree at all.

## Troubleshooting

**Setup script fails on create**

The executed command comes from the `setup:` steps in `.seretos/worktree-setup.yml` —
never supplied by the caller. Confirm a `setup:` block is present and correctly
configured, that `isolation: full` is set (required whenever `setup:` is present), and
that the contract file exists at the repository root (not only inside the worktree).

**Port leak after crash / restart**

State is persistent and disk-backed (`~/.agent-worktree/state.yaml`) and is reconciled
on startup, so a crash does not simply forget tracked environments the way pure
in-memory tracking would. If an OS port nonetheless remains bound after a crash
(process did not exit cleanly and reconciliation could not recover it), resolve it at
the OS level: identify the process holding the port (`netstat -ano | findstr <port>` on
Windows, `lsof -i :<port>` on Linux) and terminate it directly.

**Worktree directory locked by a foreign process (Windows)**

Processes whose working directory sits inside the worktree can prevent directory
deletion. Pass `kill_blocking_processes=True` to `worktree_remove` to have the tool
terminate those foreign processes automatically before removal:

**Tracked vs. foreign.** Your own `environment_start` process is not what this flag is
for — `worktree_remove` stops every tracked role (from `pids`) as its first step,
before this flag's scan ever runs, so it does not need `kill_blocking_processes`. The
flag exists for genuinely foreign holders: an editor, a shell whose cwd is in the
checkout, a build/indexing tool, or a reparented orphan. The tracked stop is
best-effort, though — a tracked process that refuses to die still blocks removal and
does then need this flag.

```
worktree_remove(<id>, kill_blocking_processes=True)
```

The response's `killed_pids` field lists every terminated process (pid, name,
cmdline, cmdline_raw). `cmdline` is agent-readable — a PowerShell/pwsh
`-EncodedCommand` base64 blob is decoded to the actual script text — with the
original, undecoded argv preserved in `cmdline_raw` (`None` when nothing was
decoded). If the directory is still locked afterward, the tool raises an error —
resolve the remaining lock at the OS level and retry.

**Compound blocking (ticket #120): one retry, not a guessing sequence.** If the
directory lock AND uncommitted/untracked changes are BOTH blocking removal at
once, the raised `ValueError` names every blocking condition and the flag that
clears each in a single message — `(blocked_by: "dir_locked",
"uncommitted_changes"; required_flags: kill_blocking_processes=True,
force=True)`. Read both tokens off that one error and retry once with both
flags set, rather than discovering each condition across separate failed
attempts (plain retry → `kill_blocking_processes=True` → `force=True`).

**`environment_stop(kill_orphans=True)` vs. the unconditional tree/Job Object kill**

Easy to conflate with `kill_blocking_processes` above — both are path-scoped
scans, but they belong to different tools and different lifecycles.
`kill_blocking_processes` (on `worktree_remove`) hunts *foreign* holders of
the checkout directory; `kill_orphans` (on `environment_stop`) hunts
processes left behind by *this environment's own* tracked start that
escaped the engine's normal containment.

**The tree/Job Object kill is unconditional — it does not need
`kill_orphans`.** `environment_stop` always snapshots and kills the tracked
pid's descendant tree, and on Windows always terminates its Job Object as a
unit. A `Start-Process`/`ShellExecuteEx`-delegated grandchild outside the
ppid lineage is already killed via that Job Object, without
`kill_orphans` — the Job Object is ppid-independent, and
`CREATE_BREAKAWAY_FROM_JOB` cannot escape it (the engine sets no
`JOB_OBJECT_LIMIT_BREAKAWAY_OK`, so the OS refuses breakaway outright).

`kill_orphans=True` instead runs a **path-scoped** scan (cwd / cmdline
token / open file / Windows handle table) under the checkout path, killing
whatever it finds there regardless of who started it — a *different
scope*, not deeper containment. It is genuinely needed only for: a POSIX
`setsid()` double-fork escape (no Job Object exists on POSIX at all); a
Windows role whose job failed to create/assign, or whose handle is
unavailable at stop time; a process spawned by a `setup:` step (never
entered in `pids`, never in a job); or the sub-millisecond window between a
child's spawn and its job assignment landing. It does **not** help a
`stop_detail.reason == "job_member_list_truncated"` outcome — those
members are already dead. On Windows it also costs a system-wide
handle-table scan, so **do not pass it defensively on every call** — let
the engine tell you: re-call with `kill_orphans=True` only when a
`stop_incomplete` response's `stop_detail.kill_orphans_may_help` is `true`.

**Transport failure ("Connection closed"): confirm before retrying (ticket #116)**

A tool call can die with `Connection closed` / `MCP error -32000` before its
response is written. The response is lost; the operation may have fully landed.
Never blind-retry a mutating call — read back first. `environment_list` never
writes state, so retrying *it* is always safe, which is what makes it the
read-back tool.

1. **`worktree_create`** — `environment_list(path=<the same repo_root>)`; find the
   entry with your `branch` and `tracked: true`. Its `id` is what the lost
   response carried, and the id's 8-hex suffix is random, so read-back is the
   only way to recover it; `setup_status` says how the `setup:` steps ended. A
   blind retry is non-destructive (the duplicate guard fires before any
   worktree-creating git command — only a read-only `git rev-parse` for repo
   classification has run at that point) and self-diagnosing: the raised
   error carries `(existing_environment_id: "<id>", existing_path:
   "<path>")` — best-effort only; if the landed record can't be looked up,
   the bare engine text is raised instead, with no id invented.
2. **`worktree_remove`** — read back from the **repo root**, never from the removed
   checkout path. That path is gone if the removal landed, so passing it back
   raises `invalid checkout_path '<p>': checkout_path does not exist: ...` —
   byte-for-byte what a typo produces, and therefore no evidence at all. Entry
   absent = landed; entry present with `status: "orphaned"` = partially landed
   (directory gone, record survives), finish it with
   `worktree_remove(environment_id=<that id>)`; entry unchanged = did not land.
   **Retry by `environment_id`, not `checkout_path`** — the id form is
   self-diagnosing (soft `{"code": "not_found"}`), the path form is not.
3. **`environment_start`** — `environment_list(...)`, then `pids`. Unambiguous
   case first: your `role` (default `"main"`) present as a key means the start
   landed and the process is alive, and `variants[<role>]` names the variant. If
   the role is absent the reading is ambiguous — never started, or started and
   since exited (the listing reconciles dead pids away). The
   `returncode`/`start_log_path` heuristic breaks the tie only for a role never
   started before: for such a role non-`null` values prove a spawn happened,
   while for a role started at any earlier point they are stale leftovers that
   reconciliation does not clear and that decide nothing — inspect the file at
   `start_log_path` instead. A blind retry is protected by `code:
   "already_running"` only while the pid is **alive**; a landed-then-exited
   start will be started a second time.
4. **`environment_stop`** — `environment_list(...)`, then `pids`: role absent = no
   live tracked process remains; role present = did not land. The listing cannot
   tell you whether the contract's `stop:` steps ran — only the persisted
   `stop_detail` (and a sticky `status: "stop_incomplete"`) survives there. A
   blind retry that *returns* is safe: `code: "not_found"`, `code:
   "not_running"`, or a graceful no-op reported as `stop_attempt.outcome:
   "no_process_recorded"`. But it can also **raise `ValueError`** instead of
   returning — a bad `checkout_path`, a missing/disagreeing `environment_id`/
   `checkout_path` pair, or a `variant` that fails to resolve all raise
   rather than come back as a soft `code`. Branching on `code` only makes
   sense for a call that returned.

**Fields that can never serve as read-back evidence.** `stop_attempt`,
`killed_pids` and `shadowed_contract` are transient: they are never written to
`state.yaml`, and `environment_list` rebuilds every entry from persisted state,
so those keys are always `null`/`[]` there no matter what happened. Read them
only from the response of the call that produced them.

**What this does and does not fix.** The transport drop itself is outside this
plugin's reach. The Windows `SIGBREAK` guard (ticket #112) addresses one
mechanism — a server killed by a `CTRL_BREAK_EVENT` during a stop/remove — and
is already in place; it does not eliminate transport drops, and it does not
explain a dropped *first* `environment_start` call, which fails during argument
resolution before any signal code runs at all.

**Orphan worktree recovery**

An orphan is a linked worktree that exists on disk (`git worktree list --porcelain`
finds it) but has no persisted record — `environment_list` shows it with
`tracked: false` and a synthesised, display-only id
(`<checkout-dirname-slug>-untracked-<8-hex>`): the checkout directory's own basename,
slugged (lower-case ASCII, non-alphanumeric runs collapsed to `-`, truncated to 40
chars), plus the first 8 hex characters of a SHA-256 hash of its resolved path — no
repo slug and no branch slug are involved. That id is a one-way derivation of the
checkout's path, not a state-store key, so `worktree_remove(environment_id=<that id>)`
can never resolve it — it always comes back as a soft not-found error
(`{"error": "...", "code": "not_found"}`).

Look-alike caveat: a worktree this tool created and later lost the record for has a
checkout directory basename that already looks like `<repo-slug>-<branch-slug>-<8-hex>`
(the tracked create-id shape), so its untracked id can visually appear to contain a
repo slug and a branch slug even though it does not derive from either — a hand-made
orphan's prefix is simply whatever the directory happens to be named. The working
recipe is:

1. `environment_list(path=<repo_root>)` — find the entry with `tracked: false` (and
   `backing: "worktree"`, not `"primary"`).
2. `worktree_remove(checkout_path=<entry's path>, force=true)` — address it by its
   `path`, not its `id`. If it is safe to discard, `force=true` removes it even though
   it contains uncommitted changes.

Removing an orphan this way never touches the state store (nothing was recorded there
to remove) and never deletes its branch, even with `force=true`, since an orphan is
never recorded as owning one.

**Soft error codes.** `worktree_remove`, `environment_start`, and `environment_stop`
all return an additive machine-readable `code` field alongside `error` on their soft
(non-raising) failure paths, so callers can branch on `code` instead of parsing the
error text: `code: "not_found"` (target not found — all three tools), `code:
"already_running"` (`environment_start` when a process is already running under the
given `role`), and `code: "not_running"` (`environment_stop` when no process is
running under the given `role`).

## Pitfalls

1. **Contract in the wrong location is a no-op — but a *diagnosable* one.** The engine
   reads `<repo_root>/.seretos/worktree-setup.yml`, not a linked worktree checkout's
   copy. A contract placed only in the worktree checkout still produces
   `{"status": "ready", "pids": {}}`, but `environment_start`'s response also carries
   `no_op_reason: "contract-misplaced"` (vs `"no-contract"` for the genuinely-
   unconfigured case) — branch on that instead of inferring from `status`/`pids`. If
   instead the checkout-local copy was *edited* while a valid repo-root contract
   started normally, look for `shadowed_contract` in the response.
2. **`isolation: none` forbids every block.** Adding `setup:`, `start:`, `stop:`,
   `teardown:`, or `ports:` under `isolation: none` raises `ContractValidationError` —
   switch to `isolation: full` first.
3. **State survives a restart, but OS-level resources might not.** `state.yaml` is
   disk-backed and reconciled on startup; don't assume a crash silently wipes tracked
   environments the way pure in-memory tracking would, but do still verify leaked OS
   resources (ports, processes) directly at the OS level if reconciliation couldn't
   recover them.
4. **Windows can lock a worktree directory via a foreign process's cwd.** If plain
   `worktree_remove` fails, retry with `kill_blocking_processes=True` rather than
   fighting the lock manually. This flag is not for a process you started yourself
   with `environment_start` — removal stops every tracked role first, before this
   flag's scan runs. If the directory lock and uncommitted changes are
   BOTH blocking removal, the error names both conditions and both required
   flags (`blocked_by`/`required_flags`) in one message — set both flags in a
   single retry instead of discovering each condition one at a time.
5. **The tree/Job Object kill is unconditional — `kill_orphans` is a
   path-scoped widening, not a defensive default.** `environment_stop`
   always kills the tracked pid's descendant tree, and on Windows always
   terminates its Job Object as a unit; a `Start-Process`-style grandchild
   is already reached by that, without `kill_orphans`. `kill_orphans=True`
   is a separate, path-scoped scan under the checkout path, gated on
   `stop_detail.kill_orphans_may_help` from a `stop_incomplete` response —
   not something to pass on every call.
6. **A primary checkout can never be removed, even with `force=True`.** `worktree_remove`
   against a primary/main clone's `environment_id` always raises `ValueError` — this is
   a structural refusal checked before any teardown work runs, not a safety flag you can
   override. It exists because a primary checkout IS the repo; there is no "linked
   worktree" fallback semantics to fall back on.
7. **`environment_id` and `checkout_path` are two names for the same resolution, not
   two independent filters.** Passing both only works when they agree; a mismatch is a
   hard `ValueError` from the engine, not a "prefer one over the other" merge.
8. **`base` is not mandatory when creating a brand-new branch.** Omitting `base` for a
   `branch` that does not yet exist defaults to whatever branch is currently checked out
   at `repo_root` — you do not have to pass `base` just because the branch is new. This
   default still raises `ValueError` when `repo_root`'s HEAD is detached or unborn (no
   commits yet), since there is then no checked-out branch to default to.
9. **A multi-step contract with no step named `default` fails the *first*
   `environment_start` call from any agent, unless `variant=` is passed.**
   The `variant="default"` lone-step fallback only ever fires when the
   contract declares exactly one `start:` step total; the moment a second
   named step is added, a bare `environment_start()` call raises
   `ValueError` listing the available names instead of silently picking
   one. Check `worktree_create`'s returned `start_variants` field (or this
   contract's `start:` list) up front and pass `variant=<name>` explicitly
   whenever more than one step exists.
