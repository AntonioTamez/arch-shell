"""Package boundaries (story 1.10, AD-1, AD-4).

The import-linter contracts live in the root `pyproject.toml`. They run here
through Python (`importlinter.cli.lint_imports`), not the `lint-imports`
launcher, which OS policy may block; CI also runs them as their own step.
Seeded violations run against a scratch copy of the packages, never the tree.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "archshell-core" / "archshell"
PACK = REPO / "archshell-dotnet" / "archshell_dotnet"
DOMAIN = CORE / "domain"
LINT = "import sys; from importlinter.cli import lint_imports; sys.exit(lint_imports(no_cache=True, no_logo=True))"


def lint(cwd: Path, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = None if pythonpath is None else {**os.environ, "PYTHONPATH": str(pythonpath)}
    return subprocess.run(
        [sys.executable, "-c", LINT], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
    )


@pytest.fixture
def scratch(tmp_path) -> Path:
    """A copy of both packages and the root pyproject.toml; returns its src dir."""
    src = tmp_path / "src"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(CORE, src / "archshell", ignore=ignore)
    shutil.copytree(PACK, src / "archshell_dotnet", ignore=ignore)
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    return src


def test_contracts_pass_on_the_tree():
    proc = lint(REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "4 kept, 0 broken" in proc.stdout


def test_unchanged_scratch_copy_passes(scratch):
    proc = lint(scratch.parent, scratch)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_core_import_of_the_pack_fails_the_contracts(scratch):
    (scratch / "archshell" / "engine" / "uses_pack.py").write_text("import archshell_dotnet\n", "utf-8")
    proc = lint(scratch.parent, scratch)
    assert proc.returncode != 0
    assert "The core never imports a language pack BROKEN" in proc.stdout
    assert "archshell.engine.uses_pack -> archshell_dotnet" in proc.stdout


@pytest.mark.parametrize(
    "source, broken, named",
    [
        ("import yaml\n", "domain never imports the forbidden packages BROKEN", "-> yaml"),
        (
            "from archshell.adapters import files\n",
            "domain, render and ports never import adapters, engine or cli BROKEN",
            "-> archshell.adapters.files",
        ),
    ],
    ids=["yaml", "adapters"],
)
def test_domain_import_of_io_or_third_party_fails_the_contracts(scratch, source, broken, named):
    (scratch / "archshell" / "domain" / "x.py").write_text(source, "utf-8")
    proc = lint(scratch.parent, scratch)
    assert proc.returncode != 0
    assert broken in proc.stdout
    assert f"archshell.domain.x {named}" in proc.stdout


def disallowed_imports(source: str, module: str) -> list[str]:
    """Imports in `source` (the module `module`) outside the stdlib and `archshell.domain`."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module.rsplit(".", node.level)[0]
                names = [f"{package}.{node.module}" if node.module else package]
            else:
                names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or name == "archshell.domain" or name.startswith("archshell.domain."):
                continue
            bad.append(name)
    return bad


def test_domain_imports_only_the_standard_library_and_itself():
    files = sorted(DOMAIN.rglob("*.py"))
    assert files
    found = {}
    for path in files:
        module = ".".join(("archshell", *path.relative_to(CORE).with_suffix("").parts))
        if bad := disallowed_imports(path.read_text("utf-8"), module):
            found[str(path.relative_to(REPO))] = bad
    assert found == {}


@pytest.mark.parametrize(
    "source, bad",
    [
        ("import yaml\n", ["yaml"]),
        ("import jinja2.nodes\n", ["jinja2.nodes"]),
        ("from archshell.render.normalize import to_bytes\n", ["archshell.render.normalize"]),
        ("from archshell import __version__\n", ["archshell"]),
        ("from .. import adapters\n", ["archshell"]),
        ("import re, json\nfrom . import layout\nfrom archshell.domain.workflow import x\n", []),
    ],
)
def test_domain_allow_list_check_detects_violations(source, bad):
    assert disallowed_imports(source, "archshell.domain.x") == bad
