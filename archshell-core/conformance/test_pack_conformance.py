"""Language-pack conformance (FR-37): every installed pack loads through the core.

A pack conforms when its `archshell.language_packs` entry point loads, its
object declares the contract version this core supports, and it declares a
language no other pack claims. Golden and seeded-violation checks come later.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from archshell.adapters import extensions

GROUP = extensions.group_of("language_packs")
CONTRACT = extensions.SUPPORTED_CONTRACTS["language_packs"]
INSTALLED = sorted(entry_points(group=GROUP), key=lambda ep: ep.name)
# Packs this repository ships, with the language each must declare.
SHIPPED = {"archshell-dotnet": "csharp"}


def test_shipped_packs_are_registered():
    registered = {ep.dist.name: ep for ep in INSTALLED if ep.dist is not None}
    assert set(SHIPPED) <= set(registered), sorted(registered)


@pytest.mark.parametrize("ep", INSTALLED, ids=lambda ep: ep.name)
def test_pack_loads_through_its_entry_point(ep):
    obj = ep.load()
    assert obj is not None


@pytest.mark.parametrize("ep", INSTALLED, ids=lambda ep: ep.name)
def test_pack_declares_the_supported_contract(ep):
    assert getattr(ep.load(), "extension_contract", None) == CONTRACT


@pytest.mark.parametrize("ep", INSTALLED, ids=lambda ep: ep.name)
def test_pack_declares_its_language(ep):
    language = getattr(ep.load(), "language", None)
    assert isinstance(language, str) and language.strip(), language
    if ep.dist is not None and ep.dist.name in SHIPPED:
        assert language == SHIPPED[ep.dist.name]


def test_core_loader_accepts_every_installed_pack():
    packs = extensions.language_packs()
    assert sorted(ext.entry_point for ext in packs.values()) == [ep.name for ep in INSTALLED]
    for distribution, language in SHIPPED.items():
        assert packs[language].distribution == distribution
