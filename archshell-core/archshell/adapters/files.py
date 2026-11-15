"""File IO of the generation path (AD-5): read template text, write normalized bytes.

Every file ArchShell generates is written through `write_text`, so it is LF,
UTF-8 without BOM, NFC, with exactly one final newline.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from archshell.render.normalize import to_bytes


def path_of(root: Path, relative: str) -> Path:
    """The path of a project-relative POSIX path under `root`."""
    return root.joinpath(*relative.split("/"))


def format_errors(errors: Sequence[tuple[str, str]]) -> str:
    """(path, message) errors as one `path: message; …` line, for exception text."""
    return "; ".join(f"{path}: {message}" for path, message in errors)


def _write_synced(fd: int, data: bytes) -> None:
    """Write `data` to the open file descriptor `fd`, fsync it, and close it."""
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


# Windows keeps an unlinked file "delete pending" until its last open handle
# closes; until then an exclusive create of that name raises PermissionError,
# not FileExistsError. The window is brief, so the create is retried with a
# capped backoff (about one second in total) before the error is believed.
_WINDOWS = os.name == "nt"
_DELETE_PENDING_DELAYS = tuple(min(0.001 * 2**attempt, 0.05) for attempt in range(25))


def open_exclusive(path: Path, flags: int = os.O_WRONLY, mode: int = 0o644) -> int:
    """`os.open(path, O_CREAT | O_EXCL | flags, mode)`; return the descriptor.

    Raises FileExistsError when `path` exists. On Windows a PermissionError
    (the name is still "delete pending") is retried briefly, then re-raised.
    """
    for delay in (*_DELETE_PENDING_DELAYS, None):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | flags, mode)
        except PermissionError:
            if not _WINDOWS or delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def read_text(path: Path) -> str:
    """Read `path` as UTF-8 text (a template, for instance). Bytes are not altered."""
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> bytes:
    """Normalize `text` and write it to `path`; return the bytes on disk.

    Creates parent directories. Writes a uniquely named temporary file in the
    same directory, fsyncs it, and moves it into place with `os.replace`; on any
    failure the temporary file is removed. Skips the write when the file already
    holds the bytes.
    """
    data = to_bytes(text)
    try:
        if path.read_bytes() == data:
            return data
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        _write_synced(fd, data)
        # mkstemp creates 0600; give the file the target's mode, or 0644 when new.
        os.chmod(tmp, _mode_of(path))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return data


def _mode_of(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return 0o644
