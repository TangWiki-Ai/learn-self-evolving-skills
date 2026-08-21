"""Ticket 08 offline-first Skill v0 vertical slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ses.contracts import MeasurementKind
from ses.foundation.config import (
    ModelRole,
    ProviderId,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import read_provider_credentials
from ses.reporting.l2 import write_l2_html
from ses.runner import LiveDevelopConfig
from ses.skills.paired import run_fresh_paired
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.static_gate import StaticGateStatus, run_static_gate
from ses.skills.trigger_eval import (
    ClaudeNativeDiscovery,
    DiscoveryBackend,
    SyntheticDiscoveryFixture,
    evaluate_triggers,
)
from ses.skills.v0 import FakeV0Creator, LiveV0Creator, V0Creator, create_skill_v0
from ses.skills.workflow import SkillV0WorkflowConfig, run_skill_v0_workflow


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        prog="ses skill-v0-pipeline",
        description="Create, gate, trigger-test, pair, and render Skill v0.",
    )
    parser.add_argument("--output-root", type=Path, default=Path(".ses/ticket08"))
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument("--provider", type=ProviderId, choices=tuple(ProviderId))
    parser.add_argument(
        "--seed-manifest",
        type=Path,
        default=root / "data" / "skill-v0" / "creator" / "seed-manifest.json",
    )
    parser.add_argument("--project-root", type=Path, default=root)
    parser.add_argument("--creator-timeout", type=float, default=180)
    parser.add_argument("--trigger-timeout", type=float, default=60)
    parser.add_argument("--paired-timeout", type=float, default=300)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_skill_v0_workflow(
            SkillV0WorkflowConfig(
                project_root=args.project_root,
                output_root=args.output_root,
                seed_manifest=args.seed_manifest,
                mode=args.mode,
                provider=args.provider,
                creator_timeout=args.creator_timeout,
                trigger_timeout=args.trigger_timeout,
                paired_timeout=args.paired_timeout,
            ),
            environ=os.environ,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        message = str(exc)
        if "/" in message or "\\" in message:
            message = type(exc).__name__
        print(f"skill_v0_error:{message}", file=sys.stderr)
        return 1
    if args.as_json:
        print(summary.model_dump_json())
    else:
        for key, value in summary.model_dump(mode="json").items():
            print(f"{key}={value}")
    return 0


def skill_main(argv: Sequence[str]) -> int:
    """Create v0 or run its static gate under the ``ses skill`` namespace."""
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(prog="ses skill")
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create-v0")
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    create.add_argument("--provider", type=ProviderId, choices=tuple(ProviderId))
    create.add_argument("--project-root", type=Path, default=root)
    create.add_argument("--timeout", type=float, default=180)
    create.add_argument(
        "--seed-manifest",
        type=Path,
        default=root / "data" / "skill-v0" / "creator" / "seed-manifest.json",
    )
    create.add_argument("--json", action="store_true", dest="as_json")
    gate = commands.add_parser("static-gate")
    gate.add_argument("--skill", type=Path, required=True)
    gate.add_argument("--output", type=Path)
    gate.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        if args.action == "create-v0":
            pack = load_creator_seed_pack(args.seed_manifest, mode=args.mode)
            creator: V0Creator
            if args.mode == "fixed":
                creator = FakeV0Creator()
            else:
                config = load_runtime_config(args.project_root / "ses.json")
                provider = args.provider or config.default_provider
                lock = load_model_lock(
                    args.project_root / config.models_lock_for(provider)
                )
                if lock.provider is not provider:
                    raise ValueError("selected provider differs from its model lock")
                creator = LiveV0Creator(
                    model=lock.roles[ModelRole.CREATOR],
                    credentials=read_provider_credentials(provider, os.environ),
                    executable=config.claude_executable,
                    environ=os.environ,
                    timeout_seconds=args.timeout,
                )
            candidate = create_skill_v0(
                seed_pack=pack,
                output_dir=args.out,
                creator=creator,
                workspace_root=args.out.parent / ".creator-workspaces",
            )
            usage = getattr(creator, "usage", None)
            payload = {
                "source": str(candidate.source),
                "version": candidate.version,
                "skill_sha256": candidate.sha256,
                "seed_count": len(pack.records),
                "seed_review_status": pack.review_status,
                "mode": args.mode,
                "input_tokens": 0 if usage is None else usage.input_tokens,
                "output_tokens": 0 if usage is None else usage.output_tokens,
                "cost_amount": (
                    "0"
                    if args.mode == "fixed"
                    and (usage is None or usage.cost_amount is None)
                    else None
                    if usage is None or usage.cost_amount is None
                    else str(usage.cost_amount)
                ),
                "latency_ms": getattr(creator, "latency_ms", 0),
            }
        else:
            report = run_static_gate(args.skill, audit_path=args.output)
            payload = report.model_dump(mode="json")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        message = str(exc)
        if "/" in message or "\\" in message:
            message = type(exc).__name__
        print(f"skill_error:{message}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if args.action == "create-v0" or payload["status"] == "pass" else 1


def trigger_main(argv: Sequence[str]) -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(prog="ses trigger-eval")
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project-root", type=Path, default=root)
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument("--provider", type=ProviderId, choices=tuple(ProviderId))
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        gate = run_static_gate(args.skill)
        if gate.status is not StaticGateStatus.PASS:
            raise ValueError("static gate failed")
        config = load_runtime_config(args.project_root / "ses.json")
        provider = args.provider or config.default_provider
        lock = load_model_lock(args.project_root / config.models_lock_for(provider))
        if lock.provider is not provider:
            raise ValueError("selected provider differs from its model lock")
        discovery: DiscoveryBackend
        if args.mode == "fixed":
            discovery = SyntheticDiscoveryFixture()
        else:
            discovery = ClaudeNativeDiscovery(
                skill_source=args.skill,
                model=lock.roles[ModelRole.MAIN],
                credentials=read_provider_credentials(provider, os.environ),
                executable=config.claude_executable,
                environ=os.environ,
                workspace_root=(
                    args.workspace_root or args.skill.parent / ".trigger-workspaces"
                ),
                timeout_seconds=args.timeout,
            )
        result = evaluate_triggers(
            skill_sha256=gate.skill_sha256 or "0" * 64,
            engine_version=f"{lock.engine}:{lock.engine_version}",
            model_id=lock.roles[ModelRole.MAIN].model_id,
            measurement_kind=(
                MeasurementKind.SYNTHETIC_OFFLINE
                if args.mode == "fixed"
                else MeasurementKind.LIVE_MEASURED
            ),
            measured_at=(
                datetime(2026, 8, 17, tzinfo=UTC)
                if args.mode == "fixed"
                else datetime.now(UTC)
            ),
            discovery=discovery,
        )
        result_payload = result.model_dump(mode="json")
        payload = {
            **result_payload,
            "mode": args.mode,
            "network_used": args.mode == "live",
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_amount": (
                None
                if result.usage.cost_amount is None
                else str(result.usage.cost_amount)
            ),
        }
        if args.output is not None:
            _write_json(args.output, result_payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"trigger_eval_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def paired_main(argv: Sequence[str]) -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(prog="ses paired-comparison")
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project-root", type=Path, default=root)
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument("--provider", type=ProviderId, choices=tuple(ProviderId))
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        live_config: LiveDevelopConfig | None = None
        engine_version: str | None = None
        if args.mode == "live":
            config = load_runtime_config(args.project_root / "ses.json")
            provider = args.provider or config.default_provider
            lock_path = args.project_root / config.models_lock_for(provider)
            lock = load_model_lock(lock_path)
            if lock.provider is not provider:
                raise ValueError("selected provider differs from its model lock")
            live_config = LiveDevelopConfig(
                model=lock.roles[ModelRole.MAIN],
                credentials=read_provider_credentials(provider, os.environ),
                executable=config.claude_executable,
                environ=os.environ,
                timeout_seconds=args.timeout,
                provider=provider,
                model_lock_sha256=hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                cost_currency=("CNY" if provider is ProviderId.CHATANYWHERE else "USD"),
            )
            engine_version = f"{lock.engine}:{lock.engine_version}"
        result = run_fresh_paired(
            skill_source=args.skill,
            output_root=args.output_root,
            project_root=args.project_root,
            live_config=live_config,
            measured_at=(datetime.now(UTC) if args.mode == "live" else None),
            engine_version=engine_version,
        )
        payload = result.model_dump(mode="json")
        if args.output is not None:
            _write_json(args.output, payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"paired_comparison_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def l2_main(argv: Sequence[str]) -> int:
    from ses.contracts import TriggerEvalResult
    from ses.contracts.runner import PairedComparison

    parser = argparse.ArgumentParser(prog="ses l2-render")
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--trigger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args(argv)
    try:
        paired = PairedComparison.model_validate_json(
            args.comparison.read_text(encoding="utf-8")
        )
        trigger = TriggerEvalResult.model_validate_json(
            args.trigger.read_text(encoding="utf-8")
        )
        write_l2_html(
            paired,
            trigger,
            args.output,
            artifact_root=args.artifact_root or args.comparison.parent,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"l2_render_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"html": str(args.output)}, sort_keys=True, separators=(",", ":")))
    return 0
