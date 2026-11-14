"""The core normalizer (AD-5): every written file passes through `normalize`.

Pure: no IO, clock, randomness, or environment (AD-4).
"""

from __future__ import annotations

import unicodedata

BOM = "﻿"


def normalize(text: str) -> str:
    """Return `text` as ArchShell writes it.

    - a leading byte order mark (U+FEFF) is removed;
    - CRLF and lone CR become LF;
    - Unicode is composed to NFC;
    - the text ends with exactly one `\\n` (an empty text becomes `"\\n"`).
    """
    if text.startswith(BOM):
        text = text[len(BOM) :]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    return text.rstrip("\n") + "\n"


def to_bytes(text: str) -> bytes:
    """Normalize `text` and encode it as UTF-8 without BOM."""
    return normalize(text).encode("utf-8")
