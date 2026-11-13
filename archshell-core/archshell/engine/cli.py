"""Thin engine entry: parse, dispatch, emit.

Protocol messages are JSON over stdin/stdout, UTF-8, one message per
invocation. stdout carries only the protocol; logs go to stderr as JSON lines.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, BinaryIO, TextIO

ACTION_PROTOCOL = "archshell/action/v1"
ERROR_PROTOCOL = "archshell/error/v1"

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
            "protocol": ERROR_PROTOCOL,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def read_message(stream: BinaryIO) -> Message:
    raw = stream.read()
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            "invalid_message", f"stdin is not a UTF-8 JSON message: {exc}"
        ) from None
    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", "stdin message must be a JSON object")
    return message


def cmd_next(message: Message) -> Message:
    del message  # The tracer ignores input; story 1.2 reads state from disk.
    return {"protocol": ACTION_PROTOCOL, "type": "done"}


COMMANDS: dict[str, Callable[[Message], Message]] = {
    "next": cmd_next,
}


def log(event: str, **fields: Any) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def emit(message: Message, stdout: TextIO) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def run(argv: Sequence[str], stdin: BinaryIO, stdout: TextIO) -> int:
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
        result = handler(read_message(stdin))
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
