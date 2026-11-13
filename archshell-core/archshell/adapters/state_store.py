"""Cursor state on disk: per-feature lock file and revision compare-and-swap.

State lives at `<root>/.archshell/state/<slug>.yaml`, serialized as sorted-key
JSON (a YAML subset) until the canonical YAML emitter exists. The lock is
`<root>/.archshell/locks/<slug>.lock`, taken by exclusive create. The prompt
of a pending `llm_task` lives beside the state, at the project-relative path
the action names (`.archshell/state/prompts/<action_id>.md`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from archshell.domain.workflow import CursorState, PromptFile, StateError


class LockHeld(Exception):
    """Another invocation holds the feature's lock file."""


class RevisionConflict(Exception):
    """The state on disk is not at the revision the write expected."""


class CorruptState(Exception):
    """The state file exists but cannot be read as `archshell/state/v1`."""


def serialize(state: CursorState) -> bytes:
    text = json.dumps(state.to_dict(), sort_keys=True, ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


class StateStore:
    def __init__(self, root: Path):
        self.root = root

    def state_path(self, feature: str) -> Path:
        return self.root / ".archshell" / "state" / f"{feature}.yaml"

    def prompt_path(self, prompt: PromptFile) -> Path:
        return self.root.joinpath(*prompt.path.split("/"))

    def ensure_prompt(self, state: CursorState) -> None:
        """Hold the pending action's prompt file on disk (LF-normalized, UTF-8, no BOM).

        Writes only when the file is missing or differs. Call only while holding
        `locked(state.feature)`.
        """
        prompt = state.pending_prompt()
        if prompt is None:
            return
        path = self.prompt_path(prompt)
        data = prompt.text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        try:
            if path.read_bytes() == data:
                return
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def lock_path(self, feature: str) -> Path:
        return self.root / ".archshell" / "locks" / f"{feature}.lock"

    @contextmanager
    def locked(self, feature: str) -> Iterator[None]:
        path = self.lock_path(feature)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockHeld(str(path)) from None
        try:
            try:
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            finally:
                os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)

    def read(self, feature: str) -> CursorState | None:
        path = self.state_path(feature)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            state = CursorState.from_dict(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, StateError) as exc:
            raise CorruptState(f"{path}: {exc}") from None
        if state.feature != feature:
            raise CorruptState(f"{path}: state belongs to feature {state.feature!r}")
        return state

    def write(self, state: CursorState, expected_revision: int) -> None:
        """Replace the state if the disk is at `expected_revision` (0 = absent).

        Call only while holding `locked(state.feature)`.
        """
        current = self.read(state.feature)
        current_revision = 0 if current is None else current.revision
        if current_revision != expected_revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {current_revision}"
            )
        if state.revision <= current_revision:
            raise RevisionConflict("revision must increase")
        path = self.state_path(state.feature)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(serialize(state))
        os.replace(tmp, path)
