"""Top-level command registration for the ``ses`` CLI."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.cli import (
    automation,
    baseline,
    doctor,
    evolution,
    governance,
    judge_calibration,
    qualify_cases,
    shopping_capstone,
    skill_demo,
    skill_install,
    skill_release,
    skill_v0,
)
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
    commands.add_parser(
        "skill-install", help="Install a packaged Skill into an agent workspace."
    )
    commands.add_parser(
        "qualify-cases",
        help="Triage sources, verify cases, and build the develop catalog.",
    )
    commands.add_parser("baseline", help="Run the offline develop L1 baseline.")
    commands.add_parser(
        "judge-calibration",
        help="Run the fixed offline Judge calibration.",
    )
    commands.add_parser(
        "skill-v0-pipeline",
        help="Create, gate, trigger-test, pair, and render Skill v0.",
    )
    commands.add_parser("skill", help="Create or statically gate Skill v0.")
    commands.add_parser("trigger-eval", help="Evaluate native Skill discovery.")
    commands.add_parser("paired-comparison", help="Run fresh baseline and Skill pairs.")
    commands.add_parser("l2-render", help="Render a paired L2 HTML report.")
    commands.add_parser(
        "export-failure-evidence",
        help="Export a redacted fixture from an existing paired artifact.",
    )
    commands.add_parser(
        "candidate-patch",
        help="Validate and atomically materialize an evidence-linked candidate.",
    )
    commands.add_parser(
        "evolve",
        help="Turn failure evidence into cards, a patch, and a candidate bundle.",
    )
    commands.add_parser("gate", help="Gate a registered candidate Skill.")
    commands.add_parser("registry", help="Manage immutable Skill version history.")
    commands.add_parser(
        "auto-evolve", help="Run or resume bounded automatic Skill evolution."
    )
    commands.add_parser("final", help="Run the one-time capstone final evaluation.")
    commands.add_parser("l3-render", help="Render the bounded evolution L3 report.")
    commands.add_parser(
        "portfolio-export", help="Export the verified public capstone portfolio."
    )
    commands.add_parser(
        "capstone-index", help="Verify and index a complete shopping capstone."
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
    has_profile = any(
        value == "--profile" or value.startswith("--profile=") for value in command_args
    )
    if command == "doctor":
        if not any(
            arg in {"--config", "--profile"}
            or arg.startswith(("--config=", "--profile="))
            for arg in command_args
        ):
            command_args.extend(("--config", "ses.json"))
        return doctor.main(command_args)
    if command == "run-case":
        return _run_case_main(command_args)
    if command == "inspect":
        if has_profile:
            return shopping_capstone.inspect_main(command_args)
        return _inspect_main(command_args)
    if command == "skill-demo":
        return skill_demo.main(command_args)
    if command == "skill-install":
        return skill_install.main(command_args)
    if command == "qualify-cases":
        return qualify_cases.main(command_args)
    if command == "baseline":
        return baseline.main(command_args)
    if command == "judge-calibration":
        return judge_calibration.main(command_args)
    if command == "skill-v0-pipeline":
        return skill_v0.main(command_args)
    if command == "skill":
        if command_args and command_args[0] == "package":
            return skill_release.package_main(command_args[1:])
        if has_profile:
            return shopping_capstone.skill_main(command_args)
        return skill_v0.skill_main(command_args)
    if command == "trigger-eval":
        if has_profile:
            return shopping_capstone.trigger_main(command_args)
        return skill_v0.trigger_main(command_args)
    if command == "paired-comparison":
        if has_profile:
            return shopping_capstone.paired_main(command_args)
        return skill_v0.paired_main(command_args)
    if command == "l2-render":
        return skill_v0.l2_main(command_args)
    if command == "export-failure-evidence":
        return evolution.export_main(command_args)
    if command == "candidate-patch":
        return evolution.candidate_main(command_args)
    if command == "evolve":
        if has_profile:
            return shopping_capstone.evolve_main(command_args)
        return evolution.evolve_main(command_args)
    if command == "gate":
        if has_profile:
            return shopping_capstone.gate_main(command_args)
        return governance.gate_main(command_args)
    if command == "registry":
        if has_profile:
            return shopping_capstone.registry_main(command_args)
        return governance.registry_main(command_args)
    if command == "auto-evolve":
        if has_profile:
            return shopping_capstone.auto_main(command_args)
        return automation.main(command_args)
    if command == "final":
        return shopping_capstone.final_main(command_args)
    if command == "l3-render":
        return shopping_capstone.l3_main(command_args)
    if command == "portfolio-export":
        return shopping_capstone.portfolio_main(command_args)
    if command == "capstone-index":
        return shopping_capstone.capstone_index_main(command_args)
    build_parser().parse_args(values)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
