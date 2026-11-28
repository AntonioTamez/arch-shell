"""The `bootstrap` selection tracer (story 2.1): from a request to `architecture.yaml`.

A `ScriptedHost` (conftest) follows `commands/init.md` (the `run.md` loop with
the feature fixed to `bootstrap`) with scripted replies: the developer's typed
request, the agent's drivers JSON, and the confirmation. The engine runs as a
subprocess through `tests/pinned_engine.py`, the real CLI with fixed version
pins, so the written files are byte-checked against the `bootstrap/...`
entries of `golden/manifest.sha256` on every OS.

One test per I/O matrix row, then the state, journal, and refusal details.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest
from conftest import ScriptedHost, tree

from archshell.adapters import catalogs, extensions, journal, yaml_read
from archshell.domain import charter, layout, scoring, workflow
from archshell.engine import cli

PINNED_ENGINE = [sys.executable, str(Path(__file__).parent / "pinned_engine.py")]
GOLDEN_MANIFEST = Path(__file__).parent / "golden" / "manifest.sha256"
GOLDEN_PREFIX = "bootstrap/"
INIT = Path(__file__).resolve().parents[2] / "archshell-claude-code" / "commands" / "init.md"

EN = catalogs.load("en")
ES = catalogs.load("es")

REQUEST = (
    "A clinic system where staff schedule appointments, register patients,"
    " and bill for visits."
)
SPANISH_REQUEST = (
    "Un sistema de clínica donde el personal agenda citas, registra pacientes"
    " y factura las visitas."
)
THREE_MODULES = {
    "domain_complexity": "medium",
    "audit_needs": "basic",
    "scale": "unknown",
    "modules": [
        {"name": "Appointments", "capabilities": ["schedule appointments", "cancel appointments"]},
        {"name": "Patients", "capabilities": ["register patients"]},
        {"name": "Billing", "capabilities": ["bill visits"]},
    ],
}
ONE_MODULE = {"modules": [{"name": "Links", "capabilities": ["shorten a URL"]}]}

FLOW_TYPES = ["ask", "llm_task", "confirm", "done"]
BASE = ".archshell/charter/base.yaml"
ARCHITECTURE = "architecture.yaml"


def new_host(tmp_path: Path, lang: str = "en", name: str = "proj", **script) -> ScriptedHost:
    root = tmp_path / name
    root.mkdir()
    return ScriptedHost(
        root, lang, log_dir=tmp_path / "host-tmp", engine_argv=PINNED_ENGINE, **script
    )


def scripted(tmp_path: Path, drivers: dict, lang: str = "en", request: str = REQUEST,
             name: str = "proj", confirm: bool = True) -> ScriptedHost:
    return new_host(
        tmp_path, lang, name,
        answers=[request], agent_replies=[json.dumps(drivers)], confirmations=[confirm],
    )


def file_bytes(host: ScriptedHost, path: str) -> bytes:
    return host.root.joinpath(*path.split("/")).read_bytes()


def read_yaml(host: ScriptedHost, path: str) -> dict:
    data = yaml_read.load(file_bytes(host, path).decode("utf-8"))
    assert isinstance(data, dict)
    return data


def assert_refused(code: int, reply: dict, expected: str) -> None:
    assert code == 2, reply
    assert reply["schema"] == "archshell/error/v1"
    assert reply["code"] == expected, reply


def nothing_charter_written(host: ScriptedHost) -> None:
    assert not (host.root / ".archshell" / "charter").exists()
    assert not (host.root / ARCHITECTURE).exists()


# --- I/O matrix rows ---------------------------------------------------------


def test_clear_clean_screaming_request_writes_the_charter(tmp_path):
    host = scripted(tmp_path, THREE_MODULES)
    flow = host.run_flow("bootstrap", records=False)
    assert [a["type"] for a in flow] == FLOW_TYPES
    # The prompt carries the request; the confirm names the proposal.
    assert REQUEST.encode("utf-8") in host.prompts[0]
    assert flow[2]["question"] == EN["flow.bootstrap.confirm.question"].format(
        patterns="Clean + Screaming", modules="Appointments, Patients, Billing"
    )
    assert flow[3]["summary"] == EN["flow.bootstrap.done"].format(patterns="Clean + Screaming")
    base = read_yaml(host, BASE)
    assert base == {
        "schema": "archshell/charter/v1",
        "decision": "automatic",
        "drivers": {
            "audit_needs": "basic",
            "deployment_model": "unknown",
            "domain_complexity": "medium",
            "integrations": "unknown",
            "read_write_asymmetry": "unknown",
            "scale": "unknown",
            "team_size": "unknown",
        },
        "modules": THREE_MODULES["modules"],
        "patterns": ["clean", "screaming"],
        "request": REQUEST,
    }
    assert read_yaml(host, ARCHITECTURE) == {
        "language": "csharp",
        "modules": ["Appointments", "Patients", "Billing"],
        "patterns": ["clean", "screaming"],
        "versions": {"core": "0.0.0-golden", "packs": {"archshell-dotnet": "0.0.0-golden"}},
    }
    assert not (host.root / ".archshell" / "txn.json").exists()
    # The layout is created at the write.
    assert (host.root / ".archshell" / "config.yaml").is_file()
    assert (host.root / ".archshell" / ".gitignore").is_file()


def test_clear_request_bytes_match_committed_sha256(tmp_path):
    host = scripted(tmp_path, THREE_MODULES)
    host.run_flow("bootstrap", records=False)
    outputs = {
        f"{GOLDEN_PREFIX}base.yaml": file_bytes(host, BASE),
        f"{GOLDEN_PREFIX}architecture.yaml": file_bytes(host, ARCHITECTURE),
    }
    for data in outputs.values():
        assert b"\r" not in data and data.endswith(b"\n") and not data.endswith(b"\n\n")
        assert not data.startswith(b"\xef\xbb\xbf")
    actual = "".join(
        f"{hashlib.sha256(outputs[name]).hexdigest()}  {name}\n" for name in sorted(outputs)
    )
    lines = GOLDEN_MANIFEST.read_bytes().decode("utf-8").splitlines(keepends=True)
    expected = "".join(line for line in lines if line.split("  ", 1)[1].startswith(GOLDEN_PREFIX))
    assert actual == expected, f"bootstrap bytes differ; actual manifest lines:\n{actual}"


def test_single_module_request_proposes_clean(tmp_path):
    host = scripted(tmp_path, ONE_MODULE, request="A single-purpose URL shortener.")
    flow = host.run_flow("bootstrap", records=False)
    assert [a["type"] for a in flow] == FLOW_TYPES
    assert "Clean," in flow[2]["question"] and "Screaming" not in flow[2]["question"]
    assert read_yaml(host, BASE)["patterns"] == ["clean"]
    assert read_yaml(host, ARCHITECTURE)["patterns"] == ["clean"]


def test_unestablished_drivers_are_recorded_unknown(tmp_path):
    drivers = {name: "unknown" for name in charter.DRIVERS} | ONE_MODULE
    host = scripted(tmp_path, drivers)
    host.run_flow("bootstrap", records=False)
    assert read_yaml(host, BASE)["drivers"] == {name: "unknown" for name in charter.DRIVERS}


def test_declined_proposal_reports_and_writes_nothing(tmp_path):
    host = scripted(tmp_path, THREE_MODULES, confirm=False)
    flow = host.run_flow("bootstrap", records=False)
    assert [a["type"] for a in flow] == ["ask", "llm_task", "confirm", "report"]
    assert flow[-1]["summary"] == EN["flow.bootstrap.declined"]
    nothing_charter_written(host)
    assert not (host.root / ".archshell" / "config.yaml").exists()
    # Terminal: nothing is pending.
    code, reply = host.submit(host.submission(flow[-2], {"confirmed": True}))
    assert_refused(code, reply, "no_pending_action")


def test_invalid_drivers_are_retried_twice_then_refused(tmp_path):
    bad = [
        "Here are the drivers: none",  # not JSON
        json.dumps({"modules": [], "scale": "huge"}),  # fails the schema
        json.dumps({"modules": [{"name": "Billing", "capabilities": []}] * 2}),  # duplicate
    ]
    host = new_host(tmp_path, answers=[REQUEST], agent_replies=list(bad))
    ask = host.next("bootstrap")
    first = host.answer(ask)
    retry1 = host.answer(first)
    retry2 = host.answer(retry1)
    assert [a["type"] for a in (first, retry1, retry2)] == ["llm_task"] * 3
    assert [a["revision"] for a in (first, retry1, retry2)] == [2, 3, 4]
    code, reply = host.submit(host.submission(retry2))
    assert_refused(code, reply, "invalid_output")
    retry1_prompt, retry2_prompt = host.prompts[1].decode("utf-8"), host.prompts[2].decode("utf-8")
    assert "did not match archshell/drivers/v1" in retry1_prompt and "$.text" in retry1_prompt
    assert REQUEST in retry1_prompt
    assert "$.scale" in retry2_prompt and "$.modules" in retry2_prompt
    assert reply["details"]["output_schema"] == "archshell/drivers/v1"
    assert reply["details"]["errors"] == [
        {"path": "$.modules[1].name", "message": "duplicate module name 'Billing'"}
    ]
    nothing_charter_written(host)


def test_interrupted_earlier_run_refuses_at_next_and_writes_nothing(tmp_path):
    host = new_host(tmp_path)
    txn = host.root / ".archshell" / "txn.json"
    txn.parent.mkdir()
    txn.write_bytes(b"{}\n")
    before = tree(host.root)
    code, reply = host.engine("next", "bootstrap")
    assert_refused(code, reply, "bootstrap_interrupted")
    assert reply["details"] == {"journal": ".archshell/txn.json"}
    assert reply["message"] == EN["error.bootstrap_interrupted"].format(journal=".archshell/txn.json")
    assert tree(host.root) == before


def test_interrupted_run_refuses_the_confirmation_and_writes_nothing(tmp_path):
    host = scripted(tmp_path, THREE_MODULES)
    confirm = host.answer(host.answer(host.next("bootstrap")))
    assert confirm["type"] == "confirm"
    (host.root / ".archshell" / "txn.json").write_bytes(b"{}\n")
    code, reply = host.submit(host.submission(confirm))
    assert_refused(code, reply, "bootstrap_interrupted")
    nothing_charter_written(host)


def test_no_language_pack_refuses_the_write(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    monkeypatch.setattr(extensions, "language_packs", lambda found=None: {})
    confirm = engine.to_confirm()
    code, reply = engine.submit(confirm, {"confirmed": True})
    assert_refused(code, reply, "language_pack_missing")
    assert reply["details"] == {"group": "archshell.language_packs"}
    assert not (engine.root / ".archshell" / "charter").exists()
    assert not (engine.root / ARCHITECTURE).exists()
    # The confirmation is still pending: the state did not advance.
    state = json.loads((engine.root / ".archshell/state/bootstrap.yaml").read_bytes())
    assert state["action"]["type"] == "confirm"


def test_spanish_run_renders_es_and_writes_the_en_bytes(tmp_path):
    es = scripted(tmp_path, THREE_MODULES, lang="es", request=SPANISH_REQUEST, name="es")
    en = scripted(tmp_path, THREE_MODULES, lang="en", request=SPANISH_REQUEST, name="en")
    es_flow = es.run_flow("bootstrap", records=False)
    en.run_flow("bootstrap", records=False)
    assert es_flow[0]["question"] == ES["flow.bootstrap.ask.question"]
    assert es_flow[0]["header"] == ES["flow.bootstrap.ask.header"]
    assert es_flow[0]["options"] == [
        ES["flow.bootstrap.ask.example_modules"], ES["flow.bootstrap.ask.example_single"]
    ]
    assert es_flow[2]["question"] == ES["flow.bootstrap.confirm.question"].format(
        patterns="Clean + Screaming", modules="Appointments, Patients, Billing"
    )
    assert es_flow[2]["options"] == [
        ES["flow.bootstrap.confirm.accept"], ES["flow.bootstrap.confirm.decline"]
    ]
    assert es_flow[3]["summary"] == ES["flow.bootstrap.done"].format(patterns="Clean + Screaming")
    for path in (BASE, ARCHITECTURE):
        assert file_bytes(es, path) == file_bytes(en, path), path
    # English apart from the verbatim request; module names are ASCII.
    architecture = file_bytes(es, ARCHITECTURE)
    assert architecture.isascii()
    spanish_only = {ES[k] for k in ES if ES[k] != EN[k] and k.startswith("flow.bootstrap.")}
    base_text = file_bytes(es, BASE).decode("utf-8")
    assert not any(text in base_text for text in spanish_only)
    assert read_yaml(es, BASE)["request"] == SPANISH_REQUEST


def test_spanish_decline_reports_in_es_and_writes_nothing(tmp_path):
    host = scripted(tmp_path, THREE_MODULES, lang="es", request=SPANISH_REQUEST, confirm=False)
    flow = host.run_flow("bootstrap", records=False)
    assert [a["type"] for a in flow] == ["ask", "llm_task", "confirm", "report"]
    assert flow[-1]["summary"] == ES["flow.bootstrap.declined"]
    nothing_charter_written(host)


# --- in-process engine (monkeypatched extensions) -----------------------------


class InProcess:
    """The engine CLI run in-process, so a test can replace the pack loader."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path / "proj"
        self.root.mkdir()
        from archshell.adapters import versions

        monkeypatch.setattr(versions, "pins", lambda pack: ("1", {pack.distribution: "1"}))

    def call(self, *argv: str, message: dict | None = None) -> tuple[int, dict]:
        stdin = io.BytesIO(json.dumps({} if message is None else message).encode("utf-8"))
        stdout = io.StringIO()
        code = cli.run(list(argv), stdin, stdout, self.root)
        return code, json.loads(stdout.getvalue())

    def submit(self, action: dict, result: dict) -> tuple[int, dict]:
        message = {
            "schema": "archshell/submit/v1", "feature": "bootstrap",
            "action_id": action["action_id"], "revision": action["revision"], "result": result,
        }
        if action["type"] in ("ask", "confirm"):
            message |= {"nonce": action["nonce"], "source": "human_ui"}
        return self.call("submit", "--feature", "bootstrap", message=message)

    def advance(self, action: dict, result: dict) -> dict:
        code, reply = self.submit(action, result)
        assert code == 0, reply
        return reply

    def to_confirm(self, drivers: dict = THREE_MODULES) -> dict:
        code, ask = self.call("next", "--feature", "bootstrap")
        assert code == 0, ask
        task = self.advance(ask, {"answer": REQUEST})
        confirm = self.advance(task, {"text": json.dumps(drivers)})
        assert confirm["type"] == "confirm", confirm
        return confirm


class FakeEntryPoint:
    def __init__(self, name: str, obj, dist: str):
        self.name = name
        self.obj = obj
        self.dist = type("Dist", (), {"name": dist})()

    def load(self):
        return self.obj


class Pack:
    extension_contract = "archshell/language-pack-ext/v1"
    language = "csharp"


def test_a_refused_pack_reaches_error_v1_with_its_registered_code(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    real = extensions.language_packs
    fake = [FakeEntryPoint("one", Pack(), "dist-one"), FakeEntryPoint("two", Pack(), "dist-two")]
    monkeypatch.setattr(extensions, "language_packs", lambda found=None: real(found=fake))
    code, reply = engine.submit(engine.to_confirm(), {"confirmed": True})
    assert_refused(code, reply, "duplicate_language")
    assert reply["details"]["language"] == "csharp"
    assert reply["message"] == EN["error.duplicate_language"].format(language="csharp")
    assert not (engine.root / ARCHITECTURE).exists()


def test_blank_request_and_non_boolean_confirmation_are_refused(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    code, ask = engine.call("next", "--feature", "bootstrap")
    code, reply = engine.submit(ask, {"answer": "  "})
    assert_refused(code, reply, "invalid_message")
    assert reply["message"] == EN["error.answer_not_text"]
    confirm = engine.to_confirm()
    code, reply = engine.submit(confirm, {"confirmed": "yes"})
    assert_refused(code, reply, "invalid_message")
    assert reply["message"] == EN["error.confirmed_not_boolean"]
    assert not (engine.root / ARCHITECTURE).exists()


# --- state and journal ---------------------------------------------------------


def test_results_are_kept_in_state_for_later_steps(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    engine.to_confirm()
    state = json.loads((engine.root / ".archshell/state/bootstrap.yaml").read_bytes())
    assert state["results"] == {
        "request": {"answer": REQUEST},
        "drivers": THREE_MODULES,
        "proposal": {
            "card": "clean-screaming", "title": "Clean + Screaming",
            "patterns": ["clean", "screaming"],
        },
    }


def test_state_missing_an_earlier_result_is_invalid_state(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    engine.to_confirm()
    path = engine.root / ".archshell/state/bootstrap.yaml"
    data = json.loads(path.read_bytes())
    for missing in ("drivers", "proposal"):
        broken = {**data, "results": {k: v for k, v in data["results"].items() if k != missing}}
        with pytest.raises(workflow.StateError):
            workflow.CursorState.from_dict(broken)
    data["results"] = {"request": data["results"]["request"]}
    path.write_text(json.dumps(data), encoding="utf-8")
    code, reply = engine.call("next", "--feature", "bootstrap")
    assert_refused(code, reply, "invalid_state")


def test_confirm_and_write_use_the_kept_proposal_not_a_rescoring(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    confirm = engine.to_confirm()
    assert "Clean + Screaming" in confirm["question"]
    other = scoring.Proposal("clean", "Clean", ("clean",), ())
    monkeypatch.setattr(scoring, "propose", lambda drivers, cards=scoring.CARDS: other)
    code, again = engine.call("next", "--feature", "bootstrap")
    assert again["question"] == confirm["question"]
    done = engine.advance(confirm, {"confirmed": True})
    assert "Clean + Screaming" in done["summary"]
    base = yaml_read.load((engine.root / BASE).read_bytes().decode("utf-8"))
    assert base["patterns"] == ["clean", "screaming"]


def test_state_without_results_still_loads(tmp_path, monkeypatch):
    engine = InProcess(tmp_path, monkeypatch)
    code, ask = engine.call("next", "--feature", "bootstrap")
    data = json.loads((engine.root / ".archshell/state/bootstrap.yaml").read_bytes())
    assert "results" not in data  # nothing kept yet, so the field is not written
    assert workflow.CursorState.from_dict(data).results == {}
    with pytest.raises(workflow.StateError):
        workflow.CursorState.from_dict({**data, "results": {"drivers": {}}})  # no earlier step
    with pytest.raises(workflow.StateError):
        workflow.CursorState.from_dict({**data, "results": []})


def test_journal_records_plan_and_backups_then_is_deleted(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".archshell").mkdir(parents=True)
    (root / "architecture.yaml").write_bytes(b"old\n")
    writes = [journal.PlannedWrite("architecture.yaml", "new"), journal.PlannedWrite("a/b.yaml", "x")]
    document = journal.plan(root, writes)
    assert document["schema"] == layout.TXN_SCHEMA
    assert [(w["path"], w["backup"]) for w in document["writes"]] == [
        ("architecture.yaml", "b2xkCg=="), ("a/b.yaml", None)
    ]
    assert document["writes"][0]["sha256"] == hashlib.sha256(b"new\n").hexdigest()
    seen = []
    real_write = journal.write_text

    def spy(path, text):
        seen.append(json.loads(journal.journal_path(root).read_bytes()))
        return real_write(path, text)

    monkeypatch.setattr(journal, "write_text", spy)
    journal.apply(root, writes)
    assert seen == [document, document]  # the journal is on disk during every write
    assert (root / "architecture.yaml").read_bytes() == b"new\n"
    assert not journal.present(root)


def test_journal_refuses_when_present_and_writes_nothing(tmp_path):
    root = tmp_path / "proj"
    (root / ".archshell").mkdir(parents=True)
    (root / ".archshell" / "txn.json").write_bytes(b"{}\n")
    with pytest.raises(journal.JournalPresent):
        journal.apply(root, [journal.PlannedWrite("architecture.yaml", "x")])
    assert not (root / "architecture.yaml").exists()


def test_init_command_drives_the_bootstrap_feature():
    body = INIT.read_bytes().decode("utf-8")
    assert "next --feature 'bootstrap' --lang <lang>" in body
    assert "submit --feature 'bootstrap' --lang <lang>" in body
    assert "$ARGUMENTS" not in body
