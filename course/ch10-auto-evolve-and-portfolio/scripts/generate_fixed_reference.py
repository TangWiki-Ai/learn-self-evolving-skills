"""Regenerate the zero-network Lesson 10 public reference portfolio."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ses.automation.fixed import run_fixed_auto_evolve
from ses.automation.portfolio import export_portfolio, portfolio_semantic_sha256

LESSON = Path(__file__).resolve().parents[1]
ROOT = LESSON.parents[1]
CREATED_AT = datetime(2026, 8, 19, 9, 30, tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate a fixed Lesson 10 reference in a fresh directory."
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".ses/lesson-10-reference"),
        help="fresh destination (default: .ses/lesson-10-reference)",
    )
    args = parser.parse_args(argv)
    destination = args.output_root.resolve(strict=False)
    if destination.exists():
        raise RuntimeError(f"fixed Lesson 10 reference already exists: {destination}")
    with TemporaryDirectory(prefix="ses-lesson-10-") as temporary:
        experiment = Path(temporary).resolve(strict=True) / "experiment"
        state = run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=experiment,
        )
        outcomes = tuple(row.gate_outcome.value for row in state.rounds)
        if outcomes != ("accepted", "rejected"):
            raise RuntimeError("fixed Lesson 10 must reproduce accept then reject")
        if state.final_report is None:
            raise RuntimeError("fixed Lesson 10 did not produce its final aggregate")
        export_portfolio(
            experiment,
            destination,
            created_at=CREATED_AT,
        )
    print(portfolio_semantic_sha256(destination))


if __name__ == "__main__":
    main()
