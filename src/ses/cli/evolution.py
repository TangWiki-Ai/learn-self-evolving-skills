"""Thin argparse adapters for Ticket 09 evolution workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.contracts import FailureCardSet
from ses.evolution.candidate import CandidateError, load_patch
from ses.evolution.evidence import EvidenceError, export_failure_evidence
from ses.evolution.updater import ClaudeCodeUpdater, FakeUpdater, Updater
from ses.evolution.workflow import (
    EvolutionWorkflowError,
    publish_candidate_bundle,
    run_evolution_workflow,
)
from ses.evolution.workspace import UpdaterWorkspaceError
from ses.foundation.config import (
    ModelRole,
    ProviderId,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import read_provider_credentials


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    if not message or "/" in message or "\\" in message:
        return type(exc).__name__
    return message


def export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ses export-failure-evidence")
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--baseline-events", type=Path, required=True)
    parser.add_argument("--skill-events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--comparison-sha256", required=True)
    parser.add_argument("--pair-execution-sha256", required=True)
    parser.add_argument("--skill-sha256", required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def export_main(argv: Sequence[str]) -> int:
    args = export_parser().parse_args(argv)
    try:
        fixture = export_failure_evidence(
            comparison_path=args.comparison,
            baseline_events_path=args.baseline_events,
            skill_events_path=args.skill_events,
            output_path=args.output,
            expected_comparison_sha256=args.comparison_sha256,
            expected_pair_execution_sha256=args.pair_execution_sha256,
            expected_skill_sha256=args.skill_sha256,
        )
    except (EvidenceError, OSError, TypeError, ValueError) as exc:
        print(f"evidence_export_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = fixture.model_dump(mode="json")
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"output={args.output}")
        print(f"provenance={fixture.provenance.value}")
        print(f"case_count={len(fixture.cases)}")
    return 0


def _load_card_set(path: Path) -> FailureCardSet:
    try:
        return FailureCardSet.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CandidateError("invalid Failure Card set JSON") from exc


def candidate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ses candidate-patch")
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--failure-cards", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def candidate_main(argv: Sequence[str]) -> int:
    args = candidate_parser().parse_args(argv)
    try:
        candidate = publish_candidate_bundle(
            parent_dir=args.parent,
            evidence_path=args.evidence,
            card_set=_load_card_set(args.failure_cards),
            patch=load_patch(args.patch),
            output_root=args.output,
        )
    except (
        CandidateError,
        EvolutionWorkflowError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"candidate_patch_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = candidate.model_dump(mode="json")
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"output={args.output}")
        print(f"candidate_id={candidate.candidate_id}")
        print(f"content_sha256={candidate.content_sha256}")
    return 0


def evolve_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(prog="ses evolve")
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("fixed", "live"), default="fixed")
    parser.add_argument("--provider", type=ProviderId, choices=tuple(ProviderId))
    parser.add_argument("--project-root", type=Path, default=root)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def evolve_main(argv: Sequence[str]) -> int:
    args = evolve_parser().parse_args(argv)
    try:
        updater: Updater
        if args.mode == "fixed":
            updater = FakeUpdater()
        else:
            config = load_runtime_config(args.project_root / "ses.json")
            provider = args.provider or config.default_provider
            lock = load_model_lock(args.project_root / config.models_lock_for(provider))
            if lock.provider is not provider:
                raise ValueError("selected provider differs from its model lock")
            updater = ClaudeCodeUpdater(
                model=lock.roles[ModelRole.CREATOR],
                credentials=read_provider_credentials(provider, os.environ),
                executable=config.claude_executable,
                environ=os.environ,
                timeout_seconds=args.timeout,
            )
        summary = run_evolution_workflow(
            parent_dir=args.parent,
            evidence_path=args.evidence,
            output_root=args.output,
            updater=updater,
            mode=args.mode,
            workspace_root=args.workspace_root,
        )
    except (
        CandidateError,
        EvidenceError,
        EvolutionWorkflowError,
        UpdaterWorkspaceError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"evolve_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = summary.model_dump(mode="json")
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"output={args.output}")
        print(f"failure_card_count={summary.failure_card_count}")
        print(f"patch_operation_count={summary.patch_operation_count}")
        print(f"candidate_skill_sha256={summary.candidate_skill_sha256}")
    return 0
