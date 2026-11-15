"""The release gate (story 1.11, AD-22): `scripts/release_check.py` and the release workflow.

Every check runs on a scratch copy of the declaration files, so a case can
break one declaration without touching the repository.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from archshell.domain.actions import ACTION_SCHEMA

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release_check.py"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
TAG = "v0.1.0-rc.1"

_spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
release_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_check)

DECLARATION_FILES = [
    release_check.CORE_PYPROJECT,
    release_check.PACK_PYPROJECT,
    release_check.ANALYZER_CSPROJ,
    release_check.PLUGIN_MANIFEST,
    release_check.UV_LOCK,
    release_check.PACK_INIT,
    release_check.HOST_DECLARATION,
    release_check.CORE_ACTIONS,
    release_check.COMPATIBILITY,
]


@pytest.fixture
def tree(tmp_path):
    for relative in DECLARATION_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    return tmp_path


def edit(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_bytes().decode("utf-8")
    assert text.count(old) == 1, (relative, old)
    path.write_bytes(text.replace(old, new).encode("utf-8"))


# --- I/O matrix rows ---------------------------------------------------------


def test_release_tag_matching_every_declaration_passes(tree):
    assert release_check.check(TAG, tree) == []


def test_the_repository_itself_passes_for_its_release_tag():
    assert release_check.check(TAG, REPO) == []


def test_version_mismatch_names_each_file_and_its_version(tree):
    problems = release_check.check("v0.1.0-rc.2", tree)
    expected = {
        release_check.CORE_PYPROJECT: "version 0.1.0rc1, expected 0.1.0rc2",
        release_check.PACK_PYPROJECT: "version 0.1.0rc1, expected 0.1.0rc2",
        release_check.ANALYZER_CSPROJ: "version 0.1.0-rc.1, expected 0.1.0-rc.2",
        release_check.PLUGIN_MANIFEST: "version 0.1.0-rc.1, expected 0.1.0-rc.2",
        release_check.PACK_INIT: "ANALYZER_VERSION 0.1.0-rc.1, expected 0.1.0-rc.2",
    }
    for relative, text in expected.items():
        assert any(p.startswith(f"{relative}: {text}") for p in problems), (relative, problems)
    assert sum(p.startswith(f"{release_check.UV_LOCK}: ") for p in problems) == 2
    assert f"{release_check.COMPATIBILITY}: no row for v0.1.0-rc.2" in problems


@pytest.mark.parametrize("tag", ["v0.1.0", "0.1.0-rc.1", "v0.1.0-rc1", "v0.1.0-dev.1", "v0.1-rc.1"])
def test_stable_and_malformed_tags_are_refused(tree, tag):
    problems = release_check.check(tag, tree)
    assert len(problems) == 1
    assert "pre-release tags only" in problems[0]


def test_core_range_excluding_the_core_version_is_named(tree):
    edit(tree, release_check.PACK_PYPROJECT, '"archshell>=0.1.0rc1,<0.2"', '"archshell>=0.1.0,<0.2"')
    assert release_check.check(TAG, tree) == [
        f"{release_check.PACK_PYPROJECT}: core range archshell>=0.1.0,<0.2 excludes the core version 0.1.0rc1",
        f"{release_check.COMPATIBILITY}: row for {TAG} column Core range is >=0.1.0rc1,<0.2, "
        "but the committed declaration is >=0.1.0,<0.2",
    ]


def test_core_dependency_without_a_range_is_named(tree):
    edit(tree, release_check.PACK_PYPROJECT, '"archshell>=0.1.0rc1,<0.2"', '"archshell"')
    assert release_check.check(TAG, tree) == [
        f"{release_check.PACK_PYPROJECT}: the archshell dependency declares no core range"
    ]


def test_analyzer_version_pin_differing_is_named(tree):
    edit(tree, release_check.PACK_INIT, 'ANALYZER_VERSION = "0.1.0-rc.1"', 'ANALYZER_VERSION = "0.1.0"')
    assert release_check.check(TAG, tree) == [
        f"{release_check.PACK_INIT}: ANALYZER_VERSION 0.1.0, expected 0.1.0-rc.1 for {TAG}"
    ]


def test_plugin_protocol_differing_from_action_schema_is_named(tree):
    edit(tree, release_check.HOST_DECLARATION, ACTION_SCHEMA, "archshell/action/v2")
    assert release_check.check(TAG, tree) == [
        f"{release_check.HOST_DECLARATION}: protocol 'archshell/action/v2', "
        f"expected the core ACTION_SCHEMA '{ACTION_SCHEMA}'"
    ]


def test_missing_matrix_row_is_named(tree):
    edit(tree, release_check.COMPATIBILITY, f"| `{TAG}` |", "| `v0.0.9-rc.1` |")
    assert release_check.check(TAG, tree) == [f"{release_check.COMPATIBILITY}: no row for {TAG}"]


@pytest.mark.parametrize(
    ("column", "cell", "wrong", "declared"),
    [
        ("archshell", "| `0.1.0rc1` | `0.1.0rc1` | `>=", "| `0.1.0rc9` | `0.1.0rc1` | `>=", "0.1.0rc1"),
        ("archshell-dotnet", "| `0.1.0rc1` | `>=", "| `0.1.0rc9` | `>=", "0.1.0rc1"),
        ("Core range", "`>=0.1.0rc1,<0.2`", "`>=0.1.0rc1,<0.3`", ">=0.1.0rc1,<0.2"),
        ("Archshell.Analyzer", "<0.2` | `0.1.0-rc.1` |", "<0.2` | `0.1.0-rc.9` |", "0.1.0-rc.1"),
        ("Plugin", "| `0.1.0-rc.1` | `archshell/", "| `0.1.0-rc.9` | `archshell/", "0.1.0-rc.1"),
        ("Protocol", "`archshell/action/v1` |", "`archshell/action/v9` |", ACTION_SCHEMA),
    ],
)
def test_matrix_row_cell_differing_from_its_declaration_is_named(tree, column, cell, wrong, declared):
    edit(tree, release_check.COMPATIBILITY, cell, wrong)
    problems = release_check.check(TAG, tree)
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{release_check.COMPATIBILITY}: row for {TAG} column {column} is ")
    assert problems[0].endswith(f"but the committed declaration is {declared}")


def test_missing_declaration_file_is_named(tree):
    (tree / release_check.HOST_DECLARATION).unlink()
    problems = release_check.check(TAG, tree)
    assert len(problems) == 1 and problems[0].startswith(f"{release_check.HOST_DECLARATION}: cannot be read")


# --- PEP 440 subset ----------------------------------------------------------


@pytest.mark.parametrize(
    ("specifiers", "version", "included"),
    [
        (">=0.1.0rc1,<0.2", "0.1.0rc1", True),
        (">=0.1.0rc1,<0.2", "0.1.0", True),
        (">=0.1.0rc1,<0.2", "0.2.0rc1", False),  # `<V` excludes pre-releases of V
        (">=0.1.0rc1,<0.2", "0.2", False),
        (">=0.1.0", "0.1.0rc1", False),
        ("<0.2.0rc2", "0.2.0rc1", True),
        ("==0.1.0rc1", "0.1.0rc1", True),
        ("~=0.1.0rc1", "0.1.5", True),
        ("~=0.1.0rc1", "0.2.0", False),
        ("!=0.1.0rc1", "0.1.0rc1", False),
    ],
)
def test_range_includes(specifiers, version, included):
    assert release_check.range_includes(specifiers, version) is included


# --- CLI ---------------------------------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, check=False)


def test_cli_passes_for_the_release_tag():
    proc = run_cli(TAG)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode("utf-8").strip() == f"release check passed: {TAG}"


def test_cli_refuses_a_stable_tag_with_exit_1(tree):
    proc = run_cli("v0.1.0", "--root", str(tree))
    assert proc.returncode == 1
    err = proc.stderr.decode("utf-8")
    assert "release check refused v0.1.0:" in err and "pre-release tags only" in err
    assert proc.stdout == b""


# --- release workflow --------------------------------------------------------


def workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_bytes().decode("utf-8"))
    # PyYAML reads the bare `on:` key as the boolean True (YAML 1.1).
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_release_workflow_orders_verify_ci_publish_install():
    jobs = workflow(RELEASE_WORKFLOW)["jobs"]
    assert set(jobs) == {"verify", "ci", "publish", "install"}
    assert jobs["ci"]["needs"] == "verify"
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert set(jobs["publish"]["needs"]) == {"verify", "ci"}
    assert "publish" in jobs["install"]["needs"]
    assert jobs["install"]["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest", "macos-latest"]
    assert "workflow_call" in workflow(CI_WORKFLOW)["on"]


def test_publish_uploads_to_testpypi_last():
    # A TestPyPI upload is permanent: build, pack, and the NuGet push come first.
    steps = workflow(RELEASE_WORKFLOW)["jobs"]["publish"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    order = ["uv build", "dotnet pack", "dotnet nuget push", "uv publish"]
    assert [next(i for i, run in enumerate(runs) if run.startswith(cmd)) for cmd in order] == list(range(4))
    assert len(runs) == 4


def test_release_workflow_runs_only_on_tags_and_checks_the_signature():
    data = workflow(RELEASE_WORKFLOW)
    assert set(data["on"]) == {"push"} and set(data["on"]["push"]) == {"tags"}
    verify = json.dumps(data["jobs"]["verify"])
    assert "git/ref/tags/" in verify and "git/tags/" in verify and ".verification.verified" in verify
    assert "scripts/release_check.py" in verify


def test_release_workflow_grants_write_permissions_only_to_publish():
    data = workflow(RELEASE_WORKFLOW)
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "packages": "write",
    }
    for name, job in data["jobs"].items():
        if name != "publish":
            assert "write" not in job.get("permissions", {}).values(), name
    text = RELEASE_WORKFLOW.read_bytes().decode("utf-8")
    assert "pypi.org/legacy" in text and "upload.pypi.org" not in text
    assert "nuget.org/api" not in text and "api.nuget.org" not in text
    assert not re.search(r"secrets\.(?!GITHUB_TOKEN\b)", text)


# --- wheel contents ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_core_wheel_installs_only_the_archshell_package(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--no-sources", "--package", "archshell", "--out-dir", str(tmp_path)],
        cwd=REPO,
        capture_output=True,
        check=True,
    )
    (wheel,) = tmp_path.glob("archshell-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    top_levels = {name.split("/")[0] for name in names if not name.split("/")[0].endswith(".dist-info")}
    assert top_levels == {"archshell"}
    assert "archshell/schemas/llm-text-v1.json" in names
