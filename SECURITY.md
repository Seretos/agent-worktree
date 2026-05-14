# Security Policy

## Threat model

`worktree_plugin` is a **local** MCP server. It runs as a process launched
by an MCP client (typically Claude Code) on the same machine as the user,
with the user's own privileges. It does not listen on a network socket and
is not designed to be exposed beyond the host.

The trust boundary is the MCP client: anything that can reach the server's
stdio already runs as the user. The tools exposed here are accordingly
authority-equivalent to "the user runs commands themselves" — within the
scope of whatever credentials or filesystem permissions the user has.

## Intentional shell execution (setup scripts)

`agent-worktree` runs **per-project setup scripts** when a worktree is
created. Executing arbitrary commands on a freshly created worktree is the
plugin's purpose, not an oversight — analogous to how `agent-vdesktop`'s
`tab.command` field forwards a string to a shell **by design**.

What the user opts into when configuring a setup script:

- The script runs with the same OS privileges as the MCP client process
  (i.e. the user's own privileges).
- The script runs against the newly created worktree directory, which is
  derived from a repository the user has already chosen to trust on disk.
- The plugin does **not** sanitize, sandbox, or restrict the contents of
  the setup script. If the configured script can launch `rm -rf /`, the
  plugin will let it.

What is **not** opted into:

- Executing arbitrary code supplied by a tool *caller* (i.e. by Claude or
  by a remote MCP client). Setup scripts come from the user's
  configuration, not from tool arguments. Tool arguments that name a
  setup script must resolve to a script that already exists on disk in a
  user-controlled location.

## Out of scope

- Compromise of the host machine where the plugin runs (the user already
  owns it).
- Misuse of the plugin's tools by a malicious local MCP client — that client
  already runs as the user.
- Damage caused by a user-authored setup script doing what it was written
  to do (see "Intentional shell execution" above).

## Reporting a vulnerability

For unexpected authority escalation, input validation gaps that escape the
documented contract of a tool, or any other security concern, open a GitHub
issue with the label `security` (or a private security advisory if the
repository supports them).

---

<!--
EXTEND THIS FILE with plugin-specific sections as the surface area grows.
Likely additions for agent-worktree specifically:

  ## Destructive-operation gating
  Document how worktree removal interacts with uncommitted changes. State
  whether the plugin refuses by default and what flag opts into the
  destructive path.

  ## Path containment
  If tool arguments accept paths that get joined with repository roots,
  document the resolution rules (absolute vs. relative, symlink handling,
  refusing paths that escape the repo root).
-->
