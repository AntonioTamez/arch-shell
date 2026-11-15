"""Shared harness for the subprocess tests: the engine command and its runners.

Test modules import these names directly (`from conftest import ...`); pytest
puts this directory on `sys.path` before it collects them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
