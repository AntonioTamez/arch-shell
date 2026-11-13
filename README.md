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
echo '{}' | uv run archshell-engine next     # {"protocol":"archshell/action/v1","type":"done"}
uv run archshell --version
dotnet build archshell-dotnet/analyzer
dotnet run --project archshell-dotnet/analyzer
```

The engine speaks JSON over stdin/stdout, one message per invocation. stdout carries only the protocol; logs go to stderr as JSON lines. Errors are `archshell/error/v1` messages with exit code 2.

On Windows machines where Application Control (Smart App Control) blocks unsigned executables, the uv launchers (`pytest.exe`, `archshell-engine.exe`) and the analyzer build output cannot start. Use the module forms instead: `uv run python -m pytest`, `echo '{}' | uv run python -m archshell.engine next`.

## Install the Claude Code plugin from the local marketplace

The plugin runs `archshell-engine` from `PATH`, so install the core first:

```sh
uv tool install --editable ./archshell-core
```

Then, in Claude Code:

```text
/plugin marketplace add C:\git\arch-shell
/plugin install archshell@arch-shell
```

Restart Claude Code if asked, then run `/archshell:ping`. It shows the engine's `done` action.
