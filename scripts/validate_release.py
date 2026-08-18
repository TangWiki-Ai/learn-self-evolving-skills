"""Validate the complete ten-lesson release without changing repository files."""

from __future__ import annotations

import argparse
from pathlib import Path

from ses.release import CheckStatus, validate_release


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-course-tests", action="store_true")
    parser.add_argument(
        "--command-evidence",
        type=Path,
        help="JSON evidence from exact clean-room README command execution.",
    )
    parser.add_argument(
        "--full-data-bundle",
        type=Path,
        action="append",
        default=[],
        help="Fresh full-profile output; pass twice with distinct temporary runs.",
    )
    parser.add_argument(
        "--protected-holdout-root",
        type=Path,
        help=(
            "External complete protected holdout bundle. The repository keeps only "
            "public holdout manifests and commitments."
        ),
    )
    parser.add_argument(
        "--state-bench-archive",
        type=Path,
        help=(
            "Pinned STATE-Bench source tar used to reproduce and verify the external "
            "holdout."
        ),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = validate_release(
        args.root,
        run_course_tests=args.run_course_tests,
        command_evidence=args.command_evidence,
        full_data_bundles=args.full_data_bundle,
        protected_holdout_root=args.protected_holdout_root,
        state_bench_archive=args.state_bench_archive,
    )
    payload = report.json_bytes()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
    if args.as_json:
        print(payload.decode(), end="")
    else:
        print(f"status={report.status.value}")
        print(f"lessons={report.lesson_count}")
        print(f"documented_commands={report.documented_command_count}")
        for check in report.checks:
            print(f"{check.status.value}:{check.check_id}:{check.summary}")
            for detail in check.details:
                print(f"  {detail}")
    if report.status is CheckStatus.PASS:
        return 0
    if report.status is CheckStatus.DEVIATION:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
