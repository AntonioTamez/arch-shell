"""The write journal `.archshell/txn.json` (AD-10): plan, back up, write, delete.

A multi-file write first records its plan in the journal: each target's
project-relative path, the sha256 of the bytes it will hold, and a base64
backup of the bytes it held before (`null` when it did not exist). The
journal is created exclusively and fsynced, then every file is written
through `files.write_text`, then the journal is deleted. A journal still
present means an earlier write stopped part-way; nothing new is written until
it is resolved (rollback from the backups arrives in story 2.9).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from archshell.adapters.files import open_exclusive, path_of, write_text
from archshell.domain import layout
from archshell.render.normalize import to_bytes


class JournalPresent(Exception):
    """`.archshell/txn.json` exists: an earlier journaled write was interrupted."""


@dataclass(frozen=True)
class PlannedWrite:
    """A file to write: its project-relative POSIX path and its text."""

    path: str
    text: str


def journal_path(root: Path) -> Path:
    return path_of(root, layout.TXN)


def present(root: Path) -> bool:
    return os.path.lexists(journal_path(root))


def plan(root: Path, writes: Sequence[PlannedWrite]) -> dict[str, object]:
    """The journal document for `writes`, with each target's current bytes as backup."""
    entries = []
    for write in writes:
        target = path_of(root, write.path)
        try:
            backup: str | None = base64.b64encode(target.read_bytes()).decode("ascii")
        except FileNotFoundError:
            backup = None
        entries.append(
            {
                "path": write.path,
                "sha256": hashlib.sha256(to_bytes(write.text)).hexdigest(),
                "backup": backup,
            }
        )
    return {"schema": layout.TXN_SCHEMA, "writes": entries}


def _create(path: Path, document: dict[str, object]) -> None:
    """Create the journal exclusively, fsynced; `JournalPresent` if it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        fd = open_exclusive(path)
    except FileExistsError:
        raise JournalPresent(str(path)) from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def apply(root: Path, writes: Sequence[PlannedWrite]) -> None:
    """Journal `writes`, write every file, then delete the journal.

    Raises `JournalPresent`, writing nothing, when a journal already exists.
    On a failure after the journal is created, the journal stays behind.
    """
    path = journal_path(root)
    _create(path, plan(root, writes))
    for write in writes:
        write_text(path_of(root, write.path), write.text)
    path.unlink()
