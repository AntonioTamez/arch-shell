"""Claude Code plugin conformance (FR-37, AD-14).

The host plugin holds no logic and has no Python, so it is checked as files:
the manifest, well-formed frontmatter, `run.md` covering the whole closed
action set and the protocol rules it must state, the plugin agent, the hook
wiring, and the `ARCHSHELL_ENGINE` override on every engine call. The
engine-interface run (AD-18 `human_ui`) arrives with story 1.13.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archshell.domain.actions import ACTION_TYPES

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "archshell-claude-code"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
COMMANDS = sorted((PLUGIN / "commands").glob("*.md"))
AGENTS = sorted((PLUGIN / "agents").glob("*.md"))
MARKDOWN = COMMANDS + AGENTS
RUN = PLUGIN / "commands" / "run.md"
AGENT = PLUGIN / "agents" / "archshell-task.md"
ENGINE_EXPANSION = "${ARCHSHELL_ENGINE:-archshell-engine}"


def frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Parse a flat `key: value` frontmatter block; return it and the body."""
    text = path.read_bytes().decode("utf-8")
    assert not text.startswith("\ufeff"), f"{path.name} has a BOM"
    lines = text.replace("\r\n", "\n").split("\n")
    assert lines[0] == "---", f"{path.name} does not start with frontmatter"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        assert sep and key and key == key.strip() and " " not in key, (path.name, line)
        assert key not in fields, (path.name, key)
        fields[key] = value.strip()
    return fields, "\n".join(lines[end + 1 :])


def plugin_name() -> str:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    return manifest["name"]


def test_manifest_is_json_with_a_name():
    text = MANIFEST.read_bytes().decode("utf-8")
    assert not text.startswith("\ufeff")
    manifest = json.loads(text)
    assert isinstance(manifest["name"], str) and manifest["name"].strip()
    assert isinstance(manifest.get("description"), str) and manifest["description"].strip()


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_frontmatter_parses_with_a_description(path):
    fields, body = frontmatter(path)
    assert fields["description"]
    assert body.strip()


def test_run_covers_every_action_type():
    _, body = frontmatter(RUN)
    for action_type in ACTION_TYPES:
        assert f"### `{action_type}`" in body, action_type
    assert body.count("\n### `") == len(ACTION_TYPES)


def test_run_uses_the_engine_override_for_every_call():
    fields, body = frontmatter(RUN)
    assert "ARCHSHELL_ENGINE" in body
    assert f"{ENGINE_EXPANSION} next --feature" in body
    assert f"{ENGINE_EXPANSION} submit --feature" in body
    assert "archshell-engine next" not in body.replace(ENGINE_EXPANSION, "")
    assert "archshell-engine submit" not in body.replace(ENGINE_EXPANSION, "")
    assert f"Bash({ENGINE_EXPANSION} next:*)" in fields["allowed-tools"]
    assert f"Bash({ENGINE_EXPANSION} submit:*)" in fields["allowed-tools"]


def test_run_sends_messages_on_stdin():
    _, body = frontmatter(RUN)
    assert body.count("<<'ARCHSHELL_MESSAGE'") == 2


def action_section(body: str, action_type: str) -> str:
    """The text of one action's `###` section of run.md, up to the next heading."""
    start = body.index(f"### `{action_type}`")
    end = body.find("\n#", start + 1)
    return body[start:] if end == -1 else body[start:end]


@pytest.mark.parametrize("action_type", ["ask", "confirm"])
def test_run_answers_questions_with_the_tool_nonce_and_human_source(action_type):
    fields, body = frontmatter(RUN)
    assert "AskUserQuestion" in fields["allowed-tools"]
    section = action_section(body, action_type)
    assert "AskUserQuestion" in section
    assert '"nonce":"<the action\'s nonce>"' in section
    assert '"source":"human_ui"' in section
    for field in ("`question`", "`header`"):
        assert f"{field} set to the action's {field} verbatim" in section
    assert "one option per entry of the action's `options`, in order" in section


@pytest.mark.parametrize("action_type", ["llm_task", "instruct_agent", "report", "done"])
def test_run_adds_nonce_and_source_only_to_questions(action_type):
    _, body = frontmatter(RUN)
    section = action_section(body, action_type)
    assert "nonce" not in section and "human_ui" not in section


def test_run_submits_the_result_shape_of_each_action():
    _, body = frontmatter(RUN)
    assert '{"text": <the agent\'s reply, verbatim>}' in action_section(body, "llm_task")
    assert '{"answer":' in action_section(body, "ask")
    confirm = action_section(body, "confirm")
    assert '{"confirmed": true}' in confirm and '{"confirmed": false}' in confirm
    assert '{"status": "done"}' in action_section(body, "instruct_agent")
    for terminal in ("report", "done"):
        assert "Submit nothing" in action_section(body, terminal)


def test_run_never_authors_question_text_or_status_lines():
    _, body = frontmatter(RUN)
    assert "`Yes`" not in body and "`No`" not in body
    assert "`ArchShell`" not in body
    assert "running agent task" not in body


def test_run_passes_the_agent_an_absolute_prompt_path():
    _, body = frontmatter(RUN)
    section = action_section(body, "llm_task")
    assert "absolute path" in section
    assert "joined with `prompt_file`" in section


def test_run_stops_on_engine_errors_and_unknown_types():
    _, body = frontmatter(RUN)
    assert "archshell/error/v1" in body
    assert "unsupported action type" in body
    assert "do not retry" in body


def test_agent_exists_and_is_referenced_by_its_plugin_qualified_name():
    fields, _ = frontmatter(AGENT)
    assert fields["name"] == AGENT.stem == "archshell-task"
    assert fields["tools"] == "Read"
    qualified = f"{plugin_name()}:{fields['name']}"
    assert qualified == "archshell:archshell-task"
    fields_run, body = frontmatter(RUN)
    assert f"`subagent_type` `{qualified}`" in body
    assert "Agent" in fields_run["allowed-tools"]


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.name)
def test_every_command_calls_the_engine_through_the_override(path):
    fields, body = frontmatter(path)
    engine_calls = body.count(ENGINE_EXPANSION) + fields.get("allowed-tools", "").count(ENGINE_EXPANSION)
    assert engine_calls, f"{path.name} never calls the engine"
    without = body.replace(ENGINE_EXPANSION, "") + fields.get("allowed-tools", "").replace(ENGINE_EXPANSION, "")
    for command in ("next", "submit"):
        assert f"archshell-engine {command}" not in without, (path.name, command)


# --- hooks.json (story 1.4) -------------------------------------------------

HOOKS = PLUGIN / "hooks" / "hooks.json"
STDERR_LOG = '2>>"${TMPDIR:-/tmp}/archshell-engine.log"'
CD_PROJECT = 'cd "${CLAUDE_PROJECT_DIR:-.}" && '
HOOK_EVENTS = {
    "PreToolUse": ("guard-write", "Write|Edit|MultiEdit|NotebookEdit"),
    "PostToolUse": ("check-edit", "Write|Edit|MultiEdit|NotebookEdit"),
    "Stop": ("on-stop", None),
}


def hook_commands() -> dict[str, list[dict]]:
    text = HOOKS.read_bytes().decode("utf-8")
    assert not text.startswith("\ufeff")
    return json.loads(text)["hooks"]


def test_hooks_json_wires_each_event_to_its_engine_command():
    hooks = hook_commands()
    assert set(hooks) == set(HOOK_EVENTS)
    for event, (command, matcher) in HOOK_EVENTS.items():
        (group,) = hooks[event]
        assert group.get("matcher") == matcher, event
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert hook["command"].startswith(f"{CD_PROJECT}{ENGINE_EXPANSION} {command} --host claude-code {STDERR_LOG}")


def test_hook_commands_are_one_line_and_hold_no_paths():
    for groups in hook_commands().values():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                assert "\n" not in command and "\r" not in command
                assert command.count(ENGINE_EXPANSION) == 1
                assert STDERR_LOG in command
                without_log = command.replace(STDERR_LOG, "")
                for forbidden in (".archshell", "src/", ":\\", "/tmp", "--feature"):
                    assert forbidden not in without_log, (forbidden, command)


def test_guard_write_hook_fails_closed_when_the_engine_cannot_run():
    (group,) = hook_commands()["PreToolUse"]
    command = group["hooks"][0]["command"]
    assert command.endswith("exit 2; }")
    # `cd && engine || fallback`: a failed cd or a failed engine both block.
    assert command.startswith(CD_PROJECT)
    assert command.count(" && ") == 1 and command.index(" && ") < command.index(" || { ")


def test_run_passes_the_conversation_language_on_every_call():
    _, body = frontmatter(RUN)
    assert f"{ENGINE_EXPANSION} next --feature '<slug>' --lang <lang> " in body
    assert f"{ENGINE_EXPANSION} submit --feature '<slug>' --lang <lang> " in body
    assert "`es` when the developer writes to you in Spanish, `en` otherwise" in body
    assert "never translate engine text yourself" in body
