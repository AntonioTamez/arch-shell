"""Confirmation records (FR-53, AD-10): names, fingerprints, and documents.

Pure: no IO, clock, or environment (AD-4). A record lives at
`.archshell/records/<feature>/<seq>-<flow>.yaml`, `seq` zero-padded to four
digits and counted per feature from `0001`. The adapter
`archshell.adapters.records` writes and reads them, computes content
fingerprints, and validates documents against `archshell/record/v1`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from archshell.domain import layout
from archshell.domain.workflow import is_valid_slug

SEQ_WIDTH = 4
MAX_SEQ = 10**SEQ_WIDTH - 1
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_FILENAME = re.compile(r"([0-9]{4})-([a-z]+(?:-[a-z]+)*)\.yaml")


def format_seq(seq: int) -> str:
    if not 1 <= seq <= MAX_SEQ:
        raise ValueError(f"record seq out of range 1..{MAX_SEQ}: {seq}")
    return f"{seq:0{SEQ_WIDTH}d}"


def record_filename(seq: int, flow: str) -> str:
    return f"{format_seq(seq)}-{flow}.yaml"


def record_path(feature: str, seq: int, flow: str) -> str:
    """The project-relative POSIX path of a record."""
    return f"{layout.records_dir(feature)}/{record_filename(seq, flow)}"


def claim_path(feature: str, seq: int) -> str:
    """The project-relative path of a writer's claim on `seq` (gitignored)."""
    return f"{layout.record_claims_dir(feature)}/{format_seq(seq)}.claim"


def parse_filename(name: str) -> tuple[int, str] | None:
    """`(seq, flow)` of a record filename; None when `name` is not one."""
    match = _FILENAME.fullmatch(name)
    if match is None or match.group(2) not in layout.RECORD_FLOWS:
        return None
    seq = int(match.group(1))
    return (seq, match.group(2)) if seq >= 1 else None


def format_timestamp(moment: datetime) -> str:
    """`moment` (timezone-aware) as UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def build_record(
    *,
    feature: Any,
    seq: int,
    flow: Any,
    fingerprint: Any,
    confirmed_by: Any,
    confirmed_at: Any,
    derived: Any,
    content: Any = None,
) -> dict[str, Any]:
    """The record document in its canonical key order; `content` None is omitted.

    The adapter emits it with that key order kept.
    """
    document = dict(
        [
            ("schema", layout.RECORD_SCHEMA),
            ("feature", feature),
            ("seq", seq),
            ("flow", flow),
            ("fingerprint", fingerprint),
            ("confirmed_by", confirmed_by),
            ("confirmed_at", confirmed_at),
            ("derived", derived),
        ]
    )
    if content is not None:
        document["content"] = content
    return document


def semantic_errors(document: Any) -> list[tuple[str, str]]:
    """Errors the schema cannot express, as (JSON path, message).

    Run after schema validation. JSON Schema `pattern` is a search in which
    `$` also matches before a final newline, so the whole values are checked
    here: `feature` is a slug, `flow` is in the vocabulary, `fingerprint` is
    exactly `sha256:<64 hex>`, `confirmed_by` is not blank, and
    `confirmed_at` is a real UTC time.
    """
    errors: list[tuple[str, str]] = []
    if not isinstance(document, dict):
        return errors
    if "feature" in document and not is_valid_slug(document["feature"]):
        errors.append(("$.feature", f"{document['feature']!r} is not a valid feature slug"))
    if "flow" in document and document["flow"] not in layout.RECORD_FLOWS:
        errors.append(("$.flow", f"{document['flow']!r} is not a record flow"))
    fingerprint = document.get("fingerprint")
    if isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint) is None:
        errors.append(("$.fingerprint", f"{fingerprint!r} is not sha256:<64 hex>"))
    confirmed_by = document.get("confirmed_by")
    if isinstance(confirmed_by, str) and not confirmed_by.strip():
        errors.append(("$.confirmed_by", "must not be blank"))
    if isinstance(document.get("confirmed_at"), str):
        try:
            datetime.strptime(document["confirmed_at"], TIMESTAMP_FORMAT)
        except ValueError:
            errors.append(("$.confirmed_at", f"{document['confirmed_at']!r} is not a valid UTC time"))
    return errors
