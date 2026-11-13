"""Thin engine entry: parse, dispatch, emit.

Protocol messages are JSON over stdin/stdout, UTF-8, one message per
invocation. stdout carries only the protocol; logs go to stderr as JSON lines.
Usage: `archshell-engine <command> --feature <slug>`; the project root is the
working directory.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from archshell.adapters.ids import fresh_ids
from archshell.adapters.state_store import (
    CorruptState,
    LockHeld,
    RevisionConflict,
    StateStore,
)
from archshell.domain import workflow

ERROR_SCHEMA = "archshell/error/v1"

EXIT_OK = 0
EXIT_PROTOCOL_ERROR = 2

Message = dict[str, Any]


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_message(self) -> Message:
        return {
            "schema": ERROR_SCHEMA,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def read_message(stream: BinaryIO) -> Message:
    raw = stream.read()
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError(
            "invalid_message", f"stdin is not a UTF-8 JSON message: {exc}"
        ) from None
    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", "stdin message must be a JSON object")
    return message


def refuse(refusal: workflow.Refusal) -> ProtocolError:
    return ProtocolError(refusal.code, refusal.message, refusal.details)


def parse_feature(args: Sequence[str]) -> str:
    if len(args) < 2 or args[0] != "--feature":
        raise ProtocolError("invalid_feature", "expected --feature <slug>")
    if len(args) > 2:
        raise ProtocolError(
            "invalid_arguments", "unexpected arguments", {"arguments": list(args[2:])}
        )
    feature = args[1]
    if not workflow.is_valid_slug(feature):
        raise ProtocolError(
            "invalid_feature",
            f"feature must be a kebab-case slug of at most {workflow.SLUG_MAX_LENGTH} characters",
            {"feature": feature},
        )
    return feature


def require_flow(feature: str) -> None:
    if feature not in workflow.FLOWS:
        raise ProtocolError(
            "unknown_feature",
            f"no workflow for feature: {feature}",
            {"features": sorted(workflow.FLOWS)},
        )


def cmd_next(feature: str, message: Message, store: StateStore) -> Message:
    del message  # `next` takes no input; everything comes from the state on disk.
    require_flow(feature)
    with store.locked(feature):
        state = store.read(feature)
        outcome = workflow.on_next(state, feature, fresh_ids())
        if isinstance(outcome, workflow.Refusal):
            raise refuse(outcome)
        if outcome.changed:
            store.write(outcome.state, expected_revision=0 if state is None else state.revision)
    return outcome.action.to_message()


def cmd_submit(feature: str, message: Message, store: StateStore) -> Message:
    submission = workflow.parse_submission(message)
    if isinstance(submission, workflow.Refusal):
        raise refuse(submission)
    if submission.feature != feature:
        raise ProtocolError(
            "invalid_feature",
            "submit message feature does not match --feature",
            {"feature": feature, "message_feature": submission.feature},
        )
    require_flow(feature)
    with store.locked(feature):
        state = store.read(feature)
        outcome = workflow.on_submit(state, submission, fresh_ids())
        if isinstance(outcome, workflow.Refusal):
            raise refuse(outcome)
        assert state is not None  # on_submit refuses when there is no state
        store.write(outcome.state, expected_revision=state.revision)
    log("submit_accepted", feature=feature, revision=outcome.state.revision)
    return outcome.action.to_message()


COMMANDS: dict[str, Callable[[str, Message, StateStore], Message]] = {
    "next": cmd_next,
    "submit": cmd_submit,
}


def log(event: str, **fields: Any) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def emit(message: Message, stdout: TextIO) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def run(
    argv: Sequence[str], stdin: BinaryIO, stdout: TextIO, root: Path | None = None
) -> int:
    try:
        if not argv:
            raise ProtocolError(
                "unknown_command", "no command given", {"commands": sorted(COMMANDS)}
            )
        name = argv[0]
        handler = COMMANDS.get(name)
        if handler is None:
            raise ProtocolError(
                "unknown_command",
                f"unknown command: {name}",
                {"commands": sorted(COMMANDS)},
            )
        feature = parse_feature(argv[1:])
        message = read_message(stdin)
        store = StateStore(Path.cwd() if root is None else root)
        try:
            result = handler(feature, message, store)
        except LockHeld:
            raise ProtocolError(
                "locked", "another engine invocation holds this feature's lock",
                {"lock": f".archshell/locks/{feature}.lock"},
            ) from None
        except RevisionConflict as exc:
            raise ProtocolError("stale_action", f"state changed concurrently: {exc}") from None
        except CorruptState:
            raise ProtocolError(
                "invalid_state", "the feature's state file is unreadable",
                {"state": f".archshell/state/{feature}.yaml"},
            ) from None
    except ProtocolError as err:
        log("protocol_error", code=err.code)
        emit(err.to_message(), stdout)
        return EXIT_PROTOCOL_ERROR
    except Exception as exc:  # noqa: BLE001 - never a traceback on stdout
        log("internal_error", error=repr(exc))
        emit(ProtocolError("internal_error", "unexpected engine failure").to_message(), stdout)
        return EXIT_PROTOCOL_ERROR
    emit(result, stdout)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    return run(sys.argv[1:] if argv is None else argv, sys.stdin.buffer, sys.stdout)
