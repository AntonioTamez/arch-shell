"""ArchShell .NET language pack. Skeleton; the LanguagePack arrives in a later story."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("archshell-dotnet")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
