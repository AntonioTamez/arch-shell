"""Per-language message catalogs (`archshell/catalog/v1`), read from package data.

Catalogs live in `archshell/catalogs/<lang>.yaml`, restricted-subset YAML:
`schema`, `lang`, and `messages` (key -> template). Templates are normalized
to NFC on read. The domain receives `render` and never touches the files.
"""

from __future__ import annotations

import unicodedata
from functools import cache
from pathlib import Path
from types import MappingProxyType

import archshell
from archshell.adapters.yaml_read import YamlReadError, read
from archshell.domain import messages
from archshell.domain.messages import Catalog, Message

CATALOGS_DIR = Path(archshell.__file__).resolve().parent / "catalogs"
CATALOG_SCHEMA = "archshell/catalog/v1"
CATALOG_FIELDS = frozenset({"schema", "lang", "messages"})


class CatalogError(LookupError):
    """A catalog is missing or malformed (an engine defect, not a host error)."""


class UnknownLanguage(CatalogError):
    """No catalog exists for the language."""


def catalog_path(lang: str) -> Path:
    if lang not in messages.LANGUAGES:
        raise UnknownLanguage(f"no catalog for language: {lang!r}")
    return CATALOGS_DIR / f"{lang}.yaml"


def parse(data: object, lang: str, source: str) -> Catalog:
    """Validate a parsed catalog document for `lang`."""
    if not isinstance(data, dict) or set(data) != CATALOG_FIELDS:
        raise CatalogError(f"{source}: catalog must have exactly {sorted(CATALOG_FIELDS)}")
    if data["schema"] != CATALOG_SCHEMA:
        raise CatalogError(f"{source}: schema must be {CATALOG_SCHEMA}")
    if data["lang"] != lang:
        raise CatalogError(f"{source}: lang must be {lang}")
    entries = data["messages"]
    if not isinstance(entries, dict) or not entries:
        raise CatalogError(f"{source}: messages must be a non-empty mapping")
    catalog: dict[str, str] = {}
    for key, template in entries.items():
        if messages.KEY_PATTERN.fullmatch(key) is None:
            raise CatalogError(f"{source}: invalid message key {key!r}")
        if not isinstance(template, str) or not template.strip():
            raise CatalogError(f"{source}: {key} must be a non-blank string")
        try:
            messages.placeholders(template)
        except messages.MessageError as exc:
            raise CatalogError(f"{source}: {key}: {exc}") from None
        catalog[key] = unicodedata.normalize("NFC", template)
    return MappingProxyType(catalog)


@cache
def load(lang: str) -> Catalog:
    path = catalog_path(lang)
    try:
        data = read(path)
    except FileNotFoundError:
        raise CatalogError(f"no catalog file for {lang}: {path}") from None
    except (OSError, YamlReadError) as exc:
        raise CatalogError(f"{path}: {exc}") from None
    return parse(data, lang, str(path))


def render(message: Message, lang: str) -> str:
    """Render a message in `lang` (a `messages.Renderer`)."""
    return messages.render(load(lang), message)
