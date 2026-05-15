# agent-worktree

MCP server for git worktree lifecycle management. Create/list/remove worktrees with branch handling, run per-project setup scripts on creation, and detect uncommitted changes before destructive operations.

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
