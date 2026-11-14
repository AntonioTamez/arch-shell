"""Claude Code host renderer: hook payloads in, hook output out. Pure.

Claude Code hook contract (https://code.claude.com/docs/en/hooks):

- PreToolUse receives `tool_name`, `tool_input`, `cwd`. Write, Edit, and
  MultiEdit carry `tool_input.file_path`; NotebookEdit carries
  `tool_input.notebook_path`. To refuse, print (exit 0)
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": ...}}`.
  No output leaves the normal permission flow untouched; `allow` would skip
  the user's permission prompt, so it is never rendered.
- PostToolUse and Stop accept `{"decision": "block", "reason": ...}`: the
  reason is fed to the agent (after the edit, or as why it must continue).
  Omitting `decision` lets the tool result stand or the agent stop.
"""

from __future__ import annotations

import json
from typing import Any

from archshell.domain.hooks import BLOCK, HOOK_SCHEMA
from archshell.render.hosts.errors import PayloadError

HOST_ID = "claude-code"

EVENTS = {
    "guard-write": "PreToolUse",
    "check-edit": "PostToolUse",
    "on-stop": "Stop",
}

PATH_FIELDS = ("file_path", "notebook_path")


def parse_payload(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = EVENTS[command]
    if payload.get("hook_event_name") != event:
        raise PayloadError(f"{command} expects a Claude Code {event} hook payload")
    message: dict[str, Any] = {"schema": HOOK_SCHEMA}
    cwd = payload.get("cwd")
    if cwd is not None:
        message["cwd"] = cwd
    if event == "Stop":
        return message
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise PayloadError(f"{event} payload has no `tool_input` object")
    for field in PATH_FIELDS:
        if field in tool_input:
            message["path"] = tool_input[field]
            break
    else:
        raise PayloadError(
            f"{event} payload `tool_input` has neither `file_path` nor `notebook_path`"
        )
    return message


def render_verdict(command: str, verdict: dict[str, Any]) -> str:
    if verdict.get("verdict") != BLOCK:
        return ""
    reason = verdict.get("reason") or verdict.get("code") or "blocked by ArchShell"
    if command == "guard-write":
        output: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    else:
        output = {"decision": "block", "reason": reason}
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
