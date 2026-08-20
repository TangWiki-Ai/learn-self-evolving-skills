#!/usr/bin/env python3
"""Run fixed shopping capstone checks from a fresh current-worktree copy."""

from __future__ import annotations

import argparse
from pathlib import Path

from ses.release.capstone import (
    capstone_evidence_exit_code,
    run_capstone_clean_room,
    write_capstone_evidence,
)


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=root)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--implementation-variant",
        choices=("starter", "solution"),
        default="starter",
        help="Run learner starter by default; select solution only for reference replay.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    payload = run_capstone_clean_room(
        args.source_root,
        args.workspace,
        implementation_variant=args.implementation_variant,
    )
    write_capstone_evidence(args.output, payload)
    print(f"evidence={args.output}")
    print(f"workspace={args.workspace}")
    print(f"implementation_variant={args.implementation_variant}")
    print(f"learning_completion={payload['learning_completion']}")
    print("live=blocked_phase0_no_go")
    return capstone_evidence_exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
