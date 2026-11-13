"""Subprocess tests for the tracer I/O matrix.

The executables are driven as `python -m <module>`, which calls the same
`main()` the console scripts call. This keeps the suite runnable where OS
policy blocks unsigned launcher .exe files; CI also runs the real scripts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import entry_points

import pytest

from archshell import __version__

ENGINE = [sys.executable, "-m", "archshell.engine"]
CLI = [sys.executable, "-m", "archshell.cli"]


@pytest.fixture(autouse=True)
def _project_root(tmp_path, monkeypatch):
    # The engine writes state under its working directory; keep it out of the repo.
    monkeypatch.chdir(tmp_path)


def run(cmd: list[str], stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=60)


def single_json_line(stdout: bytes) -> dict:
    text = stdout.decode("utf-8")
    assert text.endswith("\n"), text
    lines = text.splitlines()
    assert len(lines) == 1, text
    return json.loads(lines[0])


def test_console_scripts_point_at_main():
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts["archshell-engine"] == "archshell.engine.cli:main"
    assert scripts["archshell"] == "archshell.cli:main"


def test_next_happy_path_returns_first_action():
    proc = run(ENGINE + ["next", "--feature", "probe"], b"{}")
    assert proc.returncode == 0, proc.stderr
    msg = single_json_line(proc.stdout)
    assert msg["schema"] == "archshell/action/v1"
    assert msg["type"] == "llm_task"
    assert msg["revision"] == 1
    assert "protocol" not in msg


@pytest.mark.parametrize(
    "payload", [b"not json", b"[]", b"", b"\xff\xfe", b"[" * 100000]
)
def test_next_bad_json_is_invalid_message(payload):
    proc = run(ENGINE + ["next", "--feature", "probe"], payload)
    assert proc.returncode == 2
    msg = single_json_line(proc.stdout)
    assert msg["schema"] == "archshell/error/v1"
    assert msg["code"] == "invalid_message"
    assert isinstance(msg["message"], str) and msg["message"]
    assert msg["details"] == {}
    assert b"Traceback" not in proc.stdout


@pytest.mark.parametrize("argv", [["frobnicate"], []])
def test_unknown_command(argv):
    proc = run(ENGINE + argv, b"{}")
    assert proc.returncode == 2
    msg = single_json_line(proc.stdout)
    assert msg["schema"] == "archshell/error/v1"
    assert msg["code"] == "unknown_command"
    assert set(msg) == {"schema", "code", "message", "details"}
    assert b"Traceback" not in proc.stdout


def test_errors_log_json_lines_to_stderr():
    proc = run(ENGINE + ["frobnicate"], b"{}")
    lines = proc.stderr.decode("utf-8").splitlines()
    assert lines
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_cli_version():
    proc = run(CLI + ["--version"])
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").strip() == f"archshell {__version__}"
    assert __version__ == "0.1.0"
