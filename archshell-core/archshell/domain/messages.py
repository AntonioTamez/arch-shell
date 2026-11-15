"""User-facing text as message keys plus arguments, rendered from a catalog.

Pure: no IO. The domain names every text the developer sees by a `Message`
(key + args); an adapter loads the per-language catalogs (`key -> template`)
and supplies the renderer. Templates use `{name}` placeholders only; literal
braces are not allowed. Files the engine writes are never localized.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

LANGUAGES = ("en", "es")
DEFAULT_LANG = "en"

KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")

Catalog = Mapping[str, str]
# (message, language) -> rendered text. Supplied by an adapter.
Renderer = Callable[["Message", str], str]


class MessageError(ValueError):
    """A key is missing from a catalog, or a template and its args disagree."""


@dataclass(frozen=True)
class Message:
    key: str
    args: Mapping[str, Any] = field(default_factory=dict)


def msg(key: str, **args: Any) -> Message:
    return Message(key, args)


def placeholders(template: str) -> frozenset[str]:
    """The placeholder names of a template; refuses a brace outside a placeholder."""
    literal = PLACEHOLDER.sub("", template)
    if "{" in literal or "}" in literal:
        raise MessageError(f"template has a brace outside a {{name}} placeholder: {template!r}")
    return frozenset(PLACEHOLDER.findall(template))


def render(catalog: Catalog, message: Message) -> str:
    try:
        template = catalog[message.key]
    except KeyError:
        raise MessageError(f"no catalog entry for {message.key!r}") from None
    expected = placeholders(template)
    if expected != set(message.args):
        raise MessageError(
            f"{message.key}: template placeholders {sorted(expected)} "
            f"do not match args {sorted(message.args)}"
        )
    return PLACEHOLDER.sub(lambda m: str(message.args[m.group(1)]), template)
