"""Thin engine entry: parse, dispatch, emit.

Protocol messages are JSON over stdin/stdout, UTF-8, one message per
invocation. stdout carries only the protocol; logs go to stderr as JSON lines.
Usage: `archshell-engine next|submit --feature <slug> [--lang en|es]`; the
project root is the working directory. `--lang` is the conversation language
of every `message` the developer sees (default `en`); written files stay
English. The hook sub-protocol (AD-17) is
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
from types import ModuleType
from typing import TYPE_CHECKING, Any, BinaryIO, TextIO

from archshell.adapters import catalogs
from archshell.adapters.ids import fresh_ids
from archshell.adapters.state_store import (
    CorruptState,
    LockHeld,
    RevisionConflict,
    StateStore,
)
from archshell.domain import hooks, layout, workflow
from archshell.domain.actions import Message
from archshell.domain.messages import DEFAULT_LANG, LANGUAGES, MessageError, msg
from archshell.domain.messages import Message as CatalogMessage
from archshell.render.hosts import HOSTS, PayloadError

if TYPE_CHECKING:
    from archshell.adapters.layout import Config

ERROR_SCHEMA = "archshell/error/v1"

EXIT_OK = 0
EXIT_PROTOCOL_ERROR = 2


class ProtocolError(Exception):
    """A refusal; `message` is a catalog message, rendered when it is emitted."""

    def __init__(
        self, code: str, message: CatalogMessage, details: dict[str, Any] | None = None,
        lang: str | None = None,
    ):
        super().__init__(message.key)
        self.code = code
        self.message = message
        self.details = details or {}
        # Set when raised after `--lang` was read but before `run()` knows it.
        self.lang = lang

    def text(self, lang: str = DEFAULT_LANG) -> str:
        """The rendered message; the bare key if the catalog itself is broken, so
        an error (and a fail-closed hook block) is still emitted."""
        try:
            return catalogs.render(self.message, lang)
        except (catalogs.CatalogError, MessageError) as exc:
            log("catalog_error", key=self.message.key, error=str(exc))
            return self.message.key

    def to_message(self, lang: str = DEFAULT_LANG) -> Message:
        return {
            "schema": ERROR_SCHEMA,
            "code": self.code,
            "message": self.text(lang),
            "details": self.details,
        }


def read_message(stream: BinaryIO) -> Message:
    raw = stream.read()
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError(
            "invalid_message", msg("error.stdin_not_json", error=exc)
        ) from None
    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", msg("error.stdin_not_object"))
    return message


def refuse(refusal: workflow.Refusal) -> ProtocolError:
    return ProtocolError(refusal.code, refusal.message, refusal.details)


def parse_arguments(args: Sequence[str]) -> tuple[str, str]:
    """`--feature <slug> [--lang en|es]` -> (slug, lang).

    Errors render in English, except those raised after a valid `--lang`."""
    if len(args) < 2 or args[0] != "--feature":
        raise ProtocolError("invalid_feature", msg("error.expected_feature"))
    rest = list(args[2:])
    lang = DEFAULT_LANG
    if rest and rest[0] == "--lang":
        if len(rest) < 2 or rest[1] not in LANGUAGES:
            raise ProtocolError(
                "invalid_arguments",
                msg("error.invalid_lang", languages=", ".join(LANGUAGES)),
                {"arguments": rest[:2], "languages": list(LANGUAGES)},
            )
        lang, rest = rest[1], rest[2:]
    if rest:
        raise ProtocolError(
            "invalid_arguments", msg("error.unexpected_arguments"), {"arguments": rest},
            lang=lang,
        )
    return args[1], lang


def validate_feature(feature: str) -> str:
    if not workflow.is_valid_slug(feature):
        raise ProtocolError(
            "invalid_feature",
            msg("error.invalid_feature_slug", max_length=workflow.SLUG_MAX_LENGTH),
            {"feature": feature},
        )
    return feature


def require_flow(feature: str) -> None:
    if feature not in workflow.FLOWS:
        raise refuse(workflow.unknown_feature(feature))


def cmd_next(feature: str, message: Message, store: StateStore, lang: str) -> Message:
    del message  # `next` takes no input; everything comes from the state on disk.
    require_flow(feature)
    with store.locked(feature):
        state = store.read(feature)
        # Only a state with retries in use needs `max_retries`; any other
        # `next` never reads the config, so an invalid one cannot block it.
        retries = {}
        if state is not None and state.attempts:
            retries["max_retries"] = _load_config_or_refuse(store.root).max_retries
        outcome = workflow.on_next(
            state, feature, fresh_ids(), render=catalogs.render, lang=lang, **retries
        )
        if isinstance(outcome, workflow.Refusal):
            raise refuse(outcome)
        store.ensure_prompt(outcome.state)
        if outcome.changed:
            store.write(outcome.state, expected_revision=0 if state is None else state.revision)
    return outcome.action.to_message()


def _load_config_or_refuse(root: Path) -> Config:
    """The project config, or an `invalid_config` refusal.

    `load_config` is imported here, so a command that never reads the config
    (the hooks, a `next` with no retries in use) does not load jsonschema.
    """
    from archshell.adapters.layout import InvalidConfig, load_config

    try:
        return load_config(root)
    except InvalidConfig as exc:
        raise ProtocolError(
            "invalid_config", msg("error.invalid_config", config=layout.CONFIG),
            {
                "config": layout.CONFIG,
                "errors": workflow.error_entries(workflow.cap_errors(exc.errors)),
            },
        ) from None


def _log_submitted(feature: str, state: workflow.CursorState) -> None:
    if state.attempts:  # nonzero only while the same step is being retried
        log(
            "submit_retry", feature=feature, revision=state.revision,
            attempt=state.attempts, error_count=len(state.errors),
        )
    else:
        log("submit_accepted", feature=feature, revision=state.revision)


def cmd_submit(feature: str, message: Message, store: StateStore, lang: str) -> Message:
    submission = workflow.parse_submission(message)
    if isinstance(submission, workflow.Refusal):
        raise refuse(submission)
    if submission.feature != feature:
        raise ProtocolError(
            "invalid_feature",
            msg("error.feature_mismatch"),
            {"feature": feature, "message_feature": submission.feature},
        )
    require_flow(feature)
    config = _load_config_or_refuse(store.root)
    # Imported here so `next` and the hooks do not pay for loading jsonschema.
    from archshell.adapters import output_schemas

    if store.read(feature) is None:
        # Refused before taking the lock, so the refusal creates no `.archshell/`
        # directory. State is written before any action reaches a host, so no
        # valid submit can race a first `next` here.
        raise refuse(workflow.NO_PENDING_ACTION)

    with store.locked(feature):
        state = store.read(feature)
        outcome = workflow.on_submit(
            state, submission, fresh_ids(),
            validate=output_schemas.validate, render=catalogs.render, lang=lang,
            max_retries=config.max_retries,
        )
        if isinstance(outcome, workflow.Refusal):
            if outcome.code == "invalid_output":
                log(
                    "submit_invalid_output", feature=feature,
                    revision=state.revision if state else None,
                    error_count=outcome.details.get("error_count"),
                )
            raise refuse(outcome)
        assert state is not None  # on_submit refuses when there is no state
        store.ensure_prompt(outcome.state)
        store.write(outcome.state, expected_revision=state.revision)
    _log_submitted(feature, outcome.state)
    return outcome.action.to_message()


COMMANDS: dict[str, Callable[[str, Message, StateStore, str], Message]] = {
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
                msg("error.hook_arguments", command=command, options=", ".join(sorted(allowed))),
                {"arguments": list(args)},
            )
        options[name] = args[index + 1]
        index += 2
    if "--feature" in options:
        validate_feature(options["--feature"])
    host = options.get("--host")
    if host is not None and host not in HOSTS:
        raise ProtocolError(
            "unknown_host", msg("error.unknown_host", host=host), {"hosts": sorted(HOSTS)}
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
        raise ProtocolError("invalid_message", msg("error.hook_payload", error=hook.message))
    if command == "guard-write":
        return guard_write(hook, root)
    if command == "check-edit":
        return hooks.check_edit(hook, str(root))
    return hooks.on_stop(hook, feature)


def _fail_closed(err: ProtocolError) -> Message:
    """The `block` verdict a fail-closed hook replies with, carrying `err` in `error`."""
    result = hooks.error_block(err.code, err.text()).to_message()
    result["error"] = err.to_message()
    return result


def _hook_error(command: str, exc: Exception) -> ProtocolError:
    """`exc` as a protocol error, logged; anything unexpected is `internal_error`."""
    if isinstance(exc, ProtocolError):
        log("hook_error", command=command, code=exc.code)
        return exc
    log("internal_error", command=command, error=repr(exc))
    return ProtocolError("internal_error", msg("error.internal"))


def _evaluate_payload(
    command: str, options: dict[str, str], host: ModuleType | None, stdin: BinaryIO, root: Path
) -> Message:
    """Read the hook payload (in the host's shape with `--host`) and return the verdict."""
    payload = read_message(stdin)
    if host is not None:
        try:
            payload = host.parse_payload(command, payload)
        except PayloadError as exc:
            raise ProtocolError("invalid_message", msg("error.hook_payload", error=exc)) from None
    return evaluate_hook(command, payload, options.get("--feature"), root).to_message()


def _render_for_host(
    command: str, host: ModuleType, result: Message, fail_closed: bool, stdout: TextIO
) -> int:
    """Write the verdict as the host's hook output.

    A broken renderer still blocks `guard-write` (fail closed); for the other
    hooks it renders nothing and exits 0.
    """
    try:
        rendered = host.render_verdict(command, result)
    except Exception as exc:  # noqa: BLE001 - guard-write fails closed on a broken renderer
        log("internal_error", command=command, error=repr(exc))
        if not fail_closed:
            return EXIT_OK
        emit(_fail_closed(ProtocolError("internal_error", msg("error.host_render"))), stdout)
        return EXIT_PROTOCOL_ERROR
    if rendered:
        stdout.write(rendered + "\n")
        stdout.flush()
    return EXIT_OK


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
        result = _evaluate_payload(command, options, host, stdin, root)
    except Exception as exc:  # noqa: BLE001 - fail closed, never a traceback
        err = _hook_error(command, exc)
        if not fail_closed:
            if host is not None:
                return EXIT_OK
            emit(err.to_message(), stdout)
            return EXIT_PROTOCOL_ERROR
        result = _fail_closed(err)
        exit_code = EXIT_PROTOCOL_ERROR
    if result["verdict"] == hooks.BLOCK:
        log("hook_block", command=command, code=result["code"])
    if host is None:
        emit(result, stdout)
        return exit_code
    return _render_for_host(command, host, result, fail_closed, stdout)


def log(event: str, **fields: Any) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def emit(message: Message, stdout: TextIO) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def project_root(start: Path) -> Path:
    """The nearest folder at or above `start` that holds `.archshell/`, else `start`.

    The walk stops at a repository root (a folder holding `.git`), so a project
    never attaches to an `.archshell/` in an unrelated outer folder. Every
    command uses it, so the hooks guard the same root that `next` and `submit`
    write in, whatever folder the host runs them from.
    """
    for folder in (start, *start.parents):
        if (folder / layout.ROOT).is_dir():
            return folder
        if (folder / ".git").exists():
            break
    return start


def run(
    argv: Sequence[str], stdin: BinaryIO, stdout: TextIO, root: Path | None = None
) -> int:
    if root is None:
        root = project_root(Path.cwd())
    if argv and argv[0] in HOOK_OPTIONS:
        return run_hook(argv[0], argv[1:], stdin, stdout, root)
    lang = DEFAULT_LANG  # until `--lang` is parsed, errors render in English
    try:
        if not argv:
            raise ProtocolError(
                "unknown_command", msg("error.no_command"), {"commands": ALL_COMMANDS}
            )
        name = argv[0]
        handler = COMMANDS.get(name)
        if handler is None:
            raise ProtocolError(
                "unknown_command",
                msg("error.unknown_command", command=name),
                {"commands": ALL_COMMANDS},
            )
        feature, lang = parse_arguments(argv[1:])
        validate_feature(feature)
        message = read_message(stdin)
        store = StateStore(root)
        try:
            result = handler(feature, message, store, lang)
        except LockHeld:
            raise ProtocolError(
                "locked", msg("error.locked"),
                {"lock": layout.lock_file(feature)},
            ) from None
        except RevisionConflict as exc:
            raise ProtocolError("stale_action", msg("error.state_conflict", error=exc)) from None
        except CorruptState:
            raise ProtocolError(
                "invalid_state", msg("error.invalid_state"),
                {"state": layout.state_file(feature)},
            ) from None
    except ProtocolError as err:
        log("protocol_error", code=err.code)
        emit(err.to_message(err.lang or lang), stdout)
        return EXIT_PROTOCOL_ERROR
    except Exception as exc:  # noqa: BLE001 - never a traceback on stdout
        log("internal_error", error=repr(exc))
        emit(ProtocolError("internal_error", msg("error.internal")).to_message(lang), stdout)
        return EXIT_PROTOCOL_ERROR
    emit(result, stdout)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    return run(sys.argv[1:] if argv is None else argv, sys.stdin.buffer, sys.stdout)
