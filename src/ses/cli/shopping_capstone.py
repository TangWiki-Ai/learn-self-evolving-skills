"""Offline learner CLI adapters for the shopping capstone stages."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ses.automation.capstone import CapstoneIndexError, build_capstone_index
from ses.automation.portfolio import export_portfolio
from ses.contracts import (
    AutoEvolveConfig,
    AutoEvolveState,
    AutoLoopStatus,
    CapstoneFinalReceipt,
    FailureCardSet,
    FailureEvidenceFixture,
    FinalAggregateReport,
    GateDecision,
    GateOutcome,
    Trace,
    Usage,
    artifact_json_bytes,
)
from ses.evolution.gate import public_gate_decision_payload
from ses.evolution.registry import RegistryError
from ses.foundation.credentials import credential_values, redact
from ses.reporting.l3 import write_l3_html
from ses.shopping.automation import build_shopping_capstone_orchestrator
from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    ShoppingLearnerReceipt,
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_course import build_fixed_develop_evaluation
from ses.shopping.manual_workflow import (
    promote_shopping_candidate,
    register_shopping_candidate,
    run_shopping_evolution_stage,
    run_shopping_gate_stage,
)
from ses.shopping.profile import LoadedShoppingProfile, load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.shopping.reviews import (
    ShoppingReviewError,
    ShoppingReviewKind,
    write_shopping_review,
)

_FIXED_TIME = datetime(2026, 8, 20, tzinfo=UTC)
_GATE_SCENARIOS = (
    "accept",
    "trigger-failure",
    "evidence-error",
    "unauthorized",
    "tie",
    "strict-regression",
    "critical-regression",
    "cost-overrun",
)


def _safe_error(exc: Exception) -> str:
    message = redact(str(exc), credential_values(os.environ))
    return message or type(exc).__name__


def _load_fixed(path: Path) -> LoadedShoppingProfile:
    profile = load_shopping_profile(path)
    if profile.profile.mode != "fixed":
        raise ValueError(
            "live route is no_go; use the ShopSimulator-inspired fixed profile"
        )
    return profile


def _base(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")


def _receipt_payload(
    receipt: ShoppingLearnerReceipt,
    *,
    receipt_path: Path,
) -> dict[str, object]:
    return {
        "stage": receipt.stage,
        "profile_sha256": receipt.profile_sha256,
        "skill_sha256": receipt.skill_sha256,
        "inputs": [item.model_dump(mode="json") for item in receipt.inputs],
        "outputs": [item.model_dump(mode="json") for item in receipt.outputs],
        "primary_metrics": dict(receipt.primary_metrics),
        "usage": receipt.usage.model_dump(mode="json"),
        "stop_reason": receipt.stop_reason,
        "next_command": receipt.next_command,
        "receipt": receipt_path.as_posix(),
    }


def _print(payload: Mapping[str, object], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return
    for name in (
        "stage",
        "inputs",
        "outputs",
        "primary_metrics",
        "usage",
        "stop_reason",
        "next_command",
        "receipt",
        "outcome",
        "candidate_id",
        "current_accepted_sha256",
        "review",
    ):
        if name in payload:
            value = payload[name]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            print(f"{name}={value}")


def _projection_root(profile_path: Path) -> Path:
    return profile_path.parent.parent / "fixtures" / "creator-projections"


def _terminal_capstone_state(
    profile: LoadedShoppingProfile,
    experiment_root: Path,
) -> tuple[AutoEvolveState, CapstoneFinalReceipt]:
    state_path = experiment_root / "state.json"
    config_path = experiment_root / "config.json"
    final_path = experiment_root / "final" / "capstone-final-receipt.json"
    state_bytes = state_path.read_bytes()
    config_bytes = config_path.read_bytes()
    final_bytes = final_path.read_bytes()
    state = AutoEvolveState.model_validate_json(state_bytes)
    config = AutoEvolveConfig.model_validate_json(config_bytes)
    final = CapstoneFinalReceipt.model_validate_json(final_bytes)
    if (
        artifact_json_bytes(state) != state_bytes
        or artifact_json_bytes(config) != config_bytes
        or artifact_json_bytes(final) != final_bytes
    ):
        raise ValueError("capstone final state must use canonical JSON")
    if (
        state.status not in {AutoLoopStatus.FINAL_COMPLETE, AutoLoopStatus.FAILED_FINAL}
        or state.experiment_id != config.experiment_id
        or config.profile_sha256 != profile.profile_sha256
        or final.experiment_id != state.experiment_id
        or final.profile_sha256 != profile.profile_sha256
        or final.subject_skill_sha256 != state.current_accepted_skill_sha256
    ):
        raise ValueError("capstone final state differs from the selected profile")
    return state, final


def _capstone_output(experiment_root: Path, output: Path, *, name: str) -> Path:
    root = experiment_root.resolve(strict=True)
    if output.exists() or output.is_symlink() or ".." in output.parts:
        raise ValueError(f"{name} output must be a new canonical path")
    resolved = output.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{name} output must stay in the experiment") from exc
    return resolved


def _review_projection(
    *,
    review_kind: str,
    artifact: Path,
    experiment_root: Path,
) -> object:
    if review_kind == "paired_trace":
        trace = Trace.model_validate_json(artifact.read_bytes())
        return {
            "case_id": trace.case_id,
            "exit_status": trace.exit_status.value,
            "session_id": trace.session_id,
            "tool_sequence": [
                event.payload.model_dump(mode="json").get("tool_name")
                for event in trace.events
                if event.payload.model_dump(mode="json").get("kind") == "tool_call"
            ],
            "trace_id": trace.trace_id,
            "usage": (
                None if trace.usage is None else trace.usage.model_dump(mode="json")
            ),
        }
    if review_kind == "failure_evidence":
        fixture = FailureEvidenceFixture.model_validate_json(artifact.read_bytes())
        return {
            "case_count": len(fixture.cases),
            "cases": [
                {
                    "case_key": row.case_key,
                    "observation": row.observation,
                    "shopping_subcode": (
                        None
                        if row.shopping_subcode is None
                        else row.shopping_subcode.value
                    ),
                    "safety_evidence_count": len(row.safety_evidence),
                }
                for row in fixture.cases
            ],
        }
    if review_kind == "failure_card":
        cards = FailureCardSet.model_validate_json(artifact.read_bytes())
        return {
            "card_count": len(cards.cards),
            "cards": [
                {
                    "failure_id": card.failure_id,
                    "category": card.category.value,
                    "shopping_subcode": (
                        None
                        if card.shopping_subcode is None
                        else card.shopping_subcode.value
                    ),
                    "observation": card.observation,
                    "suggested_scope": card.suggested_scope,
                }
                for card in cards.cards
            ],
        }
    if review_kind == "gate_decision":
        return public_gate_decision_payload(
            GateDecision.model_validate_json(artifact.read_bytes())
        )
    if review_kind == "registry_history":
        state = open_shopping_registry(experiment_root / "registry").audit()
        return {
            "current_accepted_skill_sha256": state.current_accepted_sha256,
            "event_count": len(state.events),
            "events": [
                {
                    "event_type": event.event_type.value,
                    "sequence": event.sequence,
                    "status": event.status.value,
                    "version_sha256": event.version_sha256,
                }
                for event in state.events
            ],
            "lineage_id": state.lineage_id,
        }
    raise ValueError("unsupported shopping review kind")


def skill_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses skill")
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create-v0")
    _base(create)
    static = commands.add_parser("static-gate")
    _base(static)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        if args.action == "create-v0":
            created = run_shopping_create_stage(
                profile=profile,
                projection_root=_projection_root(args.profile),
                experiment_root=args.experiment_root,
            )
            receipt = created.receipt
            receipt_path = created.receipt_path
        else:
            checked = run_shopping_static_stage(
                profile=profile,
                experiment_root=args.experiment_root,
                skill_source=args.experiment_root / "skill" / "v0",
                create_receipt=args.experiment_root / "receipts" / "create.json",
            )
            receipt = checked.receipt
            receipt_path = checked.receipt_path
    except (OSError, RegistryError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_skill_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        _receipt_payload(receipt, receipt_path=receipt_path),
        as_json=args.as_json,
    )
    return 0


def trigger_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses trigger-eval")
    _base(parser)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        result = run_shopping_trigger_stage(
            profile=profile,
            experiment_root=args.experiment_root,
            skill_source=args.experiment_root / "skill" / "v0",
            static_receipt=args.experiment_root / "receipts" / "static.json",
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_trigger_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        _receipt_payload(result.receipt, receipt_path=result.receipt_path),
        as_json=args.as_json,
    )
    return 0


def inspect_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses inspect")
    parser.add_argument(
        "review_kind",
        choices=(
            "paired-trace",
            "failure-evidence",
            "failure-card",
            "gate-decision",
            "registry-history",
        ),
    )
    parser.add_argument("artifact", type=Path)
    _base(parser)
    args = parser.parse_args(argv)
    review_kind = cast(ShoppingReviewKind, args.review_kind.replace("-", "_"))
    try:
        profile = _load_fixed(args.profile)
        result = write_shopping_review(
            profile,
            args.experiment_root,
            review_kind,
            args.artifact,
            _FIXED_TIME,
        )
        projection = _review_projection(
            review_kind=review_kind,
            artifact=(
                args.artifact
                if args.artifact.is_absolute()
                else args.experiment_root / args.artifact
            ).resolve(strict=True),
            experiment_root=args.experiment_root.resolve(strict=True),
        )
    except (
        OSError,
        RuntimeError,
        ShoppingReviewError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"shopping_inspect_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = dict(result.summary)
    payload.update(
        {
            "inputs": [args.artifact.as_posix()],
            "outputs": [result.receipt_path.as_posix()],
            "primary_metrics": {"review_kind": review_kind},
            "review": projection,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
            "stop_reason": "reviewed",
            "next_command": "continue the documented capstone stage",
        }
    )
    _print(payload, as_json=args.as_json)
    return 0


def paired_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses paired-comparison")
    _base(parser)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        skill = args.experiment_root / "skill" / "v0"
        from ses.skills.installer import normalized_skill_sha256

        fixed = build_fixed_develop_evaluation(
            profile,
            learner_skill_sha256=normalized_skill_sha256(skill),
            learner_skill_source=skill,
        )
        result = run_shopping_paired_stage(
            profile=profile,
            experiment_root=args.experiment_root,
            skill_source=skill,
            trigger_receipt=args.experiment_root / "receipts" / "trigger.json",
            tasks=fixed.tasks,
            baseline_evaluator=fixed.baseline_evaluator,
            skill_evaluator=fixed.skill_evaluator,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_pair_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        _receipt_payload(result.receipt, receipt_path=result.receipt_path),
        as_json=args.as_json,
    )
    return 0


def evolve_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses evolve")
    _base(parser)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        result = run_shopping_evolution_stage(
            profile=profile,
            experiment_root=args.experiment_root,
            paired_receipt=args.experiment_root / "receipts" / "paired.json",
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_evolve_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload: dict[str, object] = {
        "stage": "evolve",
        "inputs": [result.evidence_path.as_posix()],
        "outputs": [
            result.failure_cards_path.as_posix(),
            result.patch_path.as_posix(),
            result.candidate_bundle.joinpath("candidate.json").as_posix(),
        ],
        "primary_metrics": {
            "failure_card_count": result.summary.failure_card_count,
            "patch_operation_count": result.summary.patch_operation_count,
        },
        "usage": result.summary.updater_usage.model_dump(mode="json"),
        "stop_reason": "completed",
        "next_command": (
            "ses registry register --profile <profile> --experiment-root <root> "
            "--registry <root>/registry --candidate <root>/manual-evolution"
        ),
    }
    _print(payload, as_json=args.as_json)
    return 0


def registry_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses registry")
    commands = parser.add_subparsers(dest="action", required=True)
    initialize = commands.add_parser("init")
    _base(initialize)
    initialize.add_argument("--registry", type=Path, required=True)
    initialize.add_argument("--initial-skill", type=Path, required=True)
    initialize.add_argument("--initial-evidence", type=Path, required=True)
    register = commands.add_parser("register")
    _base(register)
    register.add_argument("--registry", type=Path, required=True)
    register.add_argument("--candidate", type=Path, required=True)
    promote = commands.add_parser("promote")
    _base(promote)
    promote.add_argument("--registry", type=Path, required=True)
    promote.add_argument("--candidate-id", required=True)
    promote.add_argument("--gate-decision", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        expected_registry = (args.experiment_root / "registry").resolve()
        if args.registry.resolve() != expected_registry:
            raise ValueError("shopping Registry must be inside its experiment root")
        if args.action == "init":
            if (
                args.initial_skill.resolve()
                != (args.experiment_root / "skill" / "v0").resolve()
                or args.initial_evidence.resolve()
                != (args.experiment_root / "v0-pipeline-summary.json").resolve()
            ):
                raise ValueError(
                    "Registry init paths do not match the learner receipts"
                )
            registry = open_shopping_registry(args.registry)
            event = registry.initialize(
                command_id="command-shopping-initialize",
                accepted_skill=args.initial_skill,
                evidence_paths=(args.initial_evidence,),
                occurred_at=_FIXED_TIME,
                lineage_id=(
                    f"lineage-shopping-{profile.profile.mode}-"
                    f"{profile.profile_sha256[:16]}"
                ),
            )
        elif args.action == "register":
            if (
                args.candidate.resolve()
                != (args.experiment_root / "manual-evolution").resolve()
            ):
                raise ValueError("Registry can register only the learner candidate")
            event = register_shopping_candidate(
                registry_root=args.registry,
                candidate_bundle=args.candidate,
            )
        else:
            event = promote_shopping_candidate(
                registry_root=args.registry,
                decision_path=args.gate_decision,
                candidate_id=args.candidate_id,
            )
    except (OSError, RegistryError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_registry_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = event.model_dump(mode="json")
    payload["stage"] = f"registry_{args.action}"
    payload["inputs"] = []
    payload["outputs"] = [
        (args.registry / "events.jsonl").as_posix(),
        (args.registry / "checkpoint.json").as_posix(),
    ]
    payload["primary_metrics"] = {
        "event_type": event.event_type.value,
        "sequence": event.sequence,
    }
    payload["usage"] = Usage(input_tokens=0, output_tokens=0).model_dump(mode="json")
    payload["stop_reason"] = "completed"
    payload["next_command"] = (
        "ses evolve --profile <profile> --experiment-root <root>"
        if args.action == "init"
        else "ses gate candidate --profile <profile> --experiment-root <root>"
        if args.action == "register"
        else "ses auto-evolve --profile <profile> --experiment-root <root>"
    )
    _print(payload, as_json=args.as_json)
    return 0


def gate_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses gate")
    commands = parser.add_subparsers(dest="action", required=True)
    candidate = commands.add_parser("candidate")
    _base(candidate)
    candidate.add_argument(
        "--fixed-scenario",
        choices=_GATE_SCENARIOS,
        default="accept",
    )
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        result = run_shopping_gate_stage(
            profile=profile,
            experiment_root=args.experiment_root,
            registry_root=args.experiment_root / "registry",
            candidate_bundle=args.experiment_root / "manual-evolution",
            scenario=args.fixed_scenario,
        )
    except (OSError, RegistryError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_gate_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = public_gate_decision_payload(result.decision)
    payload.update(
        {
            "stage": "manual_gate",
            "inputs": [
                result.decision.candidate.model_dump(mode="json"),
            ],
            "outputs": [result.decision_path.as_posix()],
            "primary_metrics": result.decision.metrics.model_dump(mode="json"),
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
            "stop_reason": "completed",
            "next_command": (
                "ses registry promote --profile <profile> --experiment-root <root> "
                f"--candidate-id {result.decision.candidate_id} "
                "--gate-decision <decision>"
                if result.decision.outcome is GateOutcome.ACCEPTED
                else "ses inspect gate-decision <decision>"
            ),
        }
    )
    _print(payload, as_json=args.as_json)
    return 0 if result.decision.outcome is GateOutcome.ACCEPTED else 1


def auto_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses auto-evolve")
    _base(parser)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        orchestrator = build_shopping_capstone_orchestrator(
            profile=profile,
            project_root=Path(__file__).resolve().parents[3],
            experiment_root=args.experiment_root,
        )
        state = orchestrator.run()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_auto_evolve_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    outcomes = [row.gate_outcome.value for row in state.rounds]
    _print(
        {
            "stage": "auto_evolve",
            "inputs": [
                (args.experiment_root / "failure-evidence.json").as_posix(),
                (args.experiment_root / "protected/selection-lock.json").as_posix(),
            ],
            "outputs": [
                (args.experiment_root / "state.json").as_posix(),
                *[
                    (args.experiment_root / row.gate_decision.path).as_posix()
                    for row in state.rounds
                ],
            ],
            "primary_metrics": {
                "completed_rounds": state.completed_rounds,
                "accepted_round_count": outcomes.count("accepted"),
                "rejected_round_count": outcomes.count("rejected"),
                "current_accepted_skill_sha256": (state.current_accepted_skill_sha256),
            },
            "usage": {
                "input_tokens": state.total_input_tokens,
                "output_tokens": state.total_output_tokens,
                "cost_amount": str(state.total_cost_amount),
                "cost_currency": state.cost_currency,
            },
            "stop_reason": (
                state.status.value
                if state.stop_reason is None
                else state.stop_reason.value
            ),
            "next_command": ("ses final --profile <profile> --experiment-root <root>"),
        },
        as_json=args.as_json,
    )
    return 0


def final_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses final")
    _base(parser)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        orchestrator = build_shopping_capstone_orchestrator(
            profile=profile,
            project_root=Path(__file__).resolve().parents[3],
            experiment_root=args.experiment_root,
        )
        state = orchestrator.run_final_once()
        aggregate_path = args.experiment_root / "final/final-aggregate.json"
        aggregate = FinalAggregateReport.model_validate_json(
            aggregate_path.read_bytes()
        )
        if artifact_json_bytes(aggregate) != aggregate_path.read_bytes():
            raise ValueError("final aggregate is not canonical")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_final_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        {
            "stage": "final",
            "inputs": [
                (args.experiment_root / "protected/final-lock.json").as_posix(),
                (args.experiment_root / "state.json").as_posix(),
            ],
            "outputs": [
                aggregate_path.as_posix(),
                (args.experiment_root / "final/capstone-final-receipt.json").as_posix(),
                (args.experiment_root / "final-consumed.checkpoint.json").as_posix(),
            ],
            "primary_metrics": {
                "case_count": aggregate.case_count,
                "full_success_count": aggregate.full_success_count,
                "mean_strict_reward": (
                    None
                    if aggregate.mean_strict_reward is None
                    else str(aggregate.mean_strict_reward)
                ),
                "safety_violation_count": aggregate.safety_violation_count,
            },
            "usage": {
                "input_tokens": aggregate.input_tokens,
                "output_tokens": aggregate.output_tokens,
                "cost_amount": str(aggregate.cost_amount),
                "cost_currency": aggregate.cost_currency,
            },
            "stop_reason": state.status.value,
            "next_command": (
                "ses l3-render --profile <profile> --experiment-root <root> "
                "--output <root>/l3.html"
            ),
        },
        as_json=args.as_json,
    )
    return 0 if state.status is AutoLoopStatus.FINAL_COMPLETE else 1


def l3_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses l3-render")
    _base(parser)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        state, final = _terminal_capstone_state(profile, args.experiment_root)
        output = _capstone_output(
            args.experiment_root,
            args.output,
            name="L3",
        )
        registry = open_shopping_registry(args.experiment_root / "registry")
        write_l3_html(
            args.experiment_root,
            output,
            registry=registry,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_l3_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        {
            "stage": "l3_render",
            "inputs": [
                (args.experiment_root / "state.json").as_posix(),
                (args.experiment_root / "registry/events.jsonl").as_posix(),
                (args.experiment_root / "final/final-aggregate.json").as_posix(),
            ],
            "outputs": [output.as_posix()],
            "primary_metrics": {
                "round_count": state.completed_rounds,
                "final_safety_violation_count": final.safety_violation_count,
            },
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
            "stop_reason": state.status.value,
            "next_command": (
                "ses portfolio-export --profile <profile> "
                "--experiment-root <root> --output <root>/portfolio"
            ),
        },
        as_json=args.as_json,
    )
    return 0


def portfolio_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses portfolio-export")
    _base(parser)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        state, final = _terminal_capstone_state(profile, args.experiment_root)
        output = _capstone_output(
            args.experiment_root,
            args.output,
            name="portfolio",
        )
        registry = open_shopping_registry(args.experiment_root / "registry")
        manifest = export_portfolio(
            args.experiment_root,
            output,
            created_at=_FIXED_TIME,
            registry=registry,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shopping_portfolio_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        {
            "stage": "portfolio_export",
            "inputs": [
                (args.experiment_root / "state.json").as_posix(),
                (args.experiment_root / "l3.html").as_posix(),
            ],
            "outputs": [(output / "manifest.json").as_posix()],
            "primary_metrics": {
                "file_count": len(manifest.files),
                "final_safety_violation_count": final.safety_violation_count,
                "round_count": state.completed_rounds,
            },
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
            "stop_reason": state.status.value,
            "next_command": (
                "ses skill package --profile <profile> --experiment-root <root> "
                "--registry <root>/registry --current-accepted "
                "--output <root>/package"
            ),
        },
        as_json=args.as_json,
    )
    return 0


def capstone_index_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ses capstone-index")
    _base(parser)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        profile = _load_fixed(args.profile)
        root = args.experiment_root.resolve(strict=True)
        output = _capstone_output(root, args.output, name="CapstoneIndex")
        create_path = root / "receipts/create.json"
        create_bytes = create_path.read_bytes()
        create = ShoppingLearnerReceipt.model_validate_json(create_bytes)
        if artifact_json_bytes(create) != create_bytes:
            raise ValueError("create receipt must use canonical JSON")
        if create.profile_sha256 != profile.profile_sha256:
            raise ValueError("create receipt differs from the selected profile")
        review_paths = tuple(
            root / "reviews" / f"{kind}.json"
            for kind in (
                "paired_trace",
                "failure_evidence",
                "failure_card",
                "gate_decision",
                "registry_history",
            )
        )
        index = build_capstone_index(
            experiment_root=root,
            output_path=output,
            create_receipt=create_path,
            static_receipt=root / "receipts/static.json",
            trigger_receipt=root / "receipts/trigger.json",
            paired_receipt=root / "receipts/paired.json",
            review_receipts=review_paths,
            failure_evidence=root / "failure-evidence.json",
            failure_cards=root / "manual-evolution/failure-cards.json",
            patch=root / "manual-evolution/patch.json",
            manual_gate_decision=(
                root / "registry/gates/gate-shopping-manual/gate-decision.json"
            ),
            registry_root=root / "registry",
            auto_evolve_state=root / "state.json",
            final_receipt=root / "final/capstone-final-receipt.json",
            l3_report=root / "l3.html",
            portfolio_manifest=root / "portfolio/manifest.json",
            release_manifest=root / "package/release-manifest.json",
            package_runtime_manifest=(root / "package/skill/skill-manifest.json"),
            created_at=_FIXED_TIME,
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
        )
    except (
        CapstoneIndexError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"shopping_capstone_index_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print(
        {
            "stage": "capstone_index",
            "inputs": [
                (root / "receipts/create.json").as_posix(),
                (root / "registry/events.jsonl").as_posix(),
                (root / "state.json").as_posix(),
                (root / "final/capstone-final-receipt.json").as_posix(),
                (root / "package/release-manifest.json").as_posix(),
            ],
            "outputs": [output.as_posix()],
            "primary_metrics": {
                "learning_completion": index.learning_completion,
                "measurement_kind": index.measurement_kind.value,
                "review_receipt_count": len(index.review_receipts),
                "current_accepted_skill_sha256": (index.current_accepted_skill_sha256),
                "network_used": index.network_used,
            },
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_amount": str(index.total_cost_amount),
                "cost_currency": index.cost_currency,
            },
            "stop_reason": "workflow_complete",
            "next_command": (
                "ses skill-install --profile <profile> --experiment-root <root> "
                "--accepted-package <root>/package/release-manifest.json "
                "--destination <skills-parent>"
            ),
        },
        as_json=args.as_json,
    )
    return 0


__all__ = [
    "auto_main",
    "capstone_index_main",
    "evolve_main",
    "final_main",
    "gate_main",
    "inspect_main",
    "l3_main",
    "paired_main",
    "portfolio_main",
    "registry_main",
    "skill_main",
    "trigger_main",
]
