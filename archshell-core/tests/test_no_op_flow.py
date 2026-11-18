"""End-to-end no-op flow (story 1.13): the whole platform contract in one run.

A `ScriptedHost` (conftest) stands in for Claude Code and the model. It reads
nothing but the plugin's contract files: it answers each action the way
`commands/run.md` says, and runs the literal `hooks/hooks.json` command strings
through bash. The engine runs as a subprocess through its public CLI. CI runs
this module on Windows, Linux, and macOS.

One test per I/O matrix row, the file invariants, and a byte check of the
id-independent output of the `es` run against `golden/manifest.sha256`
(entries named `no-op/...`; `test_golden.py` checks the others).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest
from conftest import ScriptedHost, stop_payload, tool_payload

from archshell.adapters import catalogs
from archshell.domain.actions import ACTION_TYPES

GOLDEN_MANIFEST = Path(__file__).parent / "golden" / "manifest.sha256"
GOLDEN_PREFIX = "no-op/"

PROBE_TYPES = ["llm_task", "ask", "confirm", "instruct_agent", "done"]
PROBE_REPORT_TYPES = ["llm_task", "report"]

EN = catalogs.load("en")
ES = catalogs.load("es")
# Spanish templates that differ from English: none may reach a written file.
SPANISH_ONLY = sorted({ES[k] for k in ES if ES[k] != EN[k]}, key=len, reverse=True)


def new_host(tmp_path: Path, lang: str) -> ScriptedHost:
    root = tmp_path / "proj"
    root.mkdir()
    return ScriptedHost(root, lang, log_dir=tmp_path / "host-tmp")


def full_run(host: ScriptedHost) -> tuple[list[dict], list[dict]]:
    return host.run_flow("probe"), host.run_flow("probe-report")


def assert_refused(code: int, reply: dict, expected: str) -> None:
    assert code == 2, reply
    assert reply["schema"] == "archshell/error/v1"
    assert reply["code"] == expected, reply


def state_bytes(host: ScriptedHost, feature: str = "probe") -> bytes:
    return (host.root / ".archshell" / "state" / f"{feature}.yaml").read_bytes()


def prompt_bytes(host: ScriptedHost, action: dict) -> bytes:
    return host.root.joinpath(*action["prompt_file"].split("/")).read_bytes()


@pytest.fixture(params=["en", "es"])
def lang(request) -> str:
    return request.param


# --- matrix rows -------------------------------------------------------------


def test_full_no_op_flow_en(tmp_path):
    host = new_host(tmp_path, "en")
    probe, report = full_run(host)
    assert [a["type"] for a in probe] == PROBE_TYPES
    assert [a["type"] for a in report] == PROBE_REPORT_TYPES
    assert {a["type"] for a in probe + report} == set(ACTION_TYPES)
    assert [a["revision"] for a in probe] == [1, 2, 3, 4, 5]
    assert probe[-1]["summary"] == EN["flow.probe.done"]
    assert report[-1]["summary"] == EN["flow.probe_report.report"]
    # Terminal: nothing is pending, so a further submit is refused.
    code, reply = host.submit(host.submission(report[0], result={"text": "late"}, feature="probe-report"))
    assert code == 2 and reply["schema"] == "archshell/error/v1"
    assert reply["code"] == "no_pending_action", reply
    # Questions carried the nonce and the human source; nothing else did.
    asked = [s for s in host.submissions if "nonce" in s]
    assert [s["source"] for s in asked] == ["human_ui", "human_ui"]
    assert [s["result"] for s in asked] == [{"answer": EN["flow.probe.ask.yes"]}, {"confirmed": True}]


def test_full_no_op_flow_es(tmp_path):
    host = new_host(tmp_path, "es")
    probe, report = full_run(host)
    assert [a["type"] for a in probe] == PROBE_TYPES
    assert [a["type"] for a in report] == PROBE_REPORT_TYPES
    ask, confirm, instruct, done = probe[1:]
    assert (ask["question"], ask["header"], ask["options"]) == (
        ES["flow.probe.ask.question"], ES["flow.probe.ask.header"],
        [ES["flow.probe.ask.yes"], ES["flow.probe.ask.no"]],
    )
    assert (confirm["question"], confirm["header"], confirm["options"]) == (
        ES["flow.probe.confirm.question"], ES["flow.probe.confirm.header"],
        [ES["flow.probe.confirm.confirm"], ES["flow.probe.confirm.decline"]],
    )
    assert instruct["instruction"] == ES["flow.probe.instruct"]
    assert done["summary"] == ES["flow.probe.done"]
    assert report[-1]["summary"] == ES["flow.probe_report.report"]
    # Every written file stays English: state, prompts, and records.
    text = "\n".join(data.decode("utf-8") for data in host.written.values())
    for spanish in SPANISH_ONLY:
        assert spanish not in text, spanish
    assert json.loads(state_bytes(host))["action"]["summary"] == EN["flow.probe.done"]


def test_hooks_from_hooks_json(tmp_path):
    host = new_host(tmp_path, "en")
    host.run_flow("probe")
    # PreToolUse on a protected path: the engine denies it (Claude Code exit 0 + deny).
    pre, post = host.write("Write", host.root / ".archshell" / "test.txt")
    assert pre.code == 0 and post is None
    decision = json.loads(pre.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert ".archshell/test.txt is engine-owned" in decision["permissionDecisionReason"]
    # Ordinary writes and notebook edits: allowed, silent, then a silent check-edit.
    for tool, path in (("Edit", "src/app.py"), ("NotebookEdit", "nb/a.ipynb")):
        pre, post = host.write(tool, host.root.joinpath(*path.split("/")))
        assert (pre.code, pre.stdout) == (0, "")
        assert post is not None and (post.code, post.stdout) == (0, "")
    stop = host.hook("Stop", stop_payload(host.root, active=True))
    assert (stop.code, stop.stdout) == (0, "")
    # The engine cannot run: guard-write fails closed with exit 2 (the write is
    # blocked); the other hooks do not, so a broken engine never traps the agent.
    broken = "archshell-engine-that-does-not-exist"
    ordinary = str(host.root / "src" / "app.py")
    pre = host.hook("PreToolUse", tool_payload(host.root, "Write", ordinary), engine=broken)
    assert pre.code == 2 and "guard-write could not run" in pre.stderr
    post = host.hook("PostToolUse", tool_payload(host.root, "Write", ordinary, "PostToolUse"), engine=broken)
    assert post.code != 2
    assert host.hook("Stop", stop_payload(host.root), engine=broken).code != 2
    # Engine stderr went to the log under TMPDIR, never into the project.
    log = (host.log_dir / "archshell-engine.log").read_bytes().decode("utf-8", "replace")
    assert broken in log


def test_stale_and_replayed_submissions_are_refused(tmp_path, lang):
    host = new_host(tmp_path, lang)
    llm = host.next("probe")
    ask = host.answer(llm)
    replay = host.submission(ask)
    confirm = host.answer(ask)
    before = state_bytes(host)
    # An old revision, and the answered ask replayed with its used nonce.
    # The answered llm_task's prompt file is gone, so the replay carries its own text.
    assert_refused(*host.submit(host.submission(llm, result={"text": "ok"})), "stale_action")
    assert_refused(*host.submit(replay), "stale_action")
    assert_refused(*host.submit(host.submission(confirm, revision=ask["revision"])), "stale_action")
    # The current confirm with the ask's nonce, or without the human source.
    assert_refused(*host.submit(host.submission(confirm, nonce=ask["nonce"])), "nonce_mismatch")
    assert_refused(*host.submit(host.submission(confirm, source="agent")), "invalid_source")
    assert state_bytes(host) == before
    # The flow continues from the stored state.
    assert host.next("probe") == confirm
    assert [a["type"] for a in host.finish(confirm)] == ["confirm", "instruct_agent", "done"]


def test_invalid_llm_output_is_retried_twice_then_reported(tmp_path, lang):
    host = new_host(tmp_path, lang)
    first = host.next("probe")
    bad = {"text": ""}
    code, retry1 = host.submit(host.submission(first, result=bad))
    assert code == 0 and retry1["type"] == "llm_task" and retry1["revision"] == 2
    code, retry2 = host.submit(host.submission(retry1, result=bad))
    assert code == 0 and retry2["type"] == "llm_task" and retry2["revision"] == 3
    assert len({first["action_id"], retry1["action_id"], retry2["action_id"]}) == 3
    retry_prompt = prompt_bytes(host, retry2).decode("utf-8")
    assert "did not match archshell/llm-text/v1" in retry_prompt
    before = state_bytes(host)
    code, reply = host.submit(host.submission(retry2, result=bad))
    assert_refused(code, reply, "invalid_output")
    assert reply["details"]["output_schema"] == "archshell/llm-text/v1"
    assert reply["details"]["error_count"] >= 1
    assert state_bytes(host) == before
    # Reported, not crashed: a rerun's `next` restarts the step with fresh retries.
    restarted = host.next("probe")
    assert restarted["type"] == "llm_task" and restarted["revision"] == 4
    assert "did not match" not in prompt_bytes(host, restarted).decode("utf-8")
    assert [a["type"] for a in host.finish(restarted)] == PROBE_TYPES


def test_record_written_twice_keeps_the_first_bytes(tmp_path, lang):
    host = new_host(tmp_path, lang)
    host.run_flow("probe")
    first, second = host.records
    assert first.path == ".archshell/records/probe/0001-plan.yaml"
    assert second.path == ".archshell/records/probe/0002-plan.yaml"
    assert first.document["content"] == second.document["content"] == {
        "flow": "probe", "actions": PROBE_TYPES,
    }
    assert first.document["fingerprint"] == second.document["fingerprint"]
    # `probe-report` ends in `report`, not a confirmation: no record.
    host.run_flow("probe-report")
    assert not (host.root / ".archshell" / "records" / "probe-report").exists()


# --- file invariants ---------------------------------------------------------


def test_every_written_file_is_normalized(tmp_path, lang):
    host = new_host(tmp_path, lang)
    full_run(host)
    assert host.written
    for path, data in host.written.items():
        assert b"\r" not in data, path
        assert not data.startswith(b"\xef\xbb\xbf"), path
        text = data.decode("utf-8")
        assert unicodedata.normalize("NFC", text) == text, path
        if lang == "es":
            assert text.isascii(), path


# --- cross-OS bytes ----------------------------------------------------------


def no_op_outputs(host: ScriptedHost, probe: list[dict], report: list[dict]) -> dict[str, bytes]:
    """The `es` run's output that does not depend on random ids or the clock."""
    return {
        # Prompt files are deleted once answered; the host saw them while pending.
        "no-op/es/prompt-probe.md": host.written[probe[0]["prompt_file"]],
        "no-op/es/prompt-probe-report.md": host.written[report[0]["prompt_file"]],
        **{
            f"no-op/es/{record.path.rsplit('/', 1)[1]}": host.written[record.path]
            for record in host.records
        },
        "no-op/es/done.txt": probe[-1]["summary"].encode("utf-8"),
        "no-op/es/report.txt": report[-1]["summary"].encode("utf-8"),
    }


def test_es_no_op_output_matches_committed_sha256(tmp_path):
    host = new_host(tmp_path, "es")
    outputs = no_op_outputs(host, *full_run(host))
    actual = "".join(
        f"{hashlib.sha256(outputs[name]).hexdigest()}  {name}\n" for name in sorted(outputs)
    )
    lines = GOLDEN_MANIFEST.read_bytes().decode("utf-8").splitlines(keepends=True)
    expected = "".join(line for line in lines if line.split("  ", 1)[1].startswith(GOLDEN_PREFIX))
    assert actual == expected, f"no-op bytes differ; actual no-op manifest lines:\n{actual}"
