"""Tests of the normalizer, template rendering, and the write helper (AD-5)."""

from __future__ import annotations

import pytest

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


def test_include_is_refused():
    with pytest.raises(RenderError, match="include"):
        render('{% include "other.j2" %}', {})


def test_write_text_writes_normalized_bytes(tmp_path):
    path = tmp_path / "out" / "file.md"
    data = write_text(path, "﻿a\r\nb\ré")
    assert data == "a\nb\né\n".encode("utf-8")
    assert path.read_bytes() == data
    assert not (tmp_path / "out" / "file.md.tmp").exists()


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
