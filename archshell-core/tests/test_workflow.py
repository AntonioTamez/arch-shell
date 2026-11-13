"""Unit tests of the pure action model and cursor transitions."""

from __future__ import annotations

import dataclasses

import pytest

from archshell.domain import workflow
from archshell.domain.actions import (
    ACTION_TYPES,
    ActionError,
    Ask,
    Done,
    LlmTask,
    action_from_message,
)
from archshell.domain.workflow import (
    CursorState,
    FreshIds,
    Outcome,
    Refusal,
    StateError,
    Submission,
    on_next,
    on_submit,
    parse_submission,
)


def ids(n: int) -> FreshIds:
    return FreshIds(action_id=f"id-{n}", nonce=f"nonce-{n}")


def good(action) -> Submission:
    nonce = getattr(action, "nonce", None)
    source = "human_ui" if action.requires_human else None
    return Submission(action.feature, action.action_id, action.revision, nonce, source, {})


def start(feature: str = "probe") -> Outcome:
    outcome = on_next(None, feature, ids(1))
    assert isinstance(outcome, Outcome)
    return outcome


def drive(feature: str) -> list[Outcome]:
    outcomes = [start(feature)]
    n = 2
    while not outcomes[-1].state.ended:
        current = outcomes[-1]
        nxt = on_submit(current.state, good(current.action), ids(n))
        assert isinstance(nxt, Outcome), nxt
        outcomes.append(nxt)
        n += 1
    return outcomes


def test_closed_action_set():
    assert sorted(ACTION_TYPES) == sorted(
        ["llm_task", "ask", "confirm", "instruct_agent", "report", "done"]
    )
    assert {t for t, cls in ACTION_TYPES.items() if cls.terminal} == {"report", "done"}
    assert {t for t, cls in ACTION_TYPES.items() if cls.requires_human} == {"ask", "confirm"}


def test_actions_are_immutable():
    action = LlmTask(feature="probe", action_id="a", revision=1, prompt="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        action.revision = 2  # type: ignore[misc]


def test_to_message_carries_common_fields_and_nonce():
    ask = Ask(feature="probe", action_id="a", revision=2, question="q", nonce="n")
    assert ask.to_message() == {
        "schema": "archshell/action/v1",
        "type": "ask",
        "feature": "probe",
        "action_id": "a",
        "revision": 2,
        "question": "q",
        "nonce": "n",
    }
    assert "nonce" not in Done(feature="probe", action_id="a", revision=1, summary="s").to_message()


@pytest.mark.parametrize("cls", list(ACTION_TYPES.values()))
def test_action_message_round_trip(cls):
    values = {f.name: ("x" if f.type == "str" else 3) for f in dataclasses.fields(cls)}
    action = cls(**values)
    assert action_from_message(action.to_message()) == action


@pytest.mark.parametrize(
    "message",
    [
        [],
        {"schema": "archshell/action/v0", "type": "done"},
        {"schema": "archshell/action/v1", "type": "run"},
        {"schema": "archshell/action/v1", "type": "done", "feature": "p", "action_id": "a",
         "revision": 0, "summary": "s"},
        {"schema": "archshell/action/v1", "type": "done", "feature": "p", "action_id": "a",
         "revision": 1},
        {"schema": "archshell/action/v1", "type": "done", "feature": "p", "action_id": "a",
         "revision": 1, "summary": "s", "extra": 1},
    ],
)
def test_action_from_message_rejects_invalid(message):
    with pytest.raises(ActionError):
        action_from_message(message)


def test_probe_flow_sequence_and_revisions():
    outcomes = drive("probe")
    assert [(o.action.type, o.action.revision) for o in outcomes] == [
        ("llm_task", 1), ("ask", 2), ("confirm", 3), ("instruct_agent", 4), ("done", 5)
    ]
    assert all(o.changed for o in outcomes)
    assert [o.action.action_id for o in outcomes] == ["id-1", "id-2", "id-3", "id-4", "id-5"]
    assert outcomes[1].action.nonce == "nonce-2"


def test_probe_report_flow_ends_with_report():
    outcomes = drive("probe-report")
    assert [o.action.type for o in outcomes] == ["llm_task", "report"]
    refusal = on_submit(outcomes[-1].state, good(outcomes[-1].action), ids(9))
    assert refusal == Refusal("no_pending_action", refusal.message, {})


def test_transitions_are_deterministic():
    assert drive("probe") == drive("probe")


def test_next_with_state_returns_pending_unchanged():
    first = start()
    again = on_next(first.state, "probe", ids(7))
    assert again == Outcome(first.state, first.action, changed=False)


def test_next_unknown_feature():
    refusal = on_next(None, "nope", ids(1))
    assert isinstance(refusal, Refusal) and refusal.code == "unknown_feature"


def test_submit_without_state():
    sub = Submission("probe", "id-1", 1, None, None, {})
    refusal = on_submit(None, sub, ids(2))
    assert isinstance(refusal, Refusal) and refusal.code == "no_pending_action"


@pytest.mark.parametrize(
    "change", [{"revision": 0}, {"revision": 2}, {"action_id": "other"}]
)
def test_submit_stale(change):
    first = start()
    sub = dataclasses.replace(good(first.action), **change)
    refusal = on_submit(first.state, sub, ids(2))
    assert isinstance(refusal, Refusal) and refusal.code == "stale_action"


def ask_state() -> Outcome:
    first = start()
    outcome = on_submit(first.state, good(first.action), ids(2))
    assert isinstance(outcome, Outcome) and outcome.action.type == "ask"
    return outcome


@pytest.mark.parametrize("nonce", [None, "nonce-1", "", "nonce-2x"])
def test_submit_nonce_mismatch(nonce):
    asked = ask_state()
    sub = dataclasses.replace(good(asked.action), nonce=nonce)
    refusal = on_submit(asked.state, sub, ids(3))
    assert isinstance(refusal, Refusal) and refusal.code == "nonce_mismatch"


@pytest.mark.parametrize("source", [None, "agent", "HUMAN_UI"])
def test_submit_invalid_source(source):
    asked = ask_state()
    sub = dataclasses.replace(good(asked.action), source=source)
    refusal = on_submit(asked.state, sub, ids(3))
    assert isinstance(refusal, Refusal) and refusal.code == "invalid_source"


def test_submit_nonce_replay_is_stale():
    asked = ask_state()
    consumed = good(asked.action)
    confirmed = on_submit(asked.state, consumed, ids(3))
    assert isinstance(confirmed, Outcome)
    refusal = on_submit(confirmed.state, consumed, ids(4))
    assert isinstance(refusal, Refusal) and refusal.code == "stale_action"


def test_non_human_actions_ignore_nonce_and_source():
    first = start()
    sub = dataclasses.replace(good(first.action), nonce="whatever", source="agent")
    assert isinstance(on_submit(first.state, sub, ids(2)), Outcome)


def test_state_round_trip():
    for outcome in drive("probe"):
        assert CursorState.from_dict(outcome.state.to_dict()) == outcome.state


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="archshell/state/v0"),
        lambda d: d.update(flow="other"),
        lambda d: d.update(step=9),
        lambda d: d.update(step=1),
        lambda d: d.update(revision=2),
        lambda d: d.update(feature="Bad"),
        lambda d: d.update(extra=1),
        lambda d: d["action"].update(type="done"),
        lambda d: d.pop("action"),
    ],
)
def test_state_from_dict_rejects_invalid(mutate):
    data = start().state.to_dict()
    mutate(data)
    with pytest.raises(StateError):
        CursorState.from_dict(data)


def test_parse_submission():
    message = {
        "schema": "archshell/submit/v1", "feature": "probe", "action_id": "a",
        "revision": 2, "nonce": "n", "source": "human_ui", "result": {"answer": "yes"},
    }
    assert parse_submission(message) == Submission("probe", "a", 2, "n", "human_ui", {"answer": "yes"})


@pytest.mark.parametrize(
    "value", ["probe", "probe-report", "a1-b2", "x"]
)
def test_valid_slugs(value):
    assert workflow.is_valid_slug(value)


@pytest.mark.parametrize(
    "value", ["", "Probe", "probe_x", "-p", "p-", "p--q", "a" * 65, None, 3]
)
def test_invalid_slugs(value):
    assert not workflow.is_valid_slug(value)
