#!/usr/bin/env python3
"""Run documented root and lesson commands in a fresh temporary copy."""

from __future__ import annotations

import argparse
from pathlib import Path

from ses.release.clean_room import evidence_exit_code, run_clean_room, write_evidence


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=root)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-dirty-source",
        action="store_true",
        help="Development only; resulting evidence cannot pass the release validator.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    payload = run_clean_room(
        args.source_root,
        args.workspace,
        allow_dirty_source=args.allow_dirty_source,
    )
    write_evidence(args.output, payload)
    print(f"evidence={args.output}")
    print(f"workspace={args.workspace}")
    return evidence_exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
