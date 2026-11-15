"""ArchShell .NET language pack.

The pack object is registered in the `archshell.language_packs` entry-point
group. It declares only the extension contract it implements and its
language; pack behavior (scaffolding, gates, mapping) arrives in later stories.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("archshell-dotnet")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled tree
    __version__ = "0.0.0"


class DotnetPack:
    """The .NET / C# language pack."""

    extension_contract = "archshell/language-pack-ext/v1"
    language = "csharp"


PACK = DotnetPack()

__all__ = ["PACK", "DotnetPack", "__version__"]
