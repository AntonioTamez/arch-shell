# Compatibility matrix (AD-22)

One row per release. `scripts/release_check.py` refuses a release tag whose row is missing, and requires every cell of the tag's row to equal the committed declaration behind its column:

- **archshell** and **archshell-dotnet**: the `version` in `archshell-core/pyproject.toml` and `archshell-dotnet/pyproject.toml` (PEP 440).
- **Core range**: the `archshell` dependency of `archshell-dotnet`; it must include the release's core version.
- **Archshell.Analyzer**: the csproj `<Version>` (SemVer), equal to `archshell_dotnet.ANALYZER_VERSION`.
- **Plugin** and **protocol**: `archshell-claude-code/.claude-plugin/plugin.json` `version` (SemVer) and `archshell-claude-code/archshell-host.json` `protocol`, equal to the core `ACTION_SCHEMA`.

| Release | archshell | archshell-dotnet | Core range | Archshell.Analyzer | Plugin | Protocol |
| --- | --- | --- | --- | --- | --- | --- |
| `v0.1.0-rc.1` | `0.1.0rc1` | `0.1.0rc1` | `>=0.1.0rc1,<0.2` | `0.1.0-rc.1` | `0.1.0-rc.1` | `archshell/action/v1` |
