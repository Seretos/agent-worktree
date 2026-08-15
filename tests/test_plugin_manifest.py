"""Tests for the plugin manifest's skill registration (ticket #91).

Verifies that ``.claude-plugin/plugin.json`` registers the new ``skills/worktree``
skill directory, and that the skill file itself is well-formed and documents the
worktree contract and its troubleshooting recipes.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILL_MD = REPO_ROOT / "skills" / "worktree" / "SKILL.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"


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
