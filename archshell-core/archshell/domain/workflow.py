"""Pure cursor transitions for the `next`/`submit` loop.

(state, submission, fresh ids) -> new state + next action, or a typed refusal.
No IO, clock, randomness, or environment (AD-4): the adapter reads and writes
state and supplies fresh ids.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
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

STATE_SCHEMA = "archshell/state/v1"
SUBMIT_SCHEMA = "archshell/submit/v1"
HUMAN_SOURCE = "human_ui"
SUBMIT_FIELDS = frozenset(
    {"schema", "feature", "action_id", "revision", "nonce", "source", "result"}
)

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_MAX_LENGTH = 64


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
    type: str
    text: str


# Built-in diagnostic flows; the feature slug selects the flow until real
# feature workflows exist. `done` and `report` are terminal (AD-2).
FLOWS: dict[str, tuple[Step, ...]] = {
    "probe": (
        Step("llm_task", "Probe: reply with one short sentence confirming you received this task."),
        Step("ask", "Probe: is the ArchShell action protocol reaching you?"),
        Step("confirm", "Probe: confirm to continue the protocol probe."),
        Step("instruct_agent", "Probe: no change is needed; acknowledge this instruction."),
        Step("done", "Probe finished: every action type was exchanged."),
    ),
    "probe-report": (
        Step("llm_task", "Probe: reply with one short sentence confirming you received this task."),
        Step("report", "Probe report: the protocol stopped with a report action."),
    ),
}


def build_action(step: Step, feature: str, revision: int, ids: FreshIds) -> Action:
    common = {"feature": feature, "action_id": ids.action_id, "revision": revision}
    match step.type:
        case "llm_task":
            return LlmTask(**common, prompt=step.text)
        case "ask":
            return Ask(**common, question=step.text, nonce=ids.nonce)
        case "confirm":
            return Confirm(**common, question=step.text, nonce=ids.nonce)
        case "instruct_agent":
            return InstructAgent(**common, instruction=step.text)
        case "report":
            return Report(**common, summary=step.text)
        case "done":
            return Done(**common, summary=step.text)
    raise ValueError(f"unknown step type: {step.type}")


class StateError(ValueError):
    """A serialized cursor state is not a valid `archshell/state/v1` document."""


@dataclass(frozen=True)
class CursorState:
    """Gitignored, reconstructible cursor: which step is current, at which revision."""

    feature: str
    flow: str
    step: int
    revision: int
    action: Action

    @property
    def ended(self) -> bool:
        return self.action.terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "feature": self.feature,
            "flow": self.flow,
            "step": self.step,
            "revision": self.revision,
            "action": self.action.to_message(),
        }

    @staticmethod
    def from_dict(data: Any) -> CursorState:
        if not isinstance(data, dict):
            raise StateError("state must be an object")
        if data.get("schema") != STATE_SCHEMA:
            raise StateError(f"state schema must be {STATE_SCHEMA}")
        if set(data) != {"schema", "feature", "flow", "step", "revision", "action"}:
            raise StateError("state has missing or unknown fields")
        feature, flow, step, revision = (
            data["feature"], data["flow"], data["step"], data["revision"]
        )
        if not is_valid_slug(feature):
            raise StateError("state feature is not a valid slug")
        if flow not in FLOWS:
            raise StateError(f"unknown flow: {flow!r}")
        for name, value in (("step", step), ("revision", revision)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateError(f"{name} must be an integer")
        if not 0 <= step < len(FLOWS[flow]) or revision < 1:
            raise StateError("step or revision out of range")
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
        return CursorState(feature, flow, step, revision, action)


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
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """The action to return, the state to hold, and whether it must be written."""

    state: CursorState
    action: Action
    changed: bool


def parse_submission(message: dict[str, Any]) -> Submission | Refusal:
    def invalid(text: str) -> Refusal:
        return Refusal("invalid_message", text)

    if message.get("schema") != SUBMIT_SCHEMA:
        return invalid(f"submit message schema must be {SUBMIT_SCHEMA}")
    unknown = sorted(set(message) - SUBMIT_FIELDS)
    if unknown:
        return Refusal(
            "invalid_message", "submit message has unknown fields", {"fields": unknown}
        )
    feature = message.get("feature")
    action_id = message.get("action_id")
    revision = message.get("revision")
    nonce = message.get("nonce")
    source = message.get("source")
    result = message.get("result")
    if not isinstance(feature, str):
        return invalid("feature must be a string")
    if not isinstance(action_id, str):
        return invalid("action_id must be a string")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return invalid("revision must be an integer")
    if nonce is not None and not isinstance(nonce, str):
        return invalid("nonce must be a string")
    if source is not None and not isinstance(source, str):
        return invalid("source must be a string")
    if not isinstance(result, dict):
        return invalid("result must be an object")
    return Submission(feature, action_id, revision, nonce, source, result)


def on_next(state: CursorState | None, feature: str, ids: FreshIds) -> Outcome | Refusal:
    """Return the pending action, starting the feature's flow when there is no state."""
    if state is not None:
        return Outcome(state, state.action, changed=False)
    flow = FLOWS.get(feature)
    if flow is None:
        return Refusal(
            "unknown_feature",
            f"no workflow for feature: {feature}",
            {"features": sorted(FLOWS)},
        )
    action = build_action(flow[0], feature, 1, ids)
    return Outcome(CursorState(feature, feature, 0, 1, action), action, changed=True)


def on_submit(
    state: CursorState | None, submission: Submission, ids: FreshIds
) -> Outcome | Refusal:
    """Accept a result for the pending action and advance, or refuse it."""
    if state is None or state.ended:
        return Refusal("no_pending_action", "there is no pending action to submit")
    pending = state.action
    if submission.action_id != pending.action_id or submission.revision != state.revision:
        return Refusal(
            "stale_action",
            "submission does not match the pending action",
            {"action_id": pending.action_id, "revision": state.revision},
        )
    if pending.requires_human:
        expected: str = pending.nonce  # type: ignore[union-attr]
        if submission.nonce is None or not hmac.compare_digest(
            submission.nonce.encode("utf-8"), expected.encode("utf-8")
        ):
            return Refusal("nonce_mismatch", f"{pending.type} result carries a wrong nonce")
        if submission.source != HUMAN_SOURCE:
            return Refusal(
                "invalid_source",
                f"{pending.type} result must come from the host's question UI",
                {"expected_source": HUMAN_SOURCE},
            )
    step = state.step + 1
    revision = state.revision + 1
    action = build_action(FLOWS[state.flow][step], state.feature, revision, ids)
    return Outcome(CursorState(state.feature, state.flow, step, revision, action), action, True)
