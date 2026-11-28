"""Version pins recorded in `architecture.yaml`: the core and its language pack.

The pack's version is read through `importlib.metadata` from the distribution
that registered its entry point, so the core never imports a pack (AD-1).
"""

from __future__ import annotations

from importlib.metadata import version

import archshell
from archshell.adapters.extensions import Extension


def pins(pack: Extension) -> tuple[str, dict[str, str]]:
    """(core version, {pack distribution: its version})."""
    return archshell.__version__, {pack.distribution: version(pack.distribution)}
