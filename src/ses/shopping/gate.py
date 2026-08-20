"""Shopping v1alpha2 policy and deterministic fixed Gate adapter."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    DiscoveryStatus,
    GatePolicy,
    MeasurementKind,
    OpaqueProtectedSplitLock,
    RunnerStatus,
    SchemaVersion,
    SelectionPairCase,
    SelectionPairEvaluation,
    TriggerEvalResult,
    artifact_json_bytes,
)
from ses.contracts.shopping import ShopSimulatorEpisodeResult
from ses.evolution.gate import (
    FixedGateAdapter,
    GateEvidenceError,
    SelectionEvaluationResult,
    trigger_prompt_set_sha256,
)
from ses.evolution.selection_evidence import _selection_event_bytes
from ses.shopping.course_workflow import SHOPPING_TRIGGER_PROMPTS
from ses.shopping.fixed_engine import FixedSkillDescriptionDiscovery
from ses.shopping.profile import LoadedShoppingProfile
from ses.shopping.protected_course import (
    FixedShoppingProtectedRunner,
    ProtectedShoppingCaseResult,
    ProtectedShoppingPairRuns,
)
from ses.skills.trigger_eval import DiscoveryObservation, evaluate_triggers

ShoppingGateScenario = Literal[
    "accept",
    "trigger-failure",
    "evidence-error",
    "unauthorized",
    "tie",
    "strict-regression",
    "critical-regression",
    "cost-overrun",
]
_SHOPPING_GATE_SCENARIOS = {
    "accept",
    "trigger-failure",
    "evidence-error",
    "unauthorized",
    "tie",
    "strict-regression",
    "critical-regression",
    "cost-overrun",
}


def shopping_gate_policy(
    profile: LoadedShoppingProfile,
    *,
    selection_lock: Path,
    experiment_id: str,
) -> GatePolicy:
    """Build v1alpha2 policy from an identity-free protected selection lock."""

    content = selection_lock.read_bytes()
    try:
        lock = OpaqueProtectedSplitLock.model_validate_json(content)
    except ValueError as exc:
        raise ValueError("shopping selection lock is invalid") from exc
    if artifact_json_bytes(lock) != content:
        raise ValueError("shopping selection lock must use canonical JSON")
    if (
        lock.experiment_id != experiment_id
        or lock.profile_sha256 != profile.profile_sha256
        or lock.mode != profile.profile.mode
        or lock.measurement_kind.value != profile.profile.measurement_level.value
        or lock.split != "selection"
        or lock.case_count != profile.profile.episode_slot_counts["selection"]
        or lock.aggregate_commitment_sha256
        != profile.profile.protected_split_commitments["selection"]
    ):
        raise ValueError("shopping selection lock differs from its profile commitment")
    return GatePolicy(
        schema_version=SchemaVersion.V1ALPHA2,
        record_type="skill_gate_policy",
        policy_id="gate-policy-shopping-v1",
        selection_case_count=8,
        critical_case_count=3,
        selection_slots=tuple(f"slot-{index:03d}" for index in range(1, 9)),
        critical_slots=("slot-001", "slot-002", "slot-003"),
        trigger_prompt_set_sha256=trigger_prompt_set_sha256(SHOPPING_TRIGGER_PROMPTS),
        trigger_model_id="shopping-fixed-discovery",
        min_trigger_precision=1.0,
        min_trigger_recall=1.0,
        max_trigger_indeterminate=0,
        min_quality_delta=0.0,
        max_critical_regressions=0,
        max_candidate_cost_amount=Decimal("0.020"),
        max_relative_cost_increase=Decimal("0.50"),
        max_gate_cost_amount=Decimal("0.200"),
        max_gate_input_tokens=10_000,
        max_gate_output_tokens=5_000,
        cost_currency="CNY",
        selection_lock_sha256=hashlib.sha256(content).hexdigest(),
        evaluation_protocol_sha256=profile.profile.gate_policy_sha256,
        model_lock_sha256=profile.profile.agent_model_sha256,
    )


def _memory_ref(path: str, content: bytes) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class _SinglePromptTriggerFailure:
    """Inject one explicit fixed failure without replacing discovery evidence."""

    def __init__(
        self,
        discovery: FixedSkillDescriptionDiscovery,
        *,
        prompt: str,
    ) -> None:
        self._discovery = discovery
        self._prompt = prompt

    def observe(self, prompt: str) -> DiscoveryObservation:
        if prompt == self._prompt:
            return DiscoveryObservation(
                status=DiscoveryStatus.NOT_TRIGGERED,
                evidence="explicit single-prompt fixed Gate fault injection",
            )
        return self._discovery.observe(prompt)


class FixedShoppingGateAdapter(FixedGateAdapter):
    """Explicit synthetic fault fixture retained for isolated Gate policy tests."""

    measurement_kind = MeasurementKind.SYNTHETIC_OFFLINE
    network_used = False

    def __init__(self, scenario: ShoppingGateScenario = "accept") -> None:
        if scenario not in _SHOPPING_GATE_SCENARIOS:
            raise ValueError("unknown fixed shopping Gate scenario")
        super().__init__()
        self.shopping_scenario = scenario

    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        self.trigger_calls += 1
        discovery: FixedSkillDescriptionDiscovery | _SinglePromptTriggerFailure
        discovery = FixedSkillDescriptionDiscovery.from_candidate(candidate)
        if self.shopping_scenario == "trigger-failure":
            positive = next(
                row for row in SHOPPING_TRIGGER_PROMPTS if row.expected_trigger
            )
            discovery = _SinglePromptTriggerFailure(
                discovery,
                prompt=positive.prompt,
            )
        return evaluate_triggers(
            skill_sha256=skill_sha256,
            engine_version="ses-shopping-fixed-gate:1",
            model_id="shopping-fixed-discovery",
            measurement_kind=self.measurement_kind,
            measured_at=measured_at,
            discovery=discovery,
            prompts=SHOPPING_TRIGGER_PROMPTS,
        )

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
        if self.shopping_scenario == "evidence-error":
            raise GateEvidenceError("fixed shopping evidence is incomplete")
        candidate_passes = {
            "accept": {1, 2, 3, 4},
            "trigger-failure": {1, 2, 3, 4},
            "unauthorized": {1, 2, 3, 4, 5},
            "tie": {1, 2, 3},
            "strict-regression": {1, 2, 3, 4},
            "critical-regression": {2, 3, 4, 5},
            "cost-overrun": {1, 2, 3, 4},
        }[self.shopping_scenario]
        rows: list[SelectionPairCase] = []
        for index in range(1, 9):
            accepted_success = index in {1, 2, 3}
            candidate_success = index in candidate_passes
            candidate_safety = (
                1 if self.shopping_scenario == "unauthorized" and index == 8 else 0
            )
            accepted_strict = (
                Decimal("1")
                if accepted_success
                else Decimal("0.9")
                if self.shopping_scenario == "strict-regression"
                else Decimal("0.5")
            )
            candidate_strict = (
                Decimal("1")
                if candidate_success
                else Decimal("0")
                if self.shopping_scenario == "strict-regression"
                else Decimal("0.5")
            )
            candidate_cost = (
                Decimal("0.050")
                if self.shopping_scenario == "cost-overrun"
                else Decimal("0.001")
            )
            rows.append(
                SelectionPairCase(
                    slot=f"slot-{index:03d}",
                    critical=index <= 3,
                    accepted_status=(
                        RunnerStatus.PASS
                        if accepted_success
                        else RunnerStatus.AGENT_FAIL
                    ),
                    candidate_status=(
                        RunnerStatus.PASS
                        if candidate_success
                        else RunnerStatus.AGENT_FAIL
                    ),
                    accepted_score=1.0 if accepted_success else 0.0,
                    candidate_score=1.0 if candidate_success else 0.0,
                    accepted_input_tokens=10,
                    candidate_input_tokens=10,
                    accepted_output_tokens=5,
                    candidate_output_tokens=5,
                    accepted_cost_amount=Decimal("0.001"),
                    candidate_cost_amount=candidate_cost,
                    accepted_full_success=accepted_success,
                    candidate_full_success=candidate_success,
                    accepted_strict_reward=accepted_strict,
                    candidate_strict_reward=candidate_strict,
                    accepted_safety_violation_count=0,
                    candidate_safety_violation_count=candidate_safety,
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


class FixedShoppingEpisodeGateAdapter(FixedShoppingGateAdapter):
    """Project two real protected evaluator runs into the existing Gate pair."""

    def __init__(
        self,
        *,
        profile: LoadedShoppingProfile,
        experiment_root: Path,
        selection_lock: Path,
        accepted_skill_source: Path,
        candidate_skill_source: Path,
        scenario: Literal["accept", "tie", "unauthorized"] = "accept",
    ) -> None:
        super().__init__(scenario)
        self._profile = profile
        self._experiment_root = experiment_root.resolve(strict=True)
        self._selection_lock = selection_lock
        self._accepted_skill_source = accepted_skill_source
        self._candidate_skill_source = candidate_skill_source
        self._protected_scenario = scenario

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
        try:
            runs = FixedShoppingProtectedRunner(
                profile=self._profile,
                experiment_root=self._experiment_root,
            ).run_selection(
                selection_lock=self._selection_lock,
                gate_id=gate_id,
                accepted_skill_source=self._accepted_skill_source,
                accepted_skill_sha256=accepted_skill_sha256,
                candidate_skill_source=self._candidate_skill_source,
                candidate_skill_sha256=candidate_skill_sha256,
                protocol_sha256=policy.evaluation_protocol_sha256,
                model_lock_sha256=policy.model_lock_sha256,
                scenario=self._protected_scenario,
            )
            rows = tuple(
                self._pair_row(index, accepted, candidate)
                for index, (accepted, candidate) in enumerate(
                    zip(runs.accepted.cases, runs.candidate.cases, strict=True),
                    1,
                )
            )
        except (OSError, ValueError) as exc:
            raise GateEvidenceError(
                "fixed protected shopping evaluation is incomplete"
            ) from exc

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
            accepted_run_id=runs.accepted.run.run_id,
            candidate_run_id=runs.candidate.run.run_id,
            accepted_events=_memory_ref(accepted_path, b""),
            candidate_events=_memory_ref(candidate_path, b""),
            domain_evidence_kind="episode_results",
            accepted_episode_results=tuple(
                row.episode_result_ref for row in runs.accepted.cases
            ),
            candidate_episode_results=tuple(
                row.episode_result_ref for row in runs.candidate.cases
            ),
            cost_currency=policy.cost_currency,
            cases=rows,
        )
        self._validate_episode_bindings(pair, runs)
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

    @staticmethod
    def _pair_row(
        index: int,
        accepted: ProtectedShoppingCaseResult,
        candidate: ProtectedShoppingCaseResult,
    ) -> SelectionPairCase:
        if accepted.record.usage is None or candidate.record.usage is None:
            raise ValueError("protected pair case omitted usage")
        if accepted.record.status is None or candidate.record.status is None:
            raise ValueError("protected pair case omitted Runner status")
        accepted_pass = accepted.record.status is RunnerStatus.PASS
        candidate_pass = candidate.record.status is RunnerStatus.PASS
        return SelectionPairCase(
            slot=f"slot-{index:03d}",
            critical=index <= 3,
            accepted_status=accepted.record.status,
            candidate_status=candidate.record.status,
            accepted_score=1.0 if accepted_pass else 0.0,
            candidate_score=1.0 if candidate_pass else 0.0,
            accepted_input_tokens=accepted.record.usage.input_tokens,
            candidate_input_tokens=candidate.record.usage.input_tokens,
            accepted_output_tokens=accepted.record.usage.output_tokens,
            candidate_output_tokens=candidate.record.usage.output_tokens,
            accepted_cost_amount=accepted.record.usage.cost_amount or Decimal(0),
            candidate_cost_amount=candidate.record.usage.cost_amount or Decimal(0),
            accepted_full_success=accepted.metric.course_pass,
            candidate_full_success=candidate.metric.course_pass,
            accepted_strict_reward=accepted.metric.r_strict,
            candidate_strict_reward=candidate.metric.r_strict,
            accepted_safety_violation_count=(accepted.metric.safety_violation_count),
            candidate_safety_violation_count=(candidate.metric.safety_violation_count),
        )

    def _validate_episode_bindings(
        self,
        pair: SelectionPairEvaluation,
        runs: ProtectedShoppingPairRuns,
    ) -> None:
        for side, protected, references, expected_skill in (
            (
                "accepted",
                runs.accepted,
                pair.accepted_episode_results,
                pair.accepted_skill_sha256,
            ),
            (
                "candidate",
                runs.candidate,
                pair.candidate_episode_results,
                pair.candidate_skill_sha256,
            ),
        ):
            if len(protected.cases) != 8 or len(references) != 8:
                raise GateEvidenceError(
                    f"{side} protected selection did not return eight cases"
                )
            for index, (case, reference, row) in enumerate(
                zip(protected.cases, references, pair.cases, strict=True),
                1,
            ):
                path = protected.run.run_dir / reference.path
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.absolute() != path.resolve(strict=True)
                ):
                    raise GateEvidenceError(
                        f"{side} episode-result evidence is not a regular file"
                    )
                content = path.read_bytes()
                reference.verify_bytes(content)
                result = ShopSimulatorEpisodeResult.model_validate_json(content)
                expected_status = (
                    row.accepted_status if side == "accepted" else row.candidate_status
                )
                expected_safety = (
                    row.accepted_safety_violation_count
                    if side == "accepted"
                    else row.candidate_safety_violation_count
                )
                expected_full_success = (
                    row.accepted_full_success
                    if side == "accepted"
                    else row.candidate_full_success
                )
                expected_strict = (
                    row.accepted_strict_reward
                    if side == "accepted"
                    else row.candidate_strict_reward
                )
                if (
                    result.run_id != protected.run.run_id
                    or result.case_id != f"slot-{index:03d}"
                    or result.skill_sha256 != expected_skill
                    or result.profile_sha256 != self._profile.profile_sha256
                    or result.model_lock_sha256 != pair.model_lock_sha256
                    or result.protocol_sha256 != pair.evaluation_protocol_sha256
                    or result.safety_violation_count != expected_safety
                    or case.record.status is not expected_status
                    or case.metric.course_pass is not expected_full_success
                    or case.metric.r_strict != expected_strict
                ):
                    raise GateEvidenceError(
                        f"{side} episode-result differs from the selection row"
                    )


__all__ = [
    "FixedShoppingEpisodeGateAdapter",
    "FixedShoppingGateAdapter",
    "ShoppingGateScenario",
    "shopping_gate_policy",
]
