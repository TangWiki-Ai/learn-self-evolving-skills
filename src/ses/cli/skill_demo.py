"""Parse and present the Lesson 1 Skill demo command."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from ses.evaluator import SingleCaseRunError
from ses.skills.comparison import SkillDemoComparison
from ses.skills.demo import run_skill_demo
from ses.skills.selection import CandidateMode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses skill-demo",
        description="Compare one fresh return case without and with a demo Skill.",
    )
    parser.add_argument("--output-root", type=Path, default=Path(".ses/skill-demo"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--generate",
        action="store_true",
        help="Generate the offline candidate (default).",
    )
    choice.add_argument("--candidate", type=Path, help="Use this candidate artifact.")
    choice.add_argument(
        "--reference",
        action="store_true",
        help="Explicitly use the packaged reference Skill.",
    )
    return parser


def _mode(args: argparse.Namespace) -> CandidateMode:
    if args.reference:
        return CandidateMode.REFERENCE
    if args.candidate is not None:
        return CandidateMode.CANDIDATE
    return CandidateMode.GENERATE


def _render_run(label: str, value: Mapping[str, JsonValue]) -> list[str]:
    lines = [
        f"{label} ({value.get('run_id')}): outcome={value.get('outcome')}",
        "  Messages:",
    ]
    messages = value.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                lines.append(f"    {message.get('role')}: {message.get('content')}")
    lines.append("  Tool calls:")
    tool_calls = value.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, Mapping):
                lines.append(
                    "    "
                    f"{call.get('tool_name')} "
                    f"input={json.dumps(call.get('input'), sort_keys=True)} "
                    f"error={call.get('is_error')}"
                )
    state_diff = value.get("state_diff")
    if isinstance(state_diff, Mapping):
        lines.append(
            f"  State result: changed={bool(state_diff.get('changed'))} "
            f"summary={state_diff.get('summary')}"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = run_skill_demo(
            args.output_root,
            mode=_mode(args),
            candidate_source=args.candidate,
        )
        comparison = SkillDemoComparison.model_validate_json(
            (result.output_root / result.comparison_artifact).read_bytes()
        )
    except (SingleCaseRunError, OSError, ValueError) as exc:
        print(f"skill_demo_error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(comparison.model_dump_json())
        return 0
    print(comparison.notice)
    print("\n".join(_render_run("Without Skill", comparison.runs.without_skill)))
    print("\n".join(_render_run("With Skill", comparison.runs.with_skill)))
    print(f"\nComparison artifact: {result.output_root / result.comparison_artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
