"""Core-owned JSON Schemas for model outputs, validated with draft 2020-12.

Schemas live in `archshell-core/schemas/` (shipped beside the `archshell`
package), one file per schema id: `archshell/<kind>/v<n>` is `<kind>-v<n>.json`
and its `$id` must equal the id. The domain receives `validate` as a plain
function and never touches files or the schema library.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

import archshell

SCHEMAS_DIR = Path(archshell.__file__).resolve().parent.parent / "schemas"
SCHEMA_ID_PATTERN = re.compile(r"archshell/([a-z0-9]+(?:-[a-z0-9]+)*)/v([1-9][0-9]*)")


class UnknownSchema(LookupError):
    """No usable core schema exists for the id (an engine defect, not a host error)."""


def schema_path(schema_id: str) -> Path:
    match = SCHEMA_ID_PATTERN.fullmatch(schema_id)
    if match is None:
        raise UnknownSchema(f"not a core schema id: {schema_id!r}")
    return SCHEMAS_DIR / f"{match.group(1)}-v{match.group(2)}.json"


@cache
def validator_for(schema_id: str) -> Draft202012Validator:
    path = schema_path(schema_id)
    try:
        schema = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise UnknownSchema(f"no schema file for {schema_id}: {path}") from None
    if not isinstance(schema, dict) or schema.get("$id") != schema_id:
        raise UnknownSchema(f"{path} does not declare $id {schema_id}")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(schema_id: str, instance: Any) -> list[tuple[str, str]]:
    """The instance's schema errors as (JSON path, message), sorted; empty when valid."""
    errors = validator_for(schema_id).iter_errors(instance)
    return sorted((error.json_path, error.message) for error in errors)
