"""Thin argparse adapters for Ticket 09 offline workflows."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.contracts import FailureCard
from ses.evolution.candidate import (
    CandidateError,
    create_candidate,
    load_patch,
    write_candidate_record,
)
from ses.evolution.evidence import EvidenceError, export_failure_evidence
from ses.evolution.workspace import UpdaterWorkspaceError, create_updater_workspace


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
        print(f"evidence_export_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    payload = fixture.model_dump(mode="json")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _load_cards(path: Path) -> tuple[FailureCard, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        values = raw["cards"] if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise ValueError("cards must be a JSON array")
        return tuple(FailureCard.model_validate(value) for value in values)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CandidateError("invalid failure card JSON") from exc


def candidate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ses candidate-patch")
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--failure-cards", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-output", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--parent-sha256")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def candidate_main(argv: Sequence[str]) -> int:
    args = candidate_parser().parse_args(argv)
    updater = None
    try:
        patch = load_patch(args.patch)
        cards = _load_cards(args.failure_cards)
        updater = create_updater_workspace(
            evidence_path=args.evidence,
            parent_dir=args.parent,
            root=args.workspace_root,
        )
        candidate = create_candidate(
            parent_dir=updater.workspace.root / "parent-skill",
            patch=patch,
            cards=cards,
            evidence_path=updater.workspace.root / "inputs" / args.evidence.name,
            output_dir=args.output,
            expected_parent_sha256=args.parent_sha256,
        )
        write_candidate_record(args.record_output, candidate)
    except (
        CandidateError,
        UpdaterWorkspaceError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"candidate_patch_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if updater is not None:
            updater.cleanup()
    payload = candidate.model_dump(mode="json")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0
