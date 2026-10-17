"""Command-line entry point: ``scriptorium serve | worker | admin ...``.

Subcommands are registered by the modules that implement them; this file only
dispatches. Keeping it tiny means the two container entrypoints share one
import graph and one settings object.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scriptorium")
    parser.add_subparsers(dest="command", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    print("no subcommands registered yet", file=sys.stderr)
    return 2
