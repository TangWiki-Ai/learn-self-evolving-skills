"""Top-level command registration for the ``ses`` CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser without running any business logic."""
    return argparse.ArgumentParser(
        prog="ses",
        description="Build and evaluate self-evolving skills.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse top-level arguments and return a process exit code."""
    build_parser().parse_args(argv)
    return 0
