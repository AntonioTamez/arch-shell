"""Cursor state on disk: per-feature lock file and revision compare-and-swap.

State lives at `<root>/.archshell/state/<slug>.yaml`, serialized as sorted-key
JSON (a YAML subset); moving it to canonical YAML is deferred. The lock is an
OS advisory lock on `<root>/.archshell/locks/<slug>.lock` (`flock` on POSIX,
`msvcrt.locking` on Windows), so the OS releases it when the holder exits or
is killed; the file itself stays and holds the last holder's pid. The prompt
of a pending `llm_task` lives beside the state, at the project-relative path
the action names (`.archshell/state/prompts/<action_id>.md`).
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from archshell.adapters.files import path_of
from archshell.domain import layout
from archshell.domain.workflow import CursorState, PromptFile, StateError


if os.name == "nt":
    import msvcrt

    _HELD_ERRNOS = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
    # Windows byte-range locks are mandatory: lock one byte far past the pid
    # (locking beyond end of file is allowed) so the pid stays readable.
    _LOCK_OFFSET = 1 << 30

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _HELD_ERRNOS:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class LockHeld(Exception):
    """Another invocation holds the feature's lock file."""


class RevisionConflict(Exception):
    """The state on disk is not at the revision the write expected."""


class CorruptState(Exception):
    """The state file exists but cannot be read as `archshell/state/v1`."""


def serialize(state: CursorState) -> bytes:
    text = json.dumps(state.to_dict(), sort_keys=True, ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


def _replace(path: Path, data: bytes) -> None:
    """Write `data` to `<path>.tmp`, then move it over `path` with `os.replace`.

    Bytes are written as given: no normalization, unlike `files.write_text`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class StateStore:
    def __init__(self, root: Path):
        self.root = root

    def state_path(self, feature: str) -> Path:
        return path_of(self.root, layout.state_file(feature))

    def prompt_path(self, prompt: PromptFile) -> Path:
        return path_of(self.root, prompt.path)

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
        _replace(path, data)

    def discard_prompt(self, previous: CursorState, current: CursorState) -> None:
        """Delete `previous`'s prompt file when `current` no longer points at it.

        Call after `current` is written, while holding the lock. A failed delete
        is ignored: the state is already committed, and the file is only left
        behind.
        """
        old = previous.pending_prompt()
        if old is None:
            return
        new = current.pending_prompt()
        if new is not None and new.path == old.path:
            return
        try:
            self.prompt_path(old).unlink(missing_ok=True)
        except OSError:
            pass

    def lock_path(self, feature: str) -> Path:
        return path_of(self.root, layout.lock_file(feature))

    @contextmanager
    def locked(self, feature: str) -> Iterator[None]:
        """Hold the feature's lock, or raise `LockHeld` when another holder has it.

        The file is never unlinked: unlinking a locked file would let a second
        process lock a new file at the same path while the first still holds the
        old one. A file left with no OS lock on it is free.
        """
        path = self.lock_path(feature)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o777)
        try:
            if not _try_lock(fd):
                raise LockHeld(str(path))
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

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
        _replace(self.state_path(state.feature), serialize(state))
