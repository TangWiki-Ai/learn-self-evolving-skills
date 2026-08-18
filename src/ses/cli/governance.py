"""Thin CLI adapters for candidate gates and the append-only Skill Registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ses.contracts import GateOutcome, RegistryEvent
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateError,
)
from ses.evolution.governance import CandidateGovernanceCommand, govern_candidate
from ses.evolution.registry import RegistryError, RegistryState, SkillRegistry
from ses.foundation.credentials import credential_values, redact


def _safe_error(exc: Exception) -> str:
    message = redact(str(exc), credential_values(os.environ))
    return message or type(exc).__name__


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must use ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _print_payload(payload: Mapping[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            print(f"{key}={value}")


def _event_payload(event: RegistryEvent) -> dict[str, object]:
    return dict(event.model_dump(mode="json"))


def _reference_payload(value: object) -> object:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise TypeError("Registry reference is not serializable")
    return model_dump(mode="json")


def _state_payload(state: RegistryState) -> dict[str, object]:
    versions = []
    for skill_sha256, version in sorted(state.versions.items()):
        versions.append(
            {
                "version_id": version.version_id,
                "skill_sha256": skill_sha256,
                "parent_skill_sha256": version.parent_skill_sha256,
                "status": version.status.value,
                "verified": version.verified,
                "was_current": version.was_current,
                "manifest": _reference_payload(version.manifest),
                "candidate": _reference_payload(version.candidate),
                "gate_decision": _reference_payload(version.gate_decision),
                "evidence": [
                    _reference_payload(reference) for reference in version.evidence
                ],
            }
        )
    return {
        "audit_status": "pass",
        "registry_id": state.registry_id,
        "lineage_id": state.lineage_id,
        "current_accepted_sha256": state.current_accepted_sha256,
        "event_count": len(state.events),
        "events": [event.model_dump(mode="json") for event in state.events],
        "versions": versions,
    }


def registry_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses registry",
        description="Manage and audit immutable Skill version history.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    initialize = commands.add_parser("init")
    initialize.add_argument("--registry", type=Path, required=True)
    initialize.add_argument("--accepted-skill", type=Path, required=True)
    initialize.add_argument("--evidence", type=Path, action="append", required=True)
    initialize.add_argument("--command-id", required=True)
    initialize.add_argument("--occurred-at", type=_datetime, required=True)
    initialize.add_argument("--json", action="store_true", dest="as_json")

    register = commands.add_parser("register")
    register.add_argument("--registry", type=Path, required=True)
    register.add_argument("--candidate-bundle", type=Path, required=True)
    register.add_argument("--command-id", required=True)
    register.add_argument("--occurred-at", type=_datetime, required=True)
    register.add_argument("--json", action="store_true", dest="as_json")

    promote = commands.add_parser("promote")
    promote.add_argument("--registry", type=Path, required=True)
    promote.add_argument("--candidate-id", required=True)
    promote.add_argument("--command-id", required=True)
    promote.add_argument("--occurred-at", type=_datetime, required=True)
    promote.add_argument("--json", action="store_true", dest="as_json")

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--registry", type=Path, required=True)
    rollback.add_argument("--target-skill-sha256", required=True)
    rollback.add_argument("--command-id", required=True)
    rollback.add_argument("--occurred-at", type=_datetime, required=True)
    rollback.add_argument("--json", action="store_true", dest="as_json")

    for action in ("inspect", "audit"):
        read = commands.add_parser(action)
        read.add_argument("--registry", type=Path, required=True)
        read.add_argument("--json", action="store_true", dest="as_json")
    return parser


def registry_main(argv: Sequence[str]) -> int:
    args = registry_parser().parse_args(argv)
    try:
        registry = SkillRegistry(args.registry)
        if args.action == "init":
            event = registry.initialize(
                command_id=args.command_id,
                accepted_skill=args.accepted_skill,
                evidence_paths=tuple(args.evidence),
                occurred_at=args.occurred_at,
            )
            payload = _event_payload(event)
        elif args.action == "register":
            event = registry.register_candidate(
                command_id=args.command_id,
                candidate_bundle=args.candidate_bundle,
                occurred_at=args.occurred_at,
            )
            payload = _event_payload(event)
        elif args.action == "promote":
            event = registry.promote(
                command_id=args.command_id,
                candidate_id=args.candidate_id,
                occurred_at=args.occurred_at,
            )
            payload = _event_payload(event)
        elif args.action == "rollback":
            event = registry.rollback(
                command_id=args.command_id,
                target_skill_sha256=args.target_skill_sha256,
                occurred_at=args.occurred_at,
            )
            payload = _event_payload(event)
        else:
            payload = _state_payload(registry.audit())
    except (OSError, RegistryError, TypeError, ValueError) as exc:
        print(f"registry_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    _print_payload(payload, as_json=args.as_json)
    return 0


def gate_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        prog="ses gate",
        description="Gate a registered candidate against the current accepted Skill.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--registry", type=Path, required=True)
    candidate.add_argument("--candidate-bundle", type=Path, required=True)
    candidate.add_argument(
        "--selection-lock",
        type=Path,
        default=root / "data/testset/protected/selection-manifest.json",
    )
    candidate.add_argument("--project-root", type=Path, default=root)
    candidate.add_argument("--gate-id", required=True)
    candidate.add_argument(
        "--fixed-scenario",
        choices=tuple(scenario.value for scenario in FixedGateScenario),
        default=FixedGateScenario.ACCEPT.value,
    )
    candidate.add_argument("--command-id", required=True)
    candidate.add_argument("--measured-at", type=_datetime, required=True)
    candidate.add_argument("--json", action="store_true", dest="as_json")
    return parser


def gate_main(argv: Sequence[str]) -> int:
    args = gate_parser().parse_args(argv)
    try:
        decision = govern_candidate(
            CandidateGovernanceCommand(
                registry_root=args.registry,
                candidate_bundle=args.candidate_bundle,
                selection_lock=args.selection_lock,
                project_root=args.project_root,
                gate_id=args.gate_id,
                command_id=args.command_id,
                mode="fixed",
                measured_at=args.measured_at,
            ),
            adapter=FixedGateAdapter(FixedGateScenario(args.fixed_scenario)),
        )
    except (GateError, OSError, RegistryError, TypeError, ValueError) as exc:
        print(f"gate_error:{_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = dict(decision.model_dump(mode="json"))
    _print_payload(payload, as_json=args.as_json)
    return 0 if decision.outcome is GateOutcome.ACCEPTED else 1


__all__ = ["gate_main", "gate_parser", "registry_main", "registry_parser"]
