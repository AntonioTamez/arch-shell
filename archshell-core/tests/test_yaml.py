"""Tests of the canonical YAML emitter and the safe reader (AD-11)."""

from __future__ import annotations

import decimal

import pytest
import yaml

from archshell.adapters.yaml_read import YamlReadError, load, read
from archshell.render.yaml_emit import EmitError, Ordered, emit

TRICKY = ["yes", "No", "on", "OFF", "y", "true", "null", "~", "1", "-1", "0x1f", "1.5", "1e3",
          ".inf", ": x", "a: b", "", " leading", "trailing ", "#comment", "- dash", "[x]",
          "{x}", "*alias", "&anchor", "!tag", "'q'", '"q"', "2026-11-13", "1:30", "<<",
          "a\nb", "tab\there", "\x7f ﻿", "café", "@at", "%pct", "`tick"]


def test_mapping_keys_are_sorted_by_codepoint():
    assert emit({"b": 1, "a": {"z": 2, "B": 3, "é": 4}, "_": 0}) == (
        '_: 0\na:\n  B: 3\n  z: 2\n  "é": 4\nb: 1\n'
    )


def test_ordered_mapping_keeps_caller_order():
    assert emit(Ordered([("schema", "x"), ("b", 1), ("a", 2)])) == "schema: x\nb: 1\na: 2\n"


@pytest.mark.parametrize("text", TRICKY)
def test_strings_needing_quotes_read_back_as_the_same_string(text):
    out = emit({"k": text, text: "v"})
    assert load(out) == {"k": text, text: "v"}
    # PyYAML's plain safe_load (YAML 1.1 resolver) agrees.
    assert yaml.safe_load(out) == {"k": text, text: "v"}


@pytest.mark.parametrize("text", ["yes", "1", ": x", "", " leading"])
def test_matrix_strings_are_double_quoted(text):
    assert emit({"k": text}).startswith('k: "')


def test_plain_strings_stay_plain():
    assert emit({"k": "archshell/charter/v1", "name": "plain words"}) == (
        "k: archshell/charter/v1\nname: plain words\n"
    )


def test_block_style_layout():
    data = {"list": [1, {"b": True, "a": None}, [2, 3], [], {}], "empty": {}}
    assert emit(data) == (
        "empty: {}\n"
        "list:\n"
        "  - 1\n"
        "  - a: null\n"
        "    b: true\n"
        "  - - 2\n"
        "    - 3\n"
        "  - []\n"
        "  - {}\n"
    )


def test_scalar_documents():
    assert emit(None) == "null\n"
    assert emit("x") == "x\n"
    assert emit([]) == "[]\n"


@pytest.mark.parametrize("value", [1.5, float("nan"), decimal.Decimal("1"), object(), (1, 2), {1, 2}, b"x"])
def test_unsupported_values_are_refused(value):
    with pytest.raises(EmitError):
        emit({"k": value})


def test_non_string_keys_are_refused():
    with pytest.raises(EmitError):
        emit({1: "x"})


def test_keys_equal_after_nfc_are_refused():
    with pytest.raises(EmitError, match="duplicate"):
        emit({"é": 1, "é": 2})


def test_cycles_are_refused():
    data: dict = {}
    data["self"] = data
    with pytest.raises(EmitError, match="cyclic"):
        emit(data)


def test_strings_are_composed_to_nfc():
    assert load(emit({"k": "é"})) == {"k": "é"}


@pytest.mark.parametrize(
    "text",
    [
        "a: &x 1\nb: 2\n",
        "a: &x 1\nb: *x\n",
        "a: !!str 1\n",
        "a: !custom x\n",
        "a: ! x\n",
        "--- !!map\na: 1\n",
        "a: !!python/object:os.system x\n",
    ],
)
def test_reader_refuses_anchors_aliases_and_tags(text):
    with pytest.raises(YamlReadError):
        load(text)


@pytest.mark.parametrize(
    "text",
    [
        "a: 1.5\n",
        "a: 2026-11-13\n",
        "a: 1\na: 2\n",
        "<<: {a: 1}\n",
        "? [a]\n: 1\n",
        "a: 1\n---\nb: 2\n",
        "a: [\n",
    ],
)
def test_reader_refuses_values_outside_the_subset(text):
    with pytest.raises(YamlReadError):
        load(text)


def test_round_trip_emit_then_read():
    data = {
        "schema": "archshell/charter/v1",
        "version": 3,
        "flags": [True, False, None],
        "modules": [{"name": "Billing", "deps": []}, {"name": "Appointments", "deps": ["Billing"]}],
        "strings": TRICKY,
        "nested": {"empty": {}, "deep": {"deeper": [[-1, 0, 10**30]]}},
    }
    assert load(emit(data)) == data


def test_read_from_file(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_bytes(emit({"a": 1}).encode("utf-8"))
    assert read(path) == {"a": 1}
    path.write_bytes(b"\xff")
    with pytest.raises(YamlReadError, match="UTF-8"):
        read(path)
