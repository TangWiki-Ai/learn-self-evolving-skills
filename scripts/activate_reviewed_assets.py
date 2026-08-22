"""Activate the reviewed develop catalog and v0 Skill for live validation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ses.journey.asset_activation import ActivationError, activate_reviewed_assets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Activate human-reviewed Journey assets for live validation."
    )
    parser.add_argument(
        "--confirm-signed-asset-review",
        action="store_true",
        help="confirm that the packet's asset review section was signed",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_signed_asset_review:
        print(
            "refusing activation without --confirm-signed-asset-review",
            file=sys.stderr,
        )
        return 2
    try:
        catalog_path, skill_path = activate_reviewed_assets(args.root)
    except (ActivationError, OSError) as exc:
        print(f"asset activation failed: {exc}", file=sys.stderr)
        return 1
    print(f"activated: {catalog_path}")
    print(f"activated: {skill_path}")
    print(
        "next: run both Provider live smokes, complete the release checklist, "
        "then sign the final decision"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
