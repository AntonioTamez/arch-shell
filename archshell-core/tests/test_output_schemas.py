"""Tests of the core output-schema adapter (draft 2020-12, jsonschema)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from archshell.adapters import output_schemas
from archshell.adapters.output_schemas import UnknownSchema, validate

TEXT = "archshell/llm-text/v1"


def test_schema_file_declares_draft_2020_12_and_its_id():
    path = output_schemas.schema_path(TEXT)
    assert path.name == "llm-text-v1.json" and path.parent.name == "schemas"
    schema = json.loads(path.read_bytes())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == TEXT


@pytest.mark.parametrize("result", [{"text": "ok"}, {"text": " "}])
def test_text_schema_accepts_non_empty_text(result):
    assert validate(TEXT, result) == []


@pytest.mark.parametrize(
    "result, expected",
    [
        ({}, [("$", "'text' is a required property")]),
        ({"text": ""}, [("$.text", "'' should be non-empty")]),
        ({"text": 3}, [("$.text", "3 is not of type 'string'")]),
        (
            {"text": "ok", "extra": 1},
            [("$", "Additional properties are not allowed ('extra' was unexpected)")],
        ),
        ([], [("$", "[] is not of type 'object'")]),
    ],
)
def test_text_schema_reports_path_and_message(result, expected):
    assert validate(TEXT, result) == expected


def test_errors_are_sorted():
    errors = validate(TEXT, {"text": "", "extra": 1})
    assert errors == sorted(errors) and len(errors) == 2


@pytest.mark.parametrize("schema_id", ["archshell/nope/v1", "nope", "archshell/../x/v1", ""])
def test_unknown_schema_id_raises(schema_id):
    with pytest.raises(UnknownSchema):
        validate(schema_id, {})


def test_engine_module_does_not_import_jsonschema_until_submit():
    code = "import sys, archshell.engine.cli; sys.exit('jsonschema' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 0
