# agent-worktree

Spin up isolated git worktrees — each with its own MCP servers, ports, and
lifecycle — so multiple agents can work in parallel without stepping on each other.

## What it does
- **Isolated worktrees on demand** — create a fresh git worktree per task or agent from a declarative `.seretos/worktree-setup.yml` contract.
- **Per-worktree MCP servers** — each worktree runs its own server instances, so agents don't share state.
- **Automatic port allocation** — parallel worktrees get non-colliding ports.
- **Full lifecycle management** — create, list, get, and remove worktrees through MCP tools.
- **Built for parallel agents** — purpose-built so a fleet of agents can work the same repo at once.

## Typical uses
- Running multiple coding agents in parallel on one repository.
- Isolating per-ticket or per-feature work in disposable worktrees.
- Giving each agent its own services and ports without manual bookkeeping.

## Learn more
See the README for installation, MCP configuration, and troubleshooting.
