"""Golden byte check (NFR-4, AD-4): the generation path yields the same bytes on every OS.

Each `golden/templates/<name>.j2` is rendered with `golden/contexts/<name>.yaml`,
and each `golden/data/<name>` is read and re-emitted as canonical YAML. Every
output is written through `adapters.files.write_text` and its sha256 compared
with the committed `golden/manifest.sha256` (`<sha256>  <name>` lines, sorted).
CI runs this on Windows, Linux, and macOS: all three matching one committed
manifest means they match each other.

The manifest also holds `no-op/...` entries, checked by `test_no_op_flow.py`;
this test ignores them. To update the manifest after an intended change,
replace its other lines with the "actual manifest" printed by the failing test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from archshell.adapters import yaml_read
from archshell.adapters.files import read_text, write_text
from archshell.render.templates import render
from archshell.render.yaml_emit import emit

GOLDEN = Path(__file__).parent / "golden"
NO_OP_PREFIX = "no-op/"


def _outputs() -> dict[str, str]:
    outputs: dict[str, str] = {}
    for template in sorted((GOLDEN / "templates").glob("*.j2")):
        name = template.name.removesuffix(".j2")
        context = yaml_read.read(GOLDEN / "contexts" / f"{name}.yaml")
        assert isinstance(context, dict), name
        outputs[name] = render(read_text(template), context)
    for data in sorted((GOLDEN / "data").glob("*.yaml")):
        outputs[data.name] = emit(yaml_read.read(data))
    return outputs


def _manifest(hashes: dict[str, str]) -> str:
    return "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))


def test_golden_outputs_match_committed_sha256(tmp_path):
    hashes = {}
    for name, text in _outputs().items():
        data = write_text(tmp_path / name, text)
        assert (tmp_path / name).read_bytes() == data
        hashes[name] = hashlib.sha256(data).hexdigest()
    lines = (GOLDEN / "manifest.sha256").read_bytes().decode("utf-8").splitlines(keepends=True)
    # `no-op/...` entries belong to test_no_op_flow.py (the end-to-end run).
    expected = "".join(line for line in lines if not line.split("  ", 1)[1].startswith(NO_OP_PREFIX))
    actual = _manifest(hashes)
    assert actual == expected, f"golden bytes differ; actual manifest:\n{actual}"


def test_golden_inputs_exercise_the_normalizer():
    # Guards against an editor silently "fixing" the fixtures: the template keeps a
    # decomposed e + U+0301; CR, CRLF, and the BOM come in through context escapes.
    assert "é" in read_text(GOLDEN / "templates" / "module.md.j2")
    context = yaml_read.read(GOLDEN / "contexts" / "line-endings.txt.yaml")
    assert context["bom"] == "﻿"
    assert "\r\n" in context["windows"] and "\r" in context["classic_mac"]


def test_golden_outputs_are_normalized():
    for name, text in _outputs().items():
        data = text.encode("utf-8")
        assert b"\r" not in data, name
        assert not data.startswith(b"\xef\xbb\xbf"), name
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), name


def test_manifest_is_sorted_by_name():
    lines = (GOLDEN / "manifest.sha256").read_bytes().decode("utf-8").splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert names == sorted(names) and len(set(names)) == len(names)
