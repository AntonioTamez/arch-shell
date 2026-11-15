"""Tests of the extension loader (story 1.10, AD-14, FR-36)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from archshell.adapters import extensions
from archshell.adapters.extensions import DuplicateLanguage, IncompatibleExtension


@dataclass
class FakeEntryPoint:
    name: str
    obj: Any
    dist_name: str | None = "fake-pack"

    @property
    def dist(self):
        return None if self.dist_name is None else SimpleNamespace(name=self.dist_name)

    def load(self):
        return self.obj


def pack(contract: Any = "archshell/language-pack-ext/v1", language: Any = "csharp") -> SimpleNamespace:
    fields = {}
    if contract is not None:
        fields["extension_contract"] = contract
    if language is not None:
        fields["language"] = language
    return SimpleNamespace(**fields)


def test_installed_dotnet_pack_loads_for_csharp():
    import archshell_dotnet

    packs = extensions.language_packs()
    assert set(packs) == {"csharp"}
    csharp = packs["csharp"]
    assert csharp.obj is archshell_dotnet.PACK
    assert (csharp.entry_point, csharp.distribution) == ("dotnet", "archshell-dotnet")
    assert csharp.obj.extension_contract == "archshell/language-pack-ext/v1"


def test_supported_groups_and_contracts():
    assert {kind: extensions.group_of(kind) for kind in extensions.SUPPORTED_CONTRACTS} == {
        "language_packs": "archshell.language_packs",
        "providers": "archshell.providers",
        "ci": "archshell.ci",
    }
    assert extensions.SUPPORTED_CONTRACTS == {
        "language_packs": "archshell/language-pack-ext/v1",
        "providers": "archshell/provider-ext/v1",
        "ci": "archshell/ci-ext/v1",
    }
    with pytest.raises(ValueError):
        extensions.load("hosts")


@pytest.mark.parametrize("kind", ["providers", "ci"])
def test_other_kinds_load_with_their_contract(kind):
    obj = SimpleNamespace(extension_contract=extensions.SUPPORTED_CONTRACTS[kind])
    [loaded] = extensions.load(kind, found=[FakeEntryPoint("one", obj, "fake-ext")])
    assert (loaded.obj, loaded.distribution, loaded.language) == (obj, "fake-ext", None)
    assert extensions.load(kind) == []  # nothing installed in these groups


@pytest.mark.parametrize(
    "obj, found",
    [
        (pack(contract="archshell/language-pack-ext/v2"), "archshell/language-pack-ext/v2"),
        (pack(contract=None), None),
        (pack(contract="archshell/provider-ext/v1"), "archshell/provider-ext/v1"),
    ],
    ids=["v2", "missing", "other-kind"],
)
def test_wrong_or_missing_contract_is_incompatible_extension(obj, found):
    with pytest.raises(IncompatibleExtension) as caught:
        extensions.load("language_packs", found=[FakeEntryPoint("bad", obj, "bad-pack")])
    error = caught.value
    assert error.code == "incompatible_extension"
    assert (error.entry_point, error.distribution) == ("bad", "bad-pack")
    assert (error.found, error.expected) == (found, "archshell/language-pack-ext/v1")
    assert "'bad'" in str(error) and "bad-pack" in str(error)


def test_entry_point_that_fails_to_load_is_incompatible_extension():
    class Broken(FakeEntryPoint):
        def load(self):
            raise ModuleNotFoundError("No module named 'gone'")

    with pytest.raises(IncompatibleExtension) as caught:
        extensions.load("language_packs", found=[Broken("broken", None, "broken-pack")])
    error = caught.value
    assert error.code == "incompatible_extension"
    assert (error.entry_point, error.distribution) == ("broken", "broken-pack")
    assert "ModuleNotFoundError" in str(error) and "gone" in str(error)
    assert isinstance(error.__cause__, ModuleNotFoundError)


@pytest.mark.parametrize("language", [None, "", 3])
def test_pack_without_a_language_is_incompatible_extension(language):
    with pytest.raises(IncompatibleExtension) as caught:
        extensions.load("language_packs", found=[FakeEntryPoint("nolang", pack(language=language))])
    assert caught.value.code == "incompatible_extension"
    assert caught.value.entry_point == "nolang"


def test_two_packs_claiming_one_language_is_duplicate_language():
    found = [FakeEntryPoint("second", pack(), "pack-b"), FakeEntryPoint("first", pack(), "pack-a")]
    with pytest.raises(DuplicateLanguage) as caught:
        extensions.load("language_packs", found=found)
    error = caught.value
    assert error.code == "duplicate_language"
    assert error.language == "csharp"
    assert (error.entry_point, error.distribution) == ("second", "pack-b")
    assert (error.other_entry_point, error.other_distribution) == ("first", "pack-a")
    for name in ("first", "second", "pack-a", "pack-b"):
        assert name in str(error)


def test_distinct_languages_load_sorted():
    found = [FakeEntryPoint("java", pack(language="java"), "pack-j"), FakeEntryPoint("dotnet", pack(), None)]
    loaded = extensions.load("language_packs", found=found)
    assert [(e.entry_point, e.distribution, e.language) for e in loaded] == [
        ("dotnet", extensions.UNKNOWN_DISTRIBUTION, "csharp"),
        ("java", "pack-j", "java"),
    ]
