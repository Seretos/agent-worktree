---
name: worktree
description: Teaches the agent-worktree MCP contract (.seretos/worktree-setup.yml) and the create → setup → start → work → stop → remove lifecycle for isolated git worktrees. Use when authoring or debugging a worktree-setup.yml, choosing isolation none vs full, running parallel or N-simultaneous-instance feature work, or troubleshooting worktree setup-script, port-leak, Windows directory-lock, or orphan-worktree failures.
---

# worktree

## What this skill is for

Reach for this skill when the task involves **git worktree lifecycle management** via
the `agent-worktree` MCP server — creating an isolated checkout for parallel work,
authoring or debugging a `.seretos/worktree-setup.yml` contract, deciding between
`isolation: none` and `isolation: full`, starting/stopping a per-worktree process, or
troubleshooting a failed setup script, a leaked port, a Windows directory lock, or an
orphaned worktree left on disk.

## Mental model

Every managed worktree is driven by a single contract file:

```
<repo_root>/.seretos/worktree-setup.yml
```

**Critical:** the engine reads this file from `repo_root` — the original repository
clone the worktree was created from — **not** from the worktree checkout itself.
`worktree_create` copies `.seretos/` into the new worktree as a create-time convenience
(so it is visible from inside the checkout), but that copy is *not* what
`worktree_start`/`worktree_stop` actually read. Placing the contract only in the
worktree checkout, and never at `<repo_root>/.seretos/worktree-setup.yml`, produces a
silent no-op — `worktree_start` returns `{"status": "ready", "pids": {}}` with no error,
indistinguishable from "no contract configured" (issue #87).

The contract declares up to five lifecycle hooks, each fired by a distinct MCP tool:

| Hook | Fired by | When |
|---|---|---|
| `setup:` | `worktree_create` | Once, right after the checkout is created |
| `start:` | `worktree_start` | On demand, to launch a long-running process |
| `stop:` | `worktree_stop` | On demand, before the process is signalled to exit |
| `teardown:` | `worktree_remove` | Before the worktree directory is deleted |
| `ports:` | (reservation only) | Declares named ports the worktree's services bind to |

## The contract: `.seretos/worktree-setup.yml`

Two top-level keys are always required:

- `version` — an int (currently `1`).
- `isolation` — one of `full`, `partial`, or `none` (see below).

Each step under `setup:`, `start:`, `stop:`, or `teardown:` is a YAML mapping with:

- `run:` — **required**, the shell command to execute.
- `name:` — optional; for `start:`/`stop:` steps this selects the step via
  `worktree_start`'s `variant` parameter (a single unnamed step is the implicit
  `"default"` variant, for back-compat).
- `shell:` — optional override of the shell used to run the step.

`worktree_start` supports multiple **named** `start:` variants — pass the step's `name`
as `variant` to select it (e.g. `variant="gui"` vs. the default headless launch). An
unknown variant raises a `ValueError` listing the available names.

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

Six MCP tools, all under the `worktree` server:

| Tool | Best for |
|---|---|
| `worktree_create` | Create a new worktree for a branch (runs `setup:` steps); copies `.seretos/` into the checkout as a convenience |
| `worktree_list` | Enumerate tracked worktrees, optionally filtered by `repo_root` |
| `worktree_get` | Fetch a single worktree's record (including `setup_status`) without side effects |
| `worktree_start` | Launch a named `start:` variant as a tracked, detached process |
| `worktree_stop` | Run `stop:` steps best-effort, then gracefully (and if needed forcibly) terminate the tracked process |
| `worktree_remove` | Run `teardown:` steps, then delete the worktree checkout; supports `force` and `kill_blocking_processes` |

## Lifecycle

```
worktree_create  →  setup: runs automatically
      │
      ▼
worktree_start   →  start: <variant> launches a tracked process (optional; skip if no long-running process is needed)
      │
      ▼
   ...work...
      │
      ▼
worktree_stop    →  stop: steps run best-effort, then the process is terminated
      │
      ▼
worktree_remove  →  teardown: steps run, then the checkout is deleted
```

`worktree_start`/`worktree_stop` are optional — many tickets only need
`worktree_create` → work → `worktree_remove`, with no long-running process involved.

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
there is no concurrent work to isolate from.

## Troubleshooting

**Setup script fails on create**

The executed command comes from the `setup:` steps in `.seretos/worktree-setup.yml` —
never supplied by the caller. Confirm a `setup:` block is present and correctly
configured, that `isolation: full` is set (required whenever `setup:` is present), and
that the contract file exists at the repository root (not only inside the worktree).

**Port leak after crash / restart**

The server's in-memory state does not survive a restart — after restarting,
`worktree_list` returns empty and `worktree_remove` becomes a no-op for previously
tracked worktrees. If an OS port remains bound after a crash, resolve it at the OS
level: identify the process holding the port (`netstat -ano | findstr <port>` on
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

**Orphan worktree recovery**

Call `worktree_get <id>` first to inspect the record. If it is safe to discard, call
`worktree_remove <id> force=true` to remove it even though it contains uncommitted
changes.

## Pitfalls

1. **Contract in the wrong location is a silent no-op.** The engine reads
   `<repo_root>/.seretos/worktree-setup.yml`, not the worktree checkout's copy. A
   contract placed only in the worktree checkout produces no error — just an
   indistinguishable-from-unconfigured `{"status": "ready", "pids": {}}` response.
2. **`isolation: none` forbids every block.** Adding `setup:`, `start:`, `stop:`,
   `teardown:`, or `ports:` under `isolation: none` raises `ContractValidationError` —
   switch to `isolation: full` first.
3. **In-memory tracking state does not survive a server restart.** Don't rely on
   `worktree_list`/`worktree_remove` to recover state after a crash; resolve leaked OS
   resources (ports, processes) directly at the OS level instead.
4. **Windows can lock a worktree directory via a foreign process's cwd.** If plain
   `worktree_remove` fails, retry with `kill_blocking_processes=True` rather than
   fighting the lock manually.
