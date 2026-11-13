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
    action = LlmTask(feature="probe", action_id="a", revision=1, prompt_file="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        action.revision = 2  # type: ignore[misc]


def test_to_message_carries_common_fields_and_nonce():
    ask = Ask(
        feature="probe", action_id="a", revision=2, question="q",
        header="H", options=("Yes", "No"), nonce="n",
    )
    assert ask.to_message() == {
        "schema": "archshell/action/v1",
        "type": "ask",
        "feature": "probe",
        "action_id": "a",
        "revision": 2,
        "question": "q",
        "header": "H",
        "options": ["Yes", "No"],
        "nonce": "n",
    }
    assert "nonce" not in Done(feature="probe", action_id="a", revision=1, summary="s").to_message()


@pytest.mark.parametrize("cls", list(ACTION_TYPES.values()))
def test_action_message_round_trip(cls):
    sample = {"str": "x", "int": 3, "tuple[str, ...]": ("a", "b")}
    values = {f.name: sample[f.type] for f in dataclasses.fields(cls)}
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


def question_message(type_: str = "ask", **changes) -> dict:
    message = {
        "schema": "archshell/action/v1", "type": type_, "feature": "p", "action_id": "a",
        "revision": 1, "question": "q", "header": "Probe", "options": ["Yes", "No"],
        "nonce": "n",
    }
    message.update(changes)
    return {k: v for k, v in message.items() if v is not ...}


@pytest.mark.parametrize("type_", ["ask", "confirm"])
@pytest.mark.parametrize(
    "changes",
    [
        {"header": ...},
        {"options": ...},
        {"header": ""},
        {"header": "x" * 13},
        {"header": 1},
        {"options": ["Yes"]},
        {"options": ["a", "b", "c", "d", "e"]},
        {"options": ["Yes", ""]},
        {"options": ["Yes", "   "]},
        {"options": ["Yes", "Yes"]},
        {"options": ["Yes", "No", "Yes"]},
        {"header": "   "},
        {"header": "	"},
        {"options": ["Yes", 1]},
        {"options": "Yes,No"},
        {"options": None},
    ],
)
def test_question_actions_without_valid_header_and_options_are_invalid(type_, changes):
    with pytest.raises(ActionError):
        action_from_message(question_message(type_, **changes))


@pytest.mark.parametrize("type_", ["ask", "confirm"])
@pytest.mark.parametrize(
    "changes",
    [{}, {"header": "x" * 12}, {"options": ["a", "b", "c", "d"]}, {"options": ["a", "b", "c"]}],
)
def test_question_actions_with_valid_header_and_options_round_trip(type_, changes):
    message = question_message(type_, **changes)
    action = action_from_message(message)
    assert isinstance(action.options, tuple)  # type: ignore[union-attr]
    assert action.to_message() == message


def test_probe_questions_carry_engine_header_and_options():
    outcomes = drive("probe")
    ask, confirm = outcomes[1].action, outcomes[2].action
    assert (ask.type, ask.header, ask.options) == ("ask", "Probe", ("Yes", "No"))
    assert (confirm.type, confirm.header, confirm.options) == (
        "confirm", "Probe", ("Confirm", "Decline")
    )
    for flow in workflow.FLOWS.values():
        for step in flow:
            if step.type in ("ask", "confirm"):
                assert 0 < len(step.header) <= 12 and 2 <= len(step.options) <= 4
            else:
                assert step.header == "" and step.options == ()


def test_probe_flow_sequence_and_revisions():
    outcomes = drive("probe")
    assert [(o.action.type, o.action.revision) for o in outcomes] == [
        ("llm_task", 1), ("ask", 2), ("confirm", 3), ("instruct_agent", 4), ("done", 5)
    ]
    assert all(o.changed for o in outcomes)
    assert [o.action.action_id for o in outcomes] == ["id-1", "id-2", "id-3", "id-4", "id-5"]
    assert outcomes[1].action.nonce == "nonce-2"


def test_llm_task_carries_prompt_file_not_prompt_text():
    first = start().action
    assert isinstance(first, LlmTask)
    message = first.to_message()
    assert "prompt" not in message
    assert message["prompt_file"] == ".archshell/state/prompts/id-1.md"


def test_pending_prompt_is_the_step_text():
    first = start()
    prompt = first.state.pending_prompt()
    assert prompt == workflow.PromptFile(
        ".archshell/state/prompts/id-1.md", workflow.FLOWS["probe"][0].text
    )
    asked = on_submit(first.state, good(first.action), ids(2))
    assert isinstance(asked, Outcome) and asked.state.pending_prompt() is None


@pytest.mark.parametrize("action_id", ["", "../x", "a/b", "a.b", "-a", "a" * 129, None])
def test_prompt_file_for_rejects_unsafe_action_ids(action_id):
    with pytest.raises(ValueError):
        workflow.prompt_file_for(action_id)  # type: ignore[arg-type]


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
        lambda d: d["action"].update(prompt_file="../../outside.md"),
        lambda d: d["action"].update(prompt_file=".archshell/state/prompts/other.md"),
        lambda d: d["action"].update(
            action_id="../x", prompt_file=".archshell/state/prompts/../x.md"
        ),
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
