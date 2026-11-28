"""The engine CLI with fixed version pins, for byte checks of `architecture.yaml`.

`architecture.yaml` records the installed core and pack versions, which change
with every release; the golden bytes must not. This launcher runs the real
engine (`archshell.engine.cli.main`) with `archshell.adapters.versions.pins`
replaced by `PINS`. Usage: `python pinned_engine.py <engine arguments>`.
"""

from __future__ import annotations

import sys

from archshell.adapters import versions
from archshell.engine import cli

CORE_VERSION = "0.0.0-golden"
PACK_VERSION = "0.0.0-golden"


def _pins(pack):
    return CORE_VERSION, {pack.distribution: PACK_VERSION}


if __name__ == "__main__":
    versions.pins = _pins
    sys.exit(cli.main())
