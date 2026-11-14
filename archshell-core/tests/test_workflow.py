"""Unit tests of the pure action model and cursor transitions."""

from __future__ import annotations

import dataclasses

import pytest

from archshell.adapters.output_schemas import validate
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
    result = {"text": "ok"} if action.type == "llm_task" else {}
    return Submission(action.feature, action.action_id, action.revision, nonce, source, result)


def submit(state, sub, fresh, **options):
    return on_submit(state, sub, fresh, validate=validate, **options)


def start(feature: str = "probe") -> Outcome:
    outcome = on_next(None, feature, ids(1))
    assert isinstance(outcome, Outcome)
    return outcome


def drive(feature: str) -> list[Outcome]:
    outcomes = [start(feature)]
    n = 2
    while not outcomes[-1].state.ended:
        current = outcomes[-1]
        nxt = submit(current.state, good(current.action), ids(n))
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
    asked = submit(first.state, good(first.action), ids(2))
    assert isinstance(asked, Outcome) and asked.state.pending_prompt() is None


@pytest.mark.parametrize("action_id", ["", "../x", "a/b", "a.b", "-a", "a" * 129, None])
def test_prompt_file_for_rejects_unsafe_action_ids(action_id):
    with pytest.raises(ValueError):
        workflow.prompt_file_for(action_id)  # type: ignore[arg-type]


def test_probe_report_flow_ends_with_report():
    outcomes = drive("probe-report")
    assert [o.action.type for o in outcomes] == ["llm_task", "report"]
    refusal = submit(outcomes[-1].state, good(outcomes[-1].action), ids(9))
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
    refusal = submit(None, sub, ids(2))
    assert isinstance(refusal, Refusal) and refusal.code == "no_pending_action"


@pytest.mark.parametrize(
    "change", [{"revision": 0}, {"revision": 2}, {"action_id": "other"}]
)
def test_submit_stale(change):
    first = start()
    sub = dataclasses.replace(good(first.action), **change)
    refusal = submit(first.state, sub, ids(2))
    assert isinstance(refusal, Refusal) and refusal.code == "stale_action"


def ask_state() -> Outcome:
    first = start()
    outcome = submit(first.state, good(first.action), ids(2))
    assert isinstance(outcome, Outcome) and outcome.action.type == "ask"
    return outcome


@pytest.mark.parametrize("nonce", [None, "nonce-1", "", "nonce-2x"])
def test_submit_nonce_mismatch(nonce):
    asked = ask_state()
    sub = dataclasses.replace(good(asked.action), nonce=nonce)
    refusal = submit(asked.state, sub, ids(3))
    assert isinstance(refusal, Refusal) and refusal.code == "nonce_mismatch"


@pytest.mark.parametrize("source", [None, "agent", "HUMAN_UI"])
def test_submit_invalid_source(source):
    asked = ask_state()
    sub = dataclasses.replace(good(asked.action), source=source)
    refusal = submit(asked.state, sub, ids(3))
    assert isinstance(refusal, Refusal) and refusal.code == "invalid_source"


def test_submit_nonce_replay_is_stale():
    asked = ask_state()
    consumed = good(asked.action)
    confirmed = submit(asked.state, consumed, ids(3))
    assert isinstance(confirmed, Outcome)
    refusal = submit(confirmed.state, consumed, ids(4))
    assert isinstance(refusal, Refusal) and refusal.code == "stale_action"


def test_non_human_actions_ignore_nonce_and_source():
    first = start()
    sub = dataclasses.replace(good(first.action), nonce="whatever", source="agent")
    assert isinstance(submit(first.state, sub, ids(2)), Outcome)


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


# --- llm_task output validation and bounded retries ---------------------------


def llm_sub(outcome: Outcome, result: dict) -> Submission:
    return dataclasses.replace(good(outcome.action), result=result)


def retry(outcome: Outcome, result: dict, n: int, **options) -> Outcome:
    nxt = submit(outcome.state, llm_sub(outcome, result), ids(n), **options)
    assert isinstance(nxt, Outcome), nxt
    return nxt


def test_probe_llm_steps_name_the_text_schema():
    for flow in ("probe", "probe-report"):
        assert workflow.FLOWS[flow][0].output_schema == "archshell/llm-text/v1"
    assert all(
        step.output_schema == ""
        for flow in workflow.FLOWS.values()
        for step in flow
        if step.type != "llm_task"
    )


def test_valid_output_advances_and_keeps_attempts_at_zero():
    nxt = retry(start(), {"text": "ok"}, 2)
    assert (nxt.state.step, nxt.action.type, nxt.state.attempts, nxt.state.errors) == (
        1, "ask", 0, ()
    )


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"text": ""}, [("$.text", "'' should be non-empty")]),
        ({}, [("$", "'text' is a required property")]),
    ],
)
def test_first_invalid_output_reissues_the_same_step(result, expected):
    nxt = retry(start(), result, 2)
    assert nxt.changed
    assert isinstance(nxt.action, LlmTask)
    assert (nxt.state.step, nxt.state.revision, nxt.state.attempts) == (0, 2, 1)
    assert (nxt.action.action_id, nxt.action.revision) == ("id-2", 2)
    assert list(nxt.state.errors) == expected
    prompt = nxt.state.pending_prompt()
    assert prompt.path == ".archshell/state/prompts/id-2.md"
    step_text = workflow.FLOWS["probe"][0].text
    lines = "".join(f"- {p}: {m}\n" for p, m in expected)
    assert prompt.text == (
        f"{step_text}\n\nYour previous answer did not match archshell/llm-text/v1:\n{lines}"
    )


def test_second_invalid_output_issues_the_second_retry():
    third = retry(retry(start(), {}, 2), {"text": ""}, 3)
    assert isinstance(third.action, LlmTask)
    assert (third.state.step, third.state.revision, third.state.attempts) == (0, 3, 2)
    assert third.state.errors == (("$.text", "'' should be non-empty"),)


def test_third_invalid_output_is_refused_with_errors():
    third = retry(retry(start(), {}, 2), {"text": ""}, 3)
    refusal = submit(third.state, llm_sub(third, {"text": "", "x": 1}), ids(4))
    assert isinstance(refusal, Refusal)
    assert refusal.code == "invalid_output"
    assert refusal.details == {
        "output_schema": "archshell/llm-text/v1",
        "error_count": 2,
        "errors": [
            {"path": "$", "message": "Additional properties are not allowed ('x' was unexpected)"},
            {"path": "$.text", "message": "'' should be non-empty"},
        ],
    }


@pytest.mark.parametrize("retries", [1, 2])
def test_valid_output_after_retries_advances_and_resets_attempts(retries):
    outcome = start()
    for n in range(retries):
        outcome = retry(outcome, {}, n + 2)
    nxt = retry(outcome, {"text": "fixed"}, 9)
    assert nxt.action.type == "ask"
    assert (nxt.state.step, nxt.state.attempts, nxt.state.errors) == (1, 0, ())
    assert nxt.state.revision == retries + 2


def test_stale_submit_after_retry_is_not_counted():
    first = start()
    second = retry(first, {}, 2)
    refusal = submit(second.state, llm_sub(first, {}), ids(3))
    assert isinstance(refusal, Refusal) and refusal.code == "stale_action"
    # The refusal leaves the state as it was: still one retry issued.
    third = retry(second, {}, 4)
    assert third.state.attempts == 2


def test_zero_max_retries_refuses_at_once():
    first = start()
    refusal = submit(first.state, llm_sub(first, {}), ids(2), max_retries=0)
    assert isinstance(refusal, Refusal) and refusal.code == "invalid_output"
    assert refusal.message == (
        "llm_task result does not match archshell/llm-text/v1 "
        "and the retry limit (0) is used up"
    )


def test_errors_are_capped():
    many = [(f"$.f{i:02}" + "p" * 500, "m" * 500) for i in range(25)]
    first = start()
    nxt = on_submit(first.state, llm_sub(first, {}), ids(2), validate=lambda s, r: many)
    assert isinstance(nxt, Outcome)
    assert len(nxt.state.errors) == workflow.MAX_REPORTED_ERRORS
    assert all(
        len(p) == len(m) == workflow.MAX_ERROR_MESSAGE_LENGTH for p, m in nxt.state.errors
    )
    assert CursorState.from_dict(nxt.state.to_dict()) == nxt.state


def test_validator_runs_only_for_llm_task_steps():
    calls = []

    def spy(schema_id, result):
        calls.append(schema_id)
        return validate(schema_id, result)

    outcome = start()
    n = 2
    while not outcome.state.ended:
        outcome = on_submit(outcome.state, good(outcome.action), ids(n), validate=spy)
        n += 1
    assert calls == ["archshell/llm-text/v1"]


def test_state_round_trip_with_attempts():
    second = retry(start(), {}, 2)
    data = second.state.to_dict()
    assert data["attempts"] == 1
    assert data["errors"] == [{"path": "$", "message": "'text' is a required property"}]
    assert CursorState.from_dict(data) == second.state


def test_state_without_attempts_reads_as_no_retry():
    data = start().state.to_dict()
    del data["attempts"], data["errors"]
    state = CursorState.from_dict(data)
    assert (state.attempts, state.errors) == (0, ())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(attempts=-1),
        lambda d: d.update(attempts=True),
        lambda d: d.update(attempts="1"),
        lambda d: d.update(attempts=0),
        lambda d: d.update(errors=[]),
        lambda d: d.update(errors="x"),
        lambda d: d.update(errors=[{"path": "$"}]),
        lambda d: d.update(errors=[{"path": "$", "message": 1}]),
        lambda d: d.update(errors=[{"path": "$", "message": "m"}] * 11),
        lambda d: d.pop("errors"),
    ],
)
def test_retry_state_from_dict_rejects_invalid(mutate):
    data = retry(start(), {}, 2).state.to_dict()
    mutate(data)
    with pytest.raises(StateError):
        CursorState.from_dict(data)


def test_attempts_on_a_step_without_schema_is_invalid_state():
    data = ask_state().state.to_dict()
    data.update(attempts=1, errors=[{"path": "$", "message": "m"}])
    with pytest.raises(StateError):
        CursorState.from_dict(data)
