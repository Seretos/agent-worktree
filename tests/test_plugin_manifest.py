"""Tests for the plugin manifest's skill registration (ticket #91).

Verifies that ``.claude-plugin/plugin.json`` registers the new ``skills/worktree``
skill directory, and that the skill file itself is well-formed and documents the
worktree contract and its troubleshooting recipes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILL_MD = REPO_ROOT / "skills" / "worktree" / "SKILL.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
WORKTREE_PY = REPO_ROOT / "src" / "worktree_plugin" / "tools" / "worktree.py"


def _read_frontmatter_and_body(text: str) -> tuple[dict, str]:
    """Split a ``---``-fenced YAML frontmatter block from the Markdown body."""
    assert text.startswith("---"), "SKILL.md must start with a YAML frontmatter fence"
    _, frontmatter_raw, body = text.split("---", 2)
    frontmatter = yaml.safe_load(frontmatter_raw)
    return frontmatter, body


def test_plugin_json_registers_skills_dir():
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["skills"] == "./skills"
    assert SKILL_MD.exists()


def test_skill_frontmatter_wellformed():
    text = SKILL_MD.read_text(encoding="utf-8")
    frontmatter, _ = _read_frontmatter_and_body(text)
    assert isinstance(frontmatter, dict)
    assert frontmatter.get("name")
    assert frontmatter.get("description")


def test_skill_documents_contract_and_recipes():
    text = SKILL_MD.read_text(encoding="utf-8")
    load_bearing_tokens = [
        ".seretos/worktree-setup.yml",
        "isolation: none",
        "isolation: full",
        "setup:",
        "start:",
        "stop:",
        "kill_blocking_processes",
        "force=true",
    ]
    for token in load_bearing_tokens:
        assert token in text, f"SKILL.md is missing expected token: {token!r}"


# ---- Ticket #99: MCP tool-surface split (worktree_* -> checkout/environment) ----


def test_skill_and_agents_teach_two_lifecycle_split():
    """SKILL.md and AGENTS.md must both teach the ticket #99 two-lifecycle
    split (checkout vs. environment tools), including the checkout_path
    addressing deviation, and must contain no trace of the six removed
    tool names/params they replace."""
    required_tokens = [
        "environment_list",
        "environment_start",
        "environment_stop",
        "environment_id",
        "checkout_path",
    ]
    # Any of these phrases is sufficient evidence of a "primary/main clone"
    # mention -- the split's key conceptual addition.
    primary_mentions = ["primary checkout", "primary/main clone", "main clone"]
    forbidden_tokens = [
        "worktree_list",
        "worktree_get",
        "worktree_start",
        "worktree_stop",
        "worktree_id",
    ]

    for path in (SKILL_MD, AGENTS_MD):
        text = path.read_text(encoding="utf-8")
        for token in required_tokens:
            assert token in text, f"{path.name} is missing expected token: {token!r}"
        assert any(phrase in text for phrase in primary_mentions), (
            f"{path.name} does not mention the primary/main clone"
        )
        for token in forbidden_tokens:
            assert token not in text, (
                f"{path.name} still references removed token: {token!r}"
            )


# ---- Ticket #113: untracked orphan recovery doc contract ----


def _windows_after(text: str, token: str, window: int = 600) -> list[str]:
    """Return the ``window``-char slice following each occurrence of
    ``token`` in ``text`` -- used to check that a follow-up token (e.g.
    ``checkout_path``) appears near a given mention, without depending on
    exact heading/section structure."""
    return [
        text[match.start() : match.start() + window]
        for match in re.finditer(re.escape(token), text)
    ]


def test_docs_state_untracked_orphan_recovery():
    """AGENTS.md and SKILL.md must document the true untracked-linked-
    worktree id contract (a synthesised ``-untracked-<8hex>`` id, addressed
    for removal via ``checkout_path``) and must no longer claim such an
    id is the empty string ``""`` (the stale claim ticket #113 fixes)."""
    stale_claims = ['is `""`', 'id is ""', "empty string"]

    for path in (AGENTS_MD, SKILL_MD):
        text = path.read_text(encoding="utf-8")

        assert "-untracked-" in text, (
            f"{path.name} does not document the synthesised -untracked-<8hex> id"
        )

        windows = _windows_after(text, "worktree_remove")
        assert any("checkout_path" in w for w in windows), (
            f"{path.name} does not mention checkout_path near worktree_remove"
        )

        for stale in stale_claims:
            assert stale not in text, (
                f"{path.name} still contains the stale untracked-id claim: {stale!r}"
            )


def test_source_docstrings_state_untracked_orphan_recovery():
    """``worktree.py``'s ``_entry_to_dict`` and ``environment_list``
    docstrings must document the true synthesised-linked-worktree id shape
    (``<repo-slug>-<branch-slug>-untracked-<8-hex>``, via
    ``untracked_id_for()``) and must no longer claim such an id is the
    empty string ``""`` (the stale claim ticket #113's review fix removes
    from the source, not just the docs)."""
    text = WORKTREE_PY.read_text(encoding="utf-8")

    stale_claims = ['id == ""', "is the empty string"]
    for stale in stale_claims:
        assert stale not in text, (
            f"worktree.py still contains the stale untracked-id claim: {stale!r}"
        )

    assert text.count("-untracked-<8-hex>") >= 2, (
        "worktree.py's _entry_to_dict and environment_list docstrings must "
        "both state the true synthesised id shape "
        "<repo-slug>-<branch-slug>-untracked-<8-hex>"
    )
    assert text.count("untracked_id_for") >= 2, (
        "worktree.py's _entry_to_dict and environment_list docstrings must "
        "both attribute the synthesised id to untracked_id_for()"
    )
