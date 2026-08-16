"""Top-level command registration for the ``ses`` CLI."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.cli import doctor, skill_demo
from ses.evaluator import SingleCaseRunError, run_pinned_case
from ses.foundation.credentials import credential_values, redact
from ses.reporting import l1_json_bytes, load_l1_result, render_l1_text


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser without running any business logic."""
    parser = argparse.ArgumentParser(
        prog="ses",
        description="Build and evaluate self-evolving skills.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("doctor", help="Check local data and the optional live path.")
    commands.add_parser("run-case", help="Evaluate the pinned return case offline.")
    commands.add_parser("inspect", help="Inspect a persisted L1 case result.")
    commands.add_parser(
        "skill-demo", help="Compare a return case without and with a demo Skill."
    )
    return parser


def _safe_error(message: str) -> str:
    return redact(message, credential_values(os.environ))


def _run_case_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ses run-case",
        description="Run the pinned STATE-Bench return case with FakeEngine.",
    )
    parser.add_argument("--output-root", type=Path, default=Path(".ses/runs"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        completed = run_pinned_case(args.output_root)
        result = load_l1_result(
            args.output_root,
            completed.run_id,
            completed.case_id,
        )
    except SingleCaseRunError as exc:
        print(f"{exc.outcome.value}: {_safe_error(str(exc))}", file=sys.stderr)
        return 1
    if args.as_json:
        print(l1_json_bytes(result).decode("utf-8"))
    else:
        print(f"run_id={completed.run_id}")
        print(f"case_id={completed.case_id}")
        print(f"outcome={completed.outcome.value}")
        print(f"result={completed.result_path}")
    return 0


def _inspect_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ses inspect",
        description="Read a persisted L1 result without re-running judges.",
    )
    parser.add_argument("run_id")
    parser.add_argument("case_id", nargs="?")
    parser.add_argument("--output-root", type=Path, default=Path(".ses/runs"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        result = load_l1_result(args.output_root, args.run_id, args.case_id)
    except ValueError as exc:
        print(f"inspect_error: {_safe_error(str(exc))}", file=sys.stderr)
        return 1
    if args.as_json:
        print(l1_json_bytes(result).decode("utf-8"))
    else:
        print(render_l1_text(result))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse top-level arguments and return a process exit code."""
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        build_parser().print_help()
        return 0
    command, command_args = values[0], values[1:]
    if command == "doctor":
        if not any(
            arg == "--config" or arg.startswith("--config=") for arg in command_args
        ):
            command_args.extend(("--config", "ses.json"))
        return doctor.main(command_args)
    if command == "run-case":
        return _run_case_main(command_args)
    if command == "inspect":
        return _inspect_main(command_args)
    if command == "skill-demo":
        return skill_demo.main(command_args)
    build_parser().parse_args(values)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
