"""Shared harness for the subprocess tests: the engine command, its runners,
Claude Code hook payloads, and the scripted no-op host (`ScriptedHost`).

Test modules import these names directly (`from conftest import ...`); pytest
puts this directory on `sys.path` before it collects them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ENGINE = [sys.executable, "-m", "archshell.engine"]


def tree(root: Path) -> list[str]:
    """Every path under `root`, relative and POSIX, sorted."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def engine_process(
    cwd: Path | None, *argv: str, stdin: bytes = b"{}"
) -> subprocess.CompletedProcess[bytes]:
    """Run the engine with `argv` in `cwd` (the current directory when None)."""
    return subprocess.run(
        ENGINE + list(argv), input=stdin, capture_output=True, cwd=cwd, timeout=60
    )


def single_json_line(stdout: bytes | str, stderr: bytes = b"") -> dict:
    """The one JSON message on `stdout`, which must be exactly one LF-ended line."""
    text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    assert text.endswith("\n") and len(text.splitlines()) == 1, (text, stderr)
    return json.loads(text)


def run_engine(
    cwd: Path, *argv: str, stdin: bytes = b"{}", single_line: bool = True
) -> tuple[int, dict]:
    """Run the engine in `cwd`; return its exit code and stdout message.

    With `single_line=False` stdout is parsed as JSON without checking that it
    is exactly one line.
    """
    proc = engine_process(cwd, *argv, stdin=stdin)
    if single_line:
        return proc.returncode, single_json_line(proc.stdout, proc.stderr)
    return proc.returncode, json.loads(proc.stdout.decode("utf-8"))


class Project:
    """A temporary project root the engine runs in."""

    def __init__(self, root: Path):
        self.root = root

    def engine(self, *argv: str, stdin: bytes = b"{}") -> tuple[int, dict]:
        return run_engine(self.root, *argv, stdin=stdin)


# --- Claude Code hook payloads ----------------------------------------------


def tool_payload(root: Path, tool: str, path: str, event: str = "PreToolUse") -> dict:
    """A Claude Code PreToolUse/PostToolUse payload for `tool` writing `path`."""
    field = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {
        "session_id": "s",
        "transcript_path": "t.jsonl",
        "cwd": str(root),
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {field: path},
    }


def stop_payload(root: Path, active: bool = False) -> dict:
    """A Claude Code Stop payload."""
    return {"cwd": str(root), "hook_event_name": "Stop", "stop_hook_active": active}


# --- the scripted no-op host (story 1.13) -------------------------------------

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "archshell-claude-code"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
RECORD_SCHEMA_FILE = REPO / "archshell-core" / "archshell" / "schemas" / "record-v1.json"
TERMINAL_TYPES = ("done", "report")
# The scripted host's fixed confirmation time, so record bytes match on every OS.
RECORD_CONFIRMED_AT = "2026-11-15T00:00:00Z"


def find_bash() -> str:
    """The POSIX shell Claude Code runs hook commands with (Git Bash on Windows)."""
    found = shutil.which("bash")
    if os.name != "nt":
        assert found, "bash is not on PATH"
        return found
    # Skip the WSL launchers, which run a different system's shell.
    if found and not any(part in found.lower() for part in ("system32", "windowsapps")):
        return found
    git = shutil.which("git")
    if git:
        for base in Path(git).resolve().parents:
            candidate = base / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    raise AssertionError("Git Bash is not installed (hook commands need a POSIX shell)")


def hook_commands() -> dict[str, tuple[str, str | None]]:
    """Event name -> (command, matcher) that `hooks.json` gives Claude Code.

    Each event must hold exactly one command hook; `matcher` is None for Stop.
    """
    hooks = json.loads(HOOKS_JSON.read_bytes().decode("utf-8"))["hooks"]
    commands: dict[str, tuple[str, str | None]] = {}
    for event, groups in hooks.items():
        found = [
            (hook["command"], group.get("matcher"))
            for group in groups
            for hook in group["hooks"]
            if hook["type"] == "command"
        ]
        assert len(found) == 1, (event, found)
        commands[event] = found[0]
    return commands


@dataclass
class HookRun:
    event: str
    payload: dict
    code: int
    stdout: str
    stderr: str


class ScriptedHost:
    """A no-op Claude Code host built only from the plugin's contract files.

    It follows `commands/run.md`: `next`, then per action type submit the
    result shape run.md names (`{"text"}` for `llm_task`, `{"answer"}` /
    `{"confirmed"}` plus the action's `nonce` and `source: "human_ui"` for
    `ask` / `confirm`, `{"status": "done"}` for `instruct_agent`) until a
    terminal `done` or `report`, always with `--lang`. The "user" picks the
    first option; the "agent" reads the prompt file and replies with one
    sentence. Hooks run the literal `hooks/hooks.json` command strings through
    bash, with `ARCHSHELL_ENGINE` set to the module form (Smart App Control
    blocks the uv launchers). Every action, submission, hook run, and
    engine-written file is recorded.
    """

    def __init__(self, root: Path, lang: str = "en", log_dir: Path | None = None):
        self.root = root
        self.lang = lang
        self.log_dir = log_dir if log_dir is not None else root.parent / "host-tmp"
        self.actions: list[dict] = []
        self.submissions: list[dict] = []
        self.hook_runs: list[HookRun] = []
        self.records: list = []  # the records read back after the last `done`
        # Project-relative POSIX path -> the last bytes seen there.
        self.written: dict[str, bytes] = {}

    # engine calls ---------------------------------------------------------

    def engine(self, command: str, feature: str, message: dict | None = None) -> tuple[int, dict]:
        stdin = json.dumps({} if message is None else message).encode("utf-8")
        result = run_engine(
            self.root, command, "--feature", feature, "--lang", self.lang, stdin=stdin
        )
        self._collect_written()
        return result

    def next(self, feature: str) -> dict:
        code, action = self.engine("next", feature)
        assert code == 0 and action["schema"] == "archshell/action/v1", action
        self.actions.append(action)
        return action

    def submit(self, message: dict) -> tuple[int, dict]:
        self.submissions.append(message)
        code, reply = self.engine("submit", message["feature"], message)
        if code == 0:
            self.actions.append(reply)
        return code, reply

    # run.md rules ---------------------------------------------------------

    def agent_reply(self, action: dict) -> str:
        """The no-op agent: read the prompt file by its absolute path, reply once."""
        prompt = self.root.joinpath(*action["prompt_file"].split("/")).read_bytes()
        assert prompt.strip(), action
        return "Received the task."

    def result_for(self, action: dict) -> dict:
        kind = action["type"]
        if kind == "llm_task":
            return {"text": self.agent_reply(action)}
        if kind == "ask":
            return {"answer": action["options"][0]}
        if kind == "confirm":
            return {"confirmed": True}
        if kind == "instruct_agent":
            return {"status": "done"}
        raise AssertionError(f"unsupported action type: {kind}")

    def submission(self, action: dict, result: dict | None = None, **overrides) -> dict:
        message = {
            "schema": "archshell/submit/v1",
            "feature": action["feature"],
            "action_id": action["action_id"],
            "revision": action["revision"],
            "result": self.result_for(action) if result is None else result,
        }
        if action["type"] in ("ask", "confirm"):
            message["nonce"] = action["nonce"]
            message["source"] = "human_ui"
        message.update(overrides)
        return message

    def answer(self, action: dict) -> dict:
        """Submit run.md's answer to `action`; return the next action."""
        code, reply = self.submit(self.submission(action))
        assert code == 0 and reply["schema"] == "archshell/action/v1", reply
        return reply

    def finish(self, action: dict) -> list[dict]:
        """Answer from `action` on until a terminal action, then run the Stop hook.

        After `done`, the confirmed flow is recorded: a `plan` record, twice.
        """
        flow = [action]
        while flow[-1]["type"] not in TERMINAL_TYPES:
            flow.append(self.answer(flow[-1]))
        run = self.hook("Stop", stop_payload(self.root))
        assert (run.code, run.stdout) == (0, ""), run
        if flow[-1]["type"] == "done":
            content = {"flow": flow[-1]["feature"], "actions": [a["type"] for a in flow]}
            self.records = self.write_records(flow[-1]["feature"], content)
        return flow

    def run_flow(self, feature: str) -> list[dict]:
        """Drive `feature` from `next` to its terminal action, then the Stop hook."""
        return self.finish(self.next(feature))

    # hooks.json -----------------------------------------------------------

    def hook_env(self, engine: str | None = None) -> dict[str, str]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # `python` resolves to this interpreter, so the override needs no quoting.
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        env["ARCHSHELL_ENGINE"] = engine if engine is not None else "python -m archshell.engine"
        env["CLAUDE_PROJECT_DIR"] = self.root.as_posix()
        env["TMPDIR"] = self.log_dir.as_posix()
        return env

    def hook(self, event: str, payload: dict, engine: str | None = None) -> HookRun:
        """Run the `hooks.json` command for `event` with `payload` on stdin."""
        before = tree(self.root)
        proc = subprocess.run(
            [find_bash(), "-c", hook_commands()[event][0]],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=self.hook_env(engine),
            timeout=30,
        )
        assert tree(self.root) == before, "a hook command wrote a file"
        run = HookRun(
            event, payload, proc.returncode,
            proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8", "replace"),
        )
        self.hook_runs.append(run)
        return run

    def write(self, tool: str, path: Path) -> tuple[HookRun, HookRun | None]:
        """An agent tool write: PreToolUse, then (only when not denied) PostToolUse."""
        commands = hook_commands()
        for event in ("PreToolUse", "PostToolUse"):
            matcher = commands[event][1]
            assert matcher is not None and re.fullmatch(matcher, tool), (event, matcher, tool)
        pre = self.hook("PreToolUse", tool_payload(self.root, tool, str(path)))
        if pre.code != 0 or pre.stdout:
            return pre, None
        post = self.hook("PostToolUse", tool_payload(self.root, tool, str(path), "PostToolUse"))
        return pre, post

    # confirmation records -------------------------------------------------

    def write_records(self, feature: str, content: dict) -> list:
        """After `done`: append a `plan` record twice, read both back, validate them."""
        from jsonschema import Draft202012Validator

        from archshell.adapters.records import append_record, read_records

        def append():
            return append_record(
                self.root, feature=feature, flow="plan", confirmed_by="developer",
                content=content, confirmed_at=RECORD_CONFIRMED_AT,
            )

        first = append()
        first_file = self.root.joinpath(*first.path.split("/"))
        first_bytes = first_file.read_bytes()
        written = [first, append()]
        assert first_file.read_bytes() == first_bytes, "the second append changed the first record"
        self._collect_written()
        read = read_records(self.root, feature)
        assert [r.path for r in read] == [r.path for r in written]
        schema = json.loads(RECORD_SCHEMA_FILE.read_bytes().decode("utf-8"))
        validator = Draft202012Validator(schema)
        for record in read:
            validator.validate(record.document)
        return read

    # written files --------------------------------------------------------

    def _collect_written(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_file():
                self.written[path.relative_to(self.root).as_posix()] = path.read_bytes()


# --- Windows delete-pending simulation ----------------------------------------


@pytest.fixture
def delete_pending(monkeypatch):
    """Simulate Windows "delete pending" for exclusive creates.

    Returns `pend(name, times)`: the next `times` `O_CREAT | O_EXCL` opens of a
    file named `name` raise `PermissionError`, as Windows does while an unlinked
    file still has an open handle. The adapters run their Windows path, without
    real sleeps. `pend` returns the list of attempts made on `name`.
    """
    from archshell.adapters import files

    monkeypatch.setattr(files, "_WINDOWS", True, raising=False)
    delays = getattr(files, "_DELETE_PENDING_DELAYS", ())
    monkeypatch.setattr(files, "_DELETE_PENDING_DELAYS", (0.0,) * len(delays), raising=False)
    real_open = os.open
    pending: dict[str, int] = {}
    attempts: dict[str, list[int]] = {}

    def fake_open(path, flags, *args, **kwargs):
        name = os.path.basename(os.fspath(path))
        if flags & os.O_EXCL and name in pending:
            attempts[name].append(flags)
            if pending[name] > 0:
                pending[name] -= 1
                raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)

    def pend(name: str, times: int) -> list[int]:
        pending[name] = times
        attempts[name] = []
        return attempts[name]

    return pend
