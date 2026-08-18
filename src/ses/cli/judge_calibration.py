"""Standalone CLI logic for the judge calibration experiment."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from ses.evaluation.calibration import (
    execute_fixed_calibration,
    load_calibration_fixture,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for the ``ses judge-calibration`` subcommand."""

    parser = argparse.ArgumentParser(
        prog="ses judge-calibration",
        description="Execute fixed Judge protocols against reference labels.",
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print a strict JSON calibration report without running a live model."""

    args = build_parser().parse_args(argv)
    report = asyncio.run(
        execute_fixed_calibration(load_calibration_fixture(args.fixture))
    )
    payload = report.model_dump_json(indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
