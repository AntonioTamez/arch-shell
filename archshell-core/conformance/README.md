# ArchShell conformance suite

Checks every extension must pass (FR-37). Run it from the repository root:

```sh
uv run python -m pytest archshell-core/conformance
```

CI runs the same command as its own step on Ubuntu, Windows, and macOS.

| Module | Checks |
| --- | --- |
| `test_pack_conformance.py` | every installed `archshell.language_packs` entry point loads, declares `extension_contract = "archshell/language-pack-ext/v1"` and a non-blank `language`, and the core loader accepts the set (no duplicate language); the shipped `archshell-dotnet` pack declares `csharp` |
| `test_plugin_conformance.py` | the Claude Code plugin, checked as files: the manifest, the host protocol declaration (`archshell-host.json` equals the core `ACTION_SCHEMA`), frontmatter of every command and agent, `run.md` covering all six action types and the protocol rules it must state, the plugin agent, the hook wiring in `hooks/hooks.json`, and the `ARCHSHELL_ENGINE` override on every engine call; and the engine-interface (`human_ui`) run: `tests/conftest.py`'s `ScriptedHost` drives `probe` and `probe-report` in `en` and `es` the way `run.md` says, with every `ask`/`confirm` answer carrying the action's nonce and `source: "human_ui"`, and a question answered without it refused (`invalid_source`) |

Adding a check means adding a `test_*.py` file here. Golden and seeded-violation checks for packs arrive with the extensibility launch epic.
