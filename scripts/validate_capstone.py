#!/usr/bin/env python3
"""Validate the independent shopping capstone without changing course files."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ses.release.capstone import CheckStatus, validate_capstone_course


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--command-evidence", type=Path)
    parser.add_argument("--run-course-tests", action="store_true")
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="Fail only on invalid contracts; report unexecuted CLI evidence as deviations.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = validate_capstone_course(
        args.root,
        command_evidence=args.command_evidence,
        run_course_tests=args.run_course_tests,
    )
    payload = report.json_bytes()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
    if args.as_json:
        print(payload.decode(), end="")
    else:
        print(f"status={report.status.value}")
        print(f"milestones={report.milestone_count}")
        print(f"target_commands={report.target_command_count}")
        for check in report.checks:
            print(f"{check.status.value}:{check.check_id}:{check.summary}")
            for detail in check.details:
                print(f"  {detail}")
    if report.status is CheckStatus.PASS:
        return 0
    if report.status is CheckStatus.DEVIATION:
        return 0 if args.structure_only else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
