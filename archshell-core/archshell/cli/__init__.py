"""archshell: human and CI commands. Tracer stub: only --version."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from archshell import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archshell")
    parser.add_argument("--version", action="version", version=f"archshell {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
