"""Ticket 08 offline-first Skill v0 vertical slice."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.reporting.l2 import write_l2_html
from ses.skills.paired import run_fresh_paired
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.static_gate import StaticGateStatus, run_static_gate
from ses.skills.trigger_eval import (
    ClaudeNativeDiscovery,
    DiscoveryBackend,
    FixedNativeDiscovery,
    evaluate_triggers,
)
from ses.skills.v0 import FakeV0Creator, LiveV0Creator, V0Creator, create_skill_v0


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        prog="ses skill-v0-pipeline",
        description="Create, gate, trigger-test, pair, and render Skill v0.",
    )
    parser.add_argument("--output-root", type=Path, default=Path(".ses/ticket08"))
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument(
        "--seed-manifest",
        type=Path,
        default=root / "data" / "skill-v0" / "creator" / "seed-manifest.json",
    )
    parser.add_argument("--project-root", type=Path, default=root)
    parser.add_argument("--creator-timeout", type=float, default=180)
    parser.add_argument("--trigger-timeout", type=float, default=60)
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
        if args.output_root.exists() and any(args.output_root.iterdir()):
            raise ValueError("output root must be absent or empty for a fresh pipeline")
        config = load_runtime_config(args.project_root / "ses.json")
        lock = load_model_lock(args.project_root / config.models_lock)
        credentials = None
        if args.mode == "live":
            # Fail before creating artifacts or model workspaces when credentials are absent.
            credentials = read_siliconflow_credentials(os.environ)
        pack = load_creator_seed_pack(args.seed_manifest)
        creator: V0Creator
        if args.mode == "fixed":
            creator = FakeV0Creator()
        else:
            assert credentials is not None
            creator = LiveV0Creator(
                model=lock.roles[ModelRole.CREATOR],
                credentials=credentials,
                executable=config.claude_executable,
                environ=os.environ,
                timeout_seconds=args.creator_timeout,
            )
        skill = create_skill_v0(
            seed_pack=pack,
            output_dir=args.output_root / "skill" / "v0",
            creator=creator,
            workspace_root=args.output_root / "creator-workspaces",
        )
        gate = run_static_gate(
            skill.source, audit_path=args.output_root / "static-gate.json"
        )
        if gate.status is not StaticGateStatus.PASS:
            raise ValueError("v0 candidate failed static gate")
        discovery: DiscoveryBackend
        if args.mode == "fixed":
            discovery = FixedNativeDiscovery()
        else:
            assert credentials is not None
            discovery = ClaudeNativeDiscovery(
                skill_source=skill.source,
                model=lock.roles[ModelRole.MAIN],
                credentials=credentials,
                executable=config.claude_executable,
                environ=os.environ,
                workspace_root=args.output_root / "trigger-workspaces",
                timeout_seconds=args.trigger_timeout,
            )
        trigger = evaluate_triggers(
            skill_sha256=skill.sha256,
            engine_version=f"{lock.engine}:{lock.engine_version}",
            discovery=discovery,
        )
        _write_json(
            args.output_root / "trigger-eval.json",
            trigger.model_dump(mode="json"),
        )
        paired = run_fresh_paired(
            skill_source=skill.source,
            output_root=args.output_root,
            project_root=args.project_root,
        )
        _write_json(
            args.output_root / "paired-comparison.json",
            paired.model_dump(mode="json"),
        )
        write_l2_html(
            paired,
            trigger,
            args.output_root / "l2.html",
            result_kind=(
                "fixed_offline_reference"
                if args.mode == "fixed"
                else "live_creator_trigger_fixed_paired"
            ),
        )
        creator_usage = getattr(creator, "usage", None)
        summary = {
            "schema_version": "v1alpha1",
            "record_type": "skill_v0_pipeline_summary",
            "mode": args.mode,
            "network_used": args.mode == "live",
            "live_provider_used": args.mode == "live",
            "creator_live_model_measured": args.mode == "live",
            "trigger_live_model_measured": args.mode == "live",
            "paired_live_model_measured": False,
            "seed_count": len(pack.records),
            "skill_sha256": skill.sha256,
            "static_gate": gate.status.value,
            "trigger_precision": trigger.precision,
            "trigger_recall": trigger.recall,
            "paired_case_count": len(paired.cases),
            "baseline_pass_rate": paired.baseline_pass_rate,
            "skill_pass_rate": paired.skill_pass_rate,
            "baseline_input_tokens": paired.baseline_input_tokens,
            "skill_input_tokens": paired.skill_input_tokens,
            "baseline_output_tokens": paired.baseline_output_tokens,
            "skill_output_tokens": paired.skill_output_tokens,
            "baseline_cost_amount": str(paired.baseline_cost_amount),
            "skill_cost_amount": str(paired.skill_cost_amount),
            "baseline_latency_ms": paired.baseline_latency_ms,
            "skill_latency_ms": paired.skill_latency_ms,
            "creator_input_tokens": 0
            if creator_usage is None
            else creator_usage.input_tokens,
            "creator_output_tokens": 0
            if creator_usage is None
            else creator_usage.output_tokens,
            "creator_cost_amount": (
                "0"
                if creator_usage is None or creator_usage.cost_amount is None
                else str(creator_usage.cost_amount)
            ),
            "creator_latency_ms": getattr(creator, "latency_ms", 0),
            "trigger_input_tokens": getattr(discovery, "input_tokens", 0),
            "trigger_output_tokens": getattr(discovery, "output_tokens", 0),
            "trigger_cost_amount": str(getattr(discovery, "cost_amount", 0)),
            "l2": "l2.html",
        }
        _write_json(args.output_root / "summary.json", summary)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        message = str(exc)
        if "/" in message or "\\" in message:
            message = type(exc).__name__
        print(f"skill_v0_error:{message}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        for key, value in summary.items():
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
            pack = load_creator_seed_pack(args.seed_manifest)
            creator: V0Creator
            if args.mode == "fixed":
                creator = FakeV0Creator()
            else:
                config = load_runtime_config(args.project_root / "ses.json")
                lock = load_model_lock(args.project_root / config.models_lock)
                creator = LiveV0Creator(
                    model=lock.roles[ModelRole.CREATOR],
                    credentials=read_siliconflow_credentials(os.environ),
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
                "mode": args.mode,
                "input_tokens": 0 if usage is None else usage.input_tokens,
                "output_tokens": 0 if usage is None else usage.output_tokens,
                "cost_amount": (
                    "0"
                    if usage is None or usage.cost_amount is None
                    else str(usage.cost_amount)
                ),
                "latency_ms": getattr(creator, "latency_ms", 0),
            }
        else:
            report = run_static_gate(args.skill, audit_path=args.output)
            payload = report.model_dump(mode="json")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"skill_error:{type(exc).__name__}", file=sys.stderr)
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
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        gate = run_static_gate(args.skill)
        if gate.status is not StaticGateStatus.PASS:
            raise ValueError("static gate failed")
        config = load_runtime_config(args.project_root / "ses.json")
        lock = load_model_lock(args.project_root / config.models_lock)
        discovery: DiscoveryBackend
        if args.mode == "fixed":
            discovery = FixedNativeDiscovery()
        else:
            discovery = ClaudeNativeDiscovery(
                skill_source=args.skill,
                model=lock.roles[ModelRole.MAIN],
                credentials=read_siliconflow_credentials(os.environ),
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
            discovery=discovery,
        )
        result_payload = result.model_dump(mode="json")
        payload = {
            **result_payload,
            "mode": args.mode,
            "network_used": args.mode == "live",
            "input_tokens": getattr(discovery, "input_tokens", 0),
            "output_tokens": getattr(discovery, "output_tokens", 0),
            "cost_amount": str(getattr(discovery, "cost_amount", 0)),
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
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        result = run_fresh_paired(
            skill_source=args.skill,
            output_root=args.output_root,
            project_root=args.project_root,
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
    from ses.contracts.runner import PairedComparison
    from ses.skills.trigger_eval import TriggerEvalResult

    parser = argparse.ArgumentParser(prog="ses l2-render")
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--trigger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--result-kind",
        choices=(
            "fixed_offline_reference",
            "live_measured",
            "live_creator_trigger_fixed_paired",
        ),
        default="fixed_offline_reference",
    )
    args = parser.parse_args(argv)
    try:
        paired = PairedComparison.model_validate_json(
            args.comparison.read_text(encoding="utf-8")
        )
        trigger = TriggerEvalResult.model_validate_json(
            args.trigger.read_text(encoding="utf-8")
        )
        write_l2_html(paired, trigger, args.output, result_kind=args.result_kind)
    except (OSError, TypeError, ValueError) as exc:
        print(f"l2_render_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"html": str(args.output)}, sort_keys=True, separators=(",", ":")))
    return 0
