"""Host renderers keyed by host id (AD-21).

Each host module is pure and exposes:

- `parse_payload(command, payload) -> dict`: the host's hook payload as an
  `archshell/hook/v1` message; raises `PayloadError` when it cannot.
- `render_verdict(command, verdict) -> str`: an `archshell/verdict/v1` message
  as the host's hook stdout (empty string for no output).

A new host adds a module and an entry in `HOSTS`; nothing else changes.
"""

from __future__ import annotations

from types import ModuleType

from archshell.render.hosts import claude_code
from archshell.render.hosts.errors import PayloadError

HOSTS: dict[str, ModuleType] = {
    "claude-code": claude_code,
}

__all__ = ["HOSTS", "PayloadError"]
