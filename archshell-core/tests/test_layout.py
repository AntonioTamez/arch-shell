"""Tests of the `.archshell/` layout and config loading (story 1.7, AD-10)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from archshell.adapters.layout import Config, InvalidConfig, create_layout, load_config
from archshell.domain import layout

ENGINE = [sys.executable, "-m", "archshell.engine"]

LAYOUT_PATHS = {
    ".gitignore",
    "config.yaml",
    "charter",
    "vocabulary",
    "features",
    "records",
    "derived",
    "state",
    "locks",
}
DEFAULT_CONFIG = b"schema: archshell/config/v1\nmax_retries: 2\n"


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under `root`, relative and POSIX, with file bytes (None for dirs)."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def engine(root: Path, *argv: str, stdin: dict | None = None) -> tuple[int, dict]:
    proc = subprocess.run(
        ENGINE + list(argv),
        input=json.dumps({} if stdin is None else stdin).encode("utf-8"),
        capture_output=True,
        cwd=root,
        timeout=60,
    )
    return proc.returncode, json.loads(proc.stdout.decode("utf-8"))


def invalid_submit(action: dict) -> dict:
    return {
        "schema": "archshell/submit/v1",
        "feature": action["feature"],
        "action_id": action["action_id"],
        "revision": action["revision"],
        "result": {},
    }


def write_config(root: Path, text: str) -> None:
    path = root / ".archshell" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def submit_invalid_until_refused(root: Path) -> list[dict]:
    """Submit invalid `llm_task` results until a refusal; return every reply."""
    code, action = engine(root, "next", "--feature", "probe")
    assert code == 0, action
    replies = []
    for _ in range(12):
        code, msg = engine(root, "submit", "--feature", "probe", stdin=invalid_submit(action))
        replies.append(msg)
        if code != 0:
            return replies
        action = msg
    raise AssertionError("never refused")


# --- matrix rows -------------------------------------------------------------


def test_fresh_project_gets_exactly_the_ad10_layout(tmp_path):
    create_layout(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {".archshell"}
    files = snapshot(tmp_path / ".archshell")
    assert set(files) == LAYOUT_PATHS
    assert files[".gitignore"] == b"state/\nlocks/\ntxn.json\n"
    assert files["config.yaml"] == DEFAULT_CONFIG
    assert {name for name, data in files.items() if data is None} == {
        d.removeprefix(".archshell/") for d in layout.DIRECTORIES
    }


def test_rerun_changes_no_bytes(tmp_path):
    create_layout(tmp_path)
    before = snapshot(tmp_path)
    create_layout(tmp_path)
    assert snapshot(tmp_path) == before


def test_edited_config_is_kept(tmp_path):
    write_config(tmp_path, "schema: archshell/config/v1\r\nmax_retries:   4\r\n")
    before = (tmp_path / ".archshell" / "config.yaml").read_bytes()
    create_layout(tmp_path)
    assert (tmp_path / ".archshell" / "config.yaml").read_bytes() == before
    assert load_config(tmp_path) == Config(max_retries=4)


def test_no_config_means_default_retries(tmp_path):
    assert load_config(tmp_path) == Config(max_retries=2)
    replies = submit_invalid_until_refused(tmp_path)
    assert [m.get("type") for m in replies] == ["llm_task", "llm_task", None]
    assert replies[-1]["code"] == "invalid_output"
    assert not (tmp_path / ".archshell" / "config.yaml").exists()


def test_config_override_zero_refuses_the_first_invalid_result(tmp_path):
    write_config(tmp_path, "schema: archshell/config/v1\nmax_retries: 0\n")
    replies = submit_invalid_until_refused(tmp_path)
    assert len(replies) == 1
    assert replies[0]["code"] == "invalid_output"
    assert "retry limit (0)" in replies[0]["message"]


def test_config_override_raises_the_retry_limit(tmp_path):
    write_config(tmp_path, "schema: archshell/config/v1\nmax_retries: 4\n")
    replies = submit_invalid_until_refused(tmp_path)
    assert len(replies) == 5 and replies[-1]["code"] == "invalid_output"


@pytest.mark.parametrize(
    ("text", "errors"),
    [
        (
            "schema: archshell/config/v1\nmax_retries: -1\n",
            [{"path": "$.max_retries", "message": "-1 is less than the minimum of 0"}],
        ),
        (
            "schema: archshell/config/v1\nmax_retries: 11\n",
            [{"path": "$.max_retries", "message": "11 is greater than the maximum of 10"}],
        ),
        (
            "schema: archshell/config/v1\nmax_retries: true\n",
            [{"path": "$.max_retries", "message": "True is not of type 'integer'"}],
        ),
        (
            "schema: archshell/config/v1\nretries: 3\n",
            [
                {
                    "path": "$",
                    "message": "Additional properties are not allowed ('retries' was unexpected)",
                }
            ],
        ),
        (
            "max_retries: 2\n",
            [{"path": "$", "message": "'schema' is a required property"}],
        ),
        (
            "schema: archshell/config/v2\n",
            [{"path": "$.schema", "message": "'archshell/config/v1' was expected"}],
        ),
    ],
)
def test_invalid_config_is_refused_and_state_unchanged(tmp_path, text, errors):
    code, action = engine(tmp_path, "next", "--feature", "probe")
    assert code == 0
    state_path = tmp_path / ".archshell" / "state" / "probe.yaml"
    before = state_path.read_bytes()
    write_config(tmp_path, text)
    code, msg = engine(tmp_path, "submit", "--feature", "probe", stdin=invalid_submit(action))
    assert code == 2
    assert msg["schema"] == "archshell/error/v1" and msg["code"] == "invalid_config"
    assert msg["details"] == {"config": ".archshell/config.yaml", "errors": errors}
    assert state_path.read_bytes() == before


@pytest.mark.parametrize(
    "text",
    ["schema: [unclosed\n", "schema: archshell/config/v1\nschema: archshell/config/v1\n", "- a\n- b\n", ""],
)
def test_bad_yaml_config_is_invalid_config(tmp_path, text):
    code, action = engine(tmp_path, "next", "--feature", "probe")
    before = (tmp_path / ".archshell" / "state" / "probe.yaml").read_bytes()
    write_config(tmp_path, text)
    code, msg = engine(tmp_path, "submit", "--feature", "probe", stdin=invalid_submit(action))
    assert (code, msg["code"]) == (2, "invalid_config")
    assert msg["details"]["config"] == ".archshell/config.yaml"
    assert msg["details"]["errors"] and all(set(e) == {"path", "message"} for e in msg["details"]["errors"])
    assert str(tmp_path) not in json.dumps(msg)
    assert (tmp_path / ".archshell" / "state" / "probe.yaml").read_bytes() == before


def test_non_utf8_config_is_invalid_config(tmp_path):
    (tmp_path / ".archshell").mkdir()
    (tmp_path / ".archshell" / "config.yaml").write_bytes(b"schema: \xff\n")
    with pytest.raises(InvalidConfig) as caught:
        load_config(tmp_path)
    assert caught.value.errors == [("$", ".archshell/config.yaml is not UTF-8")]


def test_unreadable_config_is_invalid_config(tmp_path):
    code, action = engine(tmp_path, "next", "--feature", "probe")
    before = (tmp_path / ".archshell" / "state" / "probe.yaml").read_bytes()
    (tmp_path / ".archshell" / "config.yaml").mkdir()
    code, msg = engine(tmp_path, "submit", "--feature", "probe", stdin=invalid_submit(action))
    assert (code, msg["code"]) == (2, "invalid_config")
    assert msg["message"] == ".archshell/config.yaml is invalid"
    [error] = msg["details"]["errors"]
    assert error["path"] == "$" and error["message"].startswith("cannot read .archshell/config.yaml: ")
    assert "None" not in error["message"]
    assert str(tmp_path) not in json.dumps(msg)
    assert (tmp_path / ".archshell" / "state" / "probe.yaml").read_bytes() == before


@pytest.mark.parametrize(
    ("argv", "stdin", "code"),
    [
        (["nope"], None, "unknown_command"),
        (["next", "--feature", "Bad"], None, "invalid_feature"),
        (["next", "--feature", "probe", "--x"], None, "invalid_arguments"),
        (["next", "--feature", "some-feature"], None, "unknown_feature"),
        (["submit", "--feature", "probe"], {"schema": "nope"}, "invalid_message"),
        (
            ["submit", "--feature", "probe"],
            {"schema": "archshell/submit/v1", "feature": "probe", "action_id": "x", "revision": 1, "result": {}},
            "no_pending_action",
        ),
    ],
)
def test_refusal_in_empty_dir_creates_no_archshell(tmp_path, argv, stdin, code):
    exit_code, msg = engine(tmp_path, *argv, stdin=stdin)
    assert (exit_code, msg["code"]) == (2, code)
    assert not (tmp_path / ".archshell").exists()


# --- acceptance criteria -----------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_ignores_engine_state_but_not_config(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    create_layout(tmp_path)

    def ignored(relative: str) -> bool:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", relative],
            cwd=tmp_path, capture_output=True, timeout=60,
        )
        assert proc.returncode in (0, 1), proc.stderr
        return proc.returncode == 0

    assert ignored(".archshell/state/x.yaml")
    assert ignored(".archshell/locks/x.lock")
    assert ignored(".archshell/txn.json")
    assert not ignored(".archshell/config.yaml")
    assert not ignored(".archshell/.gitignore")


def test_next_and_hooks_do_not_load_jsonschema(tmp_path):
    script = (
        "import io, sys\n"
        "from archshell.engine.cli import run\n"
        "for argv, data in [(['next', '--feature', 'probe'], b'{}'),\n"
        "                   (['guard-write'], b'{\"schema\":\"archshell/hook/v1\",\"path\":\"a\"}'),\n"
        "                   (['check-edit'], b'{\"schema\":\"archshell/hook/v1\",\"path\":\"a\"}'),\n"
        "                   (['on-stop'], b'{\"schema\":\"archshell/hook/v1\"}')]:\n"
        "    run(argv, io.BytesIO(data), io.StringIO())\n"
        "print('jsonschema' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, timeout=60
    )
    assert proc.stdout.decode().strip() == "False", proc.stderr


def test_layout_names_every_engine_path():
    assert layout.state_file("probe") == ".archshell/state/probe.yaml"
    assert layout.lock_file("probe") == ".archshell/locks/probe.lock"
    assert layout.PROMPTS_DIR == ".archshell/state/prompts"
    assert layout.RECORDS_DIR == ".archshell/records"
    assert layout.records_dir("probe") == ".archshell/records/probe"
    assert layout.record_claims_dir("probe") == ".archshell/locks/records/probe"
    assert all(d.startswith(f"{layout.ROOT}/") for d in layout.DIRECTORIES)
