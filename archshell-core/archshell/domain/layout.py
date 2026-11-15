"""The `.archshell/` layout (AD-10): every engine-owned path, named once.

Pure: no IO (AD-4). Paths are POSIX and relative to the project root. The
adapter `archshell.adapters.layout` creates the layout and reads the config.
"""

from __future__ import annotations

ROOT = ".archshell"

CONFIG = f"{ROOT}/config.yaml"
GITIGNORE = f"{ROOT}/.gitignore"
TXN = f"{ROOT}/txn.json"
STATE_DIR = f"{ROOT}/state"
LOCKS_DIR = f"{ROOT}/locks"
# `llm_task` prompts live beside cursor state, one file per action id.
PROMPTS_DIR = f"{STATE_DIR}/prompts"
# Confirmation records (FR-53): committed, append-only, one directory per feature.
RECORDS_DIR = f"{ROOT}/records"
# Sequence-number claims of in-flight record writers (gitignored with `locks/`).
RECORD_CLAIMS_DIR = f"{LOCKS_DIR}/records"

# Directories `create_layout` makes, in this order.
DIRECTORIES: tuple[str, ...] = (
    f"{ROOT}/charter",
    f"{ROOT}/vocabulary",
    f"{ROOT}/features",
    RECORDS_DIR,
    f"{ROOT}/derived",
    STATE_DIR,
    LOCKS_DIR,
)

# Lines of `.archshell/.gitignore`, relative to `.archshell/`. The project's
# root `.gitignore` is human-owned and never written.
GITIGNORE_LINES: tuple[str, ...] = ("state/", "locks/", "txn.json")

CONFIG_SCHEMA = "archshell/config/v1"
# An invalid `llm_task` result re-issues the step at most this many times
# before the submit is refused with `invalid_output` (config `max_retries`).
DEFAULT_MAX_RETRIES = 2

RECORD_SCHEMA = "archshell/record/v1"
# The closed vocabulary of confirmation flows; a record's filename names one.
RECORD_FLOWS: tuple[str, ...] = (
    "plan",
    "extension",
    "contract",
    "contract-revision",
    "breaking-change",
    "growth",
    "vocabulary",
)


def state_file(feature: str) -> str:
    return f"{STATE_DIR}/{feature}.yaml"


def lock_file(feature: str) -> str:
    return f"{LOCKS_DIR}/{feature}.lock"


def records_dir(feature: str) -> str:
    return f"{RECORDS_DIR}/{feature}"


def record_claims_dir(feature: str) -> str:
    return f"{RECORD_CLAIMS_DIR}/{feature}"


def gitignore_text() -> str:
    return "".join(f"{line}\n" for line in GITIGNORE_LINES)


def default_config() -> list[tuple[str, object]]:
    """The default `config.yaml` document as ordered (key, value) pairs."""
    return [("schema", CONFIG_SCHEMA), ("max_retries", DEFAULT_MAX_RETRIES)]
