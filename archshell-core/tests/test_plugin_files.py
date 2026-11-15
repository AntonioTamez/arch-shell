"""Checks of this repository's own plugin commands (story 1.3).

The generic plugin checks (manifest, frontmatter, `run.md`, the agent, hooks,
the engine override) live in the conformance suite,
`archshell-core/conformance/test_plugin_conformance.py`. This file keeps what
is specific to this repository: the `/archshell:ping` smoke command.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PING = REPO / "archshell-claude-code" / "commands" / "ping.md"
ENGINE_EXPANSION = "${ARCHSHELL_ENGINE:-archshell-engine}"


def test_ping_asks_the_engine_for_the_probe_flow():
    text = PING.read_bytes().decode("utf-8")
    assert f"{ENGINE_EXPANSION} next --feature probe" in text
    assert f"allowed-tools: Bash({ENGINE_EXPANSION} next:*)" in text
