"""Tests of the normalizer, template rendering, and the write helper (AD-5)."""

from __future__ import annotations

import os

import pytest

from archshell.adapters import files
from archshell.adapters.files import read_text, write_text
from archshell.render.normalize import normalize, to_bytes
from archshell.render.templates import RenderError, render


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\r\nb\r\n", "a\nb\n"),
        ("a\rb\r", "a\nb\n"),
        ("a\r\n\rb", "a\n\nb\n"),
    ],
)
def test_line_endings_become_lf(text, expected):
    assert normalize(text) == expected


def test_leading_bom_is_removed():
    assert normalize("﻿hello\n") == "hello\n"
    assert not to_bytes("﻿hello").startswith(b"\xef\xbb\xbf")
    assert to_bytes("﻿hello") == b"hello\n"


def test_unicode_is_composed_to_nfc():
    assert normalize("café\n") == "café\n"
    assert to_bytes("é") == "é\n".encode("utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no newline", "no newline\n"),
        ("several\n\n\n", "several\n"),
        ("several crlf\r\n\r\n", "several crlf\n"),
        ("", "\n"),
        ("keeps inner\n\nblank lines\n", "keeps inner\n\nblank lines\n"),
    ],
)
def test_exactly_one_final_newline(text, expected):
    assert normalize(text) == expected


def test_normalize_is_idempotent():
    text = "﻿a\r\nb\ré\n\n"
    assert normalize(normalize(text)) == normalize(text)


def test_render_output_is_normalized():
    template = "{{ greeting }}, café\r\n{% for n in names %}- {{ n }}\r\n{% endfor %}\n\n"
    assert render(template, {"greeting": "Hi", "names": ["a", "b"]}) == "Hi, café\n- a\n- b\n"


def test_render_does_not_autoescape():
    assert render("{{ x }}", {"x": "<a & 'b'>"}) == "<a & 'b'>\n"


def test_undefined_variable_is_refused_with_its_name():
    with pytest.raises(RenderError) as info:
        render("Hello {{ name }} and {{ other }}", {"other": 1})
    assert info.value.names == ("name",)
    assert "name" in str(info.value)


def test_undefined_variable_in_untaken_branch_is_refused():
    with pytest.raises(RenderError) as info:
        render("{% if false %}{{ missing }}{% endif %}", {})
    assert info.value.names == ("missing",)


def test_undefined_attribute_is_refused():
    with pytest.raises(RenderError) as info:
        render("{{ module.name }}", {"module": {}})
    assert "name" in str(info.value)


def test_loop_and_set_variables_are_not_undefined():
    template = "{% set sep = ', ' %}{% for x in xs %}{{ x }}{{ sep if not loop.last }}{% endfor %}"
    assert render(template, {"xs": [1, 2]}) == "1, 2\n"


def test_syntax_error_is_a_render_error():
    with pytest.raises(RenderError, match="syntax"):
        render("{{ unclosed", {})


@pytest.mark.parametrize("template", ["{{ lipsum(1) }}", "{{ [1, 2, 3] | random }}"])
def test_nondeterministic_global_and_filter_are_refused(template):
    with pytest.raises(RenderError):
        render(template, {})


def test_deterministic_helpers_stay_available():
    template = (
        "{% set c = cycler('a', 'b') %}{% set j = joiner('|') %}{% set ns = namespace(n=0) %}"
        "{% for _ in range(3) %}{{ j() }}{{ c.next() }}{% set ns.n = ns.n + 1 %}{% endfor %}"
        " {{ ns.n }} {{ dict(k=1)['k'] }}"
    )
    assert render(template, {}) == "a|b|a 3 1\n"


@pytest.mark.parametrize(
    ("template", "context"),
    [("{{ 1 / 0 }}", {}), ("{{ x + 1 }}", {"x": "s"}), ("{{ x.boom() }}", {"x": object()})],
)
def test_any_exception_while_rendering_is_a_render_error(template, context):
    with pytest.raises(RenderError):
        render(template, context)


def test_include_is_refused():
    with pytest.raises(RenderError, match="include"):
        render('{% include "other.j2" %}', {})


def test_write_text_writes_normalized_bytes(tmp_path):
    path = tmp_path / "out" / "file.md"
    data = write_text(path, "﻿a\r\nb\ré")
    assert data == "a\nb\né\n".encode("utf-8")
    assert path.read_bytes() == data
    assert os.listdir(tmp_path / "out") == ["file.md"]


def test_write_text_failed_replace_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "file.md"
    path.write_bytes(b"old\n")

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(files.os, "replace", failing_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_text(path, "new")
    assert os.listdir(tmp_path) == ["file.md"]
    assert path.read_bytes() == b"old\n"


def test_write_text_skips_identical_content(tmp_path):
    path = tmp_path / "file.txt"
    write_text(path, "same")
    before = path.stat().st_mtime_ns
    write_text(path, "same\r\n")
    assert path.stat().st_mtime_ns == before
    assert path.read_bytes() == b"same\n"


def test_read_text_keeps_bytes(tmp_path):
    path = tmp_path / "t.j2"
    path.write_bytes(b"\xef\xbb\xbfa\r\n")
    assert read_text(path) == "﻿a\r\n"
