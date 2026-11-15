"""Create the `.archshell/` layout and read `.archshell/config.yaml` (AD-10).

`create_layout` is explicit: only a caller that sets up a project runs it.
Every other command keeps creating the directories it needs lazily.
`load_config` imports the schema library on call, so commands that never read
the config (`next`, the hooks) do not load it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from archshell.adapters import yaml_read
from archshell.adapters.files import path_of, write_text
from archshell.domain import layout
from archshell.render.yaml_emit import Ordered, emit


@dataclass(frozen=True)
class Config:
    max_retries: int = layout.DEFAULT_MAX_RETRIES


class InvalidConfig(Exception):
    """`config.yaml` is unreadable or does not match `archshell/config/v1`."""

    def __init__(self, errors: list[tuple[str, str]]):
        super().__init__("; ".join(f"{path}: {message}" for path, message in errors))
        self.errors = errors


def create_layout(root: Path) -> None:
    """Create the AD-10 directories, `.gitignore`, and a default `config.yaml`.

    Idempotent: an existing `config.yaml` is never overwritten, and a rerun
    changes no bytes.
    """
    for directory in layout.DIRECTORIES:
        path_of(root, directory).mkdir(parents=True, exist_ok=True)
    write_text(path_of(root, layout.GITIGNORE), layout.gitignore_text())
    config = path_of(root, layout.CONFIG)
    if not config.exists():
        write_text(config, emit(Ordered(layout.default_config())))


def load_config(root: Path) -> Config:
    """The project config; defaults when `config.yaml` is missing.

    Raises `InvalidConfig` with the errors as (JSON path, message) when the
    file is not UTF-8, not restricted-subset YAML, or fails the schema.
    """
    path = path_of(root, layout.CONFIG)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise InvalidConfig([("$", f"cannot read {layout.CONFIG}: {reason}")]) from None
    try:
        data = yaml_read.load(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise InvalidConfig([("$", f"{layout.CONFIG} is not UTF-8")]) from None
    except yaml_read.YamlReadError as exc:
        raise InvalidConfig([("$", str(exc))]) from None
    from archshell.adapters import output_schemas

    errors = output_schemas.validate(layout.CONFIG_SCHEMA, data)
    if errors:
        raise InvalidConfig(errors)
    assert isinstance(data, dict)
    return Config(max_retries=data.get("max_retries", layout.DEFAULT_MAX_RETRIES))
