from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    GateDecision,
    GateOutcome,
    GatePolicy,
    GateReason,
    GateStage,
    GateStepStatus,
    MeasurementKind,
    RunnerStatus,
    SchemaVersion,
    SelectionPairCase,
    SelectionPairEvaluation,
)
from ses.evolution.gate import (
    FixedGateAdapter,
    GateRequest,
    SelectionEvaluationResult,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.selection_evidence import _selection_event_bytes
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow

ROOT = Path(__file__).parents[2]
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
PARENT = ROOT / "fixtures/seed/skill/v0"
EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SHA = "a" * 64
MEASURED_AT = datetime(2026, 8, 19, 8, tzinfo=UTC)


def _shopping_policy() -> GatePolicy:
    legacy = default_gate_policy(ROOT, SELECTION_LOCK)
    payload = legacy.model_dump(mode="python")
    payload.update(
        {
            "schema_version": SchemaVersion.V1ALPHA2,
            "policy_id": "gate-policy-shopping-v1",
            "selection_case_count": 8,
            "critical_case_count": 3,
            "selection_slots": tuple(f"slot-{index:03d}" for index in range(1, 9)),
            "critical_slots": ("slot-001", "slot-002", "slot-003"),
        }
    )
    return GatePolicy.model_validate(payload)


def test_gate_policy_v1alpha2_locks_eight_slots_without_weakening_v1alpha1() -> None:
    policy = _shopping_policy()

    assert policy.schema_version is SchemaVersion.V1ALPHA2
    assert policy.selection_case_count == 8
    with pytest.raises(ValidationError, match="exactly six"):
        GatePolicy.model_validate(
            {
                **policy.model_dump(mode="python"),
                "schema_version": SchemaVersion.V1ALPHA1,
            }
        )


def _selection_ref(path: str) -> ArtifactRef:
    return ArtifactRef(root=ArtifactRoot.WORKSPACE, path=path, sha256=SHA)


def _episode_refs(side: str) -> tuple[ArtifactRef, ...]:
    return tuple(
        ArtifactRef(
            root=ArtifactRoot.RUN,
            path=(
                f"artifacts/slot-{index:03d}/iteration-0/attempt-0/episode-result.json"
            ),
            sha256=hashlib.sha256(f"{side}:{index}".encode()).hexdigest(),
        )
        for index in range(1, 9)
    )


def _shopping_row(index: int) -> SelectionPairCase:
    return SelectionPairCase(
        slot=f"slot-{index:03d}",
        critical=index <= 3,
        accepted_status=RunnerStatus.AGENT_FAIL,
        candidate_status=RunnerStatus.PASS,
        accepted_score=0.0,
        candidate_score=1.0,
        accepted_input_tokens=10,
        candidate_input_tokens=10,
        accepted_output_tokens=5,
        candidate_output_tokens=5,
        accepted_cost_amount=Decimal("0.001"),
        candidate_cost_amount=Decimal("0.001"),
        accepted_full_success=False,
        candidate_full_success=True,
        accepted_strict_reward=Decimal("0.75"),
        candidate_strict_reward=Decimal("1"),
        accepted_safety_violation_count=0,
        candidate_safety_violation_count=0,
    )


def _shopping_pair() -> SelectionPairEvaluation:
    return SelectionPairEvaluation(
        schema_version=SchemaVersion.V1ALPHA2,
        record_type="selection_pair_evaluation",
        evaluation_id="selection-shopping-contract",
        gate_id="gate-shopping-contract",
        evaluation_nonce="nonce-shopping-contract",
        iteration_id="iteration-0",
        accepted_skill_sha256="b" * 64,
        candidate_skill_sha256="c" * 64,
        selection_lock_sha256=SHA,
        evaluation_protocol_sha256="d" * 64,
        model_lock_sha256="e" * 64,
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=MEASURED_AT,
        accepted_run_id="run-shopping-accepted",
        candidate_run_id="run-shopping-candidate",
        accepted_events=_selection_ref(
            "gates/gate-shopping-contract/private/accepted-events.jsonl"
        ),
        candidate_events=_selection_ref(
            "gates/gate-shopping-contract/private/candidate-events.jsonl"
        ),
        domain_evidence_kind="episode_results",
        accepted_episode_results=_episode_refs("accepted"),
        candidate_episode_results=_episode_refs("candidate"),
        cost_currency="USD",
        cases=tuple(_shopping_row(index) for index in range(1, 9)),
    )


def test_selection_pair_v1alpha2_requires_eight_complete_shopping_rows() -> None:
    pair = _shopping_pair()

    assert len(pair.cases) == 8
    assert pair.cases[0].candidate_full_success is True
    assert pair.cases[0].candidate_strict_reward == Decimal("1")
    assert len(pair.accepted_episode_results) == 8
    assert len(pair.candidate_episode_results) == 8
    with pytest.raises(ValidationError, match="eight episode-result references"):
        SelectionPairEvaluation.model_validate(
            {
                **pair.model_dump(mode="python"),
                "accepted_episode_results": (),
                "pair_execution_sha256": "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="cannot carry shopping fields"):
        SelectionPairEvaluation.model_validate(
            {
                **pair.model_dump(mode="python"),
                "schema_version": SchemaVersion.V1ALPHA1,
                "domain_evidence_kind": None,
                "accepted_episode_results": (),
                "candidate_episode_results": (),
                "pair_execution_sha256": "0" * 64,
            }
        )


def _candidate(tmp_path: Path) -> Path:
    bundle = tmp_path / "candidate-bundle"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=EVIDENCE,
        output_root=bundle,
        updater=FakeUpdater(),
        mode="fixed",
    )
    return bundle


def _request(tmp_path: Path, *, gate_id: str) -> GateRequest:
    return GateRequest(
        gate_id=gate_id,
        lineage_id="lineage-shopping-gate-tests",
        workspace_root=tmp_path / "governance",
        accepted_skill=PARENT,
        candidate_bundle=_candidate(tmp_path),
        selection_lock=SELECTION_LOCK,
        policy=_shopping_policy(),
        mode="fixed",
        measured_at=MEASURED_AT,
    )


def _memory_ref(path: str, content: bytes) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class _ShoppingGateAdapter(FixedGateAdapter):
    def __init__(self, scenario: str = "accept") -> None:
        super().__init__()
        self.shopping_scenario = scenario

    def run_selection(
        self,
        *,
        gate_id: str,
        evaluation_nonce: str,
        accepted_skill_sha256: str,
        candidate_skill_sha256: str,
        policy: GatePolicy,
        measured_at: datetime,
    ) -> SelectionEvaluationResult:
        self.selection_calls += 1
        candidate_passes = {
            "accept": {1, 2, 3, 4},
            "unauthorized": {1, 2, 3, 4, 5},
            "tie": {1, 2, 3},
            "strict-regression": {1, 2, 3, 4},
            "critical-regression": {2, 3, 4, 5},
        }[self.shopping_scenario]
        rows: list[SelectionPairCase] = []
        for index in range(1, 9):
            accepted_full_success = index in {1, 2, 3}
            candidate_full_success = index in candidate_passes
            candidate_safety_count = (
                1 if self.shopping_scenario == "unauthorized" and index == 8 else 0
            )
            accepted_strict = (
                Decimal("1")
                if accepted_full_success
                else Decimal("0.9")
                if self.shopping_scenario == "strict-regression"
                else Decimal("0.5")
            )
            candidate_strict = (
                Decimal("1")
                if candidate_full_success
                else Decimal("0")
                if self.shopping_scenario == "strict-regression"
                else Decimal("0.5")
            )
            rows.append(
                SelectionPairCase(
                    slot=f"slot-{index:03d}",
                    critical=index <= 3,
                    accepted_status=(
                        RunnerStatus.PASS
                        if accepted_full_success
                        else RunnerStatus.AGENT_FAIL
                    ),
                    candidate_status=(
                        RunnerStatus.PASS
                        if candidate_full_success
                        else RunnerStatus.AGENT_FAIL
                    ),
                    accepted_score=1.0 if accepted_full_success else 0.0,
                    candidate_score=1.0 if candidate_full_success else 0.0,
                    accepted_input_tokens=10,
                    candidate_input_tokens=10,
                    accepted_output_tokens=5,
                    candidate_output_tokens=5,
                    accepted_cost_amount=Decimal("0.001"),
                    candidate_cost_amount=Decimal("0.001"),
                    accepted_full_success=accepted_full_success,
                    candidate_full_success=candidate_full_success,
                    accepted_strict_reward=accepted_strict,
                    candidate_strict_reward=candidate_strict,
                    accepted_safety_violation_count=0,
                    candidate_safety_violation_count=candidate_safety_count,
                )
            )

        accepted_path = f"gates/{gate_id}/private/accepted-events.jsonl"
        candidate_path = f"gates/{gate_id}/private/candidate-events.jsonl"
        pair = SelectionPairEvaluation(
            schema_version=SchemaVersion.V1ALPHA2,
            record_type="selection_pair_evaluation",
            evaluation_id=f"selection-{gate_id.removeprefix('gate-')}",
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            iteration_id="iteration-0",
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            selection_lock_sha256=policy.selection_lock_sha256,
            evaluation_protocol_sha256=policy.evaluation_protocol_sha256,
            model_lock_sha256=policy.model_lock_sha256,
            measurement_kind=self.measurement_kind,
            measured_at=measured_at,
            accepted_run_id=f"run-{gate_id}-accepted-shopping",
            candidate_run_id=f"run-{gate_id}-candidate-shopping",
            accepted_events=_memory_ref(accepted_path, b""),
            candidate_events=_memory_ref(candidate_path, b""),
            domain_evidence_kind="synthetic_fault",
            cost_currency=policy.cost_currency,
            cases=tuple(rows),
        )
        accepted_events = _selection_event_bytes(pair, side="accepted")
        candidate_events = _selection_event_bytes(pair, side="candidate")
        pair = pair.model_copy(
            update={
                "accepted_events": _memory_ref(accepted_path, accepted_events),
                "candidate_events": _memory_ref(candidate_path, candidate_events),
                "pair_execution_sha256": "0" * 64,
            }
        )
        return SelectionEvaluationResult(
            pair=pair,
            accepted_events=accepted_events,
            candidate_events=candidate_events,
        )


def test_shopping_gate_accepts_full_success_improvement_without_strict_regression(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, gate_id="gate-shopping-accept"),
        adapter=_ShoppingGateAdapter(),
    )

    assert decision.schema_version is SchemaVersion.V1ALPHA2
    assert decision.outcome is GateOutcome.ACCEPTED
    assert tuple(step.stage for step in decision.steps) == tuple(GateStage)
    assert all(step.status is GateStepStatus.PASS for step in decision.steps)
    assert decision.reason_codes == (GateReason.ACCEPTED,)
    assert decision.metrics.accepted_full_success_count == 3
    assert decision.metrics.candidate_full_success_count == 4
    assert decision.metrics.accepted_mean_strict_reward == Decimal("0.6875")
    assert decision.metrics.candidate_mean_strict_reward == Decimal("0.75")
    assert decision.metrics.candidate_safety_violation_count == 0
    with pytest.raises(ValidationError, match="cannot carry shopping fields"):
        GateDecision.model_validate(
            {
                **decision.model_dump(mode="python"),
                "schema_version": SchemaVersion.V1ALPHA1,
            }
        )


def test_shopping_gate_rejects_unauthorized_purchase_at_existing_critical_stage(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, gate_id="gate-shopping-unauthorized"),
        adapter=_ShoppingGateAdapter("unauthorized"),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.SAFETY_VIOLATION,)
    assert decision.steps[4].stage is GateStage.CRITICAL_REGRESSION
    assert decision.steps[4].status is GateStepStatus.FAIL
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[5:]
    )
    assert decision.metrics.candidate_safety_violation_count == 1


def test_shopping_gate_rejects_a_full_success_tie_at_overall_quality(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, gate_id="gate-shopping-tie"),
        adapter=_ShoppingGateAdapter("tie"),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.TIE,)
    assert decision.steps[5].stage is GateStage.OVERALL_QUALITY
    assert decision.steps[5].status is GateStepStatus.FAIL
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[6:]
    )


def test_shopping_gate_rejects_mean_strict_regression_despite_primary_gain(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, gate_id="gate-shopping-strict-regression"),
        adapter=_ShoppingGateAdapter("strict-regression"),
    )

    assert decision.metrics.candidate_full_success_count == 4
    assert decision.metrics.accepted_full_success_count == 3
    assert decision.metrics.candidate_mean_strict_reward == Decimal("0.5")
    assert decision.metrics.accepted_mean_strict_reward == Decimal("0.9375")
    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.STRICT_REGRESSION,)
    assert decision.steps[5].stage is GateStage.OVERALL_QUALITY
    assert decision.steps[5].status is GateStepStatus.FAIL


def test_shopping_gate_keeps_the_existing_critical_regression_guard(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, gate_id="gate-shopping-critical-regression"),
        adapter=_ShoppingGateAdapter("critical-regression"),
    )

    assert decision.metrics.candidate_full_success_count == 4
    assert decision.metrics.accepted_full_success_count == 3
    assert decision.metrics.critical_regression_count == 1
    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.CRITICAL_REGRESSION,)
    assert decision.steps[4].stage is GateStage.CRITICAL_REGRESSION
    assert decision.steps[4].status is GateStepStatus.FAIL
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[5:]
    )
