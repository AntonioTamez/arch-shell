"""File IO of the generation path (AD-5): read template text, write normalized bytes.

Every file ArchShell generates is written through `write_text`, so it is LF,
UTF-8 without BOM, NFC, with exactly one final newline.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from archshell.render.normalize import to_bytes


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
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
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
