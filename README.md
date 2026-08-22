# agent-worktree

MCP server for git worktree lifecycle management. Create/list/remove worktrees with branch handling, run per-project setup scripts on creation, and detect uncommitted changes before destructive operations.

## What it does

A thin MCP wrapper around [`lib-python-worktree`](https://github.com/Seretos/lib-python-worktree). Ships as a self-contained frozen binary — no Python needed on the host. Five MCP tools split along the two real lifecycles a checkout goes through: checkout lifecycle (`worktree_create`, `worktree_remove` — create/delete the directory) and environment lifecycle (`environment_list`, `environment_start`, `environment_stop` — the process running against any checkout, the primary/main clone included). See `AGENTS.md` for the full tool reference.

## Quickstart

1. **Install** — see [Quick install](#quick-install) below.
2. **MCP config** — the plugin manifest wires this automatically after install, but if you need a manual entry the server key and shape are:
   ```json
   {
     "mcpServers": {
       "worktree": {
         "command": "/path/to/plugin/bin/worktree",
         "args": []
       }
     }
   }
   ```
   The `command` value is extensionless (`bin/worktree`); the host OS resolves it to `bin/worktree.exe` on Windows and `bin/worktree` on Linux — one config entry serves both platforms.
3. **First `.seretos/worktree-setup.yml`** — place this file at the root of your repository. The following is an illustrative shape (valid contract with `isolation: full` and a `setup:` step):
   ```yaml
   version: 1
   isolation: full
   setup:
     - run: npm install
   ```
   An `isolation: none` contract is simply `version: 1` + `isolation: none` with no `setup:`, `teardown:`, or `ports:` blocks. Optional `start:`/`stop:` steps (used by `environment_start`/`environment_stop`) follow the same per-step shape — a required `run:` (the shell command) and an optional `name:` (selects the step via `environment_start`'s `variant` parameter):
   ```yaml
   start:
     - name: web
       run: start-web.sh
     - name: worker
       run: start-worker.sh
   stop:
     - name: web
       run: stop-web.sh
   ```
   For full contract documentation and working examples, see the [lib-python-worktree README](https://github.com/Seretos/lib-python-worktree#readme).

## Quick install

**Claude Code:**

```
/plugin marketplace add Seretos/agent-marketplace
/plugin install agent-worktree@agent-marketplace
```

Self-contained binary (Windows `.exe` or Linux ELF) — no Python, no `pip install`, no dependencies. The release zip ships both binaries; the host OS auto-selects via the extensionless `command: bin/worktree` in `plugin.json`.

## Alternative installs

### From the GitHub Releases page

1. Download `agent-worktree-<version>.zip` from [Releases](https://github.com/Seretos/agent-worktree/releases).
2. Unpack to a stable folder (e.g. `C:\Users\<you>\.claude\plugins\agent-worktree\`).
3. In Claude Code:
   ```
   /plugin install <path-to-unpacked-folder>
   ```

### From the release branch

The `release` branch always carries the latest install-ready files (no zip step):

```
git clone --branch release --depth 1 https://github.com/Seretos/agent-worktree.git
```

Then `/plugin install <cloned-path>` in Claude Code.

### Build from source

Requires Python 3.11+ and PowerShell 7 (`pwsh`).

```bash
git clone https://github.com/Seretos/agent-worktree.git
cd agent-worktree
python -m pip install -e ".[build]"
pwsh -File scripts/build.ps1 -Clean -Package
```

Output: `bin/worktree` (Linux) or `bin/worktree.exe` (Windows), plus a
`build/stage/agent-worktree/` payload for this OS. The official release zip
is produced by `release.yml`'s matrix-then-assemble pipeline and merges
both OS payloads into a single archive.

## Troubleshooting

**Setup script fails on create**

The executed command comes from the `setup:` steps defined in `.seretos/worktree-setup.yml` inside the worktree — it is never supplied by the tool caller. Confirm that a `setup:` block is present and correctly configured, that `isolation: full` is set (required for `setup:` steps), and that the file is present at the repository root of the newly created worktree.

**Port leak after crash / restart**

State is persistent and disk-backed (`~/.agent-worktree/state.yaml`), reconciled on startup. If an OS port nonetheless remains bound after a crash (process did not exit cleanly and reconciliation could not recover it), resolve it at the OS level: identify the process holding the port (e.g. `netstat -ano | findstr <port>` on Windows, `lsof -i :<port>` on Linux) and terminate it.

**Worktree directory locked by a foreign process (Windows)**

On Windows, processes whose working directory is set to a path inside the worktree can prevent directory deletion. Pass `kill_blocking_processes=True` to `worktree_remove` to have the tool automatically terminate those foreign processes before removal:

A tracked process started via `environment_start` is stopped by `worktree_remove` itself before deletion and normally does not need this flag; it is for genuinely foreign holders instead — an editor, a shell whose cwd is in the checkout, a build tool. That tracked stop is best-effort, though: a tracked process that refuses to die degrades into the same blocking condition and *does* then need this flag.

```
worktree_remove(<id>, kill_blocking_processes=True)
```

The response's `killed_pids` field lists every process that was terminated (pid, name, cmdline). If the directory is still locked after the kill attempt, the tool raises an error — you can then resolve the remaining lock at the OS level and retry.

**Compound blocking (ticket #120).** If the directory lock and uncommitted/untracked changes are BOTH blocking removal at once, the raised error names every blocking condition and the flag needed to clear each in one message — `(blocked_by: "dir_locked", "uncommitted_changes"; required_flags: kill_blocking_processes=True, force=True)`. Retry once with both flags set instead of discovering each condition across separate failed attempts.

**Transport failure ("Connection closed"): confirm before retrying (ticket #116)**

If a call dies with `Connection closed` / `MCP error -32000`, the response was lost — but the operation may have landed. Do not blind-retry a mutating call. Read back with `environment_list(path=<repo root>)` first; that call never writes state, so retrying *it* is always safe.

| Lost call | Landed if `environment_list` shows |
| --- | --- |
| `worktree_create` | an entry with your `branch` and `tracked: true` — its `id` is the id the lost response carried (the 8-hex suffix is random and not re-derivable) |
| `worktree_remove` | the entry is gone; `status: "orphaned"` means partially landed |
| `environment_start` | your `role` present as a key in `pids` |
| `environment_stop` | your `role` absent from `pids` |

Read `worktree_remove` back from the **repo root**, never from the removed checkout path: that path is gone, so passing it back raises `invalid checkout_path '<p>': checkout_path does not exist: ...` — the exact text a typo produces, which proves nothing. Retry a removal **by `environment_id`, not `checkout_path`**: the id form is self-diagnosing (soft `{"code": "not_found"}`). A blind `worktree_create` retry is self-diagnosing too — it raises a duplicate error naming the landed environment inline, `(existing_environment_id: "<id>", existing_path: "<path>")` — best-effort: if the landed record can't be looked up, you just get the bare "already exists" text with no tokens.

**Orphan worktree on disk**

An orphan is a linked worktree that exists on disk but has no persisted record — `environment_list` shows it with `tracked: false` and a synthesised, display-only id (`<checkout-dirname-slug>-untracked-<8-hex>`): the checkout directory's own basename, slugged (lower-case ASCII, non-alphanumeric runs collapsed to `-`, truncated to 40 chars), plus the first 8 hex characters of a SHA-256 hash of its resolved path — no repo slug and no branch slug are involved. That id is a one-way derivation of its checkout path, not a lookup key. Removing it by that id (`worktree_remove(environment_id=...)`) always comes back as a not-found error. Instead:

1. Call `environment_list(path=<repo_root>)` and find the entry with `tracked: false`.
2. Call `worktree_remove(checkout_path=<entry's path>, force=true)` — addressed by `path`, not `id` — to remove it even if it contains uncommitted changes.
