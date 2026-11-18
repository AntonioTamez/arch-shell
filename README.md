# arch-shell

ArchShell: an architecture shell that keeps an AI coding agent inside a confirmed architecture.

## Layout

| Path | What it is |
| --- | --- |
| `pyproject.toml`, `uv.lock` | uv workspace root; one lockfile for all Python packages |
| `archshell-core/` | distribution `archshell`: domain, render, ports, adapters, and the executables `archshell-engine` (host protocol) and `archshell` (human/CI commands); `conformance/` holds the extension conformance suite |
| `archshell-dotnet/` | distribution `archshell-dotnet`: the .NET language pack (`archshell_dotnet/`) and the Roslyn analyzer (`analyzer/`, `net10.0` .NET tool) |
| `archshell-claude-code/` | Claude Code host plugin; holds no logic and forwards to `archshell-engine`; `archshell-host.json` declares the action protocol it speaks |
| `.claude-plugin/marketplace.json` | local marketplace that lists the plugin |
| `global.json` | pins the .NET SDK band (10.0.4xx, `rollForward: latestFeature`) |
| `.github/workflows/ci.yml` | CI on Ubuntu, Windows, and macOS |
| `.github/workflows/release.yml` | signed pre-release publishing to TestPyPI and GitHub Packages (see [Releases](#releases)) |
| `scripts/release_check.py` | stdlib release gate: tag shape, versions, compatibility declarations |
| `docs/compatibility.md` | compatibility matrix, one row per release |
| `.gitattributes` | forces LF for every text file |

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.12 or later, .NET SDK 10.0.4xx.

```sh
uv sync --locked
uv run pytest
uv run python -c "import sys; from importlinter.cli import lint_imports; sys.exit(lint_imports(no_cache=True))"   # package boundaries (or: uv run lint-imports --no-cache)
uv run python -m pytest archshell-core/conformance   # extension conformance suite
echo '{}' | uv run archshell-engine next --feature probe   # first probe action: llm_task, revision 1
echo '{}' | uv run archshell-engine next --feature probe --lang es   # same, texts in Spanish from the ask on
uv run archshell --version
dotnet build archshell-dotnet/analyzer
dotnet run --project archshell-dotnet/analyzer
```

The engine speaks JSON over stdin/stdout, one message per invocation. stdout carries only the protocol; logs go to stderr as JSON lines. Errors are `archshell/error/v1` messages with exit code 2.

## Boundaries and extensions

### Package boundaries (AD-1, AD-4)

[import-linter](https://import-linter.readthedocs.io/) (pinned in `uv.lock`, configured under `[tool.importlinter]` in the root `pyproject.toml`) enforces four contracts. CI runs them as their own step; a broken contract fails the build.

| Contract | Rule |
| --- | --- |
| `core-never-imports-packs` | the core `archshell` never imports `archshell_dotnet` |
| `core-layers` | `archshell.engine` and `archshell.cli` above `adapters`, above `render`, above `domain`; a lower layer never imports a higher one |
| `inner-modules-stay-inner` | `domain`, `render`, and `ports` never import `adapters`, `engine`, or `cli` |
| `domain-forbidden-imports` | `domain` never imports `render`, `ports`, `adapters`, `engine`, `cli`, `archshell_dotnet`, `jinja2`, `jsonschema`, or `yaml` (a forbidden list) |

The stdlib-only rule (every domain import is the standard library or `archshell.domain`) is an allow-list enforced by `archshell-core/tests/test_boundaries.py` in the pytest step, not by import-linter. That file also proves that a core import of the pack, or a domain import of `yaml` or `archshell.adapters`, breaks the contracts (on a scratch copy). Where the `lint-imports` launcher is blocked (see below), run the contracts through Python:

```sh
uv run python -c "import sys; from importlinter.cli import lint_imports; sys.exit(lint_imports(no_cache=True))"
```

### Extension loading (AD-14, FR-36)

Extensions are objects that distributions register under entry-point groups. `archshell.adapters.extensions.load(kind)` loads one group; `language_packs()` returns the packs keyed by language. No engine or CLI command calls the loader yet. There is no `archshell.hosts` group: host adapters (the Claude Code plugin) are files, not Python.

| Kind | Entry-point group | `extension_contract` this core supports |
| --- | --- | --- |
| `language_packs` | `archshell.language_packs` | `archshell/language-pack-ext/v1` |
| `providers` | `archshell.providers` | `archshell/provider-ext/v1` |
| `ci` | `archshell.ci` | `archshell/ci-ext/v1` |

Each loaded object must expose `extension_contract` equal to that version; a language pack must also expose a non-blank `language`. Refusals are typed errors that name the entry point and the distribution:

| Code | Error | When |
| --- | --- | --- |
| `incompatible_extension` | `IncompatibleExtension` | `extension_contract` missing or another version, or a language pack without `language` |
| `duplicate_language` | `DuplicateLanguage` | two language packs claim the same `language` (names both) |

`archshell-dotnet` registers `dotnet = "archshell_dotnet:PACK"` in `archshell.language_packs`, with `language = "csharp"`.

### Conformance suite (FR-37)

`archshell-core/conformance/` is a pytest suite every extension must pass; CI runs it as its own step. It checks every installed language pack (loads through its entry point, declares the supported contract and its language) and the Claude Code plugin as files (manifest, `run.md` covering all six action types, the plugin agent, hook wiring, the `ARCHSHELL_ENGINE` override), plus an engine-interface (`human_ui`) run through the scripted no-op host (see "End-to-end no-op flow"). Run it locally with:

```sh
uv run python -m pytest archshell-core/conformance
```

Adding a check means adding a `test_*.py` file there; see `archshell-core/conformance/README.md`.

## Engine protocol (`archshell/action/v1`)

The host loops: `archshell-engine next --feature <slug>`, execute the returned action, `archshell-engine submit --feature <slug>` with the result, until the action is `done` or `report`. `--feature <slug>` (kebab-case, at most 64 characters) comes first, optionally followed by `--lang en|es` (see "Conversation language"); nothing else is accepted. The project root is the nearest folder at or above the working directory that holds `.archshell/`, never walking past a repository root (a folder holding `.git`); the working directory itself when none does (see cursor state below). Every message carries `schema: archshell/<kind>/v1`.

| Command | stdin | stdout |
| --- | --- | --- |
| `next` | `{}` | the pending action; on first call the flow starts at revision 1. Repeating `next` returns the same action and writes nothing. |
| `submit` | an `archshell/submit/v1` message | the next action, revision +1 |

Action fields (all types): `schema`, `type`, `feature`, `action_id`, `revision`, plus one text field per type:

| `type` | Text field | Extra | Terminal |
| --- | --- | --- | --- |
| `llm_task` | `prompt_file` | | no |
| `ask` | `question` | `header`, `options`, `nonce` | no |
| `confirm` | `question` | `header`, `options`, `nonce` | no |
| `instruct_agent` | `instruction` | | no |
| `report` | `summary` | | yes |
| `done` | `summary` | | yes |

An `llm_task` does not carry its prompt inline. `prompt_file` is a POSIX path, relative to the project root, of `.archshell/state/prompts/<action_id>.md`, which the engine writes before it replies and which holds the prompt with line endings normalized to LF, UTF-8 without BOM. The file stays while the task is pending; a repeated `next` returns the same `prompt_file` and rewrites the file only if it is missing or differs from the prompt.

An `ask` or `confirm` also carries the question text the host shows, written by the engine: `header` (non-blank, at most 12 characters) and `options` (2 to 4 unique, non-blank labels). The host never writes its own question text. For `confirm`, the first option confirms and every other option declines. An `ask` or `confirm` without valid `header` and `options` is not a valid action.

A submit echoes `feature`, `action_id`, and `revision`, and carries `result` (an object). For `ask` and `confirm` it also echoes the issued `nonce` and sets `source: "human_ui"`; only the host's native question tool may produce that. A submit never carries a verdict.

```json
{"schema":"archshell/submit/v1","feature":"probe","action_id":"…","revision":2,"nonce":"…","source":"human_ui","result":{"answer":"yes"}}
```

An `llm_task` result must match the core JSON Schema (draft 2020-12, `archshell-core/archshell/schemas/`) that its step names. Every `llm_task` today uses `archshell/llm-text/v1`: an object with exactly one property, `text`, a non-empty string, e.g. `{"text":"…"}`. The engine validates the result on `submit`, under the lock, after the `action_id`/`revision` checks. An invalid result re-issues the same step as a new `llm_task` (fresh `action_id`, `revision` +1, new `prompt_file`) whose prompt is the step's prompt followed by `Your previous answer did not match archshell/llm-text/v1:` and one `- <path>: <message>` line per schema error (path as a JSON path such as `$` or `$.text`). At most `max_retries` retries (default 2, set in `.archshell/config.yaml`) are issued per step; the next invalid result is refused with `invalid_output` and the state stays as it was, with the last retry still pending. The retry count is kept in the state file (`attempts`, with the errors that caused the latest retry in `errors`) and resets when the cursor advances. A stale submit is refused before validation and does not count. `ask`, `confirm`, and `instruct_agent` results are not schema-checked. The host never retries on its own: a retry is simply the next `llm_task` it receives.

Every command works in the project root: the nearest folder at or above the working directory that holds `.archshell/`, never walking past a repository root (a folder holding `.git`); the working directory itself when none does. So `next`/`submit` run from a subfolder use the same state, and the hooks guard the same root. Cursor state lives at `.archshell/state/<slug>.yaml` (sorted-key JSON; gitignored once the layout's `.archshell/.gitignore` exists, see below). Every write is a revision compare-and-swap under an OS advisory lock on `.archshell/locks/<slug>.lock` (`flock` on POSIX, `msvcrt.locking` on Windows). The OS releases the lock once the holder's process has ended, even when it was killed (a command run in that brief moment can still be refused with `locked`), so a lock file left behind by a killed engine needs no cleanup. The file stays and holds the holder's pid, readable while the lock is held. A refused command never changes the state file.

### `.archshell/` layout (AD-10)

Every engine-owned path is named in one module, `archshell.domain.layout`. `archshell.adapters.layout.create_layout(root)` builds the layout in a project; no CLI command runs it yet (bootstrap comes later). It is idempotent: it never overwrites an existing `config.yaml`, and a rerun changes no bytes. `next`, `submit`, and the hooks do not create the layout; they create only the directories they write into, and a refused command creates no `.archshell/` at all.

| Path | Tracked by git | Purpose |
| --- | --- | --- |
| `.archshell/config.yaml` | yes | project config (`archshell/config/v1`) |
| `.archshell/.gitignore` | yes | generated; ignores `state/`, `locks/`, `txn.json` (the project's root `.gitignore` is never written) |
| `.archshell/charter/`, `vocabulary/`, `features/`, `records/`, `derived/` | yes | created empty (git does not track empty directories) |
| `.archshell/state/` | no | cursor state `<slug>.yaml`, `llm_task` prompts under `state/prompts/` |
| `.archshell/locks/` | no | per-feature lock files |
| `.archshell/txn.json` | no | reserved for the transaction journal |

`config.yaml` is written with the canonical YAML emitter and read with the restricted-subset reader, then validated against `archshell-core/archshell/schemas/config-v1.json`:

```yaml
schema: archshell/config/v1
max_retries: 2
```

| Field | Required | Value |
| --- | --- | --- |
| `schema` | yes | `archshell/config/v1` |
| `max_retries` | no (default 2) | integer 0–10: retries issued for an invalid `llm_task` result before `invalid_output` |

No other keys are allowed. A missing `config.yaml` means the defaults. `submit` reads it on every call; an invalid one is refused with `invalid_config`.

### Confirmation records (`archshell/record/v1`)

Every human confirmation leaves one structured record (FR-53), committed with the change it approves, at `.archshell/records/<feature-slug>/<seq>-<flow>.yaml`. `seq` is zero-padded to four digits and counted per feature from `0001`, shared by all flows (`0001-plan.yaml`, `0002-contract.yaml`). `flow` is one of `plan`, `extension`, `contract`, `contract-revision`, `breaking-change`, `growth`, `vocabulary`. The slug follows the `--feature` rule; `bootstrap` is a valid slug.

```yaml
schema: archshell/record/v1
feature: cancel-appointment
seq: 1
flow: plan
fingerprint: "sha256:…"
confirmed_by: ana
confirmed_at: "2026-11-14T09:30:00Z"
derived: false
content:
  decision: cancel within 24 hours
```

| Field | Required | Value |
| --- | --- | --- |
| `schema` | yes | `archshell/record/v1` |
| `feature`, `seq`, `flow` | yes | must match the record's path |
| `fingerprint` | yes | `sha256:<64 hex>` of the canonical YAML bytes of the confirmed content |
| `confirmed_by` | yes | non-empty, self-reported (AD-18) |
| `confirmed_at` | yes | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `derived` | yes | `true` when the content is derived with no new decisions (FR-59) |
| `content` | no | the confirmed content |

No other keys are allowed. Records are **append-only**: `archshell.adapters.records.append_record(root, …)` validates the record against `archshell-core/archshell/schemas/record-v1.json`, then creates the next file exclusively and never replaces or edits an existing one. Concurrent writers claim a `seq` by exclusive create of `.archshell/locks/records/<feature>/<seq>.claim` (gitignored, removed after the write), so each gets a distinct `seq`; the record is written to a temporary file, fsynced, and hard-linked into place, so it is either fully present or absent. A writer killed mid-write may leave a gap in the sequence. `read_records(root, feature)` returns a feature's records in `seq` order, each validated, and raises `InvalidRecord` naming the file when one fails the schema, is not a `<seq>-<flow>.yaml` name, or names a different feature, seq, or flow than its path. No command writes records yet; agents still cannot write under `.archshell/`.

Until real feature workflows exist, two built-in flows exercise the action set: `probe` (`llm_task` → `ask` → `confirm` → `instruct_agent` → `done`) and `probe-report` (`llm_task` → `report`).

### Conversation language

`next` and `submit` take an optional `--lang en|es` (default `en`): the language of the developer's conversation, which only the host can see. The host passes `es` when the developer writes in Spanish and `en` otherwise (`archshell-claude-code/commands/run.md` says so); it never translates engine text itself. The engine renders every developer-facing text in that language: an `ask`/`confirm` `question`, `header`, and `options`, an `instruct_agent` `instruction`, a `report`/`done` `summary`, and the `message` of every error. Codes, `details`, every other field, and the one-message protocol do not change. An error raised before a valid `--lang` is read (an unknown command, a missing `--feature`, a bad `--lang`) is English; one raised after it (such as unexpected arguments after `--lang es`) is in that language.

The text is rendered when an action is issued. A repeated `next` returns the pending action exactly as it was issued, whatever `--lang` it carries; a new language takes effect from the next action on. The state file holds the pending action in English plus the language it was issued in (`lang`; a state file without it reads as `en`).

Texts live in per-language catalogs, `archshell-core/archshell/catalogs/en.yaml` and `es.yaml` (`schema: archshell/catalog/v1`, `lang`, and `messages`: a message key such as `flow.probe.ask.question` or `error.stale_action` mapped to a template with `{name}` placeholders; read as NFC). The domain names text only by key plus arguments; `archshell.adapters.catalogs` loads the files. Every catalog has the same keys with the same placeholders, and every key the code uses exists; `tests/test_messages.py` enforces both. Never localized: the `llm_task` prompt file and its retry text, the state file, records, config, logs, every other written file, and hook verdicts and hook errors (a hook gets no conversation language).

Error codes (`archshell/error/v1`, exit code 2):

| Code | Meaning |
| --- | --- |
| `unknown_command` | no command, or not `next`/`submit`/`guard-write`/`check-edit`/`on-stop` |
| `invalid_feature` | `--feature` missing or not a valid slug, or the submit's `feature` differs from it |
| `invalid_arguments` | arguments after `--feature <slug>` other than one `--lang en\|es`, including `--lang` with no value or another language (`details` holds `arguments` and `languages`); for a hook command, an option it does not take, a repeated option, or an option without a value |
| `unknown_host` | a hook command's `--host` names no known host renderer |
| `invalid_message` | stdin is not a UTF-8 JSON object, the submit message is malformed, or a hook message (or, with `--host`, the host's hook payload) is not a valid `archshell/hook/v1` message, e.g. a missing or empty `path` |
| `unknown_feature` | no workflow exists for this slug |
| `no_pending_action` | submit before the flow started or after `done`/`report` |
| `stale_action` | the submit's `action_id` or `revision` is not the pending one (includes replaying a consumed `ask`/`confirm`) |
| `nonce_mismatch` | `ask`/`confirm` result with a missing or wrong `nonce` |
| `invalid_source` | `ask`/`confirm` result without `source: "human_ui"` |
| `invalid_output` | an `llm_task` result still does not match its output schema after the retries; `details` holds `output_schema`, `error_count`, and `errors` (at most 10 `{path, message}` entries, paths and messages cut at 200 characters each) |
| `locked` | another invocation holds the feature's lock |
| `invalid_state` | the state file is unreadable |
| `invalid_config` | `submit` found a `.archshell/config.yaml` that is not UTF-8, not restricted-subset YAML, or does not match `archshell/config/v1`; `details` holds `config` (the path) and `errors` (at most 10 `{path, message}` entries); the state is unchanged |
| `internal_error` | unexpected engine failure |

On Windows machines where Application Control (Smart App Control) blocks unsigned executables, the uv launchers (`pytest.exe`, `archshell-engine.exe`) and the analyzer build output cannot start. Use the module forms instead: `uv run python -m pytest`, `echo '{}' | uv run python -m archshell.engine next --feature probe`.

## Hook sub-protocol (`archshell/hook/v1` → `archshell/verdict/v1`)

Three commands give per-write, per-edit, and stop-time verdicts. The engine computes every verdict and the protected set; host config holds no paths or rules.

| Command | Options | stdin | Rule today |
| --- | --- | --- | --- |
| `guard-write` | `[--host <id>]` | `{"schema":"archshell/hook/v1","path":"<path>","cwd":"<dir>"}` (`cwd` optional) | `block` every path under `.archshell/` (AD-10), else `allow` |
| `check-edit` | `[--host <id>]` | same as `guard-write` | always `allow` (per-edit checks come with epic 4) |
| `on-stop` | `[--feature <slug>] [--host <id>]` | `{"schema":"archshell/hook/v1"}` | always `allow` (stop gates come later) |

The verdict is one line on stdout, exit code 0:

```json
{"schema":"archshell/verdict/v1","verdict":"block","code":"protected_path","reason":"ArchShell: .archshell/x is engine-owned. …"}
```

`verdict` is `allow` or `block`; `code` is `allowed` for an allow; `reason` is the text shown to the agent. Paths are resolved against `cwd` (default: the project root), `.` and `..` are collapsed, Win32 device prefixes are stripped, trailing dots and spaces in names are ignored, `\` counts as a separator, and the result is compared with the project root case-insensitively. A drive-relative path such as `C:x` cannot be resolved and is blocked (`unresolvable_path`). The check runs twice, once lexically and once with symlinks resolved (`realpath`) for both root and path; either block wins. A path outside the project root is allowed.

**Fail-closed rule:** `guard-write` never fails open. Bad arguments, an unknown host, a non-JSON or malformed message, or any unexpected engine error yields a `block` verdict whose `code` is the error code (`invalid_arguments`, `unknown_host`, `invalid_message`, `internal_error`, …) with the `archshell/error/v1` message in `error`, exit code 2. `check-edit` and `on-stop` errors are plain `archshell/error/v1` messages, exit code 2.

```sh
echo '{"schema":"archshell/hook/v1","path":".archshell/x"}' | uv run python -m archshell.engine guard-write   # block, protected_path
echo '{"schema":"archshell/hook/v1","path":"src/app.py"}' | uv run python -m archshell.engine guard-write   # allow
```

### Host renderers (`--host`)

`--host <id>` reads the host's own hook payload instead of `archshell/hook/v1` and prints the host's hook output instead of the verdict, with exit code 0. For `guard-write`, two cases exit 2 with a host-neutral `block` verdict (carrying `error`) instead of host output: an unknown `--host` (`unknown_host`, no renderer to use) and a renderer that fails (`internal_error`). For `check-edit` and `on-stop`, an unknown `--host` exits 2 with a plain `archshell/error/v1` message, and a failing renderer prints nothing and exits 0. Renderers are pure functions under `archshell/render/hosts/`, keyed by host id; today only `claude-code`:

| Command | Claude Code event | Payload read | `block` renders as | `allow` renders as |
| --- | --- | --- | --- | --- |
| `guard-write` | PreToolUse | `tool_input.file_path` (Write, Edit, MultiEdit) or `tool_input.notebook_path` (NotebookEdit), `cwd` | `hookSpecificOutput.permissionDecision: "deny"` with the engine's reason | nothing (the normal permission flow continues) |
| `check-edit` | PostToolUse | same | `{"decision":"block","reason":…}` (reason fed to the agent) | nothing |
| `on-stop` | Stop | `cwd` | `{"decision":"block","reason":…}` (agent keeps working) | nothing |

With `--host`, a `guard-write` error still renders a deny; `check-edit` and `on-stop` errors render nothing, so a broken hook never traps the agent.

## Generation path

Every file ArchShell generates goes through one path, so its bytes are identical on Windows, Linux, and macOS (AD-4, AD-5). Nothing writes a generated file any other way, and no formatter ever runs on the output.

| Step | Module | Kind |
| --- | --- | --- |
| read template text | `archshell.adapters.files.read_text` | IO |
| render | `archshell.render.templates.render(text, context)`: Jinja2 3.1.6, `StrictUndefined`, no autoescape, `keep_trailing_newline`, no loader (`include`/`import`/`extends` refused), nondeterministic `lipsum` and `random` removed | pure |
| emit YAML | `archshell.render.yaml_emit.emit(data)` | pure |
| normalize | `archshell.render.normalize.normalize(text)`: drop a leading BOM, CRLF and lone CR to LF, NFC, exactly one final `\n` | pure |
| write | `archshell.adapters.files.write_text(path, text)`: normalizes, encodes UTF-8 without BOM, writes a unique temp file in the same directory, fsyncs it, and `os.replace`s it (removed on failure), skips identical bytes | IO |
| read YAML | `archshell.adapters.yaml_read.read(path)` / `load(text)` | IO |

`render` and `emit` already return normalized text; `write_text` normalizes again, so text from anywhere reaches disk in the same form.

A template that uses a name the context does not define is refused with `RenderError` naming it (`names`), even inside a branch not taken; an undefined attribute at render time, a syntax error, and any exception raised while rendering are `RenderError` too.

**YAML subset (AD-11).** The emitter writes only `str`, `int`, `bool`, `null`, lists, and mappings with `str` keys, in block style with two-space indentation (`{}` and `[]` for empty collections). Keys are sorted by codepoint unless the mapping is an `Ordered`, whose order the caller fixes. Strings are composed to NFC; a string is plain only if every YAML 1.1 and 1.2 reader reads it back unchanged (it starts with a letter or `_`, uses only letters, digits, `_./-` and single inner spaces, and is not a word such as `yes`, `no`, `on`, `off`, `y`, `n`, `true`, `false`, `null`); otherwise it is double-quoted with JSON-style escapes. Floats and every other type are refused with `EmitError`. The reader is PyYAML's `SafeLoader` plus checks: anchors, aliases, any explicit tag, merge keys, plain (unquoted) scalars the emitter would quote (only `true`, `false`, `null`, `~`, empty, and decimal integers are accepted unquoted, so `1:30`, `010`, `0x1f`, `yes` are refused), duplicate keys, more than one document, and values outside the subset (floats, timestamps) are refused with `YamlReadError`. Emit then read returns equal data.

**Golden byte check.** `archshell-core/tests/golden/` holds templates, their contexts, YAML data, and `manifest.sha256`. `tests/test_golden.py` renders or re-emits each one through `write_text` and compares the sha256 of the written bytes with the manifest; CI runs it on all three OSes, so a match everywhere means byte identity across them. After an intended change, copy the actual manifest printed by the failing test into `manifest.sha256`.

**Line endings.** `.gitattributes` sets `* text=auto eol=lf` (binary formats marked), so tracked text files are LF in the index and the working tree on every OS, including Windows with `core.autocrlf=true`.

## Install the Claude Code plugin from the local marketplace

The plugin runs `archshell-engine` from `PATH` unless the `ARCHSHELL_ENGINE` environment variable is set, in which case every engine call uses that command instead. Install the core first:

```sh
uv tool install --editable ./archshell-core
```

Then, in Claude Code:

```text
/plugin marketplace add C:\git\arch-shell
/plugin install archshell@arch-shell
```

Restart Claude Code if asked, then run `/archshell:ping`. It shows the first action of the `probe` flow (`llm_task`). It writes cursor state under `.archshell/` in the project root, so repeated pings show the same action.

### Override the engine command

On machines where the `archshell-engine` launcher cannot run (for example under Application Control), set `ARCHSHELL_ENGINE` before starting Claude Code to the module form, pointing at this checkout:

```sh
export ARCHSHELL_ENGINE="uv run --project C:\git\arch-shell python -m archshell.engine"
```

```powershell
$env:ARCHSHELL_ENGINE = "uv run --project C:\git\arch-shell python -m archshell.engine"
```

`/archshell:ping` and `/archshell:run` both use it.

### Run a flow: `/archshell:run <feature>`

`/archshell:run probe` drives the engine loop inside Claude Code: `next`, execute the action, `submit` the result, until `done` or `report`. Every submit goes to the engine on stdin. The plugin holds no workflow logic; all user-facing text comes from the engine.

| Action | How the plugin executes it | `result` it submits |
| --- | --- | --- |
| `llm_task` | a fresh `archshell:archshell-task` agent, given only the prompt file's absolute path (project root joined with `prompt_file`); the agent reads the file and answers | `{"text": <agent answer>}` |
| `ask` | Claude Code's native question tool (`AskUserQuestion`), showing the action's `question`, `header`, and `options` verbatim, plus free text | `{"answer": <chosen option or typed text>}`, plus the action's `nonce` and `source: "human_ui"` |
| `confirm` | the native question tool, showing the action's `question`, `header`, and `options` verbatim | `{"confirmed": true\|false}` (`true` for the first option), plus the action's `nonce` and `source: "human_ui"` |
| `instruct_agent` | the session agent carries the instruction out | `{"status": "done"}` |
| `report`, `done` | `summary` shown verbatim; the loop ends | nothing |

An `archshell/error/v1` reply stops the loop and shows its `code` and `message`; nothing is retried. An unknown action type also stops the loop with a visible error.

Try both built-in flows:

```text
/archshell:run probe          # agent task, question, confirmation, instruction, then the done summary
/archshell:run probe-report   # agent task, then the report summary
```

Cursor state persists under `.archshell/` in the project root, so a finished flow keeps returning its terminal action; delete `.archshell/state/<feature>.yaml` to run it again.

### End-to-end no-op flow (no Claude Code, no model)

`archshell-core/tests/test_no_op_flow.py` runs the whole platform contract at once, as the plugin would. A scripted host (`ScriptedHost` in `archshell-core/tests/conftest.py`) stands in for Claude Code and the model, built only from the plugin's own contract files: it answers each action the way `commands/run.md` says (the "user" picks the first option, the "agent" reads the prompt file and replies with one sentence) and runs the literal `hooks/hooks.json` command strings through bash, with `ARCHSHELL_ENGINE` set to `python -m archshell.engine`. The engine runs as a subprocess through its public CLI. Changing a `run.md` result shape or a hook command therefore breaks the run.

One run covers `probe` then `probe-report` (all six action types; `done` and `report` both terminal), all three hooks (a protected write denied, ordinary writes allowed, `check-edit` and `on-stop` silent, and `guard-write` failing closed with exit 2 when the engine cannot start), stale and replayed submissions, an `llm_task` answered invalidly three times (retried twice, then `invalid_output`), and, after `done`, a `plan` confirmation record appended twice by the host and validated against `record-v1.json` (no engine command writes records yet). It runs in `en` and `es`: question and summary texts come from the `es` catalog, while every written file stays English, ASCII, LF-only, UTF-8 without BOM, and NFC. The id-independent output of the `es` run (prompt files, both records, the done and report summaries) is hashed against the `no-op/...` entries of `archshell-core/tests/golden/manifest.sha256`, so CI proves the same bytes on Ubuntu, Windows, and macOS. The same host gives the conformance suite its engine-interface (`human_ui`) run. Run it locally from the repository root (on Windows it needs Git Bash):

```sh
uv run python -m pytest -v archshell-core/tests/test_no_op_flow.py
uv run python -m pytest -v archshell-core/conformance/test_plugin_conformance.py -k engine_interface
```

### Hooks: see a protected write refused

The plugin ships `archshell-claude-code/hooks/hooks.json`, which Claude Code loads with the plugin. Each hook is a one-line forward that first changes to `${CLAUDE_PROJECT_DIR:-.}` (so the protected root is the project root even after the agent runs `cd`) and then calls the engine through the same `ARCHSHELL_ENGINE` override, with engine stderr appended to `${TMPDIR:-/tmp}/archshell-engine.log`:

| Event | Matcher | Engine command |
| --- | --- | --- |
| PreToolUse | `Write\|Edit\|MultiEdit\|NotebookEdit` | `guard-write --host claude-code`; if the `cd` fails or the engine cannot run at all, the hook exits 2 and the write is blocked |
| PostToolUse | `Write\|Edit\|MultiEdit\|NotebookEdit` | `check-edit --host claude-code` |
| Stop | (all) | `on-stop --host claude-code` |

After updating the plugin (`/plugin` → update, then restart Claude Code), ask the agent to create `.archshell/test.txt`. Claude Code refuses the write and shows the engine's reason (`ArchShell: .archshell/test.txt is engine-owned. …`). Ordinary edits proceed with no hook output. Writes made through shell commands are not intercepted; `archshell verify` in CI is the real guard.

## Releases

Only signed pre-releases are published: tags `vX.Y.Z-(alpha|beta|rc).N`. `archshell` and `archshell-dotnet` go to [TestPyPI](https://test.pypi.org/); `Archshell.Analyzer` goes to GitHub Packages (NuGet). Nothing is published to PyPI or nuget.org, and stable tags are refused.

### Versions and the compatibility matrix

The committed files are the source of truth; the release refuses a tag that does not match them and never rewrites a version. For tag `v0.1.0-rc.1`:

| File | Value |
| --- | --- |
| `archshell-core/pyproject.toml`, `archshell-dotnet/pyproject.toml`, `uv.lock` | `0.1.0rc1` (PEP 440) |
| `archshell-dotnet/analyzer/Archshell.Analyzer.csproj` `<Version>`, `archshell_dotnet.ANALYZER_VERSION` | `0.1.0-rc.1` (SemVer) |
| `archshell-claude-code/.claude-plugin/plugin.json` `version` | `0.1.0-rc.1` (SemVer) |
| `archshell-dotnet/pyproject.toml` `archshell` dependency (the pack's core range) | must include the core version, e.g. `>=0.1.0rc1,<0.2` |
| `archshell-claude-code/archshell-host.json` `protocol` | the core `ACTION_SCHEMA`, `archshell/action/v1` |
| `docs/compatibility.md` | a row for the tag |

Check a tag locally before pushing it (exit 1 lists every problem, naming the file):

```sh
uv run python scripts/release_check.py v0.1.0-rc.1
```

### One-time setup

1. **TestPyPI Trusted Publishing.** On TestPyPI, under *Your projects → Publishing*, add a pending publisher for each of `archshell` and `archshell-dotnet`: owner `AntonioTamez`, repository `arch-shell`, workflow `release.yml`, environment `testpypi`. No API token is stored anywhere.
2. **GitHub environment.** In the repository settings, create the environment `testpypi` (optionally restrict it to tags `v*`).
3. **Signing key.** Register your GPG or SSH signing key on GitHub (*Settings → SSH and GPG keys*, as a signing key) and configure git to sign tags with it (`git config user.signingkey …`; for SSH also `git config gpg.format ssh`).
4. **GitHub Packages.** Nothing to create: the first publish creates `Archshell.Analyzer`, linked to this repository through the csproj `RepositoryUrl`. Reading it needs a token with `read:packages`, even though the package is public.

### Cut a signed pre-release

1. On a branch, bump every version file above to the new pre-release, run `uv lock`, and add the tag's row to `docs/compatibility.md`. Run `uv run python scripts/release_check.py vX.Y.Z-rc.N`, merge to `main`.
2. Tag the merged commit with a signed, annotated tag and push it:

   ```sh
   git tag -s v0.1.0-rc.1 -m "v0.1.0-rc.1"
   git push origin v0.1.0-rc.1
   ```

3. The `release` workflow runs:
   - `verify`: refuses a tag that is not a pre-release, is lightweight, or whose signature GitHub does not report as verified, then runs `scripts/release_check.py`;
   - `ci`: the normal CI (`ci.yml`) on the tagged commit;
   - `publish`: builds both wheels and sdists, packs the analyzer and pushes it to GitHub Packages with `GITHUB_TOKEN`, and only then uploads the Python distributions to TestPyPI through Trusted Publishing, last because that upload is permanent; only this job has write permissions;
   - `install`: on Ubuntu, Windows, and macOS, a clean uv project installs `archshell` and `archshell-dotnet` pinned to TestPyPI (every other dependency from PyPI) and runs `archshell --version`, then installs `Archshell.Analyzer` from GitHub Packages with `dotnet tool install` and runs `archshell-analyzer`.

A failed step publishes nothing after it: a failure in `verify`, `ci`, the build, or the pack publishes nothing at all, and a failed GitHub Packages push leaves TestPyPI untouched. Re-running `publish` is safe: the NuGet push skips a version already there, and `uv publish` skips files already uploaded. TestPyPI never accepts different files under a version it already holds, so to change a published release, cut the next `rc.N`.

To install a published pre-release by hand, use the same project shape as the `install` job: TestPyPI as an `explicit = true` `[[tool.uv.index]]`, with `[tool.uv.sources]` pointing only `archshell` and `archshell-dotnet` at it.
