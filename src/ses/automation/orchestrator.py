"""Bounded auto-evolution composed from the existing evolution and Gate modules."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from ses.automation.state import AutoStateError, AutoStateStore, StepBudgetUsage
from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    AutoEvolveConfig,
    AutoEvolveState,
    AutoLoopStatus,
    AutoRolloutReceipt,
    AutoRoundRecord,
    AutoStopReason,
    CandidateArtifact,
    CapstoneFinalReceipt,
    EvolutionPipelineSummary,
    FailureCardSet,
    FailureEvidenceFixture,
    FinalAggregateReport,
    FinalConsumedCheckpoint,
    FinalLifecycle,
    FinalRunReceipt,
    GateDecision,
    GateOutcome,
    GatePolicy,
    MeasurementKind,
    OpaqueProtectedSplitLock,
    Patch,
    SchemaVersion,
    ShoppingFinalScenarioMetrics,
    SplitLockFormat,
    TriggerEvalResult,
    Usage,
    artifact_json_bytes,
    content_sha256,
)
from ses.evolution.diagnosis import (
    RETURN_DIAGNOSIS_POLICY,
    FailureDiagnosisPolicy,
    build_failure_card_set,
)
from ses.evolution.gate import GateEvaluationAdapter, SelectionEvaluationResult
from ses.evolution.governance import CandidateGovernanceCommand, govern_candidate
from ses.evolution.registry import SkillRegistry
from ses.evolution.updater import (
    RETURN_UPDATER_POLICY,
    Updater,
    UpdaterPolicy,
    UpdaterRequest,
)
from ses.evolution.workflow import run_evolution_workflow
from ses.foundation.credentials import credential_values, redact, redact_data
from ses.skills.static_gate import (
    DEFAULT_STATIC_GATE_POLICY,
    StaticGatePolicy,
    run_static_gate,
)
from ses.testset.holdout import HoldoutManifest


class AutoEvolveError(ValueError):
    """The bounded loop cannot safely continue or resume."""


@dataclass(frozen=True, slots=True)
class BudgetAllowance:
    """Maximum additional usage one adapter may consume in this experiment."""

    input_tokens: int
    output_tokens: int
    cost_amount: Decimal
    cost_currency: str


class _BudgetHalt(Exception):
    def __init__(self, reason: AutoStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class _NoFailureEvidence(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RolloutExecution:
    """Adapter result before the orchestrator writes its immutable receipt."""

    evidence: FailureEvidenceFixture
    measurement_kind: MeasurementKind
    network_used: bool
    source_kind: Literal[
        "fixed_reference_fixture",
        "fresh_fixed_execution",
        "fresh_develop_run",
    ]
    usage: Usage
    cost_complete: bool


class RolloutAdapter(Protocol):
    """Produce fresh round-bound failure evidence from the accepted Skill."""

    def run(
        self,
        *,
        experiment_id: str,
        round_number: int,
        parent_skill: Path,
        parent_skill_sha256: str,
        executed_at: datetime,
    ) -> RolloutExecution: ...


@dataclass(frozen=True, slots=True)
class FinalProtocolLock:
    """Exact runtime identities a final adapter must consume and attest."""

    engine_id: str
    simulator_id: str
    judge_id: str
    provider_id: str
    model_lock_sha256: str
    evaluation_protocol_sha256: str
    report_protocol_sha256: str


@dataclass(frozen=True, slots=True)
class FinalExecution:
    """Private per-slot outcomes returned by a trusted final adapter."""

    case_passes: tuple[bool, ...]
    private_payload: Mapping[str, object]
    measurement_kind: MeasurementKind
    network_used: bool
    result_source: Literal[
        "fixed_reference",
        "fresh_fixed_execution",
        "canonical_live",
    ]
    usage: Usage
    cost_complete: bool
    actual_protocol: FinalProtocolLock
    run_set_sha256: str
    safety_violation_count: int = 0
    scenario_metrics: tuple[ShoppingFinalScenarioMetrics, ...] | None = None


class FinalEvaluationAdapter(Protocol):
    """Run the final split without exposing it to the modification loop."""

    def run(
        self,
        *,
        experiment_id: str,
        subject_skill: Path,
        subject_skill_sha256: str,
        final_manifest: Path,
        executed_at: datetime,
        protocol: FinalProtocolLock,
    ) -> FinalExecution: ...


@dataclass(frozen=True, slots=True)
class AutoEvolveCommand:
    """Complete immutable input for one experiment or exact resume."""

    project_root: Path
    output_root: Path
    registry_root: Path
    accepted_skill: Path
    initial_evidence: Path
    failure_fixture: Path
    selection_lock: Path
    final_lock: Path
    config: AutoEvolveConfig
    policy: GatePolicy
    started_at: datetime
    final_lifecycle: FinalLifecycle = FinalLifecycle.INLINE_LEGACY
    profile_sha256: str | None = None
    split_lock_format: SplitLockFormat = SplitLockFormat.HOLDOUT_MANIFEST
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY
    diagnosis_policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY
    updater_policy: UpdaterPolicy = RETURN_UPDATER_POLICY

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("auto-evolve start time must include a timezone")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        if self.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE:
            profile = self.profile_sha256
            if (
                profile is None
                or len(profile) != 64
                or any(character not in "0123456789abcdef" for character in profile)
            ):
                raise ValueError(
                    "independent capstone final requires a profile SHA-256"
                )
            if (
                self.config.final_lifecycle is not FinalLifecycle.INDEPENDENT_CAPSTONE
                or self.config.profile_sha256 != profile
            ):
                raise ValueError(
                    "independent final lifecycle must be bound into loop config"
                )
            if (
                self.split_lock_format is not SplitLockFormat.CONTENT_ADDRESSED
                or self.config.split_lock_format
                is not SplitLockFormat.CONTENT_ADDRESSED
            ):
                raise ValueError(
                    "independent capstone requires content-addressed split locks"
                )
        elif (
            self.config.final_lifecycle is not None
            or self.config.profile_sha256 is not None
            or self.config.split_lock_format is not None
        ):
            raise ValueError("legacy final cannot use capstone config locks")
        elif self.split_lock_format is not SplitLockFormat.HOLDOUT_MANIFEST:
            raise ValueError("legacy final requires the holdout manifest lock format")
        if self.diagnosis_policy.policy_id != self.updater_policy.policy_id:
            raise ValueError("auto-evolve diagnosis and Updater domains must match")


class AutoEvolveOrchestrator:
    """Run and resume one serial, budgeted lineage without bypassing Gate."""

    def __init__(
        self,
        command: AutoEvolveCommand,
        *,
        rollout_adapter: RolloutAdapter,
        updater_factory: Callable[[int], Updater],
        gate_adapter_factory: Callable[[int], GateEvaluationAdapter],
        final_adapter: FinalEvaluationAdapter | None = None,
    ) -> None:
        self.command = command
        self.rollout_adapter = rollout_adapter
        self.updater_factory = updater_factory
        self.gate_adapter_factory = gate_adapter_factory
        self.final_adapter = final_adapter
        self.store = AutoStateStore(command.output_root)
        self.registry = SkillRegistry(
            command.registry_root,
            initial_static_gate=lambda source: run_static_gate(
                source,
                policy=command.static_gate_policy,
            ),
        )
        self._validate_layout()

    def run(self) -> AutoEvolveState:
        """Continue until a configured stop, then run final at most once."""

        with self.store.experiment_lock():
            return self._run_locked()

    def run_final_once(self) -> AutoEvolveState:
        """Run or verify the independent capstone final without resuming rounds."""

        if self.command.final_lifecycle is not FinalLifecycle.INDEPENDENT_CAPSTONE:
            raise AutoEvolveError(
                "independent final requires the independent_capstone lifecycle"
            )
        with self.store.experiment_lock():
            self._initialize_registry()
            registry_state = self.registry.audit()
            state = self.store.initialize(
                self.command.config,
                accepted_skill_sha256=registry_state.current_accepted_sha256,
            )
            if not self._registry_matches_resume(
                state=state,
                registry_sha256=registry_state.current_accepted_sha256,
            ):
                raise AutoEvolveError(
                    "Registry accepted Skill changed; declare a new experiment"
                )
            self._validate_split_locks()
            if state.status in {
                AutoLoopStatus.FINAL_COMPLETE,
                AutoLoopStatus.FAILED_FINAL,
            }:
                self._verify_final_resume(state)
                self._verify_capstone_final_receipt(state)
                return state
            if (
                state.status is not AutoLoopStatus.STOPPED
                or state.stop_reason is AutoStopReason.INTERRUPTED_STEP
            ):
                raise AutoEvolveError(
                    "independent final requires a durably stopped auto-evolve state"
                )
            if self.final_adapter is None:
                raise AutoEvolveError("independent final requires a final adapter")
            try:
                return self._run_final(state)
            except Exception:
                self._persist_interruption()
                raise

    def _run_locked(self) -> AutoEvolveState:
        self._initialize_registry()
        registry_state = self.registry.audit()
        state = self.store.initialize(
            self.command.config,
            accepted_skill_sha256=registry_state.current_accepted_sha256,
        )
        if state.status is AutoLoopStatus.STOPPED and (
            state.stop_reason is AutoStopReason.INTERRUPTED_STEP
        ):
            if not self.store.can_reconcile_interrupted():
                return state
            state = self._resume_interrupted(state)
        if not self._registry_matches_resume(
            state=state,
            registry_sha256=registry_state.current_accepted_sha256,
        ):
            raise AutoEvolveError(
                "Registry accepted Skill changed; declare a new experiment"
            )
        if state.status in {
            AutoLoopStatus.FINAL_COMPLETE,
            AutoLoopStatus.FAILED_FINAL,
        }:
            self._verify_final_resume(state)
            if self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE:
                self._verify_capstone_final_receipt(state)
            return state
        if state.status is AutoLoopStatus.RUNNING:
            try:
                state = self._run_rounds(state)
            except _BudgetHalt as halt:
                state = self._stop(
                    self.store.load(),
                    reason=halt.reason,
                    round_number=self.store.load().completed_rounds,
                )
            except Exception:
                self._persist_interruption()
                raise
        if (
            state.status is AutoLoopStatus.STOPPED
            and state.stop_reason is not AutoStopReason.INTERRUPTED_STEP
            and self.final_adapter is not None
            and self.command.final_lifecycle is FinalLifecycle.INLINE_LEGACY
        ):
            try:
                state = self._run_final(state)
            except _BudgetHalt as halt:
                state = self._stop(
                    self.store.load(),
                    reason=halt.reason,
                    round_number=self.store.load().completed_rounds,
                )
            except Exception:
                self._persist_interruption()
                raise
        return state

    def _run_rounds(self, state: AutoEvolveState) -> AutoEvolveState:
        for round_number in range(
            state.completed_rounds + 1,
            self.command.config.max_rounds + 1,
        ):
            if self.command.config.frozen or self.store.freeze_requested():
                return self._stop(
                    state,
                    reason=AutoStopReason.FROZEN,
                    round_number=state.completed_rounds,
                )
            self._budget_allowance(state)
            try:
                state = self._run_round(state, round_number=round_number)
            except _NoFailureEvidence:
                return self._stop(
                    self.store.load(),
                    reason=AutoStopReason.NO_FAILURE_EVIDENCE,
                    round_number=state.completed_rounds,
                )
            reason = self._stop_reason(state)
            if reason is not None:
                state = self._stop(state, reason=reason, round_number=round_number)
                break
        if state.status is AutoLoopStatus.RUNNING:
            state = self._stop(
                state,
                reason=AutoStopReason.MAX_ROUNDS,
                round_number=self.command.config.max_rounds,
            )
        return state

    def _run_round(
        self,
        state: AutoEvolveState,
        *,
        round_number: int,
    ) -> AutoEvolveState:
        round_root = self.command.output_root / "rounds" / f"round-{round_number:03d}"
        round_root.mkdir(parents=True, exist_ok=True)
        expected_parent = state.current_accepted_skill_sha256
        candidate_path = round_root / "candidate" / "candidate.json"
        if candidate_path.is_file() and not candidate_path.is_symlink():
            candidate = CandidateArtifact.model_validate_json(
                candidate_path.read_bytes()
            )
            if candidate.parent_skill_sha256 != expected_parent:
                raise AutoEvolveError("resumed candidate uses another accepted parent")

        executed_at = self.command.started_at + timedelta(minutes=round_number)
        allowance = self._budget_allowance(state)
        rollout = self._rollout(
            round_root=round_root,
            round_number=round_number,
            parent_sha256=expected_parent,
            executed_at=executed_at,
            allowance=allowance,
        )
        state = self._sync_pending_budget(state, round_number=round_number)
        self._budget_allowance(state)
        reflection = self._reflect(
            round_root=round_root,
            round_number=round_number,
            evidence_path=round_root / "failure-evidence.json",
        )
        summary, candidate = self._patch(
            round_root=round_root,
            round_number=round_number,
            parent_sha256=expected_parent,
            allowance=self._budget_allowance(state),
        )
        state = self._sync_pending_budget(state, round_number=round_number)
        self._budget_allowance(state)
        bundled_reflection = FailureCardSet.model_validate_json(
            (round_root / "candidate/failure-cards.json").read_bytes()
        )
        if bundled_reflection != reflection:
            raise AutoEvolveError(
                "candidate reflection differs from the round evidence"
            )

        registered = self.registry.register_candidate(
            command_id=f"command-auto-r{round_number:03d}-register",
            candidate_bundle=round_root / "candidate",
            occurred_at=executed_at + timedelta(seconds=1),
        )
        if (
            registered.version_id != candidate.candidate_id
            or registered.version_sha256 != candidate.content_sha256
        ):
            raise AutoEvolveError("Registry registration differs from the candidate")
        decision = self._gate(
            round_root=round_root,
            round_number=round_number,
            executed_at=executed_at + timedelta(seconds=2),
            allowance=self._budget_allowance(state),
        )
        state = self._sync_pending_budget(state, round_number=round_number)
        if decision.outcome is GateOutcome.ACCEPTED:
            self.registry.promote(
                command_id=f"command-auto-r{round_number:03d}-promote",
                candidate_id=candidate.candidate_id,
                occurred_at=executed_at + timedelta(seconds=3),
            )
        current = self.registry.audit().current_accepted_sha256
        expected_current = (
            candidate.content_sha256
            if decision.outcome is GateOutcome.ACCEPTED
            else expected_parent
        )
        if current != expected_current:
            raise AutoEvolveError("Registry pointer does not match the gated outcome")

        updater_cost, updater_complete = _usage_cost(
            summary.updater_usage,
            currency=self.command.config.cost_currency,
            fixed=self.command.config.mode == "fixed",
        )
        rollout_cost = rollout.cost_amount
        decision_cost = decision.metrics.total_cost_amount
        categories = tuple(sorted({card.category for card in reflection.cards}))
        targets = tuple(
            sorted(operation.target for operation in candidate.patch.operations)
        )
        record = AutoRoundRecord(
            round_number=round_number,
            parent_skill_sha256=expected_parent,
            candidate_id=candidate.candidate_id,
            candidate_skill_sha256=candidate.content_sha256,
            rollout=_ref(self.command.output_root, round_root / "rollout.json"),
            candidate=_ref(self.command.output_root, candidate_path),
            gate_decision=_ref(
                self.command.output_root,
                self.command.registry_root
                / "gates"
                / f"gate-auto-r{round_number:03d}"
                / "gate-decision.json",
            ),
            gate_outcome=decision.outcome,
            promoted=decision.outcome is GateOutcome.ACCEPTED,
            quality_delta=decision.metrics.quality_delta,
            cost_amount=rollout_cost + updater_cost + decision_cost,
            cost_currency=self.command.config.cost_currency,
            cost_complete=(
                rollout.cost_complete
                and updater_complete
                and decision.metrics.cost_complete
            ),
            input_tokens=(
                rollout.input_tokens
                + summary.updater_usage.input_tokens
                + decision.metrics.total_input_tokens
            ),
            output_tokens=(
                rollout.output_tokens
                + summary.updater_usage.output_tokens
                + decision.metrics.total_output_tokens
            ),
            failure_categories=categories,
            patch_targets=targets,
        )
        rounds = (*state.rounds, record)
        if (
            state.pending_cost_amount != record.cost_amount
            or state.pending_cost_complete != record.cost_complete
            or state.pending_input_tokens != record.input_tokens
            or state.pending_output_tokens != record.output_tokens
        ):
            raise AutoEvolveError("round budget receipts disagree with its record")
        next_state = AutoEvolveState(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="auto_evolve_state",
            experiment_id=state.experiment_id,
            config_sha256=state.config_sha256,
            status=AutoLoopStatus.RUNNING,
            current_accepted_skill_sha256=current,
            completed_rounds=len(rounds),
            rounds=rounds,
            total_cost_amount=sum((row.cost_amount for row in rounds), Decimal(0)),
            cost_currency=state.cost_currency,
            cost_complete=all(row.cost_complete for row in rounds),
            total_input_tokens=sum(row.input_tokens for row in rounds),
            total_output_tokens=sum(row.output_tokens for row in rounds),
            pending_cost_amount=Decimal(0),
            pending_cost_complete=True,
            pending_input_tokens=0,
            pending_output_tokens=0,
            final_cost_amount=state.final_cost_amount,
            final_cost_complete=state.final_cost_complete,
            final_input_tokens=state.final_input_tokens,
            final_output_tokens=state.final_output_tokens,
            consecutive_rejections=(
                0
                if decision.outcome is GateOutcome.ACCEPTED
                else state.consecutive_rejections + 1
            ),
        )
        self.store.write(next_state)
        return next_state

    def _rollout(
        self,
        *,
        round_root: Path,
        round_number: int,
        parent_sha256: str,
        executed_at: datetime,
        allowance: BudgetAllowance,
    ) -> AutoRolloutReceipt:
        evidence_path = round_root / "failure-evidence.json"
        receipt_path = round_root / "rollout.json"
        inputs = {"parent_skill_sha256": parent_sha256}
        should_run = self.store.begin_step(
            round_number=round_number,
            step="rollout",
            expected_outputs=(evidence_path, receipt_path),
            input_hashes=inputs,
        )
        if should_run:
            execution = self._run_rollout_adapter(
                allowance=allowance,
                experiment_id=self.command.config.experiment_id,
                round_number=round_number,
                parent_skill=self.registry.version_path(parent_sha256),
                parent_skill_sha256=parent_sha256,
                executed_at=executed_at,
            )
            expected_measurement = (
                MeasurementKind.SYNTHETIC_OFFLINE
                if self.command.config.mode == "fixed"
                else MeasurementKind.LIVE_MEASURED
            )
            if (
                execution.measurement_kind is not expected_measurement
                or execution.evidence.source.measurement_kind
                is not expected_measurement
                or execution.evidence.source.skill_sha256 != parent_sha256
            ):
                raise AutoEvolveError(
                    "rollout evidence does not match the loop mode and accepted parent"
                )
            _atomic_write(evidence_path, artifact_json_bytes(execution.evidence))
            cost, usage_complete = _usage_cost(
                execution.usage,
                currency=self.command.config.cost_currency,
                fixed=self.command.config.mode == "fixed",
            )
            step_budget = StepBudgetUsage(
                cost_amount=cost,
                cost_currency=self.command.config.cost_currency,
                cost_complete=execution.cost_complete and usage_complete,
                input_tokens=execution.usage.input_tokens,
                output_tokens=execution.usage.output_tokens,
            )
            self._validate_step_usage(step_budget, allowance=allowance)
            receipt = AutoRolloutReceipt(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="auto_rollout_receipt",
                experiment_id=self.command.config.experiment_id,
                round_number=round_number,
                rollout_id=(
                    f"rollout-{self.command.config.experiment_id.removeprefix('experiment-')}"
                    f"-r{round_number:03d}"
                ),
                parent_skill_sha256=parent_sha256,
                measurement_kind=execution.measurement_kind,
                network_used=execution.network_used,
                source_kind=execution.source_kind,
                executed_at=executed_at,
                failure_evidence=_ref(self.command.output_root, evidence_path),
                cost_amount=cost,
                cost_currency=self.command.config.cost_currency,
                cost_complete=step_budget.cost_complete,
                input_tokens=execution.usage.input_tokens,
                output_tokens=execution.usage.output_tokens,
            )
            _atomic_write(receipt_path, artifact_json_bytes(receipt))
        try:
            receipt = AutoRolloutReceipt.model_validate_json(receipt_path.read_bytes())
            receipt.failure_evidence.verify_bytes(evidence_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise AutoEvolveError("rollout receipt or evidence is invalid") from exc
        if (
            receipt.round_number != round_number
            or receipt.parent_skill_sha256 != parent_sha256
            or receipt.experiment_id != self.command.config.experiment_id
        ):
            raise AutoEvolveError("rollout receipt belongs to another round")
        budget = StepBudgetUsage(
            cost_amount=receipt.cost_amount,
            cost_currency=receipt.cost_currency,
            cost_complete=receipt.cost_complete,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
        )
        self._validate_step_usage(budget, allowance=allowance)
        self.store.complete_step(
            round_number=round_number,
            step="rollout",
            expected_outputs=(evidence_path, receipt_path),
            input_hashes=inputs,
            budget=budget,
        )
        return receipt

    def _reflect(
        self,
        *,
        round_root: Path,
        round_number: int,
        evidence_path: Path,
    ) -> FailureCardSet:
        reflection_path = round_root / "reflection.json"
        inputs = {"failure_evidence_sha256": _file_sha256(evidence_path)}
        pending_reflection: FailureCardSet | None = None
        intent_path = self.store.intent_path(
            round_number=round_number,
            step="reflect",
        )
        if not reflection_path.exists() and not intent_path.exists():
            try:
                pending_reflection = build_failure_card_set(
                    evidence_path,
                    policy=self.command.diagnosis_policy,
                )
            except ValueError as exc:
                raise _NoFailureEvidence from exc
        should_run = self.store.begin_step(
            round_number=round_number,
            step="reflect",
            expected_outputs=(reflection_path,),
            input_hashes=inputs,
        )
        if should_run:
            if pending_reflection is None:
                try:
                    pending_reflection = build_failure_card_set(
                        evidence_path,
                        policy=self.command.diagnosis_policy,
                    )
                except ValueError as exc:
                    raise _NoFailureEvidence from exc
            reflection = pending_reflection
            _atomic_write(reflection_path, artifact_json_bytes(reflection))
        try:
            reflection = FailureCardSet.model_validate_json(
                reflection_path.read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise AutoEvolveError("round reflection is invalid") from exc
        self.store.complete_step(
            round_number=round_number,
            step="reflect",
            expected_outputs=(reflection_path,),
            input_hashes=inputs,
        )
        return reflection

    def _patch(
        self,
        *,
        round_root: Path,
        round_number: int,
        parent_sha256: str,
        allowance: BudgetAllowance,
    ) -> tuple[EvolutionPipelineSummary, CandidateArtifact]:
        candidate_root = round_root / "candidate"
        summary_path = candidate_root / "summary.json"
        candidate_path = candidate_root / "candidate.json"
        inputs = {
            "failure_evidence_sha256": _file_sha256(
                round_root / "failure-evidence.json"
            ),
            "parent_skill_sha256": parent_sha256,
            "reflection_sha256": _file_sha256(round_root / "reflection.json"),
        }
        should_run = self.store.begin_step(
            round_number=round_number,
            step="patch",
            expected_outputs=(summary_path, candidate_path),
            input_hashes=inputs,
        )
        if should_run:
            updater = self._budgeted_updater(
                self.updater_factory(round_number), allowance=allowance
            )
            run_evolution_workflow(
                parent_dir=self.registry.version_path(parent_sha256),
                evidence_path=round_root / "failure-evidence.json",
                output_root=candidate_root,
                updater=updater,
                mode=self.command.config.mode,
                workspace_root=round_root / "updater-workspaces",
                diagnosis_policy=self.command.diagnosis_policy,
                updater_policy=self.command.updater_policy,
                static_gate_policy=self.command.static_gate_policy,
            )
        try:
            summary = EvolutionPipelineSummary.model_validate_json(
                summary_path.read_bytes()
            )
            candidate = CandidateArtifact.model_validate_json(
                candidate_path.read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise AutoEvolveError("candidate bundle is invalid") from exc
        if (
            summary.parent_skill_sha256 != parent_sha256
            or summary.candidate_skill_sha256 != candidate.content_sha256
            or candidate.parent_skill_sha256 != parent_sha256
        ):
            raise AutoEvolveError("candidate bundle does not match its round parent")
        updater_cost, updater_complete = _usage_cost(
            summary.updater_usage,
            currency=self.command.config.cost_currency,
            fixed=self.command.config.mode == "fixed",
        )
        budget = StepBudgetUsage(
            cost_amount=updater_cost,
            cost_currency=self.command.config.cost_currency,
            cost_complete=updater_complete,
            input_tokens=summary.updater_usage.input_tokens,
            output_tokens=summary.updater_usage.output_tokens,
        )
        self._validate_step_usage(budget, allowance=allowance)
        self.store.complete_step(
            round_number=round_number,
            step="patch",
            expected_outputs=(summary_path, candidate_path),
            input_hashes=inputs,
            budget=budget,
        )
        return summary, candidate

    def _gate(
        self,
        *,
        round_root: Path,
        round_number: int,
        executed_at: datetime,
        allowance: BudgetAllowance,
    ) -> GateDecision:
        decision_path = (
            self.command.registry_root
            / "gates"
            / f"gate-auto-r{round_number:03d}"
            / "gate-decision.json"
        )
        inputs = {
            "candidate_sha256": _file_sha256(round_root / "candidate/candidate.json"),
            "gate_policy_sha256": self.command.config.gate_policy_sha256,
            "selection_lock_sha256": self.command.config.selection_lock_sha256,
        }
        should_run = self.store.begin_step(
            round_number=round_number,
            step="gate",
            expected_outputs=(decision_path,),
            input_hashes=inputs,
        )
        if should_run:
            adapter = self._budgeted_gate_adapter(
                self.gate_adapter_factory(round_number), allowance=allowance
            )
            decision = govern_candidate(
                CandidateGovernanceCommand(
                    registry_root=self.command.registry_root,
                    candidate_bundle=round_root / "candidate",
                    selection_lock=self.command.selection_lock,
                    project_root=self.command.project_root,
                    gate_id=f"gate-auto-r{round_number:03d}",
                    command_id=f"command-auto-r{round_number:03d}-decision",
                    mode=self.command.config.mode,
                    measured_at=executed_at,
                    policy=self.command.policy,
                    static_gate_policy=self.command.static_gate_policy,
                ),
                adapter=adapter,
            )
        else:
            try:
                decision = GateDecision.model_validate_json(decision_path.read_bytes())
            except (OSError, ValueError) as exc:
                raise AutoEvolveError("resumed GateDecision is invalid") from exc
        budget = StepBudgetUsage(
            cost_amount=decision.metrics.total_cost_amount,
            cost_currency=decision.metrics.cost_currency,
            cost_complete=decision.metrics.cost_complete,
            input_tokens=decision.metrics.total_input_tokens,
            output_tokens=decision.metrics.total_output_tokens,
        )
        self._validate_step_usage(budget, allowance=allowance)
        self.store.complete_step(
            round_number=round_number,
            step="gate",
            expected_outputs=(decision_path,),
            input_hashes=inputs,
            budget=budget,
        )
        return decision

    def _budget_allowance(self, state: AutoEvolveState) -> BudgetAllowance:
        config = self.command.config
        if not state.cost_complete:
            raise _BudgetHalt(AutoStopReason.COST_BUDGET)
        remaining_input = config.max_input_tokens - state.total_input_tokens
        remaining_output = config.max_output_tokens - state.total_output_tokens
        remaining_cost = config.max_cost_amount - state.total_cost_amount
        if remaining_input <= 0 or remaining_output <= 0:
            raise _BudgetHalt(AutoStopReason.TOKEN_BUDGET)
        if remaining_cost <= 0:
            raise _BudgetHalt(AutoStopReason.COST_BUDGET)
        return BudgetAllowance(
            input_tokens=remaining_input,
            output_tokens=remaining_output,
            cost_amount=remaining_cost,
            cost_currency=config.cost_currency,
        )

    @staticmethod
    def _validate_step_usage(
        usage: StepBudgetUsage,
        *,
        allowance: BudgetAllowance,
    ) -> None:
        if usage.cost_currency != allowance.cost_currency:
            raise AutoEvolveError("step usage uses another budget currency")
        if (
            usage.input_tokens > allowance.input_tokens
            or usage.output_tokens > allowance.output_tokens
            or usage.cost_amount > allowance.cost_amount
        ):
            raise AutoEvolveError("adapter exceeded its remaining budget allowance")

    def _sync_pending_budget(
        self,
        state: AutoEvolveState,
        *,
        round_number: int,
    ) -> AutoEvolveState:
        if round_number != state.completed_rounds + 1:
            raise AutoEvolveError("pending budget belongs to another round")
        budgets: list[StepBudgetUsage] = []
        for step in ("rollout", "patch", "gate"):
            receipt_path = self.store.receipt_path(
                round_number=round_number,
                step=step,
            )
            if not receipt_path.exists():
                continue
            receipt = self.store.step_receipt(
                round_number=round_number,
                step=step,
            )
            if receipt.budget is None:
                raise AutoEvolveError("paid step receipt lacks budget usage")
            budgets.append(receipt.budget)
        pending_cost = sum((row.cost_amount for row in budgets), Decimal(0))
        pending_input = sum(row.input_tokens for row in budgets)
        pending_output = sum(row.output_tokens for row in budgets)
        pending_complete = all(row.cost_complete for row in budgets)
        round_cost = sum((row.cost_amount for row in state.rounds), Decimal(0))
        round_input = sum(row.input_tokens for row in state.rounds)
        round_output = sum(row.output_tokens for row in state.rounds)
        payload = state.model_dump(mode="python")
        payload.update(
            {
                "pending_cost_amount": pending_cost,
                "pending_cost_complete": pending_complete,
                "pending_input_tokens": pending_input,
                "pending_output_tokens": pending_output,
                "total_cost_amount": round_cost
                + pending_cost
                + state.final_cost_amount,
                "cost_complete": all(row.cost_complete for row in state.rounds)
                and pending_complete
                and state.final_cost_complete,
                "total_input_tokens": round_input
                + pending_input
                + state.final_input_tokens,
                "total_output_tokens": round_output
                + pending_output
                + state.final_output_tokens,
            }
        )
        updated = AutoEvolveState.model_validate(payload)
        self.store.write(updated)
        return updated

    def _run_rollout_adapter(
        self,
        *,
        allowance: BudgetAllowance,
        **kwargs: Any,
    ) -> RolloutExecution:
        if self.command.config.mode == "fixed":
            return self.rollout_adapter.run(**kwargs)
        method = getattr(self.rollout_adapter, "run_budgeted", None)
        if not callable(method):
            raise AutoEvolveError(
                "live rollout adapter must enforce the remaining budget allowance"
            )
        result = cast(Callable[..., object], method)(budget=allowance, **kwargs)
        if not isinstance(result, RolloutExecution):
            raise AutoEvolveError("live rollout adapter returned an invalid result")
        return result

    def _budgeted_updater(
        self,
        updater: Updater,
        *,
        allowance: BudgetAllowance,
    ) -> Updater:
        if self.command.config.mode == "fixed":
            return updater
        if not callable(getattr(updater, "propose_budgeted", None)):
            raise AutoEvolveError(
                "live Updater must enforce the remaining budget allowance"
            )
        return _BudgetedUpdaterProxy(
            updater,
            allowance=allowance,
            validate=self._validate_step_usage,
        )

    def _budgeted_gate_adapter(
        self,
        adapter: GateEvaluationAdapter,
        *,
        allowance: BudgetAllowance,
    ) -> GateEvaluationAdapter:
        if self.command.config.mode == "fixed":
            return adapter
        if not all(
            callable(getattr(adapter, name, None))
            for name in ("run_trigger_budgeted", "run_selection_budgeted")
        ):
            raise AutoEvolveError(
                "live Gate adapter must enforce the remaining budget allowance"
            )
        return _BudgetedGateProxy(
            adapter,
            allowance=allowance,
            validate=self._validate_step_usage,
        )

    def _run_final_adapter(
        self,
        *,
        allowance: BudgetAllowance,
        **kwargs: Any,
    ) -> FinalExecution:
        assert self.final_adapter is not None
        if self.command.config.mode == "fixed":
            return self.final_adapter.run(**kwargs)
        method = getattr(self.final_adapter, "run_budgeted", None)
        if not callable(method):
            raise AutoEvolveError(
                "live final adapter must enforce the remaining budget allowance"
            )
        result = cast(Callable[..., object], method)(budget=allowance, **kwargs)
        if not isinstance(result, FinalExecution):
            raise AutoEvolveError("live final adapter returned an invalid result")
        return result

    def _final_protocol_lock(self) -> FinalProtocolLock:
        return FinalProtocolLock(
            engine_id=self.command.config.final_engine_id,
            simulator_id=self.command.config.final_simulator_id,
            judge_id=self.command.config.final_judge_id,
            provider_id=self.command.config.final_provider_id,
            model_lock_sha256=self.command.policy.model_lock_sha256,
            evaluation_protocol_sha256=(self.command.policy.evaluation_protocol_sha256),
            report_protocol_sha256=(self.command.config.final_report_protocol_sha256),
        )

    def _validate_split_lock(
        self,
        path: Path,
        *,
        split: Literal["selection", "final"],
        count: int,
    ) -> str:
        """Validate one legacy or capstone lock against the command identity."""

        if self.command.split_lock_format is SplitLockFormat.HOLDOUT_MANIFEST:
            return _validate_locked_manifest(path, split=split, count=count)
        lexical = validate_locked_manifest_path(path)
        try:
            content = lexical.read_bytes()
            lock = OpaqueProtectedSplitLock.model_validate_json(content)
            if artifact_json_bytes(lock) != content:
                raise ValueError("split lock is not canonical")
        except (OSError, UnicodeError, ValueError) as exc:
            raise AutoEvolveError("content-addressed split lock is invalid") from exc
        expected_measurement = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.command.config.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if (
            lock.experiment_id != self.command.config.experiment_id
            or lock.profile_sha256 != self.command.profile_sha256
            or lock.mode != self.command.config.mode
            or lock.measurement_kind is not expected_measurement
            or lock.split != split
            or lock.case_count != count
        ):
            raise AutoEvolveError(
                "content-addressed split lock differs from the experiment"
            )
        return hashlib.sha256(content).hexdigest()

    def _validate_split_locks(self) -> None:
        selection_hash = self._validate_split_lock(
            self.command.selection_lock,
            split="selection",
            count=self.command.policy.selection_case_count,
        )
        final_hash = self._validate_split_lock(
            self.command.final_lock,
            split="final",
            count=12,
        )
        if self.command.selection_lock.resolve() == self.command.final_lock.resolve():
            raise AutoEvolveError("selection and final require distinct lock artifacts")
        if selection_hash == final_hash:
            raise AutoEvolveError("selection and final lock hashes must be distinct")
        if selection_hash != self.command.config.selection_lock_sha256:
            raise AutoEvolveError("selection lock differs from the experiment config")
        if final_hash != self.command.config.final_lock_sha256:
            raise AutoEvolveError("final lock differs from the experiment config")

    def _stop_reason(self, state: AutoEvolveState) -> AutoStopReason | None:
        config = self.command.config
        if not state.cost_complete:
            return AutoStopReason.COST_BUDGET
        if state.total_input_tokens >= config.max_input_tokens:
            return AutoStopReason.TOKEN_BUDGET
        if state.total_output_tokens >= config.max_output_tokens:
            return AutoStopReason.TOKEN_BUDGET
        if state.total_cost_amount >= config.max_cost_amount:
            return AutoStopReason.COST_BUDGET
        if state.consecutive_rejections >= config.max_consecutive_rejections:
            return AutoStopReason.CONSECUTIVE_REJECTIONS
        if config.cooldown_rounds and len(state.rounds) >= 2:
            recent = state.rounds[-(config.cooldown_rounds + 1) :]
            if len(recent) >= 2 and all(
                row.gate_outcome is GateOutcome.REJECTED for row in recent
            ):
                common = set(recent[0].patch_targets)
                for row in recent[1:]:
                    common.intersection_update(row.patch_targets)
                if common:
                    return AutoStopReason.COOLDOWN
        window = state.rounds[-config.convergence_rounds :]
        if len(window) == config.convergence_rounds and all(
            row.quality_delta <= config.min_quality_improvement for row in window
        ):
            return AutoStopReason.CONVERGED
        if state.completed_rounds >= config.max_rounds:
            return AutoStopReason.MAX_ROUNDS
        return None

    def _stop(
        self,
        state: AutoEvolveState,
        *,
        reason: AutoStopReason,
        round_number: int,
    ) -> AutoEvolveState:
        payload = state.model_dump(mode="python")
        payload.update(
            {
                "status": AutoLoopStatus.STOPPED,
                "stopped_at": self.command.started_at
                + timedelta(minutes=round_number, seconds=4),
                "stop_reason": reason,
            }
        )
        stopped = AutoEvolveState.model_validate(payload)
        self.store.write(stopped)
        return stopped

    def _run_final(self, state: AutoEvolveState) -> AutoEvolveState:
        assert self.final_adapter is not None
        self._validate_split_locks()
        allowance = self._budget_allowance(state)
        final_root = self.command.output_root / "final"
        aggregate_path = final_root / "final-aggregate.json"
        private_path = final_root / "private-results.json"
        run_receipt_path = final_root / "final-run-receipt.json"
        capstone_receipt_path = final_root / "capstone-final-receipt.json"
        consumed_checkpoint_path = (
            self.command.output_root / "final-consumed.checkpoint.json"
        )
        core_outputs = (
            aggregate_path,
            private_path,
            run_receipt_path,
            consumed_checkpoint_path,
        )
        final_outputs = (
            (*core_outputs, capstone_receipt_path)
            if self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
            else core_outputs
        )
        final_round = self.command.config.max_rounds + 1
        inputs = {
            "final_lock_sha256": self.command.config.final_lock_sha256,
            "subject_skill_sha256": state.current_accepted_skill_sha256,
        }
        should_run = self.store.begin_step(
            round_number=final_round,
            step="final",
            expected_outputs=final_outputs,
            input_hashes=inputs,
        )
        if should_run:
            executed_at = (state.stopped_at or self.command.started_at) + timedelta(
                seconds=1
            )
            protocol = self._final_protocol_lock()
            execution = self._run_final_adapter(
                allowance=allowance,
                experiment_id=state.experiment_id,
                subject_skill=self.registry.version_path(
                    state.current_accepted_skill_sha256
                ),
                subject_skill_sha256=state.current_accepted_skill_sha256,
                final_manifest=self.command.final_lock,
                executed_at=executed_at,
                protocol=protocol,
            )
            if len(execution.case_passes) != 12:
                raise AutoEvolveError("final adapter returned the wrong case count")
            expected_measurement = (
                MeasurementKind.SYNTHETIC_OFFLINE
                if self.command.config.mode == "fixed"
                else MeasurementKind.LIVE_MEASURED
            )
            expected_source = "canonical_live"
            if self.command.config.mode == "fixed":
                expected_source = (
                    "fresh_fixed_execution"
                    if self.command.final_lifecycle
                    is FinalLifecycle.INDEPENDENT_CAPSTONE
                    else "fixed_reference"
                )
            if (
                execution.measurement_kind is not expected_measurement
                or execution.result_source != expected_source
                or execution.network_used != (self.command.config.mode == "live")
                or execution.actual_protocol != protocol
                or type(execution.safety_violation_count) is not int
                or execution.safety_violation_count < 0
            ):
                raise AutoEvolveError("final evidence does not match the locked mode")
            try:
                expected_run_set_sha256 = final_execution_run_set_sha256(
                    case_passes=execution.case_passes,
                    private_payload=execution.private_payload,
                )
            except (TypeError, ValueError) as exc:
                raise AutoEvolveError(
                    "final private result is not canonical JSON"
                ) from exc
            if execution.run_set_sha256 != expected_run_set_sha256:
                raise AutoEvolveError("final adapter run-set receipt is invalid")
            secrets = credential_values(os.environ)
            if (
                redact_data(execution.private_payload, secrets)
                != execution.private_payload
            ):
                raise AutoEvolveError("final private result contains credentials")
            cost, usage_complete = _usage_cost(
                execution.usage,
                currency=self.command.config.cost_currency,
                fixed=self.command.config.mode == "fixed",
            )
            step_budget = StepBudgetUsage(
                cost_amount=cost,
                cost_currency=self.command.config.cost_currency,
                cost_complete=execution.cost_complete and usage_complete,
                input_tokens=execution.usage.input_tokens,
                output_tokens=execution.usage.output_tokens,
            )
            self._validate_step_usage(step_budget, allowance=allowance)
            if self.command.config.mode == "live" and not step_budget.cost_complete:
                raise AutoEvolveError("live final requires complete monetary cost")
            private_bytes = json.dumps(
                {
                    "case_passes": execution.case_passes,
                    "details": execution.private_payload,
                    "executed_at": executed_at.isoformat().replace("+00:00", "Z"),
                    "experiment_id": state.experiment_id,
                    "safety_violation_count": execution.safety_violation_count,
                    "subject_skill_sha256": state.current_accepted_skill_sha256,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            text = private_bytes.decode("utf-8")
            if redact(text, secrets) != text:
                raise AutoEvolveError("final private result contains credentials")
            _atomic_write(private_path, private_bytes, mode=0o600)
            pass_count = sum(execution.case_passes)
            report_payload: dict[str, object] = {
                "schema_version": (
                    SchemaVersion.V1ALPHA2
                    if self.command.final_lifecycle
                    is FinalLifecycle.INDEPENDENT_CAPSTONE
                    else SchemaVersion.V1ALPHA1
                ),
                "record_type": "final_aggregate_report",
                "experiment_id": state.experiment_id,
                "subject_skill_sha256": state.current_accepted_skill_sha256,
                "final_lock_sha256": self.command.config.final_lock_sha256,
                "mode": self.command.config.mode,
                "measurement_kind": execution.measurement_kind,
                "network_used": execution.network_used,
                "result_source": execution.result_source,
                "executed_at": executed_at,
                "case_count": 12,
                "pass_count": pass_count,
                "pass_rate": pass_count / 12,
                "cost_amount": cost,
                "cost_currency": self.command.config.cost_currency,
                "cost_complete": step_budget.cost_complete,
                "input_tokens": execution.usage.input_tokens,
                "output_tokens": execution.usage.output_tokens,
                "private_results_sha256": hashlib.sha256(private_bytes).hexdigest(),
            }
            if self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE:
                if execution.scenario_metrics is None:
                    raise AutoEvolveError(
                        "capstone final adapter omitted shopping scenario aggregates"
                    )
                mean_strict_reward = (
                    sum(
                        (
                            row.mean_strict_reward * row.case_count
                            for row in execution.scenario_metrics
                        ),
                        Decimal(0),
                    )
                    / 12
                )
                report_payload.update(
                    {
                        "full_success_count": pass_count,
                        "mean_strict_reward": mean_strict_reward,
                        "safety_violation_count": execution.safety_violation_count,
                        "scenario_metrics": execution.scenario_metrics,
                    }
                )
            try:
                report = FinalAggregateReport.model_validate(report_payload)
            except ValueError as exc:
                raise AutoEvolveError(
                    "final aggregate metrics violate the locked report contract"
                ) from exc
            _atomic_write(aggregate_path, artifact_json_bytes(report))
            private_sha256 = hashlib.sha256(private_bytes).hexdigest()
            aggregate_sha256 = _file_sha256(aggregate_path)
            run_receipt = FinalRunReceipt(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="final_run_receipt",
                experiment_id=state.experiment_id,
                subject_skill_sha256=state.current_accepted_skill_sha256,
                final_lock_sha256=self.command.config.final_lock_sha256,
                mode=self.command.config.mode,
                measurement_kind=execution.measurement_kind,
                network_used=execution.network_used,
                engine_id=execution.actual_protocol.engine_id,
                simulator_id=execution.actual_protocol.simulator_id,
                judge_id=execution.actual_protocol.judge_id,
                provider_id=execution.actual_protocol.provider_id,
                model_lock_sha256=execution.actual_protocol.model_lock_sha256,
                evaluation_protocol_sha256=(
                    execution.actual_protocol.evaluation_protocol_sha256
                ),
                report_protocol_sha256=(
                    execution.actual_protocol.report_protocol_sha256
                ),
                executed_at=executed_at,
                run_set_sha256=execution.run_set_sha256,
                private_results_sha256=private_sha256,
                aggregate_report_sha256=aggregate_sha256,
                cost_amount=step_budget.cost_amount,
                cost_currency=step_budget.cost_currency,
                cost_complete=step_budget.cost_complete,
                input_tokens=step_budget.input_tokens,
                output_tokens=step_budget.output_tokens,
            )
            _atomic_write(run_receipt_path, artifact_json_bytes(run_receipt))
            checkpoint = FinalConsumedCheckpoint(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="final_consumed_checkpoint",
                experiment_id=state.experiment_id,
                subject_skill_sha256=state.current_accepted_skill_sha256,
                final_lock_sha256=self.command.config.final_lock_sha256,
                consumed=True,
                final_run_receipt_sha256=_file_sha256(run_receipt_path),
                aggregate_report_sha256=aggregate_sha256,
                private_results_sha256=private_sha256,
            )
            _atomic_write(
                consumed_checkpoint_path,
                artifact_json_bytes(checkpoint),
                mode=0o600,
            )
            if self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE:
                assert self.command.profile_sha256 is not None
                registry_state = self.registry.audit()
                capstone = CapstoneFinalReceipt(
                    schema_version=SchemaVersion.V1ALPHA1,
                    record_type="capstone_final_receipt",
                    experiment_id=state.experiment_id,
                    lineage_id=registry_state.lineage_id,
                    profile_sha256=self.command.profile_sha256,
                    subject_skill_sha256=state.current_accepted_skill_sha256,
                    measurement_kind=execution.measurement_kind,
                    completed=True,
                    safety_violation_count=(
                        report.safety_violation_count
                        if report.safety_violation_count is not None
                        else 0
                    ),
                    result_origin=(
                        "fresh_fixed_execution"
                        if execution.result_source == "fresh_fixed_execution"
                        else "live_measured"
                    ),
                    aggregate=_ref(self.command.output_root, aggregate_path),
                    final_run_receipt=_ref(self.command.output_root, run_receipt_path),
                    one_time_checkpoint=_ref(
                        self.command.output_root, consumed_checkpoint_path
                    ),
                )
                _atomic_write(
                    capstone_receipt_path,
                    artifact_json_bytes(capstone),
                )
        report, run_receipt = self._verify_final_bundle(state)
        if (
            report.experiment_id != state.experiment_id
            or report.subject_skill_sha256 != state.current_accepted_skill_sha256
            or report.final_lock_sha256 != self.command.config.final_lock_sha256
        ):
            raise AutoEvolveError("final result belongs to another experiment")
        budget = StepBudgetUsage(
            cost_amount=run_receipt.cost_amount,
            cost_currency=run_receipt.cost_currency,
            cost_complete=run_receipt.cost_complete,
            input_tokens=run_receipt.input_tokens,
            output_tokens=run_receipt.output_tokens,
        )
        self._validate_step_usage(budget, allowance=allowance)
        if self.command.config.mode == "live" and not budget.cost_complete:
            raise AutoEvolveError("live final requires complete monetary cost")
        self.store.complete_step(
            round_number=final_round,
            step="final",
            expected_outputs=final_outputs,
            input_hashes=inputs,
            budget=budget,
        )
        final_status = AutoLoopStatus.FINAL_COMPLETE
        if self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE:
            capstone = self._verify_capstone_final_receipt(state)
            if capstone.safety_violation_count > 0:
                final_status = AutoLoopStatus.FAILED_FINAL
        payload = state.model_dump(mode="python")
        payload.update(
            {
                "status": final_status,
                "final_report": _ref(self.command.output_root, aggregate_path),
                "final_cost_amount": budget.cost_amount,
                "final_cost_complete": budget.cost_complete,
                "final_input_tokens": budget.input_tokens,
                "final_output_tokens": budget.output_tokens,
                "total_cost_amount": state.total_cost_amount + budget.cost_amount,
                "cost_complete": state.cost_complete and budget.cost_complete,
                "total_input_tokens": state.total_input_tokens + budget.input_tokens,
                "total_output_tokens": state.total_output_tokens + budget.output_tokens,
            }
        )
        completed = AutoEvolveState.model_validate(payload)
        self.store.write(completed)
        return completed

    def _verify_final_resume(self, state: AutoEvolveState) -> None:
        self._verify_final_bundle(state)

    def _verify_capstone_final_receipt(
        self,
        state: AutoEvolveState,
    ) -> CapstoneFinalReceipt:
        if self.command.final_lifecycle is not FinalLifecycle.INDEPENDENT_CAPSTONE:
            raise AutoEvolveError("legacy final has no capstone receipt")
        assert self.command.profile_sha256 is not None
        path = self.command.output_root / "final/capstone-final-receipt.json"
        aggregate_path = self.command.output_root / "final/final-aggregate.json"
        run_receipt_path = self.command.output_root / "final/final-run-receipt.json"
        checkpoint_path = self.command.output_root / "final-consumed.checkpoint.json"
        try:
            payload = path.read_bytes()
            receipt = CapstoneFinalReceipt.model_validate_json(payload)
            aggregate = FinalAggregateReport.model_validate_json(
                aggregate_path.read_bytes()
            )
            if artifact_json_bytes(receipt) != payload:
                raise ValueError("capstone receipt is not canonical")
            receipt.aggregate.verify_bytes(aggregate_path.read_bytes())
            receipt.final_run_receipt.verify_bytes(run_receipt_path.read_bytes())
            receipt.one_time_checkpoint.verify_bytes(checkpoint_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise AutoEvolveError(
                "persisted capstone final receipt is invalid"
            ) from exc
        registry_state = self.registry.audit()
        expected_origin = (
            "fresh_fixed_execution"
            if self.command.config.mode == "fixed"
            else "live_measured"
        )
        if (
            receipt.experiment_id != state.experiment_id
            or receipt.lineage_id != registry_state.lineage_id
            or receipt.profile_sha256 != self.command.profile_sha256
            or receipt.subject_skill_sha256 != state.current_accepted_skill_sha256
            or receipt.result_origin != expected_origin
            or receipt.aggregate.path != "final/final-aggregate.json"
            or receipt.final_run_receipt.path != "final/final-run-receipt.json"
            or receipt.one_time_checkpoint.path != "final-consumed.checkpoint.json"
            or aggregate.schema_version is not SchemaVersion.V1ALPHA2
            or aggregate.safety_violation_count != receipt.safety_violation_count
        ):
            raise AutoEvolveError(
                "persisted capstone final receipt violates its experiment lock"
            )
        if state.status is AutoLoopStatus.FINAL_COMPLETE and (
            receipt.safety_violation_count != 0
        ):
            raise AutoEvolveError("successful final has safety violations")
        if state.status is AutoLoopStatus.FAILED_FINAL and (
            receipt.safety_violation_count == 0
        ):
            raise AutoEvolveError("failed final lacks a safety violation")
        return receipt

    def _verify_final_bundle(
        self,
        state: AutoEvolveState,
    ) -> tuple[FinalAggregateReport, FinalRunReceipt]:
        aggregate_path = self.command.output_root / "final/final-aggregate.json"
        private_path = self.command.output_root / "final/private-results.json"
        receipt_path = self.command.output_root / "final/final-run-receipt.json"
        checkpoint_path = self.command.output_root / "final-consumed.checkpoint.json"
        if state.final_report is None:
            if state.status in {
                AutoLoopStatus.FINAL_COMPLETE,
                AutoLoopStatus.FAILED_FINAL,
            }:
                raise AutoEvolveError("terminal final state lacks a report")
        elif self.command.output_root / state.final_report.path != aggregate_path:
            raise AutoEvolveError("final state points to another aggregate report")
        try:
            if state.final_report is not None:
                state.final_report.verify_bytes(aggregate_path.read_bytes())
            report = FinalAggregateReport.model_validate_json(
                aggregate_path.read_bytes()
            )
            if report.private_results_sha256 != _file_sha256(private_path):
                raise ValueError("final private result hash mismatch")
            private_payload = json.loads(private_path.read_text(encoding="utf-8"))
            run_receipt = FinalRunReceipt.model_validate_json(receipt_path.read_bytes())
            checkpoint = FinalConsumedCheckpoint.model_validate_json(
                checkpoint_path.read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise AutoEvolveError("persisted final bundle is invalid") from exc
        private_sha256 = _file_sha256(private_path)
        aggregate_sha256 = _file_sha256(aggregate_path)
        receipt_sha256 = _file_sha256(receipt_path)
        stored_cases = (
            private_payload.get("case_passes")
            if isinstance(private_payload, dict)
            else None
        )
        stored_details = (
            private_payload.get("details")
            if isinstance(private_payload, dict)
            else None
        )
        expected_run_set = (
            final_execution_run_set_sha256(
                case_passes=tuple(stored_cases),
                private_payload=stored_details,
            )
            if isinstance(stored_cases, list)
            and all(type(value) is bool for value in stored_cases)
            and isinstance(stored_details, dict)
            else None
        )
        expected_source = "canonical_live"
        if self.command.config.mode == "fixed":
            expected_source = (
                "fresh_fixed_execution"
                if self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
                else "fixed_reference"
            )
        if (
            report.subject_skill_sha256 != state.current_accepted_skill_sha256
            or report.experiment_id != state.experiment_id
            or report.final_lock_sha256 != self.command.config.final_lock_sha256
            or report.result_source != expected_source
            or (
                self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
                and (
                    report.schema_version is not SchemaVersion.V1ALPHA2
                    or report.safety_violation_count is None
                    or not report.scenario_metrics
                )
            )
            or (
                self.command.final_lifecycle is FinalLifecycle.INLINE_LEGACY
                and report.schema_version is not SchemaVersion.V1ALPHA1
            )
            or not isinstance(private_payload, dict)
            or private_payload.get("experiment_id") != state.experiment_id
            or private_payload.get("subject_skill_sha256")
            != state.current_accepted_skill_sha256
            or not isinstance(private_payload.get("case_passes"), list)
            or len(private_payload["case_passes"]) != 12
            or any(type(value) is not bool for value in private_payload["case_passes"])
            or sum(private_payload["case_passes"]) != report.pass_count
            or (
                self.command.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
                and (
                    type(private_payload.get("safety_violation_count")) is not int
                    or private_payload["safety_violation_count"]
                    != report.safety_violation_count
                )
            )
            or run_receipt.experiment_id != state.experiment_id
            or run_receipt.subject_skill_sha256 != state.current_accepted_skill_sha256
            or run_receipt.final_lock_sha256 != self.command.config.final_lock_sha256
            or run_receipt.mode != self.command.config.mode
            or run_receipt.measurement_kind is not report.measurement_kind
            or run_receipt.network_used != report.network_used
            or run_receipt.engine_id != self.command.config.final_engine_id
            or run_receipt.simulator_id != self.command.config.final_simulator_id
            or run_receipt.judge_id != self.command.config.final_judge_id
            or run_receipt.provider_id != self.command.config.final_provider_id
            or run_receipt.model_lock_sha256 != self.command.policy.model_lock_sha256
            or run_receipt.evaluation_protocol_sha256
            != self.command.policy.evaluation_protocol_sha256
            or run_receipt.report_protocol_sha256
            != self.command.config.final_report_protocol_sha256
            or run_receipt.executed_at != report.executed_at
            or run_receipt.run_set_sha256 != expected_run_set
            or run_receipt.private_results_sha256 != private_sha256
            or run_receipt.aggregate_report_sha256 != aggregate_sha256
            or run_receipt.cost_amount != report.cost_amount
            or run_receipt.cost_currency != report.cost_currency
            or run_receipt.cost_complete != report.cost_complete
            or run_receipt.input_tokens != report.input_tokens
            or run_receipt.output_tokens != report.output_tokens
            or checkpoint.experiment_id != state.experiment_id
            or checkpoint.subject_skill_sha256 != state.current_accepted_skill_sha256
            or checkpoint.final_lock_sha256 != self.command.config.final_lock_sha256
            or checkpoint.final_run_receipt_sha256 != receipt_sha256
            or checkpoint.aggregate_report_sha256 != aggregate_sha256
            or checkpoint.private_results_sha256 != private_sha256
        ):
            raise AutoEvolveError("persisted final bundle violates its protocol lock")
        return report, run_receipt

    def _resume_interrupted(self, state: AutoEvolveState) -> AutoEvolveState:
        payload = state.model_dump(mode="python")
        payload.update(
            {
                "status": AutoLoopStatus.RUNNING,
                "stopped_at": None,
                "stop_reason": None,
            }
        )
        resumed = AutoEvolveState.model_validate(payload)
        self.store.write(resumed)
        return resumed

    def _persist_interruption(self) -> None:
        try:
            state = self.store.load()
        except AutoStateError:
            return
        if state.status in {
            AutoLoopStatus.FINAL_COMPLETE,
            AutoLoopStatus.FAILED_FINAL,
        }:
            return
        self._stop(
            state,
            reason=AutoStopReason.INTERRUPTED_STEP,
            round_number=state.completed_rounds,
        )

    def _registry_matches_resume(
        self,
        *,
        state: AutoEvolveState,
        registry_sha256: str,
    ) -> bool:
        if state.current_accepted_skill_sha256 == registry_sha256:
            return True
        if state.status is not AutoLoopStatus.RUNNING:
            return False
        candidate_path = (
            self.command.output_root
            / "rounds"
            / f"round-{state.completed_rounds + 1:03d}"
            / "candidate"
            / "candidate.json"
        )
        try:
            candidate = CandidateArtifact.model_validate_json(
                candidate_path.read_bytes()
            )
        except (OSError, ValueError):
            return False
        return (
            candidate.parent_skill_sha256 == state.current_accepted_skill_sha256
            and candidate.content_sha256 == registry_sha256
        )

    def _initialize_registry(self) -> None:
        if self.registry.events_path.exists():
            self.registry.audit()
            return
        self.registry.initialize(
            command_id="command-auto-initialize",
            accepted_skill=self.command.accepted_skill,
            evidence_paths=(self.command.initial_evidence,),
            occurred_at=self.command.started_at,
        )

    def _validate_layout(self) -> None:
        try:
            self.command.registry_root.resolve().relative_to(
                self.command.output_root.resolve()
            )
        except ValueError as exc:
            raise AutoEvolveError(
                "auto-evolve Registry must be inside the experiment root"
            ) from exc
        if (
            content_sha256(self.command.policy)
            != self.command.config.gate_policy_sha256
        ):
            raise AutoEvolveError("Gate policy differs from the loop config")
        if (
            self.command.policy.selection_lock_sha256
            != self.command.config.selection_lock_sha256
        ):
            raise AutoEvolveError("selection lock differs from the loop config")
        self._validate_split_locks()


class _BudgetedUpdaterProxy:
    def __init__(
        self,
        delegate: Updater,
        *,
        allowance: BudgetAllowance,
        validate: Callable[..., None],
    ) -> None:
        self._delegate = delegate
        self._allowance = allowance
        self._validate = validate
        self.measurement_kind = delegate.measurement_kind
        self.usage = delegate.usage
        self.latency_ms = delegate.latency_ms

    def propose(self, request: UpdaterRequest) -> Patch:
        method = cast(
            Callable[..., object],
            cast(Any, self._delegate).propose_budgeted,
        )
        result = method(request, budget=self._allowance)
        if not isinstance(result, Patch):
            raise AutoEvolveError("live Updater returned an invalid Patch")
        self.usage = self._delegate.usage
        self.latency_ms = self._delegate.latency_ms
        cost, complete = _usage_cost(
            self.usage,
            currency=self._allowance.cost_currency,
            fixed=False,
        )
        usage = StepBudgetUsage(
            cost_amount=cost,
            cost_currency=self._allowance.cost_currency,
            cost_complete=complete,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
        )
        if not complete:
            raise AutoEvolveError("live Updater returned incomplete cost evidence")
        self._validate(usage, allowance=self._allowance)
        return result


class _BudgetedGateProxy:
    def __init__(
        self,
        delegate: GateEvaluationAdapter,
        *,
        allowance: BudgetAllowance,
        validate: Callable[..., None],
    ) -> None:
        self._delegate = delegate
        self._allowance = allowance
        self._validate = validate
        self.measurement_kind = delegate.measurement_kind
        self.network_used = delegate.network_used

    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        self._require_remaining()
        method = cast(
            Callable[..., object],
            cast(Any, self._delegate).run_trigger_budgeted,
        )
        result = method(
            candidate=candidate,
            skill_sha256=skill_sha256,
            measured_at=measured_at,
            budget=self._allowance,
        )
        if not isinstance(result, TriggerEvalResult):
            raise AutoEvolveError("live Gate Trigger returned an invalid result")
        cost, complete = _usage_cost(
            result.usage,
            currency=self._allowance.cost_currency,
            fixed=False,
        )
        usage = StepBudgetUsage(
            cost_amount=cost,
            cost_currency=self._allowance.cost_currency,
            cost_complete=complete,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )
        if not complete:
            raise AutoEvolveError("live Gate Trigger returned incomplete cost evidence")
        self._validate(usage, allowance=self._allowance)
        self._allowance = _consume_allowance(self._allowance, usage)
        return result

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
        self._require_remaining()
        method = cast(
            Callable[..., object],
            cast(Any, self._delegate).run_selection_budgeted,
        )
        result = method(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
            budget=self._allowance,
        )
        if not isinstance(result, SelectionEvaluationResult):
            raise AutoEvolveError("live Gate selection returned an invalid result")
        pair = result.pair
        usage = StepBudgetUsage(
            cost_amount=sum(
                (
                    row.accepted_cost_amount + row.candidate_cost_amount
                    for row in pair.cases
                ),
                Decimal(0),
            ),
            cost_currency=pair.cost_currency,
            cost_complete=True,
            input_tokens=sum(
                row.accepted_input_tokens + row.candidate_input_tokens
                for row in pair.cases
            ),
            output_tokens=sum(
                row.accepted_output_tokens + row.candidate_output_tokens
                for row in pair.cases
            ),
        )
        self._validate(usage, allowance=self._allowance)
        self._allowance = _consume_allowance(self._allowance, usage)
        return result

    def _require_remaining(self) -> None:
        if (
            self._allowance.input_tokens <= 0
            or self._allowance.output_tokens <= 0
            or self._allowance.cost_amount <= 0
        ):
            raise AutoEvolveError("Gate exhausted the remaining experiment budget")


def _consume_allowance(
    allowance: BudgetAllowance,
    usage: StepBudgetUsage,
) -> BudgetAllowance:
    return BudgetAllowance(
        input_tokens=allowance.input_tokens - usage.input_tokens,
        output_tokens=allowance.output_tokens - usage.output_tokens,
        cost_amount=allowance.cost_amount - usage.cost_amount,
        cost_currency=allowance.cost_currency,
    )


def final_execution_run_set_sha256(
    *,
    case_passes: tuple[bool, ...],
    private_payload: Mapping[str, object],
) -> str:
    payload = json.dumps(
        {
            "case_passes": case_passes,
            "private_payload": private_payload,
        },
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _usage_cost(
    usage: Usage,
    *,
    currency: str,
    fixed: bool,
) -> tuple[Decimal, bool]:
    if usage.cost_amount is None:
        return (Decimal(0), True) if fixed else (Decimal(0), False)
    if usage.cost_currency != currency:
        return Decimal(0), False
    return usage.cost_amount, True


def validate_locked_manifest_path(path: Path) -> Path:
    """Reject a missing lock or any lexical symlink before reading its bytes."""

    if ".." in path.parts:
        raise AutoEvolveError("locked split manifest path must be canonical")
    lexical = path if path.is_absolute() else Path.cwd() / path
    if any(component.is_symlink() for component in (lexical, *lexical.parents)):
        raise AutoEvolveError("locked split manifest path cannot contain symlinks")
    if not lexical.is_file():
        raise AutoEvolveError("locked split manifest must be a regular file")
    return lexical


def _validate_locked_manifest(path: Path, *, split: str, count: int) -> str:
    lexical = validate_locked_manifest_path(path)
    try:
        content = lexical.read_bytes()
        lock = HoldoutManifest.model_validate_json(content)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AutoEvolveError("locked split manifest is invalid") from exc
    if lock.split != split or lock.case_count != count:
        raise AutoEvolveError("locked split manifest has the wrong inventory")
    return hashlib.sha256(content).hexdigest()


def _ref(root: Path, path: Path) -> ArtifactRef:
    try:
        relative = path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise AutoEvolveError("auto-evolve artifact escapes its experiment") from exc
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=relative.as_posix(),
        sha256=_file_sha256(path),
    )


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AutoEvolveError("auto-evolve input must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise AutoEvolveError("immutable auto-evolve artifact already exists")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, mode)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


__all__ = [
    "AutoEvolveCommand",
    "AutoEvolveError",
    "AutoEvolveOrchestrator",
    "FinalEvaluationAdapter",
    "FinalExecution",
    "FinalProtocolLock",
    "RolloutAdapter",
    "RolloutExecution",
    "final_execution_run_set_sha256",
    "validate_locked_manifest_path",
]
