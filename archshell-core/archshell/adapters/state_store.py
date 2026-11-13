"""Cursor state on disk: per-feature lock file and revision compare-and-swap.

State lives at `<root>/.archshell/state/<slug>.yaml`, serialized as sorted-key
JSON (a YAML subset) until the canonical YAML emitter exists. The lock is
`<root>/.archshell/locks/<slug>.lock`, taken by exclusive create.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from archshell.domain.workflow import CursorState, StateError


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
