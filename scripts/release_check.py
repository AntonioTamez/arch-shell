"""Release check: refuse a tag whose versions or compatibility declarations disagree.

Stdlib only, so it runs before anything is installed. `check(tag, root)` returns
the list of named problems (empty when the release may proceed); the CLI prints
them and exits 1. The tag signature is not checked here: the release workflow
asks GitHub for it (see `.github/workflows/release.yml`).

Only pre-release tags are accepted: `vX.Y.Z-(alpha|beta|rc).N`. The Python
distributions carry the PEP 440 form `X.Y.Z(a|b|rc)N`; the analyzer and the
plugin carry the SemVer form `X.Y.Z-(alpha|beta|rc).N`. The committed files are
the source of truth; nothing here rewrites a version.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)-(alpha|beta|rc)\.(\d+)")
PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}

CORE_PYPROJECT = "archshell-core/pyproject.toml"
PACK_PYPROJECT = "archshell-dotnet/pyproject.toml"
ANALYZER_CSPROJ = "archshell-dotnet/analyzer/Archshell.Analyzer.csproj"
PLUGIN_MANIFEST = "archshell-claude-code/.claude-plugin/plugin.json"
UV_LOCK = "uv.lock"
PACK_INIT = "archshell-dotnet/archshell_dotnet/__init__.py"
HOST_DECLARATION = "archshell-claude-code/archshell-host.json"
CORE_ACTIONS = "archshell-core/archshell/domain/actions.py"
COMPATIBILITY = "docs/compatibility.md"

CORE = "archshell"
PACK = "archshell-dotnet"


class Problem(Exception):
    """A declaration file that cannot be read or does not hold the expected value."""


# --- PEP 440 subset: release segments plus an optional a/b/rc pre-release ----

_VERSION = re.compile(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?")
_PRE_ORDER = {"a": 0, "b": 1, "rc": 2}
_SPECIFIER = re.compile(r"(~=|==|!=|<=|>=|<|>)\s*(\S+)")


def _parse_version(text: str) -> tuple[tuple[int, ...], tuple[int, int] | None]:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(text)
    release = tuple(int(part) for part in match.group(1).split("."))
    pre = (_PRE_ORDER[match.group(2)], int(match.group(3))) if match.group(2) else None
    return release, pre


def _key(release: tuple[int, ...], pre: tuple[int, int] | None, width: int) -> tuple:
    padded = release + (0,) * (width - len(release))
    # A final release sorts after every pre-release of the same release.
    return padded, pre if pre is not None else (3, 0)


def _satisfies(version: str, op: str, bound: str) -> bool:
    v_release, v_pre = _parse_version(version)
    b_release, b_pre = _parse_version(bound)
    width = max(len(v_release), len(b_release))
    v, b = _key(v_release, v_pre, width), _key(b_release, b_pre, width)
    same_release = v[0] == b[0]
    if op == "==":
        return v == b
    if op == "!=":
        return v != b
    if op == ">=":
        return v >= b
    if op == "<=":
        return v <= b
    if op == ">":
        return v > b
    if op == "<":
        # PEP 440: `<V` excludes pre-releases of V unless V is itself a pre-release.
        return v < b and not (b_pre is None and v_pre is not None and same_release)
    if op == "~=":
        if len(b_release) < 2:
            raise ValueError(bound)
        prefix = b_release[:-1]
        return v >= b and v_release[: len(prefix)] == prefix
    raise ValueError(op)


def range_includes(specifiers: str, version: str) -> bool:
    """Whether a comma-separated PEP 440 specifier set includes the version."""
    parts = [part.strip() for part in specifiers.split(",") if part.strip()]
    if not parts:
        raise ValueError(specifiers)
    for part in parts:
        match = _SPECIFIER.fullmatch(part)
        if match is None:
            raise ValueError(part)
        if not _satisfies(version, match.group(1), match.group(2)):
            return False
    return True


# --- readers -----------------------------------------------------------------


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Problem(f"{relative}: cannot be read ({error.__class__.__name__})") from error


def _toml(root: Path, relative: str) -> dict:
    try:
        return tomllib.loads(_read(root, relative))
    except tomllib.TOMLDecodeError as error:
        raise Problem(f"{relative}: not valid TOML ({error})") from error


def _json(root: Path, relative: str) -> dict:
    try:
        data = json.loads(_read(root, relative))
    except json.JSONDecodeError as error:
        raise Problem(f"{relative}: not valid JSON ({error.msg})") from error
    if not isinstance(data, dict):
        raise Problem(f"{relative}: not a JSON object")
    return data


def _pyproject_version(root: Path, relative: str) -> str:
    version = _toml(root, relative).get("project", {}).get("version")
    if not isinstance(version, str):
        raise Problem(f"{relative}: no [project] version")
    return version


def _csproj_version(root: Path) -> str:
    match = re.search(r"<Version>\s*([^<\s]+)\s*</Version>", _read(root, ANALYZER_CSPROJ))
    if match is None:
        raise Problem(f"{ANALYZER_CSPROJ}: no <Version>")
    return match.group(1)


def _plugin_version(root: Path) -> str:
    version = _json(root, PLUGIN_MANIFEST).get("version")
    if not isinstance(version, str):
        raise Problem(f"{PLUGIN_MANIFEST}: no version")
    return version


def _lock_version(root: Path, name: str) -> str:
    for package in _toml(root, UV_LOCK).get("package", []):
        if package.get("name") == name:
            return str(package.get("version"))
    raise Problem(f"{UV_LOCK}: no package {name}")


def _module_string(root: Path, relative: str, name: str) -> str:
    """The value of a module-level `NAME = "<str>"` assignment, read without importing."""
    try:
        tree = ast.parse(_read(root, relative))
    except SyntaxError as error:
        raise Problem(f"{relative}: not valid Python ({error.msg})") from error
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) and node.value else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
                raise Problem(f"{relative}: {name} is not a string literal")
    raise Problem(f"{relative}: no {name}")


def _core_range(root: Path) -> str:
    dependencies = _toml(root, PACK_PYPROJECT).get("project", {}).get("dependencies", [])
    for dependency in dependencies:
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*?)\s*", dependency)
        if match and re.sub(r"[-_.]+", "-", match.group(1)).lower() == CORE:
            return match.group(2)
    raise Problem(f"{PACK_PYPROJECT}: no {CORE} dependency (the pack's core range)")


MATRIX_COLUMNS = (
    "Release", "archshell", "archshell-dotnet", "Core range", "Archshell.Analyzer", "Plugin", "Protocol",
)


def _matrix_rows(root: Path) -> dict[str, list[str]]:
    """The compatibility table's rows by release, each as its unquoted cells."""
    rows: dict[str, list[str]] = {}
    header: list[str] | None = None
    for line in _read(root, COMPATIBILITY).splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip().strip("`").strip() for cell in line[1:-1].split("|")]
        if header is None:
            header = cells
            if tuple(header) != MATRIX_COLUMNS:
                raise Problem(f"{COMPATIBILITY}: table columns {header}, expected {list(MATRIX_COLUMNS)}")
        elif not all(set(cell) <= {"-", ":"} for cell in cells):
            rows[cells[0]] = cells
    return rows


# --- the check ---------------------------------------------------------------


def check(tag: str, root: Path) -> list[str]:
    """Every named problem that refuses `tag` for the tree at `root`."""
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        return [f"{tag}: pre-release tags only (vX.Y.Z-(alpha|beta|rc).N); stable and other tags are refused"]
    major, minor, patch, label, number = match.groups()
    python_version = f"{major}.{minor}.{patch}{PEP440_PRE[label]}{number}"
    semver_version = f"{major}.{minor}.{patch}-{label}.{number}"

    problems: list[str] = []
    # The committed value behind each matrix column, when it could be read.
    declared: dict[str, str] = {"Release": tag}

    def expect(relative: str, read, expected: str, what: str = "version", column: str | None = None) -> None:
        try:
            actual = read()
        except Problem as problem:
            problems.append(str(problem))
            return
        if column is not None:
            declared[column] = actual
        if actual != expected:
            problems.append(f"{relative}: {what} {actual}, expected {expected} for {tag}")

    expect(CORE_PYPROJECT, lambda: _pyproject_version(root, CORE_PYPROJECT), python_version, column="archshell")
    expect(
        PACK_PYPROJECT, lambda: _pyproject_version(root, PACK_PYPROJECT), python_version, column="archshell-dotnet"
    )
    expect(ANALYZER_CSPROJ, lambda: _csproj_version(root), semver_version, column="Archshell.Analyzer")
    expect(PLUGIN_MANIFEST, lambda: _plugin_version(root), semver_version, column="Plugin")
    expect(UV_LOCK, lambda: _lock_version(root, CORE), python_version, f"{CORE} version")
    expect(UV_LOCK, lambda: _lock_version(root, PACK), python_version, f"{PACK} version")

    expect(
        PACK_INIT,
        lambda: _module_string(root, PACK_INIT, "ANALYZER_VERSION"),
        semver_version,
        "ANALYZER_VERSION",
    )

    try:
        core_range = _core_range(root)
        if core_range:
            declared["Core range"] = core_range
        if not core_range:
            problems.append(f"{PACK_PYPROJECT}: the {CORE} dependency declares no core range")
        elif not range_includes(core_range, python_version):
            problems.append(
                f"{PACK_PYPROJECT}: core range {CORE}{core_range} excludes the core version {python_version}"
            )
    except Problem as problem:
        problems.append(str(problem))
    except ValueError:
        problems.append(f"{PACK_PYPROJECT}: core range {CORE}{core_range} cannot be evaluated")

    try:
        action_schema = _module_string(root, CORE_ACTIONS, "ACTION_SCHEMA")
        protocol = _json(root, HOST_DECLARATION).get("protocol")
        declared["Protocol"] = action_schema
        if protocol != action_schema:
            problems.append(
                f"{HOST_DECLARATION}: protocol {protocol!r}, expected the core ACTION_SCHEMA {action_schema!r}"
            )
    except Problem as problem:
        problems.append(str(problem))

    try:
        row = _matrix_rows(root).get(tag)
        if row is None:
            problems.append(f"{COMPATIBILITY}: no row for {tag}")
        elif len(row) != len(MATRIX_COLUMNS):
            problems.append(f"{COMPATIBILITY}: row for {tag} has {len(row)} cells, expected {len(MATRIX_COLUMNS)}")
        else:
            for column, cell in zip(MATRIX_COLUMNS, row):
                if column in declared and cell != declared[column]:
                    problems.append(
                        f"{COMPATIBILITY}: row for {tag} column {column} is {cell}, "
                        f"but the committed declaration is {declared[column]}"
                    )
    except Problem as problem:
        problems.append(str(problem))

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag, e.g. v0.1.0-rc.1")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: this script's repository)",
    )
    args = parser.parse_args(argv)
    problems = check(args.tag, args.root)
    if problems:
        print(f"release check refused {args.tag}:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print(f"release check passed: {args.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
