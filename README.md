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
| `unknown_command` | no command, or not `next`/`submit` |
| `invalid_feature` | `--feature` missing or not a valid slug, or the submit's `feature` differs from it |
| `invalid_arguments` | arguments after `--feature <slug>` |
| `invalid_message` | stdin is not a UTF-8 JSON object, or the submit message is malformed |
| `unknown_feature` | no workflow exists for this slug |
| `no_pending_action` | submit before the flow started or after `done`/`report` |
| `stale_action` | the submit's `action_id` or `revision` is not the pending one (includes replaying a consumed `ask`/`confirm`) |
| `nonce_mismatch` | `ask`/`confirm` result with a missing or wrong `nonce` |
| `invalid_source` | `ask`/`confirm` result without `source: "human_ui"` |
| `locked` | another invocation holds the feature's lock |
| `invalid_state` | the state file is unreadable |
| `internal_error` | unexpected engine failure |

On Windows machines where Application Control (Smart App Control) blocks unsigned executables, the uv launchers (`pytest.exe`, `archshell-engine.exe`) and the analyzer build output cannot start. Use the module forms instead: `uv run python -m pytest`, `echo '{}' | uv run python -m archshell.engine next --feature probe`.

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
