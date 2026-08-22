"""Top-level command registration for the ``ses`` CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ses.cli import journey


def build_parser() -> argparse.ArgumentParser:
    """Create the public learner parser without running business logic."""
    parser = argparse.ArgumentParser(
        prog="ses",
        description="Improve a Skill with executable evaluation and regression checks.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser(
        "journey", help="Start, resume, inspect, or view the eight-step workflow."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse top-level arguments and return a process exit code."""
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        build_parser().print_help()
        return 0
    command, command_args = values[0], values[1:]
    if command == "journey":
        return journey.main(command_args)
    build_parser().parse_args(values)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
