"""Thin engine entry: parse, dispatch, emit.

Protocol messages are JSON over stdin/stdout, UTF-8, one message per
invocation. stdout carries only the protocol; logs go to stderr as JSON lines.
Usage: `archshell-engine <command> --feature <slug>`; the project root is the
working directory. The hook sub-protocol (AD-17) is
`archshell-engine guard-write|check-edit|on-stop [--feature <slug>] [--host <id>]`
with an `archshell/hook/v1` message on stdin and an `archshell/verdict/v1`
message on stdout (or the host's hook output with `--host`).
"""

from __future__ import annotations

import json
import os
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
from archshell.domain import hooks, workflow
from archshell.render.hosts import HOSTS, PayloadError

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
    return validate_feature(args[1])


def validate_feature(feature: str) -> str:
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
        store.ensure_prompt(outcome.state)
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
        store.ensure_prompt(outcome.state)
        store.write(outcome.state, expected_revision=state.revision)
    log("submit_accepted", feature=feature, revision=outcome.state.revision)
    return outcome.action.to_message()


COMMANDS: dict[str, Callable[[str, Message, StateStore], Message]] = {
    "next": cmd_next,
    "submit": cmd_submit,
}


# Hook sub-protocol (AD-17). Each hook takes these options; `--feature` only
# applies to `on-stop`. A hook command never writes a file.
HOOK_OPTIONS: dict[str, frozenset[str]] = {
    "guard-write": frozenset({"--host"}),
    "check-edit": frozenset({"--host"}),
    "on-stop": frozenset({"--host", "--feature"}),
}

ALL_COMMANDS = sorted([*COMMANDS, *HOOK_OPTIONS])


def parse_hook_options(command: str, args: Sequence[str]) -> dict[str, str]:
    allowed = HOOK_OPTIONS[command]
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        name = args[index]
        if name not in allowed or name in options or index + 1 >= len(args):
            raise ProtocolError(
                "invalid_arguments",
                f"{command} takes only {', '.join(sorted(allowed))}, each once with a value",
                {"arguments": list(args)},
            )
        options[name] = args[index + 1]
        index += 2
    if "--feature" in options:
        validate_feature(options["--feature"])
    host = options.get("--host")
    if host is not None and host not in HOSTS:
        raise ProtocolError(
            "unknown_host", f"unknown host: {host}", {"hosts": sorted(HOSTS)}
        )
    return options


def guard_write(hook: hooks.HookInput, root: Path) -> hooks.Verdict:
    """Check the path lexically, then again with symlinks resolved.

    The lexical pass catches forms the OS would resolve differently (device
    prefixes, drive-relative paths); the realpath pass catches a symlinked
    root or target (e.g. macOS `/var` vs `/private/var`). Either block wins.
    """
    verdict = hooks.guard_write(hook, str(root))
    if verdict.verdict == hooks.BLOCK or hook.path is None:
        return verdict
    base = str(root) if hook.cwd is None else os.path.join(str(root), hook.cwd)
    target = os.path.realpath(os.path.join(base, hook.path))
    return hooks.guard_write(hooks.HookInput(path=target, cwd=None), os.path.realpath(root))


def evaluate_hook(
    command: str, message: Message, feature: str | None, root: Path
) -> hooks.Verdict:
    hook = hooks.parse_hook(message, requires_path=command != "on-stop")
    if isinstance(hook, hooks.Invalid):
        raise ProtocolError("invalid_message", hook.message)
    if command == "guard-write":
        return guard_write(hook, root)
    if command == "check-edit":
        return hooks.check_edit(hook, str(root))
    return hooks.on_stop(hook, feature)


def run_hook(command: str, args: Sequence[str], stdin: BinaryIO, stdout: TextIO, root: Path) -> int:
    """Run one hook. `guard-write` fails closed: any error is a `block`.

    Without `--host` the reply is an `archshell/verdict/v1` message (exit 0),
    or on error a fail-closed `block` verdict carrying the `archshell/error/v1`
    message in `error` for guard-write and a plain error message for the other
    hooks (exit 2). With `--host` the verdict is rendered in the host's hook
    output (exit 0); errors in check-edit and on-stop render nothing, since
    those hooks are best effort and must not trap the agent.
    """
    fail_closed = command == "guard-write"
    host = None
    exit_code = EXIT_OK
    try:
        options = parse_hook_options(command, args)
        if "--host" in options:
            host = HOSTS[options["--host"]]
        payload = read_message(stdin)
        if host is not None:
            try:
                payload = host.parse_payload(command, payload)
            except PayloadError as exc:
                raise ProtocolError("invalid_message", str(exc)) from None
        verdict = evaluate_hook(command, payload, options.get("--feature"), root)
        result: Message = verdict.to_message()
    except Exception as exc:  # noqa: BLE001 - fail closed, never a traceback
        if isinstance(exc, ProtocolError):
            err = exc
            log("hook_error", command=command, code=err.code)
        else:
            err = ProtocolError("internal_error", "unexpected engine failure")
            log("internal_error", command=command, error=repr(exc))
        if not fail_closed:
            if host is not None:
                return EXIT_OK
            emit(err.to_message(), stdout)
            return EXIT_PROTOCOL_ERROR
        result = hooks.error_block(err.code, err.message).to_message()
        result["error"] = err.to_message()
        exit_code = EXIT_PROTOCOL_ERROR
    if result["verdict"] == hooks.BLOCK:
        log("hook_block", command=command, code=result["code"])
    if host is None:
        emit(result, stdout)
        return exit_code
    try:
        rendered = host.render_verdict(command, result)
    except Exception as exc:  # noqa: BLE001 - a broken renderer still blocks
        log("internal_error", command=command, error=repr(exc))
        if not fail_closed:
            return EXIT_OK
        err = ProtocolError("internal_error", "host render failed")
        result = hooks.error_block(err.code, err.message).to_message()
        result["error"] = err.to_message()
        emit(result, stdout)
        return EXIT_PROTOCOL_ERROR
    if rendered:
        stdout.write(rendered + "\n")
        stdout.flush()
    return EXIT_OK


def log(event: str, **fields: Any) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def emit(message: Message, stdout: TextIO) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def run(
    argv: Sequence[str], stdin: BinaryIO, stdout: TextIO, root: Path | None = None
) -> int:
    if argv and argv[0] in HOOK_OPTIONS:
        return run_hook(argv[0], argv[1:], stdin, stdout, Path.cwd() if root is None else root)
    try:
        if not argv:
            raise ProtocolError(
                "unknown_command", "no command given", {"commands": ALL_COMMANDS}
            )
        name = argv[0]
        handler = COMMANDS.get(name)
        if handler is None:
            raise ProtocolError(
                "unknown_command",
                f"unknown command: {name}",
                {"commands": ALL_COMMANDS},
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
