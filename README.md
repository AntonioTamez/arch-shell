# arch-shell

ArchShell: an architecture shell that keeps an AI coding agent inside a confirmed architecture.

## Layout

| Path | What it is |
| --- | --- |
| `pyproject.toml`, `uv.lock` | uv workspace root; one lockfile for all Python packages |
| `archshell-core/` | distribution `archshell`: domain, render, ports, adapters, and the executables `archshell-engine` (host protocol) and `archshell` (human/CI commands) |
| `archshell-dotnet/` | distribution `archshell-dotnet`: the .NET language pack (`archshell_dotnet/`) and the Roslyn analyzer (`analyzer/`, `net10.0` .NET tool) |
| `archshell-claude-code/` | Claude Code host plugin; holds no logic and forwards to `archshell-engine` |
| `.claude-plugin/marketplace.json` | local marketplace that lists the plugin |
| `global.json` | pins the .NET SDK band (10.0.4xx, `rollForward: latestFeature`) |
| `.github/workflows/ci.yml` | CI on Ubuntu, Windows, and macOS |
| `.gitattributes` | forces LF for every text file |

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.12 or later, .NET SDK 10.0.4xx.

```sh
uv sync --locked
uv run pytest
echo '{}' | uv run archshell-engine next --feature probe   # first probe action: llm_task, revision 1
uv run archshell --version
dotnet build archshell-dotnet/analyzer
dotnet run --project archshell-dotnet/analyzer
```

The engine speaks JSON over stdin/stdout, one message per invocation. stdout carries only the protocol; logs go to stderr as JSON lines. Errors are `archshell/error/v1` messages with exit code 2.

## Engine protocol (`archshell/action/v1`)

The host loops: `archshell-engine next --feature <slug>`, execute the returned action, `archshell-engine submit --feature <slug>` with the result, until the action is `done` or `report`. `--feature <slug>` (kebab-case, at most 64 characters) is the only argument besides the command. The project root is the working directory. Every message carries `schema: archshell/<kind>/v1`.

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

Cursor state lives at `.archshell/state/<slug>.yaml` (sorted-key JSON, gitignored). Every write is a revision compare-and-swap under the lock file `.archshell/locks/<slug>.lock`. If an engine process is killed while holding the lock, delete the lock file by hand. A refused command never changes the state file.

Until real feature workflows exist, two built-in flows exercise the action set: `probe` (`llm_task` → `ask` → `confirm` → `instruct_agent` → `done`) and `probe-report` (`llm_task` → `report`).

Error codes (`archshell/error/v1`, exit code 2):

| Code | Meaning |
| --- | --- |
| `unknown_command` | no command, or not `next`/`submit`/`guard-write`/`check-edit`/`on-stop` |
| `invalid_feature` | `--feature` missing or not a valid slug, or the submit's `feature` differs from it |
| `invalid_arguments` | arguments after `--feature <slug>`; for a hook command, an option it does not take, a repeated option, or an option without a value |
| `unknown_host` | a hook command's `--host` names no known host renderer |
| `invalid_message` | stdin is not a UTF-8 JSON object, the submit message is malformed, or a hook message (or, with `--host`, the host's hook payload) is not a valid `archshell/hook/v1` message, e.g. a missing or empty `path` |
| `unknown_feature` | no workflow exists for this slug |
| `no_pending_action` | submit before the flow started or after `done`/`report` |
| `stale_action` | the submit's `action_id` or `revision` is not the pending one (includes replaying a consumed `ask`/`confirm`) |
| `nonce_mismatch` | `ask`/`confirm` result with a missing or wrong `nonce` |
| `invalid_source` | `ask`/`confirm` result without `source: "human_ui"` |
| `locked` | another invocation holds the feature's lock |
| `invalid_state` | the state file is unreadable |
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

`verdict` is `allow` or `block`; `code` is `allowed` for an allow; `reason` is the text shown to the agent. Paths are resolved against `cwd` (default: the project root, which is the working directory), `.` and `..` are collapsed, Win32 device prefixes are stripped, trailing dots and spaces in names are ignored, `\` counts as a separator, and the result is compared with the project root case-insensitively. A drive-relative path such as `C:x` cannot be resolved and is blocked (`unresolvable_path`). The check runs twice, once lexically and once with symlinks resolved (`realpath`) for both root and path; either block wins. A path outside the project root is allowed.

**Fail-closed rule:** `guard-write` never fails open. Bad arguments, an unknown host, a non-JSON or malformed message, or any unexpected engine error yields a `block` verdict whose `code` is the error code (`invalid_arguments`, `unknown_host`, `invalid_message`, `internal_error`, …) with the `archshell/error/v1` message in `error`, exit code 2. `check-edit` and `on-stop` errors are plain `archshell/error/v1` messages, exit code 2.

```sh
echo '{"schema":"archshell/hook/v1","path":".archshell/x"}' | uv run python -m archshell.engine guard-write   # block, protected_path
echo '{"schema":"archshell/hook/v1","path":"src/app.py"}' | uv run python -m archshell.engine guard-write   # allow
```

### Host renderers (`--host`)

`--host <id>` reads the host's own hook payload instead of `archshell/hook/v1` and prints the host's hook output instead of the verdict, with exit code 0. Two cases exit 2 with a host-neutral `block` verdict (carrying `error`) instead of host output: an unknown `--host` (`unknown_host`, no renderer to use) and a renderer that fails (`internal_error`). Renderers are pure functions under `archshell/render/hosts/`, keyed by host id; today only `claude-code`:

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

Restart Claude Code if asked, then run `/archshell:ping`. It shows the first action of the `probe` flow (`llm_task`). It writes cursor state under `.archshell/` in the working directory, so repeated pings show the same action.

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

Cursor state persists under `.archshell/` in the working directory, so a finished flow keeps returning its terminal action; delete `.archshell/state/<feature>.yaml` to run it again.

### Hooks: see a protected write refused

The plugin ships `archshell-claude-code/hooks/hooks.json`, which Claude Code loads with the plugin. Each hook is a one-line forward that first changes to `${CLAUDE_PROJECT_DIR:-.}` (so the protected root is the project root even after the agent runs `cd`) and then calls the engine through the same `ARCHSHELL_ENGINE` override, with engine stderr appended to `${TMPDIR:-/tmp}/archshell-engine.log`:

| Event | Matcher | Engine command |
| --- | --- | --- |
| PreToolUse | `Write\|Edit\|MultiEdit\|NotebookEdit` | `guard-write --host claude-code`; if the `cd` fails or the engine cannot run at all, the hook exits 2 and the write is blocked |
| PostToolUse | `Write\|Edit\|MultiEdit\|NotebookEdit` | `check-edit --host claude-code` |
| Stop | (all) | `on-stop --host claude-code` |

After updating the plugin (`/plugin` → update, then restart Claude Code), ask the agent to create `.archshell/test.txt`. Claude Code refuses the write and shows the engine's reason (`ArchShell: .archshell/test.txt is engine-owned. …`). Ordinary edits proceed with no hook output. Writes made through shell commands are not intercepted; `archshell verify` in CI is the real guard.
