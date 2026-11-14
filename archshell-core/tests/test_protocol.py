"""Subprocess tests for the action protocol I/O matrix (story 1.2).

Each test runs the engine with a temporary project root as its working
directory and asserts the stdout message, the exit code, and the state file
bytes (unchanged on every refusal).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from archshell.domain.workflow import FLOWS

ENGINE = [sys.executable, "-m", "archshell.engine"]


class Project:
    def __init__(self, root: Path):
        self.root = root

    def state_path(self, feature: str = "probe") -> Path:
        return self.root / ".archshell" / "state" / f"{feature}.yaml"

    def prompt_path(self, action: dict) -> Path:
        return self.root.joinpath(*action["prompt_file"].split("/"))

    def lock_path(self, feature: str = "probe") -> Path:
        return self.root / ".archshell" / "locks" / f"{feature}.lock"

    def state_bytes(self, feature: str = "probe") -> bytes | None:
        path = self.state_path(feature)
        return path.read_bytes() if path.exists() else None

    def engine(self, *argv: str, stdin: bytes = b"{}") -> tuple[int, dict]:
        proc = subprocess.run(
            ENGINE + list(argv), input=stdin, capture_output=True, cwd=self.root, timeout=60
        )
        text = proc.stdout.decode("utf-8")
        assert text.endswith("\n") and len(text.splitlines()) == 1, (text, proc.stderr)
        return proc.returncode, json.loads(text)

    def next(self, feature: str = "probe") -> tuple[int, dict]:
        return self.engine("next", "--feature", feature)

    def submit(self, message: dict, feature: str = "probe") -> tuple[int, dict]:
        return self.engine(
            "submit", "--feature", feature, stdin=json.dumps(message).encode("utf-8")
        )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


def submission_for(action: dict, **overrides) -> dict:
    message = {
        "schema": "archshell/submit/v1",
        "feature": action["feature"],
        "action_id": action["action_id"],
        "revision": action["revision"],
        "result": {"text": "ok"} if action["type"] == "llm_task" else {"answer": "yes"},
    }
    if action["type"] in ("ask", "confirm"):
        message["nonce"] = action["nonce"]
        message["source"] = "human_ui"
    message.update(overrides)
    return {k: v for k, v in message.items() if v is not None}


def assert_ok(code: int, msg: dict) -> dict:
    assert code == 0, msg
    assert msg["schema"] == "archshell/action/v1"
    return msg


def assert_refused(code: int, msg: dict, expected: str) -> None:
    assert code == 2, msg
    assert msg["schema"] == "archshell/error/v1"
    assert msg["code"] == expected
    assert set(msg) == {"schema", "code", "message", "details"}
    assert isinstance(msg["message"], str) and msg["message"]


def advance_to(project: Project, action_type: str, feature: str = "probe") -> dict:
    action = assert_ok(*project.next(feature))
    while action["type"] != action_type:
        action = assert_ok(*project.submit(submission_for(action), feature))
    return action


# --- matrix rows -------------------------------------------------------------


def test_first_next_issues_first_action_and_writes_state(project):
    assert project.state_bytes() is None
    action = assert_ok(*project.next())
    assert action["type"] == "llm_task"
    assert action["feature"] == "probe"
    assert action["revision"] == 1
    assert isinstance(action["action_id"], str) and action["action_id"]
    state = json.loads(project.state_bytes())
    assert state["schema"] == "archshell/state/v1"
    assert state["revision"] == 1
    assert state["action"] == action
    assert not project.lock_path().exists()


def test_repeat_next_returns_same_action_without_writing(project):
    first = assert_ok(*project.next())
    before = project.state_bytes()
    again = assert_ok(*project.next())
    assert again == first
    assert project.state_bytes() == before


def test_good_submit_returns_next_action_and_bumps_revision(project):
    first = assert_ok(*project.next())
    second = assert_ok(*project.submit(submission_for(first)))
    assert second["type"] == "ask"
    assert second["revision"] == first["revision"] + 1
    assert second["action_id"] != first["action_id"]
    assert isinstance(second["nonce"], str) and second["nonce"]
    third = assert_ok(*project.submit(submission_for(second)))
    assert third["type"] == "confirm"
    assert third["revision"] == 3
    assert json.loads(project.state_bytes())["action"] == third


def test_stale_revision_is_refused(project):
    first = assert_ok(*project.next())
    second = assert_ok(*project.submit(submission_for(first)))
    before = project.state_bytes()
    stale = submission_for(second, revision=first["revision"])
    assert_refused(*project.submit(stale), "stale_action")
    assert project.state_bytes() == before


def test_unknown_action_id_is_refused(project):
    first = assert_ok(*project.next())
    before = project.state_bytes()
    wrong = submission_for(first, action_id="00000000-0000-4000-8000-000000000000")
    assert_refused(*project.submit(wrong), "stale_action")
    assert project.state_bytes() == before


def test_unknown_action_id_on_ask_is_refused(project):
    ask = advance_to(project, "ask")
    before = project.state_bytes()
    wrong = submission_for(ask, action_id="00000000-0000-4000-8000-000000000000")
    assert wrong["nonce"] == ask["nonce"]
    assert_refused(*project.submit(wrong), "stale_action")
    assert project.state_bytes() == before


@pytest.mark.parametrize("kind", ["ask", "confirm"])
@pytest.mark.parametrize("nonce", ["not-the-nonce", None])
def test_nonce_mismatch_is_refused(project, kind, nonce):
    action = advance_to(project, kind)
    before = project.state_bytes()
    message = submission_for(action)
    if nonce is None:
        del message["nonce"]
    else:
        message["nonce"] = nonce
    assert_refused(*project.submit(message), "nonce_mismatch")
    assert project.state_bytes() == before


@pytest.mark.parametrize("kind", ["ask", "confirm"])
def test_nonce_reuse_is_refused(project, kind):
    action = advance_to(project, kind)
    consumed = submission_for(action)
    assert_ok(*project.submit(consumed))
    before = project.state_bytes()
    assert_refused(*project.submit(consumed), "stale_action")
    assert project.state_bytes() == before


@pytest.mark.parametrize("kind", ["ask", "confirm"])
@pytest.mark.parametrize("source", ["agent", None])
def test_result_not_from_the_ui_is_refused(project, kind, source):
    action = advance_to(project, kind)
    before = project.state_bytes()
    message = submission_for(action)
    if source is None:
        del message["source"]
    else:
        message["source"] = source
    assert_refused(*project.submit(message), "invalid_source")
    assert project.state_bytes() == before


def test_submit_before_any_next_is_refused(project):
    message = submission_for(
        {"type": "llm_task", "feature": "probe", "action_id": "x", "revision": 1}
    )
    assert_refused(*project.submit(message), "no_pending_action")
    assert project.state_bytes() is None


def test_submit_after_done_is_refused(project):
    done = advance_to(project, "done")
    before = project.state_bytes()
    assert_refused(*project.submit(submission_for(done)), "no_pending_action")
    assert project.state_bytes() == before
    assert assert_ok(*project.next()) == done


@pytest.mark.parametrize("command", ["next", "submit"])
def test_held_lock_is_refused(project, command):
    first = assert_ok(*project.next())
    before = project.state_bytes()
    project.lock_path().write_bytes(b"12345\n")
    if command == "next":
        result = project.next()
    else:
        result = project.submit(submission_for(first))
    assert_refused(*result, "locked")
    assert project.state_bytes() == before
    assert project.lock_path().read_bytes() == b"12345\n"


def test_held_lock_on_first_next_writes_no_state(project):
    project.lock_path().parent.mkdir(parents=True)
    project.lock_path().write_bytes(b"")
    assert_refused(*project.next(), "locked")
    assert project.state_bytes() is None


@pytest.mark.parametrize("command", ["next", "submit"])
@pytest.mark.parametrize(
    "args",
    [
        ["--feature", "Not_A_Slug"],
        ["--feature", "-probe"],
        ["--feature", "probe-"],
        ["--feature", ""],
        ["--feature", "a" * 65],
        ["--feature"],
        [],
        ["probe"],
    ],
)
def test_bad_feature_is_refused(project, command, args):
    assert_refused(*project.engine(command, *args), "invalid_feature")
    assert not (project.root / ".archshell").exists()


def test_extra_arguments_are_refused(project):
    assert_refused(*project.engine("next", "--feature", "probe", "--x"), "invalid_arguments")
    assert not (project.root / ".archshell").exists()


# --- llm_task prompt files -------------------------------------------------


def test_llm_task_carries_prompt_file_holding_the_prompt_exactly(project):
    action = assert_ok(*project.next())
    assert action["type"] == "llm_task"
    assert set(action) == {"schema", "type", "feature", "action_id", "revision", "prompt_file"}
    assert action["prompt_file"] == f".archshell/state/prompts/{action['action_id']}.md"
    raw = project.prompt_path(action).read_bytes()
    assert raw == FLOWS["probe"][0].text.encode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw


def test_repeat_next_keeps_the_same_prompt_file(project):
    first = assert_ok(*project.next())
    path = project.prompt_path(first)
    before = path.read_bytes()
    again = assert_ok(*project.next())
    assert again["prompt_file"] == first["prompt_file"]
    assert path.read_bytes() == before


def test_repeat_next_restores_a_missing_prompt_file(project):
    first = assert_ok(*project.next())
    path = project.prompt_path(first)
    path.unlink()
    state = project.state_bytes()
    assert assert_ok(*project.next()) == first
    assert path.read_bytes() == FLOWS["probe"][0].text.encode("utf-8")
    assert project.state_bytes() == state


def test_prompt_file_stays_while_later_actions_are_pending(project):
    first = assert_ok(*project.next())
    ask = assert_ok(*project.submit(submission_for(first)))
    assert ask["type"] == "ask"
    assert project.prompt_path(first).exists()


def test_probe_report_llm_task_has_its_own_prompt_file(project):
    first = assert_ok(*project.next("probe-report"))
    assert project.prompt_path(first).read_bytes() == FLOWS["probe-report"][0].text.encode("utf-8")


def test_tampered_prompt_file_in_state_is_invalid_state(project):
    assert_ok(*project.next())
    state = json.loads(project.state_bytes())
    state["action"]["prompt_file"] = "../outside.md"
    tampered = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")
    project.state_path().write_bytes(tampered)
    assert_refused(*project.next(), "invalid_state")
    assert not (project.root.parent / "outside.md").exists()
    assert project.state_bytes() == tampered


# --- question text -----------------------------------------------------------


@pytest.mark.parametrize(
    ("action_type", "options"), [("ask", ["Yes", "No"]), ("confirm", ["Confirm", "Decline"])]
)
def test_questions_carry_engine_header_and_options(project, action_type, options):
    action = advance_to(project, action_type)
    assert set(action) == {
        "schema", "type", "feature", "action_id", "revision",
        "question", "header", "options", "nonce",
    }
    assert action["header"] == "Probe"
    assert action["options"] == options
    assert assert_ok(*project.next()) == action


def test_pending_question_without_options_in_state_is_invalid_state(project):
    advance_to(project, "ask")
    state = json.loads(project.state_bytes())
    del state["action"]["options"]
    tampered = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")
    project.state_path().write_bytes(tampered)
    assert_refused(*project.next(), "invalid_state")
    assert project.state_bytes() == tampered


# --- further refusals --------------------------------------------------------


def test_unknown_feature_is_refused(project):
    assert_refused(*project.next("some-feature"), "unknown_feature")
    assert not (project.root / ".archshell").exists()


def test_submit_unknown_feature_is_refused(project):
    message = submission_for(
        {"type": "llm_task", "feature": "some-feature", "action_id": "x", "revision": 1}
    )
    assert_refused(*project.submit(message, "some-feature"), "unknown_feature")
    assert not (project.root / ".archshell").exists()


def test_submit_feature_must_match_argument(project):
    first = assert_ok(*project.next())
    before = project.state_bytes()
    message = submission_for(first, feature="probe-report")
    assert_refused(*project.submit(message), "invalid_feature")
    assert project.state_bytes() == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "archshell/action/v1"},
        {"revision": "1"},
        {"revision": True},
        {"action_id": 7},
        {"result": "yes"},
        {"result": None},
        {"verdict": "pass"},
        {"anything": 1},
    ],
)
def test_malformed_submit_is_invalid_message(project, overrides):
    first = assert_ok(*project.next())
    before = project.state_bytes()
    message = submission_for(first)
    message.update(overrides)
    message = {k: v for k, v in message.items() if v is not None}
    assert_refused(*project.submit(message), "invalid_message")
    assert project.state_bytes() == before


def test_corrupt_state_is_invalid_state(project):
    assert_ok(*project.next())
    project.state_path().write_bytes(b"{not json")
    assert_refused(*project.next(), "invalid_state")
    assert project.state_bytes() == b"{not json"
    assert not project.lock_path().exists()


# --- llm_task output validation (story 1.5) ----------------------------------


def invalid_submit(action: dict, result: dict | None = None) -> dict:
    return submission_for(action, result={} if result is None else result)


def test_invalid_output_reissues_the_step_with_errors_in_the_prompt(project):
    first = assert_ok(*project.next())
    retry = assert_ok(*project.submit(invalid_submit(first, {"text": ""})))
    assert retry["type"] == "llm_task"
    assert retry["revision"] == first["revision"] + 1
    assert retry["action_id"] != first["action_id"]
    assert retry["prompt_file"] != first["prompt_file"]
    raw = project.prompt_path(retry).read_bytes()
    assert raw.decode("utf-8") == (
        FLOWS["probe"][0].text
        + "\n\nYour previous answer did not match archshell/llm-text/v1:\n"
        + "- $.text: '' should be non-empty\n"
    )
    state = json.loads(project.state_bytes())
    assert (state["step"], state["attempts"], state["action"]) == (0, 1, retry)


def test_repeat_next_during_retry_keeps_the_retry_prompt(project):
    first = assert_ok(*project.next())
    retry = assert_ok(*project.submit(invalid_submit(first)))
    path = project.prompt_path(retry)
    before, state = path.read_bytes(), project.state_bytes()
    path.unlink()
    assert assert_ok(*project.next()) == retry
    assert path.read_bytes() == before
    assert project.state_bytes() == state


def test_retries_exhausted_is_invalid_output_and_state_unchanged(project):
    action = assert_ok(*project.next())
    for _ in range(2):
        action = assert_ok(*project.submit(invalid_submit(action)))
        assert action["type"] == "llm_task"
    before = project.state_bytes()
    code, msg = project.submit(invalid_submit(action, {"text": ""}))
    assert_refused(code, msg, "invalid_output")
    assert msg["details"] == {
        "output_schema": "archshell/llm-text/v1",
        "error_count": 1,
        "errors": [{"path": "$.text", "message": "'' should be non-empty"}],
    }
    assert project.state_bytes() == before
    assert assert_ok(*project.next()) == action


def test_valid_output_after_retry_advances(project):
    first = assert_ok(*project.next())
    retry = assert_ok(*project.submit(invalid_submit(first)))
    ask = assert_ok(*project.submit(submission_for(retry)))
    assert ask["type"] == "ask" and ask["revision"] == 3
    state = json.loads(project.state_bytes())
    assert (state["step"], state["attempts"], state["errors"]) == (1, 0, [])


def test_stale_submit_after_retry_is_refused_and_not_counted(project):
    first = assert_ok(*project.next())
    retry = assert_ok(*project.submit(invalid_submit(first)))
    before = project.state_bytes()
    assert_refused(*project.submit(invalid_submit(first)), "stale_action")
    assert project.state_bytes() == before
    second = assert_ok(*project.submit(invalid_submit(retry)))
    assert second["type"] == "llm_task"


def test_ask_result_content_is_not_schema_checked(project):
    ask = advance_to(project, "ask")
    confirm = assert_ok(*project.submit(submission_for(ask, result={"anything": 1})))
    assert confirm["type"] == "confirm"


# --- acceptance criteria -----------------------------------------------------


def test_probe_flow_runs_every_action_type_to_done(project):
    action = assert_ok(*project.next())
    seen = [(action["type"], action["revision"])]
    while action["type"] != "done":
        action = assert_ok(*project.submit(submission_for(action)))
        seen.append((action["type"], action["revision"]))
    assert seen == [
        ("llm_task", 1),
        ("ask", 2),
        ("confirm", 3),
        ("instruct_agent", 4),
        ("done", 5),
    ]


def test_probe_report_flow_ends_with_report(project):
    first = assert_ok(*project.next("probe-report"))
    assert first["type"] == "llm_task"
    report = assert_ok(*project.submit(submission_for(first), "probe-report"))
    assert report["type"] == "report"
    assert report["revision"] == 2
    before = project.state_bytes("probe-report")
    assert_refused(*project.submit(submission_for(report), "probe-report"), "no_pending_action")
    assert project.state_bytes("probe-report") == before


def test_three_invalid_llm_task_results_end_in_invalid_output(project):
    action = assert_ok(*project.next())
    replies = []
    for _ in range(3):
        code, msg = project.submit(invalid_submit(action))
        replies.append(msg)
        if msg["schema"] == "archshell/action/v1":
            action = msg
    assert [m.get("type") for m in replies[:2]] == ["llm_task", "llm_task"]
    assert replies[2]["schema"] == "archshell/error/v1"
    assert_refused(code, replies[2], "invalid_output")


def test_state_file_is_sorted_key_json_with_lf(project):
    assert_ok(*project.next())
    raw = project.state_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    state = json.loads(raw)
    assert raw.decode("utf-8") == json.dumps(state, sort_keys=True, indent=2) + "\n"
