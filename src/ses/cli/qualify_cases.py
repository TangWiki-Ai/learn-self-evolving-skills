"""Offline CLI for Ticket 07 case verification and develop admission."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.testset.verified import qualify_cases


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    ticket = root / "data" / "testset" / "ticket07"
    parser = argparse.ArgumentParser(
        prog="ses qualify-cases",
        description="Verify fixed candidate signals and build the develop catalog offline.",
    )
    parser.add_argument(
        "--candidates", type=Path, default=ticket / "candidate-seeds.jsonl"
    )
    parser.add_argument("--variants", type=Path, default=ticket / "variant-plan.json")
    parser.add_argument("--reviews", type=Path, default=ticket / "human-reviews.jsonl")
    parser.add_argument(
        "--protected-manifest",
        type=Path,
        action="append",
        dest="protected",
        default=None,
    )
    parser.add_argument(
        "--judge-fixture",
        type=Path,
        default=root / "tests" / "fixtures" / "judges" / "calibration.json",
    )
    parser.add_argument("--output", type=Path, default=ticket / "generated")
    parser.add_argument(
        "--split", choices=("develop", "selection", "final"), default="develop"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    protected = args.protected or [
        root / "data" / "testset" / "protected" / "creator-manifest.json",
        root / "data" / "testset" / "protected" / "selection-manifest.json",
        root / "data" / "testset" / "protected" / "final-manifest.json",
    ]
    try:
        summary = qualify_cases(
            candidate_path=args.candidates,
            variant_plan_path=args.variants,
            reviews_path=args.reviews,
            protected_manifests=protected,
            model_calibration_fixture=args.judge_fixture,
            output=args.output,
            split=args.split,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        # Avoid echoing arbitrary paths or private oracle values from nested errors.
        reason = str(exc)
        if "/" in reason or "\\" in reason:
            reason = type(exc).__name__
        print(f"qualify_cases_error:{reason}", file=sys.stderr)
        return 1
    payload = {
        "candidate_count": summary.candidate_count,
        "qualified_count": summary.qualified_count,
        "rejected_count": summary.rejected_count,
        "pending_count": summary.pending_count,
        "data_version": summary.data_version,
        "output": str(summary.output),
        "network_used": False,
        "live_provider_used": False,
    }
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        for key, value in payload.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
