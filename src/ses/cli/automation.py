"""CLI entry point for bounded automatic Skill evolution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ses.automation.fixed import run_fixed_auto_evolve
from ses.automation.orchestrator import AutoEvolveError
from ses.automation.state import AutoStateError
from ses.contracts import AutoEvolveState, FinalAggregateReport
from ses.evolution.gate import GateError
from ses.evolution.registry import RegistryError
from ses.foundation.credentials import credential_values, redact


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must use ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("cost must be a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("cost must be finite and nonnegative")
    return parsed


def automation_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        prog="ses auto-evolve",
        description="Run or resume bounded automatic Skill evolution.",
    )
    parser.add_argument("--mode", choices=("fixed",), default="fixed")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", default="experiment-fixed-auto-evolve")
    parser.add_argument("--project-root", type=Path, default=root)
    parser.add_argument(
        "--accepted-skill",
        type=Path,
        default=root / "fixtures/seed/skill/v0",
    )
    parser.add_argument(
        "--initial-evidence",
        type=Path,
        default=root / "fixtures/seed/summary.json",
    )
    parser.add_argument(
        "--failure-fixture",
        type=Path,
        default=root / "tests/fixtures/evolution/synthetic-failure-evidence.json",
    )
    parser.add_argument(
        "--selection-lock",
        type=Path,
        default=root / "data/testset/protected/selection-manifest.json",
    )
    parser.add_argument(
        "--final-lock",
        type=Path,
        default=root / "data/testset/protected/final-manifest.json",
    )
    parser.add_argument(
        "--started-at", type=_datetime, default=datetime(2026, 8, 19, 9, tzinfo=UTC)
    )
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--max-input-tokens", type=int, default=100_000)
    parser.add_argument("--max-output-tokens", type=int, default=100_000)
    parser.add_argument("--max-cost", type=_decimal, default=Decimal("1.00"))
    parser.add_argument("--max-consecutive-rejections", type=int, default=2)
    parser.add_argument("--cooldown-rounds", type=int, default=2)
    parser.add_argument("--convergence-rounds", type=int, default=2)
    parser.add_argument("--min-quality-improvement", type=float, default=0.0)
    parser.add_argument("--frozen", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _payload(state: AutoEvolveState, *, output_root: Path) -> dict[str, object]:
    final_report = state.final_report
    payload: dict[str, object] = {
        "experiment_id": state.experiment_id,
        "status": state.status.value,
        "stop_reason": state.stop_reason.value if state.stop_reason else None,
        "completed_rounds": state.completed_rounds,
        "current_accepted_skill_sha256": state.current_accepted_skill_sha256,
        "gate_outcomes": [row.gate_outcome.value for row in state.rounds],
        "promoted_rounds": [row.round_number for row in state.rounds if row.promoted],
        "total_cost_amount": str(state.total_cost_amount),
        "cost_currency": state.cost_currency,
        "cost_complete": state.cost_complete,
        "cost_basis": "fixed_synthetic_accounting",
        "provider_cost_amount": "0",
        "final_report": final_report.path if final_report else None,
        "result_kind": "fixed_reference",
        "network_used": False,
    }
    if final_report is not None:
        report_path = output_root.resolve() / final_report.path
        report_bytes = report_path.read_bytes()
        final_report.verify_bytes(report_bytes)
        report = FinalAggregateReport.model_validate_json(report_bytes)
        payload.update(
            {
                "final_case_count": report.case_count,
                "final_pass_count": report.pass_count,
                "final_pass_rate": report.pass_rate,
                "final_result_source": report.result_source,
            }
        )
    return payload


def _print_payload(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    for key, value in payload.items():
        if isinstance(value, list):
            print(f"{key}={','.join(str(item) for item in value)}")
        else:
            print(f"{key}={value}")


def main(argv: Sequence[str]) -> int:
    args = automation_parser().parse_args(argv)
    try:
        state = run_fixed_auto_evolve(
            project_root=args.project_root,
            output_root=args.output_root,
            experiment_id=args.experiment_id,
            accepted_skill=args.accepted_skill,
            initial_evidence=args.initial_evidence,
            failure_fixture=args.failure_fixture,
            selection_lock=args.selection_lock,
            final_lock=args.final_lock,
            started_at=args.started_at,
            max_rounds=args.max_rounds,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            max_cost_amount=args.max_cost,
            max_consecutive_rejections=args.max_consecutive_rejections,
            cooldown_rounds=args.cooldown_rounds,
            convergence_rounds=args.convergence_rounds,
            min_quality_improvement=args.min_quality_improvement,
            frozen=args.frozen,
        )
    except (
        AutoEvolveError,
        AutoStateError,
        GateError,
        OSError,
        RegistryError,
        TypeError,
        ValueError,
    ) as exc:
        message = redact(str(exc), credential_values(os.environ))
        print(f"auto_evolve_error:{message or type(exc).__name__}", file=sys.stderr)
        return 1
    _print_payload(
        _payload(state, output_root=args.output_root),
        as_json=args.as_json,
    )
    return 0


__all__ = ["automation_parser", "main"]
