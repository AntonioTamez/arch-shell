"""The core error-code registry: every code an `archshell/error/v1` message carries.

Pure (AD-4). Each code maps to its catalog key `error.<code>`, which both
catalogs hold (`tests/test_error_registry.py` checks that, and that every code
literal in the core is listed here). New codes are snake_case
`<noun>_<problem>`. Hook verdict codes (`protected_path`, `allowed`, …) are
not error codes and stay outside the registry.
"""

from __future__ import annotations

ERROR_CODES: dict[str, str] = {
    # protocol and engine
    "unknown_command": "error.unknown_command",
    "invalid_feature": "error.invalid_feature",
    "invalid_arguments": "error.invalid_arguments",
    "unknown_host": "error.unknown_host",
    "invalid_message": "error.invalid_message",
    "unknown_feature": "error.unknown_feature",
    "no_pending_action": "error.no_pending_action",
    "stale_action": "error.stale_action",
    "nonce_mismatch": "error.nonce_mismatch",
    "invalid_source": "error.invalid_source",
    "invalid_output": "error.invalid_output",
    "locked": "error.locked",
    "invalid_state": "error.invalid_state",
    "invalid_config": "error.invalid_config",
    "internal_error": "error.internal_error",
    # extensions (AD-14)
    "extension_error": "error.extension_error",
    "incompatible_extension": "error.incompatible_extension",
    "duplicate_language": "error.duplicate_language",
    # bootstrap flow
    "bootstrap_interrupted": "error.bootstrap_interrupted",
    "language_pack_missing": "error.language_pack_missing",
}


def catalog_key(code: str) -> str:
    """The catalog key of a registered code; `KeyError` for an unregistered one."""
    return ERROR_CODES[code]
