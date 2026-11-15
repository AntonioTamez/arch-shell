"""Write and read confirmation records (FR-53, AD-10). Append-only.

`append_record` never touches an existing file. It picks the feature's next
`seq`, claims it by exclusive create of `.archshell/locks/records/<feature>/<seq>.claim`
(gitignored), checks that no record already uses that `seq`, then writes the
record to a temporary file in the claims directory, fsyncs it, and hard-links
it to its final name, which fails rather than replace an existing path. A
record is therefore either fully present or absent. Writers racing for one
`seq` each end up with a different one. Without hard-link support the record is
created in place with `O_CREAT | O_EXCL`.

Documents are validated against `archshell/record/v1` before writing and when
read; the schema library is imported on call.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from archshell.adapters import yaml_read
from archshell.adapters.files import path_of
from archshell.domain import layout, records
from archshell.domain.workflow import is_valid_slug
from archshell.render.normalize import to_bytes
from archshell.render.yaml_emit import EmitError, Ordered, emit


class InvalidRecord(Exception):
    """A record that is not written, or a record file that does not validate.

    `errors` are (JSON path, message); `file` is the project-relative path of
    the offending record file, or None when the input was refused before any
    file was involved.
    """

    def __init__(self, errors: list[tuple[str, str]], file: str | None = None):
        detail = "; ".join(f"{path}: {message}" for path, message in errors)
        super().__init__(f"{file}: {detail}" if file else detail)
        self.errors = errors
        self.file = file


def fingerprint(content: Any) -> str:
    """`sha256:<hex>` of the canonical YAML bytes of `content`, equal on every OS.

    Raises `archshell.render.yaml_emit.EmitError` for content outside the
    restricted YAML subset.
    """
    return "sha256:" + hashlib.sha256(to_bytes(emit(content))).hexdigest()


_content_fingerprint = fingerprint  # `append_record`'s `fingerprint` argument shadows it


@dataclass(frozen=True)
class Record:
    path: str  # project-relative POSIX path
    document: dict[str, Any]


def append_record(
    root: Path,
    *,
    feature: str,
    flow: str,
    confirmed_by: str,
    derived: bool = False,
    content: Any = None,
    fingerprint: str | None = None,
    confirmed_at: str | None = None,
) -> Record:
    """Write the feature's next record and return it.

    `fingerprint` defaults to the fingerprint of `content`; when both are
    given they must agree. `confirmed_at` defaults to the current UTC time.
    Raises `InvalidRecord` (nothing written) for an invalid record.
    """
    if not is_valid_slug(feature):
        raise InvalidRecord([("$.feature", f"{feature!r} is not a valid feature slug")])
    if fingerprint is None and content is not None:
        try:
            fingerprint = _content_fingerprint(content)
        except EmitError as exc:
            raise InvalidRecord([("$.content", str(exc))]) from None
    elif fingerprint is not None and content is not None:
        try:
            expected = _content_fingerprint(content)
        except EmitError as exc:
            raise InvalidRecord([("$.content", str(exc))]) from None
        if fingerprint != expected:
            raise InvalidRecord([("$.fingerprint", f"does not match content ({expected})")])
    if confirmed_at is None:
        confirmed_at = records.format_timestamp(datetime.now(UTC))
    fields = dict(
        feature=feature,
        flow=flow,
        fingerprint=fingerprint,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        derived=derived,
        content=content,
    )
    # Validate with a placeholder seq before the filesystem is touched.
    _check(records.build_record(seq=1, **fields))

    directory = path_of(root, layout.records_dir(feature))
    claims = path_of(root, layout.record_claims_dir(feature))
    seq = max(_used_seqs(directory), default=0) + 1
    while True:
        if seq > records.MAX_SEQ:
            raise InvalidRecord([("$.seq", f"{feature} has no record seq left (max {records.MAX_SEQ})")])
        claim = path_of(root, records.claim_path(feature, seq))
        if not _claim(claims, claim):
            seq += 1
            continue
        try:
            if seq in _used_seqs(directory):
                seq += 1
                continue
            document = records.build_record(seq=seq, **fields)
            _check(document)
            relative = records.record_path(feature, seq, flow)
            try:
                _create_exclusive(path_of(root, relative), to_bytes(emit(Ordered(document))), claims)
            except FileExistsError:
                seq += 1
                continue
            return Record(relative, dict(document))
        finally:
            claim.unlink(missing_ok=True)


def read_records(root: Path, feature: str) -> list[Record]:
    """The feature's records in `seq` order, each validated.

    Raises `InvalidRecord` naming the file when a file is not a record
    filename, not restricted-subset YAML, fails the schema, names a different
    feature, seq, or flow than its path, or repeats another file's seq.
    Temporary files of an interrupted writer (names starting with `.`) are skipped.
    """
    if not is_valid_slug(feature):
        raise InvalidRecord([("$.feature", f"{feature!r} is not a valid feature slug")])
    directory = path_of(root, layout.records_dir(feature))
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []
    found: dict[int, Record] = {}
    for entry in entries:
        if entry.name.startswith("."):
            continue
        relative = f"{layout.records_dir(feature)}/{entry.name}"
        parsed = records.parse_filename(entry.name)
        if parsed is None or not entry.is_file():
            raise InvalidRecord([("$", "not a record file (<seq>-<flow>.yaml)")], relative)
        seq, flow = parsed
        document = _load(entry, relative)
        mismatches = [
            (f"$.{key}", f"{document[key]!r} does not match the filename ({expected!r})")
            for key, expected in (("feature", feature), ("seq", seq), ("flow", flow))
            if document[key] != expected
        ]
        if mismatches:
            raise InvalidRecord(mismatches, relative)
        if seq in found:
            raise InvalidRecord([("$.seq", f"seq {seq} is also used by {found[seq].path}")], relative)
        found[seq] = Record(relative, document)
    return [found[seq] for seq in sorted(found)]


def _check(document: Any, file: str | None = None) -> None:
    from archshell.adapters import output_schemas

    errors = output_schemas.validate(layout.RECORD_SCHEMA, document)
    if not errors:
        errors = records.semantic_errors(document)
    if errors:
        raise InvalidRecord(errors, file)


def _load(path: Path, relative: str) -> dict[str, Any]:
    try:
        data = yaml_read.load(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError:
        raise InvalidRecord([("$", "not UTF-8")], relative) from None
    except yaml_read.YamlReadError as exc:
        raise InvalidRecord([("$", str(exc))], relative) from None
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise InvalidRecord([("$", f"cannot read: {reason}")], relative) from None
    _check(data, relative)
    assert isinstance(data, dict)
    return data


def _used_seqs(directory: Path) -> set[int]:
    """Seqs of every record-named file in `directory`, whatever its flow."""
    try:
        names = [entry.name for entry in directory.iterdir()]
    except FileNotFoundError:
        return set()
    return {parsed[0] for name in names if (parsed := records.parse_filename(name))}


def _claim(claims: Path, claim: Path) -> bool:
    """Exclusively create `claim`; False when another writer holds it."""
    claims.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _create_exclusive(path: Path, data: bytes, tmp_dir: Path) -> None:
    """Create `path` holding `data`; FileExistsError when it exists. Never replaces.

    The temporary file lives in `tmp_dir` (the gitignored claims directory,
    on the same filesystem), so a killed writer leaves nothing under `records/`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise
        except (OSError, NotImplementedError):
            _create_in_place(path, data)
    finally:
        Path(tmp).unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _create_in_place(path: Path, data: bytes) -> None:
    """Fallback without hard links: `O_CREAT | O_EXCL`, then write and fsync."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)  # our own partial file, never an existing record
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":  # directories cannot be opened for fsync on Windows
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
