# agent-worktree

MCP server that ships as a self-contained Windows `.exe` (PyInstaller-frozen Python). End users don't need a Python toolchain.

## Layout

```
src/worktree_plugin/            # Python source (src-layout)
  server.py                       # FastMCP entry point, wires the tools
  __main__.py                     # python -m / PyInstaller entry

tests/                          # pytest, runs on every push (test.yml matrix: windows-latest + ubuntu-22.04)
scripts/build.ps1               # cross-platform pwsh: PyInstaller wrapper + smoke test + staging
worktree.spec                   # PyInstaller config (output extension picked by host OS)
pyproject.toml                  # setuptools (package-dir = src/) + pytest config
.claude-plugin/plugin.json      # plugin manifest; command is the extensionless bin/worktree
SECURITY.md                     # threat model — extend per tool surface

.github/workflows/
  test.yml                      # pytest matrix on every push and PR (fetch-depth: 0 — real git worktree ops)
  release.yml                   # manual-dispatch multi-OS release (stamp -> matrix-build -> assemble)
  dispatch.yml                  # manual recovery: re-send marketplace dispatch
```

## OS_TARGETS = [windows, linux]

Worktree management is built on local `git` operations that work identically on
both platforms — no Win32 lock-in. The pipeline ships both binaries inside a
single release zip:

- `plugin.json`'s `command` is `${CLAUDE_PLUGIN_ROOT}/bin/worktree` (no
  extension). On Windows the host OS resolves that to `worktree.exe`; on
  Linux to `worktree`. One manifest serves both platforms.
- `release.yml` is a three-stage pipeline:
  1. **stamp** (Linux) — writes the version into `pyproject.toml` +
     `.claude-plugin/plugin.json` and uploads them as an artifact so every
     downstream job pulls the same stamped sources.
  2. **build** (matrix `windows-latest` + `ubuntu-22.04`) — each runner
     calls `scripts/build.ps1 -Clean -Package` and uploads its `bin/`
     payload as `bin-<os>`.
  3. **assemble** (Linux) — merges the per-OS bins into a single
     `build/stage/agent-worktree/bin/` tree, builds the release zip with
     correct Unix mode bits via Python's `zipfile`, force-pushes the
     orphan `release` branch, creates the GitHub Release, and dispatches
     to the marketplace.

## Branches

- `main` — source of truth. All edits go here.
- `release` — orphan branch, force-pushed by `release.yml`. Contains only install-ready files: `.claude-plugin/plugin.json`, `bin/worktree.exe`, `bin/worktree`, `README.md`. Clients clone at the version tag (e.g. `agent-worktree--v0.0.1`).

The release branch shares no history with main. Don't try to merge between them.

## Release flow

Triggered manually:

```
Actions → release → Run workflow → version=X.Y.Z
```

or `gh workflow run release.yml -f version=X.Y.Z`.

The workflow:
1. Validates `X.Y.Z` is semver.
2. Fails if tag `agent-worktree--vX.Y.Z` already exists.
3. Stamps the version into `pyproject.toml` and `.claude-plugin/plugin.json` (CI checkout only — never pushed back to main).
4. Runs `scripts/build.ps1 -Clean -Package` (PyInstaller → smoke test → ZIP).
5. Stashes the ZIP outside the working tree (needed because step 6 wipes it).
6. Force-pushes the orphan `release` branch from the staged install-ready tree.
7. Creates the `agent-worktree--vX.Y.Z` tag on that commit and a GitHub Release with the ZIP attached.
8. POSTs to `Seretos/agent-marketplace/dispatches` with the plugin metadata, using `MARKETPLACE_DISPATCH_TOKEN`.

`pyproject.toml`'s `version` field is **not** load-bearing for releases. The workflow input drives everything.

## Required secret

- `MARKETPLACE_DISPATCH_TOKEN` — fine-grained PAT with `Contents: Read and write` + `Pull requests: Read and write` on `Seretos/agent-marketplace` only.

## Build conventions (`scripts/build.ps1`)

- Cross-platform: runs under **Windows PowerShell 5.1**, PowerShell 7 on
  Windows, AND `pwsh` on Linux. PS 5.1 lacks the auto `$IsWindows` variable
  so the script derives it from `$env:OS`.
- Output filename: `bin/worktree.exe` on Windows, `bin/worktree` on Linux
  (no extension). The Linux binary is explicitly `chmod +x`ed after copy.
- No global `$ErrorActionPreference = 'Stop'` — PyInstaller writes heavily to stderr, which PS 5.1 wraps as ErrorRecord and would trip a global Stop.
- Python discovery: prefers `py.exe -3` on Windows locally, falls back to
  `python` / `python3` (which is what `actions/setup-python` installs).
- The smoke test runs an MCP `initialize` handshake against the freshly built binary. The build fails if the handshake fails.
- `-Package` stages `build/stage/agent-worktree/` with this OS's binary
  only — `release.yml`'s assembly job merges the per-OS stages into the
  final zip.

## PyInstaller / src-layout notes

- The Python package is `worktree_plugin` under `src/`. `pyproject.toml` declares `package-dir = { "" = "src" }` and `[tool.pytest.ini_options] pythonpath = ["src"]`.
- `worktree.spec` references `src/worktree_plugin/__main__.py` as the entry and `pathex=[ROOT / "src"]`. Adjust both if the layout ever moves.
- If you add native-binding dependencies (e.g. `pyvda`, `pywin32`, `comtypes`), use `collect_all(...)` for them in `worktree.spec` so PyInstaller picks up lazy-generated submodules.
