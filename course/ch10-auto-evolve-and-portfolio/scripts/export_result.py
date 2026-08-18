"""Render L3 and export an allowlisted portfolio from a completed experiment."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ses.automation.portfolio import export_portfolio, portfolio_semantic_sha256
from ses.reporting.l3 import write_l3_html


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must use ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create public Lesson 10 outputs from a completed experiment."
    )
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--l3", type=Path, required=True)
    parser.add_argument("--portfolio", type=Path, required=True)
    parser.add_argument(
        "--created-at",
        type=_timestamp,
        default=datetime(2026, 8, 19, 9, 30, tzinfo=UTC),
    )
    args = parser.parse_args(argv)
    write_l3_html(args.experiment, args.l3)
    manifest = export_portfolio(
        args.experiment,
        args.portfolio,
        created_at=args.created_at,
    )
    print(
        json.dumps(
            {
                "experiment_id": manifest.experiment_id,
                "file_count": len(manifest.files),
                "l3": args.l3.as_posix(),
                "portfolio": args.portfolio.as_posix(),
                "portfolio_semantic_sha256": portfolio_semantic_sha256(args.portfolio),
                "result_kind": "fixed_offline_reference",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
