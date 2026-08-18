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
worktree's own copy. `worktree_create` copies `.seretos/` into a new worktree as a
create-time convenience (so it is visible from inside the checkout), but that copy is
*not* what `environment_start`/`environment_stop` actually read. Placing the contract
only in a worktree checkout, and never at `<repo_root>/.seretos/worktree-setup.yml`,
produces a silent no-op — `environment_start` returns `{"status": "ready", "pids": {}}`
with no error, indistinguishable from "no contract configured" (issue #87).

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
  `environment_start`'s `variant` parameter (a single unnamed step is the implicit
  `"default"` variant, for back-compat).
- `shell:` — optional override of the shell used to run the step.

`environment_start` supports multiple **named** `start:` variants — pass the step's
`name` as `variant` to select it (e.g. `variant="gui"` vs. the default headless launch).
An unknown variant raises a `ValueError` listing the available names.

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
| `environment_list` | Enumerate the environments (primary + linked worktrees) for the repo containing a given path, including `setup_status` (`"completed"` / `"failed"` / `"skipped"` / `"unknown"`, derived solely from the record's `setup_outcome`, never from `status`); `scope="all"` fans out across every tracked repo |
| `environment_start` | Launch a named `start:` variant as a tracked, detached process, against any checkout |
| `environment_stop` | Run `stop:` steps best-effort, then gracefully (and if needed forcibly) terminate the tracked process, against any checkout |

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
  `<repo-slug>-<branch-slug>-untracked-<8-hex>` (a one-way derivation of its checkout
  path), and that id can never resolve via `environment_id` — pass the checkout's
  `path` (from `environment_list`) as `checkout_path` instead. See "Orphan worktree
  recovery" below for the full recipe.

Passing both is fine only when they agree — a mismatch raises `ValueError`. Passing
neither also raises `ValueError`. This resolution is entirely the *engine's* job, not
the MCP wrapper's — the wrapper performs no validation of the pair itself — but each
tool (`worktree_remove`, `environment_start`, `environment_stop`) re-words the engine's
raw `CheckoutTargetError` text before raising `ValueError`: the engine's own message
names its internal parameter and describes the contract in engine-API vocabulary
(`start()`/`stop()`/`remove()`), so the wrapper replaces it with a message naming
`environment_id`, `checkout_path`, and the calling tool itself.

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

```
worktree_remove(<id>, kill_blocking_processes=True)
```

The response's `killed_pids` field lists every terminated process (pid, name,
cmdline). If the directory is still locked afterward, the tool raises an error —
resolve the remaining lock at the OS level and retry.

**Compound blocking (ticket #120): one retry, not a guessing sequence.** If the
directory lock AND uncommitted/untracked changes are BOTH blocking removal at
once, the raised `ValueError` names every blocking condition and the flag that
clears each in a single message — `(blocked_by: "dir_locked",
"uncommitted_changes"; required_flags: kill_blocking_processes=True,
force=True)`. Read both tokens off that one error and retry once with both
flags set, rather than discovering each condition across separate failed
attempts (plain retry → `kill_blocking_processes=True` → `force=True`).

**Orphan worktree recovery**

An orphan is a linked worktree that exists on disk (`git worktree list --porcelain`
finds it) but has no persisted record — `environment_list` shows it with
`tracked: false` and a synthesised, display-only id
(`<repo-slug>-<branch-slug>-untracked-<8-hex>`). That id is a one-way derivation of the
checkout's path, not a state-store key, so `worktree_remove(environment_id=<that id>)`
can never resolve it — it always comes back as a soft not-found error
(`{"error": "...", "code": "not_found"}`). The working
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

1. **Contract in the wrong location is a silent no-op.** The engine reads
   `<repo_root>/.seretos/worktree-setup.yml`, not a linked worktree checkout's copy. A
   contract placed only in the worktree checkout produces no error — just an
   indistinguishable-from-unconfigured `{"status": "ready", "pids": {}}` response.
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
   fighting the lock manually. If the directory lock and uncommitted changes are
   BOTH blocking removal, the error names both conditions and both required
   flags (`blocked_by`/`required_flags`) in one message — set both flags in a
   single retry instead of discovering each condition one at a time.
5. **A primary checkout can never be removed, even with `force=True`.** `worktree_remove`
   against a primary/main clone's `environment_id` always raises `ValueError` — this is
   a structural refusal checked before any teardown work runs, not a safety flag you can
   override. It exists because a primary checkout IS the repo; there is no "linked
   worktree" fallback semantics to fall back on.
6. **`environment_id` and `checkout_path` are two names for the same resolution, not
   two independent filters.** Passing both only works when they agree; a mismatch is a
   hard `ValueError` from the engine, not a "prefer one over the other" merge.
7. **`base` is not mandatory when creating a brand-new branch.** Omitting `base` for a
   `branch` that does not yet exist defaults to whatever branch is currently checked out
   at `repo_root` — you do not have to pass `base` just because the branch is new. This
   default still raises `ValueError` when `repo_root`'s HEAD is detached or unborn (no
   commits yet), since there is then no checked-out branch to default to.
