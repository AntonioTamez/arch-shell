"""Message catalogs and `--lang` (story 1.9): the I/O matrix, catalog parity,
and the keys the code uses."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

import archshell
from archshell.adapters import catalogs
from archshell.domain import messages, workflow
from archshell.domain.messages import LANGUAGES, Message, MessageError, msg
from archshell.engine import cli

EN = catalogs.load("en")
ES = catalogs.load("es")
# Spanish templates that differ from English: none may reach a written file.
SPANISH_ONLY = sorted({ES[k] for k in ES if ES[k] != EN[k]}, key=len, reverse=True)


class Engine:
    """Runs the engine in-process against a project root."""

    def __init__(self, root: Path, capsys):
        self.root = root
        self.capsys = capsys

    def call(self, *argv: str, message: dict | None = None) -> tuple[int, dict]:
        stdin = io.BytesIO(json.dumps({} if message is None else message).encode("utf-8"))
        stdout = io.StringIO()
        code = cli.run(list(argv), stdin, stdout, self.root)
        lines = stdout.getvalue().splitlines()
        assert len(lines) == 1, lines
        return code, json.loads(lines[0])

    def next(self, feature: str = "probe", *lang: str) -> dict:
        code, reply = self.call("next", "--feature", feature, *lang)
        assert code == 0, reply
        return reply

    def submit(self, action: dict, *lang: str, **overrides) -> tuple[int, dict]:
        return self.call(
            "submit", "--feature", action["feature"], *lang,
            message=submission_for(action, **overrides),
        )

    def advance(self, action: dict, *lang: str) -> dict:
        code, reply = self.submit(action, *lang)
        assert code == 0, reply
        return reply

    def drive(self, feature: str, *lang: str) -> list[dict]:
        actions = [self.next(feature, *lang)]
        while actions[-1]["type"] not in ("done", "report"):
            actions.append(self.advance(actions[-1], *lang))
        return actions

    def written_text(self) -> str:
        return "\n".join(
            path.read_bytes().decode("utf-8")
            for path in sorted((self.root / ".archshell").rglob("*"))
            if path.is_file()
        )


def submission_for(action: dict, **overrides) -> dict:
    result = {"text": "ok"} if action["type"] == "llm_task" else {"answer": "yes"}
    message = {
        "schema": "archshell/submit/v1",
        "feature": action["feature"],
        "action_id": action["action_id"],
        "revision": action["revision"],
        "result": result,
    }
    if action["type"] in ("ask", "confirm"):
        message.update(nonce=action["nonce"], source="human_ui")
    message.update(overrides)
    return message


@pytest.fixture
def engine(tmp_path: Path, capsys) -> Engine:
    return Engine(tmp_path, capsys)


ES_ARG = ("--lang", "es")


def texts(action: dict) -> dict:
    keys = ("question", "header", "options", "instruction", "summary")
    return {k: action[k] for k in keys if k in action}


# --- I/O matrix -------------------------------------------------------------


def test_spanish_ask(engine):
    first = engine.next("probe", *ES_ARG)
    assert first["type"] == "llm_task"
    ask = engine.advance(first, *ES_ARG)
    assert texts(ask) == {
        "question": ES["flow.probe.ask.question"],
        "header": ES["flow.probe.ask.header"],
        "options": [ES["flow.probe.ask.yes"], ES["flow.probe.ask.no"]],
    }
    assert ask["question"] == "Prueba: ¿te llega el protocolo de acciones de ArchShell?"


ENGLISH_PROBE = [
    {},
    {
        "question": "Probe: is the ArchShell action protocol reaching you?",
        "header": "Probe",
        "options": ["Yes", "No"],
    },
    {
        "question": "Probe: confirm to continue the protocol probe.",
        "header": "Probe",
        "options": ["Confirm", "Decline"],
    },
    {"instruction": "Probe: no change is needed; acknowledge this instruction."},
    {"summary": "Probe finished: every action type was exchanged."},
]


@pytest.mark.parametrize("lang", [(), ("--lang", "en")], ids=["default", "en"])
def test_default_is_todays_english(engine, lang):
    actions = engine.drive("probe", *lang)
    assert [texts(a) for a in actions] == ENGLISH_PROBE
    report = engine.drive("probe-report", *lang)[-1]
    assert report["summary"] == "Probe report: the protocol stopped with a report action."


def test_spanish_report(engine):
    report = engine.drive("probe-report", *ES_ARG)[-1]
    assert report["type"] == "report"
    assert report["summary"] == ES["flow.probe_report.report"]
    assert report["summary"].startswith("Informe de la prueba")


def test_spanish_run_writes_only_english_files(engine, capsys):
    actions = engine.drive("probe", *ES_ARG)
    assert [a["type"] for a in actions] == [
        "llm_task", "ask", "confirm", "instruct_agent", "done"
    ]
    for action, english in zip(actions[1:], ENGLISH_PROBE[1:]):
        assert texts(action) != english  # the host saw Spanish
    prompt = engine.root.joinpath(*actions[0]["prompt_file"].split("/"))
    assert prompt.read_bytes() == workflow.FLOWS["probe"][0].text.encode("utf-8")
    state = json.loads((engine.root / ".archshell/state/probe.yaml").read_text("utf-8"))
    assert state["action"]["summary"] == ENGLISH_PROBE[-1]["summary"]
    written = engine.written_text()
    log = capsys.readouterr().err
    for spanish in SPANISH_ONLY:
        assert spanish not in written, spanish
        assert spanish not in log, spanish


def test_repeat_next_returns_the_stored_action_unchanged(engine):
    ask_en = engine.advance(engine.next("probe"))
    assert engine.next("probe", *ES_ARG) == ask_en
    confirm_es = engine.advance(ask_en, *ES_ARG)  # the switch applies from here on
    assert confirm_es["question"] == ES["flow.probe.confirm.question"]
    assert engine.next("probe") == confirm_es
    assert engine.next("probe", "--lang", "en") == confirm_es


@pytest.mark.parametrize(
    ("args", "message", "details"),
    [
        (("--lang", "fr"), "--lang must be one of: en, es",
         {"arguments": ["--lang", "fr"], "languages": ["en", "es"]}),
        (("--lang",), "--lang must be one of: en, es",
         {"arguments": ["--lang"], "languages": ["en", "es"]}),
        (("--lang", "ES"), "--lang must be one of: en, es",
         {"arguments": ["--lang", "ES"], "languages": ["en", "es"]}),
        (("--lang", "es", "--lang", "es"), "argumentos inesperados",
         {"arguments": ["--lang", "es"]}),
        (("--lang", "es", "extra"), "argumentos inesperados", {"arguments": ["extra"]}),
        (("extra",), "unexpected arguments", {"arguments": ["extra"]}),
    ],
)
@pytest.mark.parametrize("command", ["next", "submit"])
def test_bad_arguments_are_invalid_arguments(engine, command, args, message, details):
    code, reply = engine.call(command, "--feature", "probe", *args)
    assert code == cli.EXIT_PROTOCOL_ERROR
    assert reply == {
        "schema": "archshell/error/v1", "code": "invalid_arguments",
        "message": message, "details": details,
    }
    assert not (engine.root / ".archshell").exists()


def test_refusals_render_in_the_request_language(engine):
    first = engine.next("probe")
    engine.advance(first)
    code, reply = engine.submit(first, *ES_ARG)  # stale: already consumed
    assert code == cli.EXIT_PROTOCOL_ERROR
    assert reply["code"] == "stale_action"
    assert reply["message"] == ES["error.stale_action"]
    _, english = engine.submit(first)
    assert {**english, "message": None} == {**reply, "message": None}  # only prose differs
    assert english["message"] == "submission does not match the pending action"


def test_refusal_args_fill_the_spanish_template(engine):
    code, reply = engine.call("next", "--feature", "nope", *ES_ARG)
    assert (code, reply["code"]) == (cli.EXIT_PROTOCOL_ERROR, "unknown_feature")
    assert reply["message"] == "no hay flujo de trabajo para la funcionalidad: nope"
    assert reply["details"] == {"features": sorted(workflow.FLOWS)}


def test_errors_before_lang_is_parsed_are_english(engine):
    _, reply = engine.call("bogus", "--feature", "probe", *ES_ARG)
    assert reply["message"] == "unknown command: bogus"
    _, reply = engine.call("next", *ES_ARG)
    assert reply["message"] == "expected --feature <slug>"


def test_hook_errors_stay_english(engine):
    code, reply = engine.call("check-edit", "--lang", "es")
    assert code == cli.EXIT_PROTOCOL_ERROR
    assert reply["code"] == "invalid_arguments"
    assert reply["message"] == "check-edit takes only --host, each once with a value"


def test_module_form_probe_in_spanish(tmp_path):
    """Acceptance: the module-form engine, driven with `--lang es`."""
    def call(command: str, message: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "archshell.engine", command, "--feature", "probe", *ES_ARG],
            input=json.dumps(message).encode("utf-8"), capture_output=True,
            cwd=tmp_path, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout
        for spanish in SPANISH_ONLY:
            assert spanish not in proc.stderr.decode("utf-8")
        return json.loads(proc.stdout.decode("utf-8"))

    actions = [call("next", {})]
    while actions[-1]["type"] != "done":
        actions.append(call("submit", submission_for(actions[-1])))
    expected = [
        {},
        {"question": ES["flow.probe.ask.question"], "header": ES["flow.probe.ask.header"],
         "options": [ES["flow.probe.ask.yes"], ES["flow.probe.ask.no"]]},
        {"question": ES["flow.probe.confirm.question"],
         "header": ES["flow.probe.confirm.header"],
         "options": [ES["flow.probe.confirm.confirm"], ES["flow.probe.confirm.decline"]]},
        {"instruction": ES["flow.probe.instruct"]},
        {"summary": ES["flow.probe.done"]},
    ]
    assert [texts(a) for a in actions] == expected
    written = "\n".join(
        p.read_bytes().decode("utf-8") for p in (tmp_path / ".archshell").rglob("*") if p.is_file()
    )
    for spanish in SPANISH_ONLY:
        assert spanish not in written, spanish


# --- state -------------------------------------------------------------------


def test_state_keeps_the_issued_language_and_reads_legacy_state_as_english():
    outcome = workflow.on_next(
        None, "probe", workflow.FreshIds("id-1", "n-1"), render=catalogs.render, lang="es"
    )
    data = outcome.state.to_dict()
    assert data["lang"] == "es"
    assert workflow.CursorState.from_dict(data) == outcome.state
    del data["lang"]
    assert workflow.CursorState.from_dict(data).lang == "en"
    with pytest.raises(workflow.StateError):
        workflow.CursorState.from_dict({**data, "lang": "fr"})


# --- catalogs ----------------------------------------------------------------


def parity_problems(reference: dict, other: dict) -> list[str]:
    problems = [f"missing {k}" for k in sorted(set(reference) - set(other))]
    problems += [f"extra {k}" for k in sorted(set(other) - set(reference))]
    problems += [
        f"placeholders differ in {k}"
        for k in sorted(set(reference) & set(other))
        if messages.placeholders(reference[k]) != messages.placeholders(other[k])
    ]
    return problems


@pytest.mark.parametrize("lang", LANGUAGES)
def test_catalog_parity(lang):
    assert parity_problems(dict(EN), dict(catalogs.load(lang))) == []


def test_parity_catches_a_missing_key_and_a_renamed_placeholder():
    broken = dict(ES)
    del broken["error.locked"]
    broken["error.unknown_feature"] = "sin flujo: {slug}"
    assert parity_problems(dict(EN), broken) == [
        "missing error.locked", "placeholders differ in error.unknown_feature"
    ]


CODE_ROOT = Path(archshell.__file__).resolve().parent
KEY_LITERAL = re.compile(r"""["']((?:flow|error)\.[a-z0-9_.]+)["']""")


def keys_in_code() -> set[str]:
    return {
        key
        for path in CODE_ROOT.rglob("*.py")
        for key in KEY_LITERAL.findall(path.read_text("utf-8"))
    }


def test_every_key_used_in_code_exists_and_every_entry_is_used():
    used = keys_in_code()
    assert used - set(EN) == set()
    assert set(EN) - used == set()


def test_every_step_renders_within_the_action_limits():
    for lang in LANGUAGES:
        for flow in workflow.FLOWS.values():
            for step in flow:
                if step.type in ("ask", "confirm"):
                    header = catalogs.render(Message(step.header), lang)
                    assert 0 < len(header) <= 12
                    labels = [catalogs.render(Message(k), lang) for k in step.options]
                    assert len(set(labels)) == len(labels)


def test_catalogs_are_nfc():
    for lang in LANGUAGES:
        for template in catalogs.load(lang).values():
            assert unicodedata.is_normalized("NFC", template)


def test_parse_normalizes_to_nfc():
    decomposed = unicodedata.normalize("NFD", "acción")
    parsed = catalogs.parse(
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "es", "messages": {"a.b": decomposed}},
        "es", "test",
    )
    assert parsed["a.b"] == "acción" and parsed["a.b"] != decomposed


def test_unknown_language_is_a_typed_error():
    with pytest.raises(catalogs.UnknownLanguage):
        catalogs.load("fr")


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"schema": "x", "lang": "es", "messages": {"a.b": "t"}},
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "en", "messages": {"a.b": "t"}},
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "es", "messages": {}},
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "es", "messages": {"Bad Key": "t"}},
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "es", "messages": {"a.b": 1}},
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "es", "messages": {"a.b": "{x"}},
        {"schema": catalogs.CATALOG_SCHEMA, "lang": "es", "messages": {"a.b": "t"}, "extra": 1},
    ],
)
def test_malformed_catalog_is_a_typed_error(data):
    with pytest.raises(catalogs.CatalogError):
        catalogs.parse(data, "es", "test")


def test_render_refuses_missing_keys_and_mismatched_args():
    catalog = {"a.b": "hola {name}"}
    assert messages.render(catalog, msg("a.b", name="Ana")) == "hola Ana"
    for message in (msg("a.c"), msg("a.b"), msg("a.b", name="x", other="y")):
        with pytest.raises(MessageError):
            messages.render(catalog, message)
