"""File IO of the generation path (AD-5): read template text, write normalized bytes.

Every file ArchShell generates is written through `write_text`, so it is LF,
UTF-8 without BOM, NFC, with exactly one final newline.
"""

from __future__ import annotations

import os
from pathlib import Path

from archshell.render.normalize import to_bytes


def read_text(path: Path) -> str:
    """Read `path` as UTF-8 text (a template, for instance). Bytes are not altered."""
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> bytes:
    """Normalize `text` and write it to `path`; return the bytes on disk.

    Creates parent directories. Writes through a sibling temporary file and an
    atomic replace, and skips the write when the file already holds the bytes.
    """
    data = to_bytes(text)
    try:
        if path.read_bytes() == data:
            return data
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data
