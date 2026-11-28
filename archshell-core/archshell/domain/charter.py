"""The bootstrap charter: drivers, `charter/base.yaml`, and `architecture.yaml`.

Pure (AD-4): plain data in, plain data out. The engine shell loads the
language pack and version pins, emits the documents as canonical YAML,
validates them against `charter-v1.json` / `architecture-v1.json`, and writes
them through the `.archshell/txn.json` journal. Every value written is
English or an ASCII identifier except the developer's request, kept verbatim
(FR-11, FR-12).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from archshell.domain.scoring import Proposal

DRIVERS_SCHEMA = "archshell/drivers/v1"
CHARTER_SCHEMA = "archshell/charter/v1"
ARCHITECTURE_SCHEMA = "archshell/architecture/v1"

UNKNOWN = "unknown"

# The closed driver set and each driver's values; `archshell/drivers/v1` and
# `archshell/charter/v1` hold the same enums (a test checks it).
DRIVERS: dict[str, tuple[str, ...]] = {
    "audit_needs": ("none", "basic", "strict", UNKNOWN),
    "deployment_model": ("monolith", "services", "serverless", UNKNOWN),
    "domain_complexity": ("low", "medium", "high", UNKNOWN),
    "integrations": ("none", "few", "many", UNKNOWN),
    "read_write_asymmetry": ("balanced", "read_heavy", "write_heavy", UNKNOWN),
    "scale": ("small", "medium", "large", UNKNOWN),
    "team_size": ("solo", "small", "large", UNKNOWN),
}

# The only decision this tracer records: the proposal, accepted as scored
# (an override arrives in story 2.6).
AUTOMATIC = "automatic"


@dataclass(frozen=True)
class CharterWrite:
    """The bootstrap flow's terminal write effect, executed by the engine shell."""

    request: str
    drivers: Mapping[str, Any]
    proposal: Proposal


def drivers_prompt(request: str) -> str:
    """The `llm_task` prompt that extracts `archshell/drivers/v1` from the request."""
    drivers = "".join(
        f"- `{name}`: one of {', '.join(f'`{value}`' for value in values)}\n"
        for name, values in DRIVERS.items()
    )
    return (
        "Extract the architecture drivers of the software system described in the"
        " developer's request below.\n\n"
        "Reply with one JSON object and nothing else: no prose, no code fence. It"
        f" must match {DRIVERS_SCHEMA}:\n\n"
        f"{drivers}"
        "- `modules`: a non-empty list of the system's business modules, each"
        ' `{"name": "<Name>", "capabilities": ["<capability>", ...]}`. `name` is'
        " an English PascalCase ASCII identifier (letters and digits, starting with"
        " an uppercase letter), unique in the list; `capabilities` are short English"
        " phrases naming what the module does.\n\n"
        f"Use `{UNKNOWN}` for every driver the request does not establish; never guess"
        " one. No other keys are allowed. Write English even when the request is in"
        " another language.\n\n"
        "Request:\n\n"
        f"{request}\n"
    )


MODULE_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")


def drivers_problems(drivers: Any) -> list[tuple[str, str]]:
    """Checks JSON Schema cannot state exactly: module names are unique, and
    wholly ASCII PascalCase (a schema `pattern` lets a trailing newline through)."""
    modules = drivers.get("modules") if isinstance(drivers, dict) else None
    if not isinstance(modules, list):
        return []
    seen: set[str] = set()
    problems = []
    for index, module in enumerate(modules):
        name = module.get("name") if isinstance(module, dict) else None
        if not isinstance(name, str):
            continue
        if MODULE_NAME.fullmatch(name) is None:
            problems.append(
                (f"$.modules[{index}].name", f"{name!r} is not an ASCII PascalCase name")
            )
        elif name in seen:
            problems.append((f"$.modules[{index}].name", f"duplicate module name {name!r}"))
        seen.add(name)
    return problems


def _modules(drivers: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": module["name"], "capabilities": list(module.get("capabilities", []))}
        for module in drivers["modules"]
    ]


def base_document(request: str, drivers: Mapping[str, Any], proposal: Proposal) -> dict[str, Any]:
    """`charter/base.yaml`: the request, every driver (`unknown` when not
    established), the confirmed modules with their capabilities (the epic-3
    backlog handoff), and the confirmed patterns."""
    return {
        "schema": CHARTER_SCHEMA,
        "decision": AUTOMATIC,
        "drivers": {name: drivers.get(name, UNKNOWN) for name in DRIVERS},
        "modules": _modules(drivers),
        "patterns": list(proposal.patterns),
        "request": request,
    }


def architecture_document(
    base: Mapping[str, Any], language: str, core_version: str, pack_versions: Mapping[str, str]
) -> dict[str, Any]:
    """`architecture.yaml`, derived from `base.yaml` plus the version pins.

    Everything but `language` is language-neutral (FR-11).
    """
    return {
        "language": language,
        "modules": [module["name"] for module in base["modules"]],
        "patterns": list(base["patterns"]),
        "versions": {"core": core_version, "packs": dict(pack_versions)},
    }


def module_names(drivers: Mapping[str, Any]) -> Sequence[str]:
    modules = drivers.get("modules")
    if not isinstance(modules, list):
        return ()
    return tuple(m["name"] for m in modules if isinstance(m, dict) and "name" in m)
