"""Load extensions from their entry points (AD-14, FR-36).

An extension is an object a distribution registers under one of the
entry-point groups `archshell.language_packs`, `archshell.providers`, or
`archshell.ci`. There is no `archshell.hosts` group: host adapters are files
(the Claude Code plugin), not Python. Each loaded object must expose
`extension_contract` equal to the contract version the core supports for its
kind; a language pack must also expose `language`, and no two packs may claim
the same one. Refusals are typed errors that name the entry point and its
distribution. No engine or CLI command calls the loader yet.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Protocol

GROUP_PREFIX = "archshell."
SUPPORTED_CONTRACTS = {
    "language_packs": "archshell/language-pack-ext/v1",
    "providers": "archshell/provider-ext/v1",
    "ci": "archshell/ci-ext/v1",
}
UNKNOWN_DISTRIBUTION = "<unknown distribution>"


class EntryPointLike(Protocol):
    """What the loader reads from an `importlib.metadata.EntryPoint`."""

    name: str

    @property
    def dist(self) -> Any: ...

    def load(self) -> Any: ...


class ExtensionError(Exception):
    """An extension the core refuses to load. `code` is stable; the message is not."""

    code = "extension_error"

    def __init__(self, message: str, *, entry_point: str, distribution: str):
        super().__init__(message)
        self.entry_point = entry_point
        self.distribution = distribution


class IncompatibleExtension(ExtensionError):
    """`extension_contract` is missing or not the version the core supports."""

    code = "incompatible_extension"

    def __init__(self, *, group: str, entry_point: str, distribution: str, found: Any, expected: str):
        super().__init__(
            f"{group} entry point {entry_point!r} of {distribution} implements {found!r}; "
            f"this core supports {expected!r}",
            entry_point=entry_point,
            distribution=distribution,
        )
        self.group = group
        self.found = found
        self.expected = expected


class DuplicateLanguage(ExtensionError):
    """Two language packs claim the same `language`."""

    code = "duplicate_language"

    def __init__(
        self,
        *,
        language: str,
        entry_point: str,
        distribution: str,
        other_entry_point: str,
        other_distribution: str,
    ):
        super().__init__(
            f"language {language!r} is claimed by entry point {entry_point!r} of {distribution} "
            f"and by entry point {other_entry_point!r} of {other_distribution}",
            entry_point=entry_point,
            distribution=distribution,
        )
        self.language = language
        self.other_entry_point = other_entry_point
        self.other_distribution = other_distribution


@dataclass(frozen=True)
class Extension:
    kind: str
    entry_point: str
    distribution: str
    obj: Any
    language: str | None = None  # language packs only


def group_of(kind: str) -> str:
    if kind not in SUPPORTED_CONTRACTS:
        raise ValueError(f"unknown extension kind {kind!r}; expected one of {sorted(SUPPORTED_CONTRACTS)}")
    return GROUP_PREFIX + kind


def load(kind: str, *, found: Iterable[EntryPointLike] | None = None) -> list[Extension]:
    """Load every extension of `kind`, sorted by (entry point, distribution).

    `found` replaces the installed entry points of the group (tests).
    Raises `IncompatibleExtension` or `DuplicateLanguage`; nothing is returned
    when any extension is refused.
    """
    group = group_of(kind)
    expected = SUPPORTED_CONTRACTS[kind]
    candidates = list(entry_points(group=group)) if found is None else list(found)
    candidates.sort(key=lambda ep: (ep.name, _distribution(ep)))
    loaded: list[Extension] = []
    claimed: dict[str, Extension] = {}
    for ep in candidates:
        distribution = _distribution(ep)
        try:
            obj = ep.load()
        except Exception as exc:
            raise IncompatibleExtension(
                group=group,
                entry_point=ep.name,
                distribution=distribution,
                found=f"<load failed: {type(exc).__name__}: {exc}>",
                expected=expected,
            ) from exc
        contract = getattr(obj, "extension_contract", None)
        if contract != expected:
            raise IncompatibleExtension(
                group=group, entry_point=ep.name, distribution=distribution, found=contract, expected=expected
            )
        language = None
        if kind == "language_packs":
            language = getattr(obj, "language", None)
            if not isinstance(language, str) or not language.strip():
                raise IncompatibleExtension(
                    group=group,
                    entry_point=ep.name,
                    distribution=distribution,
                    found=f"{contract} without a language",
                    expected=expected,
                )
            if language in claimed:
                first = claimed[language]
                raise DuplicateLanguage(
                    language=language,
                    entry_point=ep.name,
                    distribution=distribution,
                    other_entry_point=first.entry_point,
                    other_distribution=first.distribution,
                )
        extension = Extension(kind, ep.name, distribution, obj, language)
        if language is not None:
            claimed[language] = extension
        loaded.append(extension)
    return loaded


def language_packs(*, found: Iterable[EntryPointLike] | None = None) -> dict[str, Extension]:
    """The loaded language packs keyed by their `language`."""
    return {ext.language: ext for ext in load("language_packs", found=found) if ext.language is not None}


def _distribution(ep: EntryPointLike) -> str:
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None) if dist is not None else None
    return name if isinstance(name, str) and name else UNKNOWN_DISTRIBUTION
