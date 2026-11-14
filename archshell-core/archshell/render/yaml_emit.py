"""Canonical YAML emitter for the restricted subset (AD-11).

Pure (AD-4). The only values it writes are `str`, `int`, `bool`, `None`,
`list`, and mappings with `str` keys. Floats and every other type are refused.

Output rules:

- block style, two-space indentation; empty collections as `{}` and `[]`;
- mapping keys sorted by codepoint, unless the mapping is an `Ordered`, whose
  insertion order the caller fixes;
- strings are composed to NFC; a string is written plain only when every YAML
  1.1 and 1.2 reader reads it back as the same string, otherwise it is
  double-quoted with JSON-style escapes;
- the document passes through the core normalizer (one final `\\n`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping

from archshell.render.normalize import normalize


class EmitError(ValueError):
    """A value outside the restricted YAML subset."""


class Ordered(dict):
    """A mapping whose key order is fixed by the caller (not sorted on emit)."""


# Plain only: starts with a letter or `_`, then word characters, `.`, `-`, `/`,
# with single inner spaces. No indicator, number, or timestamp can match.
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*(?: [A-Za-z0-9_./-]+)*")
# YAML 1.1 booleans and nulls (1.2 core is a subset); compared case-folded.
_RESERVED = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
# PyYAML (simple-key scanner) refuses implicit keys of 1024 characters or more.
_MAX_KEY = 1000


def emit(data: object) -> str:
    """Return `data` as a canonical YAML document."""
    if _is_block(data):
        return normalize("\n".join(_lines(data, 0, set())))
    return normalize(_scalar(data))


def _is_block(value: object) -> bool:
    return isinstance(value, (Mapping, list)) and len(value) > 0


def _lines(value: object, indent: int, active: set[int]) -> Iterator[str]:
    if id(value) in active:
        raise EmitError("cyclic structure")
    active.add(id(value))
    pad = " " * indent
    if isinstance(value, Mapping):
        for key, item in _items(value):
            if _is_block(item):
                yield f"{pad}{key}:"
                yield from _lines(item, indent + 2, active)
            else:
                yield f"{pad}{key}: {_scalar(item)}"
    else:
        for item in value:  # type: ignore[union-attr]
            if _is_block(item):
                nested = list(_lines(item, indent + 2, active))
                yield f"{pad}- {nested[0][indent + 2 :]}"
                yield from nested[1:]
            else:
                yield f"{pad}- {_scalar(item)}"
    active.discard(id(value))


def _items(mapping: Mapping) -> list[tuple[str, object]]:
    items: dict[str, object] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise EmitError(f"mapping key must be str, not {type(key).__name__}: {key!r}")
        key = _nfc(key)
        if key in items:
            raise EmitError(f"duplicate mapping key after NFC: {key!r}")
        items[key] = item
    pairs = list(items.items())
    if not isinstance(mapping, Ordered):
        pairs.sort(key=lambda pair: pair[0])
    out = []
    for key, item in pairs:
        text = _string(key)
        if len(text) > _MAX_KEY:
            raise EmitError(f"mapping key longer than {_MAX_KEY} characters: {key[:40]!r}…")
        out.append((text, item))
    return out


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, str):
        return _string(_nfc(value))
    if isinstance(value, Mapping):
        if value:
            raise EmitError("non-empty mapping in scalar position")
        return "{}"
    if isinstance(value, list):
        if value:
            raise EmitError("non-empty list in scalar position")
        return "[]"
    raise EmitError(f"unsupported value of type {type(value).__name__}: {value!r}")


def _nfc(text: str) -> str:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise EmitError(f"string contains a lone surrogate: {text!r}")
    return unicodedata.normalize("NFC", text)


def _string(text: str) -> str:
    if _PLAIN.fullmatch(text) and text.casefold() not in _RESERVED:
        return text
    return '"' + "".join(_escape(char) for char in text) + '"'


def _escape(char: str) -> str:
    if char in _ESCAPES:
        return _ESCAPES[char]
    code = ord(char)
    if (
        code < 0x20
        or 0x7F <= code <= 0x9F
        or code in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF)
    ):
        return f"\\u{code:04x}"
    return char
