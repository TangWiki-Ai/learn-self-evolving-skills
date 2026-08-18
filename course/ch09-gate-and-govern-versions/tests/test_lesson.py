from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import pytest

from ses.contracts import GateDecision, GateOutcome, RegistryEventType, VersionStatus
from ses.evolution.gate import FixedGateScenario
from ses.evolution.registry import RegistryError, RegistryState, SkillRegistry
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow

LESSON = Path(__file__).parents[1]
ROOT = LESSON.parents[1]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
INITIAL_EVIDENCE = ROOT / "course/ch07-create-v0/artifacts/summary.json"
FAILURE_EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)


class _GovernanceResult(Protocol):
    decision: GateDecision
    state: RegistryState
    initial_skill_sha256: str
    candidate_skill_sha256: str


def _variant(name: str) -> ModuleType:
    path = LESSON / name / "governance.py"
    spec = importlib.util.spec_from_file_location(f"lesson_09_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(tmp_path: Path) -> Path:
    output = tmp_path / "candidate"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=FAILURE_EVIDENCE,
        output_root=output,
        updater=FakeUpdater(),
        mode="fixed",
    )
    return output


def _govern(
    tmp_path: Path,
    *,
    scenario: FixedGateScenario,
    rollback_after_promote: bool = False,
) -> _GovernanceResult:
    solution = _variant("solution")
    return cast(
        _GovernanceResult,
        solution.govern(
            project_root=ROOT,
            governance_root=tmp_path / "governance",
            accepted_skill=PARENT,
            initial_evidence=INITIAL_EVIDENCE,
            candidate_bundle=_candidate(tmp_path),
            selection_lock=SELECTION_LOCK,
            scenario=scenario,
            gate_id=f"gate-lesson-{scenario.value}",
            occurred_at=NOW,
            rollback_after_promote=rollback_after_promote,
        ),
    )


def test_solution_accepts_and_explicitly_promotes_an_offline_candidate(
    tmp_path: Path,
) -> None:
    result = _govern(tmp_path, scenario=FixedGateScenario.ACCEPT)

    assert result.decision.outcome is GateOutcome.ACCEPTED
    assert result.decision.mode == "fixed"
    assert result.decision.measurement_kind.value == "synthetic_offline"
    assert result.decision.network_used is False
    assert result.state.current_accepted_sha256 == result.candidate_skill_sha256
    assert tuple(event.event_type for event in result.state.events)[-2:] == (
        RegistryEventType.CANDIDATE_ACCEPTED,
        RegistryEventType.PROMOTED,
    )


def test_solution_records_rejection_without_deleting_or_promoting_candidate(
    tmp_path: Path,
) -> None:
    result = _govern(tmp_path, scenario=FixedGateScenario.TIE)
    registry = SkillRegistry(tmp_path / "governance")

    assert result.decision.outcome is GateOutcome.REJECTED
    assert result.state.current_accepted_sha256 == result.initial_skill_sha256
    rejected = result.state.versions[result.candidate_skill_sha256]
    assert rejected.status is VersionStatus.REJECTED
    assert registry.version_path(result.candidate_skill_sha256).is_dir()
    assert result.state.events[-1].event_type is RegistryEventType.CANDIDATE_REJECTED
    with pytest.raises(RegistryError, match="accepted candidate"):
        registry.promote(
            command_id="command-lesson-invalid-promote",
            candidate_id=result.decision.candidate_id,
            occurred_at=NOW,
        )


def test_solution_rolls_back_only_by_appending_a_verified_history_event(
    tmp_path: Path,
) -> None:
    result = _govern(
        tmp_path,
        scenario=FixedGateScenario.ACCEPT,
        rollback_after_promote=True,
    )

    assert result.state.current_accepted_sha256 == result.initial_skill_sha256
    assert result.state.events[-1].event_type is RegistryEventType.ROLLED_BACK
    assert result.state.versions[result.candidate_skill_sha256].status is (
        VersionStatus.ROLLED_BACK
    )
    assert len(result.state.events) == 5


@pytest.mark.parametrize(
    "function",
    ["gate_candidate", "record_and_maybe_promote", "rollback", "govern"],
)
def test_starter_keeps_the_governance_decision_gaps(function: str) -> None:
    starter = _variant("starter")
    with pytest.raises(NotImplementedError, match="Lesson 9"):
        getattr(starter, function)()


@pytest.mark.parametrize(
    ("artifact", "expected_events", "expected_outcome"),
    [
        ("fixed-accept-promote-rollback", 5, "accepted"),
        ("fixed-rejection", 3, "rejected"),
    ],
)
def test_fixed_reference_artifacts_are_offline_and_replayable(
    artifact: str,
    expected_events: int,
    expected_outcome: str,
) -> None:
    root = LESSON / "artifacts" / artifact
    state = SkillRegistry(root).audit()
    decisions = tuple((root / "gates").glob("gate-*/gate-decision.json"))

    assert len(state.events) == expected_events
    assert len(decisions) == 1
    payload = decisions[0].read_text(encoding="utf-8")
    assert f'"outcome":"{expected_outcome}"' in payload
    assert '"measurement_kind":"synthetic_offline"' in payload
    assert '"network_used":false' in payload
