"""Subprocess tests for the hook sub-protocol I/O matrix (story 1.4, AD-17).

Each test runs the engine with a temporary project root as its working
directory and asserts stdout, the exit code, and that the hook wrote nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from archshell.domain import hooks
from archshell.render.hosts import claude_code

ENGINE = [sys.executable, "-m", "archshell.engine"]
HOOK = "archshell/hook/v1"
VERDICT = "archshell/verdict/v1"


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


class Project:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *argv: str, stdin: bytes) -> tuple[int, str, bytes]:
        before = tree(self.root)
        proc = subprocess.run(
            ENGINE + list(argv), input=stdin, capture_output=True, cwd=self.root, timeout=60
        )
        assert tree(self.root) == before, "a hook command wrote a file"
        assert b"Traceback" not in proc.stdout
        return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr

    def hook(self, command: str, message: dict | bytes, *argv: str) -> tuple[int, dict]:
        stdin = message if isinstance(message, bytes) else json.dumps(message).encode("utf-8")
        code, out, err = self.run(command, *argv, stdin=stdin)
        assert out.endswith("\n") and len(out.splitlines()) == 1, (out, err)
        return code, json.loads(out)

    def host(self, command: str, payload: dict) -> tuple[int, str]:
        code, out, _ = self.run(
            command, "--host", "claude-code", stdin=json.dumps(payload).encode("utf-8")
        )
        return code, out


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    root.mkdir()
    return Project(root)


def guard(path: str, **extra) -> dict:
    return {"schema": HOOK, "path": path, **extra}


def assert_verdict(code: int, msg: dict, verdict: str, verdict_code: str | None = None) -> dict:
    assert code == 0, msg
    assert msg["schema"] == VERDICT
    assert set(msg) == {"schema", "verdict", "code", "reason"}
    assert msg["verdict"] == verdict
    if verdict_code is not None:
        assert msg["code"] == verdict_code
    return msg


# --- host-neutral: archshell/hook/v1 in, archshell/verdict/v1 out -----------


def test_protected_write_is_blocked(project):
    msg = assert_verdict(
        *project.hook("guard-write", guard(".archshell/state/probe.yaml")),
        "block",
        "protected_path",
    )
    assert ".archshell/state/probe.yaml" in msg["reason"]


def test_ordinary_write_is_allowed(project):
    assert_verdict(*project.hook("guard-write", guard("src/app.py")), "allow")


@pytest.mark.parametrize(
    "path",
    ["src/../.archshell/x", ".ARCHSHELL/x", "./.archshell", "src\\..\\.archshell\\x", "a/b/../../.ArchShell/y"],
)
def test_escape_attempts_are_blocked(project, path):
    assert_verdict(*project.hook("guard-write", guard(path)), "block", "protected_path")


def test_absolute_path_inside_the_root_is_blocked(project):
    path = str(project.root / ".archshell" / "config.yaml")
    assert_verdict(*project.hook("guard-write", guard(path)), "block", "protected_path")


def test_root_is_compared_case_insensitively(project):
    path = str(project.root / ".archshell" / "x").swapcase()
    assert_verdict(*project.hook("guard-write", guard(path)), "block", "protected_path")


def test_relative_path_resolves_against_cwd(project):
    msg = guard("../.archshell/x", cwd=str(project.root / "src"))
    assert_verdict(*project.hook("guard-write", msg), "block", "protected_path")


@pytest.mark.parametrize("name", ["other/.archshell/x", "other/file.txt"])
def test_absolute_path_outside_the_root_is_allowed(project, name):
    outside = project.root.parent / name
    assert_verdict(*project.hook("guard-write", guard(str(outside))), "allow")


def test_dotdot_out_of_the_root_is_allowed(project):
    path = f"../{project.root.name}-sibling/.archshell/x"
    assert_verdict(*project.hook("guard-write", guard(path)), "allow")


@pytest.mark.parametrize(
    "stdin",
    [
        b"not json",
        b"[]",
        json.dumps({"schema": HOOK}).encode(),
        json.dumps({"schema": HOOK, "path": ""}).encode(),
        json.dumps({"schema": HOOK, "path": 3}).encode(),
        json.dumps({"path": ".archshell/x"}).encode(),
    ],
    ids=["non-json", "array", "missing-path", "empty-path", "non-string-path", "no-schema"],
)
def test_bad_input_fails_closed(project, stdin):
    code, msg = project.hook("guard-write", stdin)
    assert code == 2
    assert msg["schema"] == VERDICT
    assert msg["verdict"] == "block"
    assert msg["code"] == "invalid_message"
    assert msg["error"]["schema"] == "archshell/error/v1"
    assert msg["error"]["code"] == "invalid_message"


@pytest.mark.parametrize(
    "argv,code",
    [(["--feature", "x"], "invalid_arguments"), (["--host"], "invalid_arguments"), (["--host", "nope"], "unknown_host")],
)
def test_bad_arguments_fail_closed(project, argv, code):
    exit_code, msg = project.hook("guard-write", guard("src/app.py"), *argv)
    assert exit_code == 2
    assert (msg["verdict"], msg["code"], msg["error"]["code"]) == ("block", code, code)


def test_engine_failure_fails_closed_without_traceback(project):
    script = (
        "import sys\n"
        "from archshell.domain import hooks\n"
        "def boom(*a, **k):\n"
        "    raise RuntimeError('boom')\n"
        "hooks.guard_write = boom\n"
        "from archshell.engine.cli import main\n"
        "raise SystemExit(main(['guard-write']))\n"
    )
    before = tree(project.root)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(guard("src/app.py")).encode(),
        capture_output=True,
        cwd=project.root,
        timeout=60,
    )
    assert tree(project.root) == before
    assert proc.returncode == 2
    msg = json.loads(proc.stdout.decode("utf-8"))
    assert (msg["verdict"], msg["code"]) == ("block", "internal_error")
    assert "boom" not in msg["reason"] and b"Traceback" not in proc.stdout


@pytest.mark.parametrize("path", ["src/app.py", ".archshell/x"])
def test_check_edit_allows(project, path):
    assert_verdict(*project.hook("check-edit", guard(path)), "allow")


def test_check_edit_bad_input_is_an_error_not_a_block(project):
    code, msg = project.hook("check-edit", b"not json")
    assert code == 2
    assert msg["schema"] == "archshell/error/v1" and msg["code"] == "invalid_message"


@pytest.mark.parametrize("argv", [[], ["--feature", "probe"], ["--feature", "anything-else"]])
def test_on_stop_allows(project, argv):
    assert_verdict(*project.hook("on-stop", {"schema": HOOK}, *argv), "allow")


def test_on_stop_rejects_an_invalid_feature(project):
    code, msg = project.hook("on-stop", {"schema": HOOK}, "--feature", "Not A Slug")
    assert code == 2 and msg["code"] == "invalid_feature"


def test_guard_write_is_within_nfr1(project):
    start = time.monotonic()
    assert_verdict(*project.hook("guard-write", guard(".archshell/x")), "block")
    assert time.monotonic() - start < 3.0


# --- --host claude-code ------------------------------------------------------


def pre_tool_use(project: Project, tool: str, path: str) -> dict:
    field = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {
        "session_id": "s",
        "transcript_path": "t.jsonl",
        "cwd": str(project.root),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {field: path},
    }


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_claude_code_pre_tool_use_denies_protected_writes(project, tool):
    path = str(project.root / ".archshell" / "test.txt")
    code, out = project.host("guard-write", pre_tool_use(project, tool, path))
    assert code == 0
    output = json.loads(out)
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": output["hookSpecificOutput"]["permissionDecisionReason"],
        }
    }
    assert ".archshell/test.txt is engine-owned" in output["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_claude_code_pre_tool_use_is_silent_for_ordinary_writes(project, tool):
    path = str(project.root / "src" / "app.py")
    assert project.host("guard-write", pre_tool_use(project, tool, path)) == (0, "")


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {}},
        {"hook_event_name": "PreToolUse", "tool_name": "Write"},
        {"hook_event_name": "PostToolUse", "tool_input": {"file_path": "src/a.py"}},
    ],
    ids=["no-path", "no-tool-input", "wrong-event"],
)
def test_claude_code_pre_tool_use_bad_payload_denies(project, payload):
    code, out = project.host("guard-write", payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "failed closed" in decision["permissionDecisionReason"]


def test_claude_code_pre_tool_use_non_json_denies(project):
    code, out, _ = project.run("guard-write", "--host", "claude-code", stdin=b"nope")
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_code_post_tool_use_allow_is_silent(project):
    payload = pre_tool_use(project, "Edit", str(project.root / "src" / "app.py"))
    payload["hook_event_name"] = "PostToolUse"
    assert project.host("check-edit", payload) == (0, "")


def test_claude_code_post_tool_use_bad_payload_is_silent(project):
    assert project.host("check-edit", {"hook_event_name": "PostToolUse"}) == (0, "")


@pytest.mark.parametrize("active", [False, True])
def test_claude_code_stop_allow_is_silent(project, active):
    payload = {"cwd": str(project.root), "hook_event_name": "Stop", "stop_hook_active": active}
    assert project.host("on-stop", payload) == (0, "")


# --- pure renderer: block verdicts for PostToolUse and Stop -------------------


@pytest.mark.parametrize("command", ["check-edit", "on-stop"])
def test_claude_code_renders_block_reason_for_the_agent(command):
    verdict = {"schema": VERDICT, "verdict": "block", "code": "c", "reason": "fix it"}
    assert json.loads(claude_code.render_verdict(command, verdict)) == {
        "decision": "block",
        "reason": "fix it",
    }


@pytest.mark.parametrize("command", ["guard-write", "check-edit", "on-stop"])
def test_claude_code_renders_nothing_for_allow(command):
    verdict = {"schema": VERDICT, "verdict": "allow", "code": "allowed", "reason": ""}
    assert claude_code.render_verdict(command, verdict) == ""


# --- review fixes: hardening and host-mode matrix rows ------------------------


def run_patched(project: Project, patch: str, argv: list[str], payload: dict) -> tuple[int, str]:
    """Run `main(argv)` in a subprocess after executing `patch` (monkeypatching)."""
    script = (
        "from archshell.domain import hooks\n"
        "from archshell.render.hosts import claude_code\n"
        f"{patch}\n"
        "from archshell.engine.cli import main\n"
        f"raise SystemExit(main({argv!r}))\n"
    )
    before = tree(project.root)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=project.root,
        timeout=60,
    )
    assert tree(project.root) == before
    assert b"Traceback" not in proc.stdout
    return proc.returncode, proc.stdout.decode("utf-8")


BOOM = "def boom(*a, **k):\n    raise RuntimeError('boom')\n"
BLOCKS = "def blocks(*a, **k):\n    return hooks.Verdict('block', 'c', 'fix it first')\n"


@pytest.mark.parametrize(
    "path",
    [".archshell./x", ".archshell /x", ".ArchShell. ./x", r"C:.archshell\x", "c:.archshell/x"],
)
def test_trailing_dot_space_and_drive_relative_forms_are_blocked(project, path):
    code, msg = project.hook("guard-write", guard(path))
    assert code == 0 and msg["verdict"] == "block", msg
    expected = "unresolvable_path" if path[1] == ":" else "protected_path"
    assert msg["code"] == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 device paths")
@pytest.mark.parametrize("prefix", ["\\\\?\\", "\\\\.\\"])
def test_win32_device_prefixed_paths_are_blocked(project, prefix):
    path = prefix + str(project.root / ".archshell" / "x")
    assert_verdict(*project.hook("guard-write", guard(path)), "block", "protected_path")


@pytest.mark.parametrize(
    "path,root",
    [
        (r"\\?\C:\proj\.archshell\x", r"C:\proj"),
        (r"\\.\C:\proj\.archshell\x", r"C:\proj"),
        (r"\\?\UNC\srv\share\proj\.archshell\x", r"\\srv\share\proj"),
        (r"C:\PROJ\.archshell.\x", r"C:\proj"),
        (r"C:\proj\.archshell \x", r"c:\proj"),
        (r"C:\proj.\.archshell\x", r"C:\proj"),
    ],
)
def test_domain_blocks_windows_forms(path, root):
    verdict = hooks.guard_write(hooks.HookInput(path=path, cwd=None), root)
    assert (verdict.verdict, verdict.code) == ("block", "protected_path")


def test_domain_blocks_drive_relative_paths_as_unresolvable():
    verdict = hooks.guard_write(hooks.HookInput(path=r"C:.archshell\x", cwd=None), r"C:\proj")
    assert (verdict.verdict, verdict.code) == ("block", "unresolvable_path")


def test_domain_missing_path_is_invalid_message():
    verdict = hooks.guard_write(hooks.HookInput(path=None, cwd=None), "/proj")
    assert (verdict.verdict, verdict.code) == ("block", "invalid_message")


@pytest.mark.parametrize("via", ["link-target", "link-cwd"])
def test_symlinked_root_is_resolved(tmp_path, via):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a directory symlink here")
    cwd, path_root = (real, link) if via == "link-target" else (link, real)
    project = Project(cwd)
    path = str(path_root / ".archshell" / "x")
    assert_verdict(*project.hook("guard-write", guard(path)), "block", "protected_path")


def test_render_failure_blocks_with_the_error_field(project):
    patch = BOOM + "claude_code.render_verdict = boom"
    payload = pre_tool_use(project, "Write", str(project.root / "src" / "app.py"))
    code, out = run_patched(project, patch, ["guard-write", "--host", "claude-code"], payload)
    assert code == 2
    msg = json.loads(out)
    assert (msg["schema"], msg["verdict"], msg["code"]) == (VERDICT, "block", "internal_error")
    assert msg["error"]["schema"] == "archshell/error/v1"
    assert msg["error"]["code"] == "internal_error"


def test_claude_code_engine_failure_denies(project):
    patch = BOOM + "hooks.guard_write = boom"
    payload = pre_tool_use(project, "Write", str(project.root / "src" / "app.py"))
    code, out = run_patched(project, patch, ["guard-write", "--host", "claude-code"], payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "failed closed" in decision["permissionDecisionReason"]
    assert "boom" not in decision["permissionDecisionReason"]


@pytest.mark.parametrize("command,event", [("check-edit", "PostToolUse"), ("on-stop", "Stop")])
def test_claude_code_block_feeds_the_reason_to_the_agent(project, command, event):
    target = "check_edit" if command == "check-edit" else "on_stop"
    payload = pre_tool_use(project, "Edit", str(project.root / "src" / "app.py"))
    payload["hook_event_name"] = event
    code, out = run_patched(
        project, BLOCKS + f"hooks.{target} = blocks", [command, "--host", "claude-code"], payload
    )
    assert code == 0
    assert json.loads(out) == {"decision": "block", "reason": "fix it first"}


@pytest.mark.parametrize("path", ["src/../.archshell/x", ".ARCHSHELL/x", r"a\..\.ArchShell\y"])
def test_claude_code_escape_attempts_deny(project, path):
    code, out = project.host("guard-write", pre_tool_use(project, "Write", path))
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_code_mixed_case_absolute_path_denies(project):
    path = str(project.root / ".archshell" / "x").swapcase()
    code, out = project.host("guard-write", pre_tool_use(project, "Write", path))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("sub", ["src", "src/deep"])
def test_claude_code_payload_cwd_in_a_subdirectory_still_protects_the_root(project, sub):
    payload = pre_tool_use(project, "Write", str(project.root / ".archshell" / "x"))
    payload["cwd"] = str(project.root / sub)
    code, out = project.host("guard-write", payload)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    depth = sub.count("/") + 1
    payload["tool_input"]["file_path"] = "../" * depth + ".archshell/x"
    code, out = project.host("guard-write", payload)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("kind", ["absolute", "relative"])
def test_claude_code_outside_project_is_silent(project, kind):
    if kind == "absolute":
        path = str(project.root.parent / "other" / ".ARCHSHELL" / "x")
    else:
        path = f"../{project.root.name}-sibling/.archshell/x"
    assert project.host("guard-write", pre_tool_use(project, "Write", path)) == (0, "")
