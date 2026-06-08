# agent-worktree

MCP server for git worktree lifecycle management. Create/list/remove worktrees with branch handling, run per-project setup scripts on creation, and detect uncommitted changes before destructive operations.

## What it does

A thin MCP wrapper around [`lib-python-worktree`](https://github.com/Seretos/lib-python-worktree). Ships as a self-contained frozen binary — no Python needed on the host. Manages git worktree lifecycle (create, list, get, remove, start, stop) via six MCP tools.

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
   An `isolation: none` contract is simply `version: 1` + `isolation: none` with no `setup:`, `teardown:`, or `ports:` blocks. For full contract documentation and working examples, see the [lib-python-worktree README](https://github.com/Seretos/lib-python-worktree#readme).

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

The server's in-memory state does not survive a restart — after restarting, `worktree_list` returns empty and `worktree_remove` is a no-op. In-memory tracking is therefore already cleared. If an OS port remains bound after a crash (process did not exit cleanly), resolve it at the OS level: identify the process holding the port (e.g. `netstat -ano | findstr <port>` on Windows, `lsof -i :<port>` on Linux) and terminate it.

**Worktree directory locked by a foreign process (Windows)**

On Windows, processes whose working directory is set to a path inside the worktree can prevent directory deletion. Pass `kill_blocking_processes=True` to `worktree_remove` to have the tool automatically terminate those foreign processes before removal:

```
worktree_remove(<id>, kill_blocking_processes=True)
```

The response's `killed_pids` field lists every process that was terminated (pid, name, cmdline). If the directory is still locked after the kill attempt, the tool raises an error — you can then resolve the remaining lock at the OS level and retry.

**Orphan worktree on disk**

Call `worktree_get <id>` to inspect the record first. If the worktree is safe to discard, call `worktree_remove <id> force=true` to remove it even if it contains uncommitted changes.
