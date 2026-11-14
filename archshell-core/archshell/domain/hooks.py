"""Hook sub-protocol verdicts (AD-17): guard-write, check-edit, on-stop.

Pure: no IO, no environment. The engine computes the protected set and every
verdict here; host config holds no paths (AD-17, AD-18). Input messages are
`archshell/hook/v1`, verdicts are `archshell/verdict/v1`.

Path rule: a path is resolved against a base directory (relative paths only),
`.` and `..` are collapsed lexically, backslashes are treated as separators,
and the result is compared with the project root case-insensitively. Only
paths inside the root are subject to the protected set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HOOK_SCHEMA = "archshell/hook/v1"
VERDICT_SCHEMA = "archshell/verdict/v1"

ALLOW = "allow"
BLOCK = "block"

PROTECTED_DIR = ".archshell"

_DRIVE = re.compile(r"[A-Za-z]:")
_DEVICE_PREFIXES = ("//?/UNC/", "//./UNC/", "//?/", "//./")


class Unresolvable(ValueError):
    """A path that cannot be resolved lexically (e.g. `C:x`)."""


def _key(segment: str) -> str:
    """Comparison key: Windows ignores trailing dots and spaces in names."""
    return segment.rstrip(". ").casefold()


@dataclass(frozen=True)
class Verdict:
    verdict: str
    code: str
    reason: str

    def to_message(self) -> dict[str, Any]:
        return {
            "schema": VERDICT_SCHEMA,
            "verdict": self.verdict,
            "code": self.code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HookInput:
    path: str | None
    cwd: str | None


@dataclass(frozen=True)
class Invalid:
    message: str


ALLOWED = Verdict(ALLOW, "allowed", "")


def parse_hook(message: dict[str, Any], *, requires_path: bool) -> HookInput | Invalid:
    """Validate an `archshell/hook/v1` message."""
    if message.get("schema") != HOOK_SCHEMA:
        return Invalid(f"hook message schema must be {HOOK_SCHEMA}")
    path = message.get("path")
    cwd = message.get("cwd")
    if requires_path and not (isinstance(path, str) and path.strip()):
        return Invalid("hook message needs a non-empty string `path`")
    if path is not None and not isinstance(path, str):
        return Invalid("hook message `path` must be a string")
    if cwd is not None and not (isinstance(cwd, str) and cwd.strip()):
        return Invalid("hook message `cwd` must be a non-empty string")
    for value in (path, cwd):
        if isinstance(value, str) and "\x00" in value:
            return Invalid("hook message paths must not contain NUL")
    return HookInput(path=path, cwd=cwd)


def _split(path: str) -> tuple[str, list[str]] | None:
    r"""Return (anchor, segments) for an absolute path, or None if relative.

    The anchor is a lowercased drive (`c:`), `//` for UNC, or `/` for POSIX.
    Win32 device prefixes (`\\?\`, `\\.\`, `\\?\UNC\`) are stripped first. A drive
    without a separator (`C:x`, relative to that drive's current directory)
    cannot be resolved lexically and raises `Unresolvable`.
    """
    text = path.replace("\\", "/")
    for prefix in _DEVICE_PREFIXES:
        if text[: len(prefix)].casefold() == prefix.casefold():
            text = ("//" if prefix.endswith("UNC/") else "") + text[len(prefix) :]
            break
    if _DRIVE.match(text):
        if text[2:3] != "/":
            raise Unresolvable(path)
        anchor, rest = text[:2].lower(), text[2:]
    elif text.startswith("//"):
        anchor, rest = "//", text[2:]
    elif text.startswith("/"):
        anchor, rest = "/", text[1:]
    else:
        return None
    return anchor, rest.split("/")


def _collapse(segments: list[str]) -> list[str]:
    out: list[str] = []
    for segment in segments:
        if segment in ("", "."):
            continue
        if segment == "..":
            if out:
                out.pop()
            continue
        out.append(segment)
    return out


def resolve(path: str, base: str) -> tuple[str, list[str]]:
    """Resolve `path` against the absolute directory `base`, collapsing `.`/`..`."""
    split = _split(path)
    if split is None:
        base_split = _split(base)
        if base_split is None:
            raise ValueError(f"base directory is not absolute: {base}")
        anchor, base_segments = base_split
        segments = base_segments + path.replace("\\", "/").split("/")
    else:
        anchor, segments = split
    return anchor, _collapse(segments)


def relative_to_root(path: str, base: str, root: str) -> list[str] | None:
    """Segments of `path` relative to `root`, or None when outside it."""
    anchor, segments = resolve(path, base)
    root_anchor, root_segments = resolve(root, root)
    if anchor != root_anchor or len(segments) < len(root_segments):
        return None
    for seg, root_seg in zip(segments, root_segments):
        if _key(seg) != _key(root_seg):
            return None
    return segments[len(root_segments) :]


def is_protected(relative: list[str]) -> bool:
    """The protected set: everything under `.archshell/` (AD-10)."""
    return bool(relative) and _key(relative[0]) == PROTECTED_DIR


def guard_write(hook: HookInput, root: str) -> Verdict:
    if hook.path is None:
        return Verdict(BLOCK, "invalid_message", "ArchShell: guard-write needs a path.")
    try:
        base = root if hook.cwd is None else _join(resolve(hook.cwd, root))
        relative = relative_to_root(hook.path, base, root)
    except Unresolvable:
        return Verdict(
            BLOCK,
            "unresolvable_path",
            f"ArchShell: cannot resolve {hook.path!r} against the project root; "
            "use an absolute path or one relative to the project root.",
        )
    if relative is None or not is_protected(relative):
        return ALLOWED
    shown = "/".join(relative)
    return Verdict(
        BLOCK,
        "protected_path",
        f"ArchShell: {shown} is engine-owned. Only archshell-engine writes under "
        f"{PROTECTED_DIR}/; change it through an ArchShell flow instead.",
    )


def _join(resolved: tuple[str, list[str]]) -> str:
    anchor, segments = resolved
    return anchor + ("" if anchor.endswith("/") else "/") + "/".join(segments)


def check_edit(hook: HookInput, root: str) -> Verdict:
    del hook, root  # per-edit checks arrive with later stories (epic 4)
    return ALLOWED


def on_stop(hook: HookInput, feature: str | None) -> Verdict:
    del hook, feature  # stop-time gates arrive with later stories
    return ALLOWED


def error_block(code: str, message: str) -> Verdict:
    """Fail-closed verdict for an engine error in guard-write."""
    return Verdict(BLOCK, code, f"ArchShell guard-write failed closed: {message}")
