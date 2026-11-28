"""The core error-code registry (story 2.1): one list of codes, joined to the
code that raises them, to both catalogs, and to the README code table."""

from __future__ import annotations

import re
from pathlib import Path

import archshell
from archshell.adapters import catalogs
from archshell.domain.errors import ERROR_CODES, catalog_key

CODE_ROOT = Path(archshell.__file__).resolve().parent
README = CODE_ROOT.parents[1] / "README.md"
# Hook verdict codes (`protected_path`, `allowed`, …) are not error codes
# (R11 accepted deviation): `domain/hooks.py` stays outside the scan.
HOOK_VERDICTS = CODE_ROOT / "domain" / "hooks.py"
# A code as the first argument of a refusal, or a `code = "..."` class attribute.
CODE_LITERAL = re.compile(
    r"""(?:ProtocolError|Refusal)\(\s*["']([a-z_]+)["']|\bcode\s*=\s*["']([a-z_]+)["']"""
)
SNAKE_NOUN_PROBLEM = re.compile(r"[a-z]+(?:_[a-z]+)+|locked")


def codes_in_code() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(CODE_ROOT.rglob("*.py")):
        if path == HOOK_VERDICTS:
            continue
        for match in CODE_LITERAL.finditer(path.read_text("utf-8")):
            code = match.group(1) or match.group(2)
            found.setdefault(code, []).append(path.relative_to(CODE_ROOT).as_posix())
    return found


def test_every_code_literal_in_the_core_is_registered():
    found = codes_in_code()
    assert found, "the scan found no codes"
    unregistered = {code: paths for code, paths in found.items() if code not in ERROR_CODES}
    assert unregistered == {}


def test_every_registered_code_is_raised_somewhere():
    assert set(ERROR_CODES) - set(codes_in_code()) == set()


def test_every_registered_code_has_its_catalog_entry_in_both_languages():
    for code, key in ERROR_CODES.items():
        assert key == f"error.{code}" == catalog_key(code)
        for lang in ("en", "es"):
            assert key in catalogs.load(lang), (lang, key)


def test_codes_are_snake_case():
    for code in ERROR_CODES:
        assert SNAKE_NOUN_PROBLEM.fullmatch(code), code


def test_scan_finds_positional_and_attribute_codes():
    source = 'raise ProtocolError(\n    "some_code", msg("x"))\n    code = "other_code"\n'
    assert [m.group(1) or m.group(2) for m in CODE_LITERAL.finditer(source)] == [
        "some_code", "other_code"
    ]


def readme_codes() -> list[str]:
    """The codes of the README's `archshell/error/v1` table, in order."""
    text = README.read_bytes().decode("utf-8")
    start = text.index("Error codes (`archshell/error/v1`")
    table = text[start:].split("\n\n", 2)[1]
    return [
        line.split("|")[1].strip().strip("`")
        for line in table.splitlines()[2:]
        if line.startswith("| `")
    ]


def test_readme_code_table_lists_exactly_the_registry():
    codes = readme_codes()
    assert len(codes) == len(set(codes))
    assert set(codes) == set(ERROR_CODES)
