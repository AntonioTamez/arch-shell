"""Pure cursor transitions for the `next`/`submit` loop.

(state, submission, fresh ids) -> new state + next action, or a typed refusal.
No IO, clock, randomness, or environment (AD-4): the adapter reads and writes
state and supplies fresh ids.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from archshell.domain.actions import (
    Action,
    ActionError,
    Ask,
    Confirm,
    Done,
    InstructAgent,
    LlmTask,
    Report,
    action_from_message,
)
from archshell.domain import charter, scoring
from archshell.domain.layout import DEFAULT_MAX_RETRIES, PROMPTS_DIR
from archshell.domain.messages import DEFAULT_LANG, LANGUAGES, Message, Renderer, msg

STATE_SCHEMA = "archshell/state/v1"
STATE_FIELDS = frozenset(
    {"schema", "feature", "flow", "step", "revision", "action", "attempts", "errors", "lang"}
)
# `results` (the kept results of earlier steps) is optional (AD-11): it is
# written only once a step has kept one, and a state without it has none.
RESULTS_FIELD = "results"
# Older state shapes, read with defaults: before message catalogs (no `lang`),
# and before retries (no `attempts`/`errors`, and no `lang` either).
PRE_CATALOG_STATE_FIELDS = STATE_FIELDS - {"lang"}
PRE_RETRY_STATE_FIELDS = STATE_FIELDS - {"attempts", "errors", "lang"}
SUBMIT_SCHEMA = "archshell/submit/v1"
HUMAN_SOURCE = "human_ui"
SUBMIT_FIELDS = frozenset(
    {"schema", "feature", "action_id", "revision", "nonce", "source", "result"}
)

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_MAX_LENGTH = 64

# An action id names its `llm_task` prompt file (`prompt_file_for`).
ACTION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")

# Schema errors kept in state, retry prompts, and `invalid_output` details.
MAX_REPORTED_ERRORS = 10
MAX_ERROR_MESSAGE_LENGTH = 200

# (schema id, result) -> schema errors as (path, message); empty when valid.
# Supplied by an adapter, so the domain stays free of files and libraries.
OutputValidator = Callable[[str, Any], Sequence[tuple[str, str]]]
# An `llm_task` result -> (the value its `text` parses to, errors); errors are
# empty when it parses. Supplied by the engine for `json_text` steps.
TextDecoder = Callable[[Any], tuple[Any, Sequence[tuple[str, str]]]]

# The kept results of earlier steps, by step name (`Step.name`).
Results = Mapping[str, Any]
NO_RESULTS: Results = MappingProxyType({})


def is_valid_slug(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= SLUG_MAX_LENGTH
        and SLUG_PATTERN.fullmatch(value) is not None
    )


@dataclass(frozen=True)
class FreshIds:
    """Ids minted by an adapter for the action a transition may issue."""

    action_id: str
    nonce: str


@dataclass(frozen=True)
class Step:
    """One flow step. `text` is the prompt itself for an `llm_task` (written to
    the prompt file, never localized) and a catalog message key for every other
    type; `header` and `options` (`ask`/`confirm` only) are catalog keys too."""

    type: str
    text: str
    # `ask`/`confirm` only: engine-written question header and option labels.
    header: str = ""
    options: tuple[str, ...] = ()
    # `llm_task` only: the core schema id its result must match.
    output_schema: str = ""
    # A named step keeps its accepted result in state under this name, for the
    # steps after it; a `confirm`'s `on_decline` names the step to go to.
    name: str = ""
    # The catalog args of the step's main text (question, instruction, or
    # summary), built from the earlier results.
    args: Callable[[Results], Mapping[str, Any]] | None = None
    # `llm_task` only: the prompt built from the earlier results, instead of `text`.
    prompt: Callable[[Results], str] | None = None
    # `llm_task` only: the result's `text` is JSON; its parsed value is what the
    # output schema checks and what the step keeps.
    json_text: bool = False
    # `llm_task` only: checks of the (parsed) result the schema cannot state.
    check: Callable[[Any], Sequence[tuple[str, str]]] | None = None
    # `confirm` only: the step a `{"confirmed": false}` goes to. Empty: a
    # decline advances like an accept (the probe flow).
    on_decline: str = ""
    # Issuing this step hands the engine shell this effect (built from the
    # earlier results) to execute before the new state is written.
    effect: Callable[[Results], Any] | None = None
    # Further results kept when this step's result is accepted, each built
    # from that (parsed) result: (name, builder) pairs.
    derives: tuple[tuple[str, Callable[[Any], Any]], ...] = ()

    def prompt_text(self, results: Results) -> str:
        """An `llm_task`'s prompt: built from the results, else `text` itself."""
        return self.prompt(results) if self.prompt is not None else self.text

    def kept_names(self) -> tuple[str, ...]:
        """The results names this step adds once its result is accepted."""
        return ((self.name,) if self.name else ()) + tuple(name for name, _ in self.derives)


LLM_TEXT_SCHEMA = "archshell/llm-text/v1"

_PROBE_TASK = Step(
    "llm_task",
    "Probe: reply with one short sentence confirming you received this task.",
    output_schema=LLM_TEXT_SCHEMA,
)

def _request(results: Results) -> str:
    return results["request"]["answer"]


def _proposal(results: Results) -> scoring.Proposal:
    """The proposal scored when the drivers were accepted, as kept in state:
    the confirm shows it and the write records it, never a re-scoring (FR-7)."""
    return scoring.from_result(results["proposal"])


def _proposal_args(results: Results) -> dict[str, str]:
    return {
        "patterns": _proposal(results).title,
        "modules": ", ".join(charter.module_names(results["drivers"])),
    }


def _charter_write(results: Results) -> charter.CharterWrite:
    return charter.CharterWrite(_request(results), results["drivers"], _proposal(results))


# The selection tracer (story 2.1): the developer's request, its drivers, a
# provisional proposal, and on confirmation the written charter.
_BOOTSTRAP: tuple[Step, ...] = (
    Step(
        "ask",
        "flow.bootstrap.ask.question",
        header="flow.bootstrap.ask.header",
        options=("flow.bootstrap.ask.example_modules", "flow.bootstrap.ask.example_single"),
        name="request",
    ),
    Step(
        "llm_task",
        "",
        output_schema=charter.DRIVERS_SCHEMA,
        name="drivers",
        prompt=lambda results: charter.drivers_prompt(_request(results)),
        json_text=True,
        check=charter.drivers_problems,
        derives=(("proposal", lambda drivers: scoring.as_result(scoring.propose(drivers))),),
    ),
    Step(
        "confirm",
        "flow.bootstrap.confirm.question",
        header="flow.bootstrap.confirm.header",
        options=("flow.bootstrap.confirm.accept", "flow.bootstrap.confirm.decline"),
        name="confirmation",
        args=_proposal_args,
        on_decline="declined",
    ),
    Step(
        "done",
        "flow.bootstrap.done",
        args=lambda results: {"patterns": _proposal(results).title},
        effect=_charter_write,
    ),
    Step("report", "flow.bootstrap.declined", name="declined"),
)

# The feature slug selects the flow: `bootstrap` (story 2.1) and the built-in
# diagnostic flows. `done` and `report` are terminal (AD-2).
FLOWS: dict[str, tuple[Step, ...]] = {
    "bootstrap": _BOOTSTRAP,
    "probe": (
        _PROBE_TASK,
        Step(
            "ask",
            "flow.probe.ask.question",
            header="flow.probe.ask.header",
            options=("flow.probe.ask.yes", "flow.probe.ask.no"),
        ),
        Step(
            "confirm",
            "flow.probe.confirm.question",
            header="flow.probe.confirm.header",
            options=("flow.probe.confirm.confirm", "flow.probe.confirm.decline"),
        ),
        Step("instruct_agent", "flow.probe.instruct"),
        Step("done", "flow.probe.done"),
    ),
    "probe-report": (
        _PROBE_TASK,
        Step("report", "flow.probe_report.report"),
    ),
}


def is_valid_action_id(value: Any) -> bool:
    """An action id is safe to use as a file name (it names the prompt file)."""
    return isinstance(value, str) and ACTION_ID_PATTERN.fullmatch(value) is not None


def prompt_file_for(action_id: str) -> str:
    """The project-relative POSIX path of an `llm_task`'s prompt file."""
    if not is_valid_action_id(action_id):
        raise ValueError(f"action id is not usable as a file name: {action_id!r}")
    return f"{PROMPTS_DIR}/{action_id}.md"


def flow_has_effects(flow: str) -> bool:
    """True when a step of `flow` hands the engine shell an effect (a file write)."""
    return any(step.effect is not None for step in FLOWS[flow])


def step_index(flow: str, name: str) -> int:
    """The index of the step called `name` in `flow`."""
    for index, step in enumerate(FLOWS[flow]):
        if step.name == name:
            return index
    raise ValueError(f"flow {flow!r} has no step named {name!r}")


def retry_prompt(
    step: Step, errors: Sequence[tuple[str, str]], results: Results = NO_RESULTS
) -> str:
    """The step's prompt, then the errors its previous answer had."""
    lines = "".join(f"- {path}: {message}\n" for path, message in errors)
    return (
        f"{step.prompt_text(results)}\n\n"
        f"Your previous answer did not match {step.output_schema}:\n{lines}"
    )


def cap_errors(errors: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path[:MAX_ERROR_MESSAGE_LENGTH], message[:MAX_ERROR_MESSAGE_LENGTH])
        for path, message in errors[:MAX_REPORTED_ERRORS]
    )


def error_entries(errors: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    """Errors as the `{path, message}` objects of state files and refusal details."""
    return [{"path": path, "message": message} for path, message in errors]


def step_texts(
    step: Step, lang: str, render: Renderer, results: Results = NO_RESULTS
) -> dict[str, Any]:
    """The step's developer-facing action fields, rendered in `lang`. The main
    text (question, instruction, or summary) takes the args the step builds
    from the earlier results."""

    def text(key: str, args: Mapping[str, Any] | None = None) -> str:
        return render(Message(key, dict(args or {})), lang)

    def main() -> str:
        return text(step.text, step.args(results) if step.args is not None else None)

    match step.type:
        case "llm_task":
            return {}
        case "ask" | "confirm":
            return {
                "question": main(),
                "header": text(step.header),
                "options": tuple(text(key) for key in step.options),
            }
        case "instruct_agent":
            return {"instruction": main()}
        case "report" | "done":
            return {"summary": main()}
    raise ValueError(f"unknown step type: {step.type}")


def build_action(
    step: Step, feature: str, revision: int, ids: FreshIds, lang: str, render: Renderer,
    results: Results = NO_RESULTS,
) -> Action:
    common = {"feature": feature, "action_id": ids.action_id, "revision": revision}
    texts = step_texts(step, lang, render, results)
    match step.type:
        case "llm_task":
            return LlmTask(**common, prompt_file=prompt_file_for(ids.action_id))
        case "ask":
            return Ask(**common, **texts, nonce=ids.nonce)
        case "confirm":
            return Confirm(**common, **texts, nonce=ids.nonce)
        case "instruct_agent":
            return InstructAgent(**common, **texts)
        case "report":
            return Report(**common, **texts)
        case "done":
            return Done(**common, **texts)
    raise ValueError(f"unknown step type: {step.type}")


class StateError(ValueError):
    """A serialized cursor state is not a valid `archshell/state/v1` document."""


@dataclass(frozen=True)
class PromptFile:
    """A prompt the engine must hold on disk before it returns an `llm_task`."""

    path: str
    text: str


@dataclass(frozen=True)
class CursorState:
    """Gitignored, reconstructible cursor: which step is current, at which revision.

    `action` is the pending action rendered in English (the state file is never
    localized); `lang` is the conversation language it was issued in, so a
    repeated `next` keeps that language (`issued`), with the wording of the
    current catalog.
    """

    feature: str
    flow: str
    step: int
    revision: int
    action: Action
    # Retries issued for the current step, and the errors that caused the
    # latest one (its prompt states them). Both reset when the cursor advances.
    attempts: int = 0
    errors: tuple[tuple[str, str], ...] = ()
    lang: str = DEFAULT_LANG
    # The kept results of the earlier named steps (`Step.name`).
    results: Results = NO_RESULTS

    @property
    def ended(self) -> bool:
        return self.action.terminal

    @property
    def pending_step(self) -> Step:
        """The flow step the pending action was issued for."""
        return FLOWS[self.flow][self.step]

    def issued(self, render: Renderer) -> Action:
        """The pending action in the language it was issued in.

        The stored action is the English record; the host's texts are rendered
        from the current catalog in every language alike, so a catalog change
        (an engine upgrade mid-flow) shows the same way in English and Spanish.
        """
        return replace(
            self.action, **step_texts(self.pending_step, self.lang, render, self.results)
        )

    def pending_prompt(self) -> PromptFile | None:
        """The prompt file the pending action points at, or None if it has none."""
        if not isinstance(self.action, LlmTask):
            return None
        step = self.pending_step
        if self.attempts:
            text = retry_prompt(step, self.errors, self.results)
        else:
            text = step.prompt_text(self.results)
        return PromptFile(self.action.prompt_file, text)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema": STATE_SCHEMA,
            "feature": self.feature,
            "flow": self.flow,
            "step": self.step,
            "revision": self.revision,
            "action": self.action.to_message(),
            "attempts": self.attempts,
            "errors": error_entries(self.errors),
            "lang": self.lang,
        }
        if self.results:
            data[RESULTS_FIELD] = dict(self.results)
        return data

    @staticmethod
    def from_dict(data: Any) -> CursorState:
        if not isinstance(data, dict):
            raise StateError("state must be an object")
        if data.get("schema") != STATE_SCHEMA:
            raise StateError(f"state schema must be {STATE_SCHEMA}")
        results = data.get(RESULTS_FIELD, {})
        data = {key: value for key, value in data.items() if key != RESULTS_FIELD}
        if set(data) == PRE_RETRY_STATE_FIELDS:
            # Written before retries existed: no retry is in progress, issued in English.
            data = {**data, "attempts": 0, "errors": [], "lang": DEFAULT_LANG}
        elif set(data) == PRE_CATALOG_STATE_FIELDS:
            # Written before message catalogs existed: issued in English.
            data = {**data, "lang": DEFAULT_LANG}
        if set(data) != STATE_FIELDS:
            raise StateError("state has missing or unknown fields")
        lang = data["lang"]
        if lang not in LANGUAGES:
            raise StateError(f"lang must be one of {', '.join(LANGUAGES)}")
        feature, flow, step, revision = (
            data["feature"], data["flow"], data["step"], data["revision"]
        )
        if not is_valid_slug(feature):
            raise StateError("state feature is not a valid slug")
        if flow not in FLOWS:
            raise StateError(f"unknown flow: {flow!r}")
        attempts = data["attempts"]
        for name, value in (("step", step), ("revision", revision), ("attempts", attempts)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateError(f"{name} must be an integer")
        if not 0 <= step < len(FLOWS[flow]) or revision < 1 or attempts < 0:
            raise StateError("step, revision, or attempts out of range")
        errors = parse_state_errors(data["errors"])
        if bool(attempts) != bool(errors) or (attempts and not FLOWS[flow][step].output_schema):
            raise StateError("attempts and errors do not match the cursor")
        try:
            action = action_from_message(data["action"])
        except ActionError as exc:
            raise StateError(f"invalid pending action: {exc}") from None
        if (
            action.feature != feature
            or action.revision != revision
            or action.type != FLOWS[flow][step].type
        ):
            raise StateError("pending action does not match the cursor")
        if isinstance(action, LlmTask) and (
            not is_valid_action_id(action.action_id)
            or action.prompt_file != prompt_file_for(action.action_id)
        ):
            raise StateError("pending llm_task prompt_file does not match its action id")
        kept = parse_state_results(results, flow, step)
        return CursorState(feature, flow, step, revision, action, attempts, errors, lang, kept)


def parse_state_results(value: Any, flow: str, step: int) -> Results:
    """`results`: an object keyed by exactly the names the steps before the
    cursor keep (a pending step's texts and prompt are built from them)."""
    if not isinstance(value, dict):
        raise StateError("results must be an object")
    expected = {name for s in FLOWS[flow][:step] for name in s.kept_names()}
    if set(value) != expected:
        raise StateError(
            f"results must hold exactly {sorted(expected)}, found {sorted(map(str, value))}"
        )
    return MappingProxyType(dict(value))


def parse_state_errors(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_REPORTED_ERRORS:
        raise StateError(f"errors must be a list of at most {MAX_REPORTED_ERRORS} entries")
    errors = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "message"}
            or not all(isinstance(entry[k], str) for k in ("path", "message"))
        ):
            raise StateError("each error must be {path, message} strings")
        errors.append((entry["path"], entry["message"]))
    return tuple(errors)


@dataclass(frozen=True)
class Submission:
    feature: str
    action_id: str
    revision: int
    nonce: str | None
    source: str | None
    result: dict[str, Any]


@dataclass(frozen=True)
class Refusal:
    """A typed refusal. `message` is a catalog message the adapter renders."""

    code: str
    message: Message
    details: dict[str, Any] = field(default_factory=dict)


NO_PENDING_ACTION = Refusal("no_pending_action", msg("error.no_pending_action"))


def unknown_feature(feature: str) -> Refusal:
    """The refusal for a feature slug that selects no flow."""
    return Refusal(
        "unknown_feature",
        msg("error.unknown_feature", feature=feature),
        {"features": sorted(FLOWS)},
    )


@dataclass(frozen=True)
class Outcome:
    """The action to return (in the request's language), the state to hold
    (in English), and whether it must be written."""

    state: CursorState
    action: Action
    changed: bool
    # What the issued step hands the engine shell to execute (`Step.effect`).
    effect: Any = None


def parse_submission(message: dict[str, Any]) -> Submission | Refusal:
    def invalid(key: str, **args: Any) -> Refusal:
        return Refusal("invalid_message", msg(key, **args))

    if message.get("schema") != SUBMIT_SCHEMA:
        return invalid("error.submit_schema", schema=SUBMIT_SCHEMA)
    unknown = sorted(set(message) - SUBMIT_FIELDS)
    if unknown:
        return Refusal(
            "invalid_message", msg("error.submit_unknown_fields"), {"fields": unknown}
        )
    feature = message.get("feature")
    action_id = message.get("action_id")
    revision = message.get("revision")
    nonce = message.get("nonce")
    source = message.get("source")
    result = message.get("result")
    if not isinstance(feature, str):
        return invalid("error.field_not_string", field="feature")
    if not isinstance(action_id, str):
        return invalid("error.field_not_string", field="action_id")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return invalid("error.field_not_integer", field="revision")
    if nonce is not None and not isinstance(nonce, str):
        return invalid("error.field_not_string", field="nonce")
    if source is not None and not isinstance(source, str):
        return invalid("error.field_not_string", field="source")
    if not isinstance(result, dict):
        return invalid("error.field_not_object", field="result")
    return Submission(feature, action_id, revision, nonce, source, result)


def issue(
    flow: str,
    feature: str,
    step: int,
    revision: int,
    ids: FreshIds,
    lang: str,
    render: Renderer,
    attempts: int = 0,
    errors: tuple[tuple[str, str], ...] = (),
    results: Results = NO_RESULTS,
) -> Outcome:
    """Issue `step` of `flow`: the state holds it in English, the host gets it in `lang`."""
    target = FLOWS[flow][step]
    kept: Results = MappingProxyType(dict(results))
    stored = build_action(target, feature, revision, ids, DEFAULT_LANG, render, kept)
    state = CursorState(feature, flow, step, revision, stored, attempts, errors, lang, kept)
    effect = target.effect(kept) if target.effect is not None else None
    return Outcome(state, state.issued(render), changed=True, effect=effect)


def on_next(
    state: CursorState | None,
    feature: str,
    ids: FreshIds,
    *,
    render: Renderer,
    lang: str = DEFAULT_LANG,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Outcome | Refusal:
    """Return the pending action, starting the feature's flow when there is no state.

    A pending action comes back as it was issued, in the language it was issued
    in, whatever `lang` this call carries (a new language takes effect from the
    next action on); its texts follow the current catalog. The one
    exception is an `llm_task` that has used every retry (`attempts > 0` and
    `attempts >= max_retries`): a new `next` is a rerun, so it re-issues the
    step as a fresh first attempt with a new retry cycle.
    """
    if state is not None:
        if state.attempts and state.attempts >= max_retries:
            return issue(
                state.flow, state.feature, state.step, state.revision + 1, ids, lang, render,
                results=state.results,
            )
        return Outcome(state, state.issued(render), changed=False)
    if feature not in FLOWS:
        return unknown_feature(feature)
    return issue(feature, feature, 0, 1, ids, lang, render)


def _check_human(pending: Action, submission: Submission) -> Refusal | None:
    """Refuse a submit for a human-only action without its nonce or human source."""
    if not pending.requires_human:
        return None
    expected: str = pending.nonce  # type: ignore[union-attr]
    if submission.nonce is None or not hmac.compare_digest(
        submission.nonce.encode("utf-8"), expected.encode("utf-8")
    ):
        return Refusal("nonce_mismatch", msg("error.nonce_mismatch", type=pending.type))
    if submission.source != HUMAN_SOURCE:
        return Refusal(
            "invalid_source",
            msg("error.invalid_source", type=pending.type),
            {"expected_source": HUMAN_SOURCE},
        )
    return None


def _invalid_output(
    output_schema: str, error_count: int, errors: Sequence[tuple[str, str]], max_retries: int
) -> Refusal:
    """The refusal once an `llm_task` step has used up its retries."""
    return Refusal(
        "invalid_output",
        msg("error.invalid_output", output_schema=output_schema, max_retries=max_retries),
        {
            "output_schema": output_schema,
            "error_count": error_count,
            "errors": error_entries(errors),
        },
    )


def _checked_output(
    step: Step, result: dict[str, Any], validate: OutputValidator, decode: TextDecoder | None
) -> tuple[Any, list[tuple[str, str]]]:
    """An `llm_task` result's value (parsed, for a `json_text` step) and its errors."""
    value: Any = result
    if step.json_text:
        if decode is None:
            raise ValueError(f"step {step.name!r} needs a text decoder")
        value, found = decode(result)
        if found:
            return None, list(found)
    found = list(validate(step.output_schema, value))
    if not found and step.check is not None:
        found = list(step.check(value))
    return value, found


def _answer_refusal(step: Step, result: dict[str, Any]) -> Refusal | None:
    """A named `ask` keeps a non-blank `answer`; a declinable `confirm` needs a
    boolean `confirmed`."""
    if step.type == "ask" and step.name:
        answer = result.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return Refusal("invalid_message", msg("error.answer_not_text"))
    if step.type == "confirm" and step.on_decline:
        if not isinstance(result.get("confirmed"), bool):
            return Refusal("invalid_message", msg("error.confirmed_not_boolean"))
    return None


def on_submit(
    state: CursorState | None,
    submission: Submission,
    ids: FreshIds,
    *,
    validate: OutputValidator,
    render: Renderer,
    lang: str = DEFAULT_LANG,
    max_retries: int = DEFAULT_MAX_RETRIES,
    decode: TextDecoder | None = None,
) -> Outcome | Refusal:
    """Accept a result for the pending action and advance, or refuse it.

    An `llm_task` result that does not match its step's output schema re-issues
    the same step as a new `llm_task` (fresh id, next revision) whose prompt
    states the errors, at most `max_retries` times; the next invalid result is
    refused with `invalid_output` and the state stays as it is.

    A named step's accepted result (for a `json_text` step, the parsed value)
    is kept in state for the steps after it. A `{"confirmed": false}` for a
    `confirm` with `on_decline` goes to that step instead of the next one.
    """
    if state is None or state.ended:
        return NO_PENDING_ACTION
    pending = state.action
    if submission.action_id != pending.action_id or submission.revision != state.revision:
        return Refusal(
            "stale_action",
            msg("error.stale_action"),
            {"action_id": pending.action_id, "revision": state.revision},
        )
    refusal = _check_human(pending, submission)
    if refusal is not None:
        return refusal
    current = FLOWS[state.flow][state.step]
    revision = state.revision + 1
    value: Any = submission.result
    if current.output_schema:
        value, found = _checked_output(current, submission.result, validate, decode)
        if found:
            errors = cap_errors(found)
            if state.attempts >= max_retries:
                return _invalid_output(current.output_schema, len(found), errors, max_retries)
            return issue(
                state.flow, state.feature, state.step, revision, ids, lang, render,
                state.attempts + 1, errors, state.results,
            )
    refusal = _answer_refusal(current, submission.result)
    if refusal is not None:
        return refusal
    results: Results = state.results
    if current.name:
        results = {**results, current.name: value}
    for name, build in current.derives:
        results = {**results, name: build(value)}
    target = state.step + 1
    if current.on_decline and submission.result["confirmed"] is False:
        target = step_index(state.flow, current.on_decline)
    return issue(state.flow, state.feature, target, revision, ids, lang, render, results=results)
