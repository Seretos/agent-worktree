# agent-worktree

MCP server for git worktree lifecycle management. Create/list/remove worktrees with branch handling, run per-project setup scripts on creation, and detect uncommitted changes before destructive operations.

## Quick install

**Claude Code:**

```
/plugin marketplace add Seretos/agent-marketplace
/plugin install agent-worktree@agent-marketplace
```

Self-contained `.exe` — no Python, no `pip install`, no dependencies.

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

Requires Python 3.11+ (standard python.org installer with the `py` launcher).

```powershell
git clone https://github.com/Seretos/agent-worktree.git
cd agent-worktree
py -3 -m pip install -e ".[build]"
.\scripts\build.ps1 -Clean -Package
```

Output: `bin/worktree.exe` plus `dist/agent-worktree-<version>.zip`. Then install via `/plugin install <path>`.
