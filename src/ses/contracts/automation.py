"""Canonical records for bounded auto-evolution and release portfolio exports."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ses.contracts.artifact import ArtifactRef, RelativeArtifactPath, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.evolution import FailureCategory, GateOutcome
from ses.contracts.primitives import (
    CurrencyCode,
    NonEmptyStr,
    StrictNonNegativeInt,
    UtcDateTime,
)
from ses.contracts.skill import MeasurementKind

FINAL_REPORT_PROTOCOL_SHA256 = hashlib.sha256(
    b"ses-final-aggregate-report:v1alpha1"
).hexdigest()
_FIXED_FINAL_ENGINE_ID = "fixed-offline-engine"
_FIXED_FINAL_SIMULATOR_ID = "fixed-offline-simulator"
_FIXED_FINAL_JUDGE_ID = "fixed-offline-judge"
_FIXED_FINAL_PROVIDER_ID = "none-offline"


class AutoLoopStatus(StrEnum):
    """Persisted lifecycle for one bounded experiment."""

    RUNNING = "running"
    STOPPED = "stopped"
    FINAL_COMPLETE = "final_complete"


class AutoStopReason(StrEnum):
    """Why the loop stopped before the one-time final step."""

    MAX_ROUNDS = "max_rounds"
    TOKEN_BUDGET = "token_budget"
    COST_BUDGET = "cost_budget"
    CONSECUTIVE_REJECTIONS = "consecutive_rejections"
    COOLDOWN = "patch_target_cooldown"
    FROZEN = "frozen"
    CONVERGED = "converged"
    NO_FAILURE_EVIDENCE = "no_failure_evidence"
    INTERRUPTED_STEP = "interrupted_step"


class AutoEvolveConfig(VersionedRecord):
    """Immutable guardrails and protocol locks for one experiment."""

    record_type: Literal["auto_evolve_config"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    mode: Literal["fixed", "live"]
    max_rounds: Annotated[int, Field(strict=True, ge=1)]
    max_input_tokens: StrictNonNegativeInt
    max_output_tokens: StrictNonNegativeInt
    max_cost_amount: Decimal
    cost_currency: CurrencyCode
    max_consecutive_rejections: Annotated[int, Field(strict=True, ge=1)]
    cooldown_rounds: StrictNonNegativeInt
    convergence_rounds: Annotated[int, Field(strict=True, ge=1)]
    min_quality_improvement: float = Field(ge=0, le=1)
    frozen: bool = False
    gate_policy_sha256: Sha256Digest
    selection_lock_sha256: Sha256Digest
    final_lock_sha256: Sha256Digest
    final_engine_id: NonEmptyStr = _FIXED_FINAL_ENGINE_ID
    final_simulator_id: NonEmptyStr = _FIXED_FINAL_SIMULATOR_ID
    final_judge_id: NonEmptyStr = _FIXED_FINAL_JUDGE_ID
    final_provider_id: NonEmptyStr = _FIXED_FINAL_PROVIDER_ID
    final_report_protocol_sha256: Sha256Digest = FINAL_REPORT_PROTOCOL_SHA256

    @field_validator("max_cost_amount", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("auto-evolve cost limit must use a decimal string")
        return value

    @model_validator(mode="after")
    def _valid_budget(self) -> AutoEvolveConfig:
        if not self.max_cost_amount.is_finite() or self.max_cost_amount < 0:
            raise ValueError("auto-evolve cost limit must be finite and nonnegative")
        fixed_protocol = (
            self.final_engine_id == _FIXED_FINAL_ENGINE_ID
            and self.final_simulator_id == _FIXED_FINAL_SIMULATOR_ID
            and self.final_judge_id == _FIXED_FINAL_JUDGE_ID
            and self.final_provider_id == _FIXED_FINAL_PROVIDER_ID
        )
        if self.mode == "fixed" and not fixed_protocol:
            raise ValueError("fixed final must use the locked offline protocol")
        if self.mode == "live" and (
            fixed_protocol or self.final_provider_id == _FIXED_FINAL_PROVIDER_ID
        ):
            raise ValueError("live final requires explicit provider and protocol IDs")
        return self


class AutoRolloutReceipt(VersionedRecord):
    """Fresh invocation receipt whose evidence is consumed by reflection."""

    record_type: Literal["auto_rollout_receipt"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    round_number: Annotated[int, Field(strict=True, ge=1)]
    rollout_id: str = Field(pattern=r"^rollout-[a-z0-9-]+$")
    parent_skill_sha256: Sha256Digest
    measurement_kind: MeasurementKind
    network_used: bool
    source_kind: Literal["fixed_reference_fixture", "fresh_develop_run"]
    executed_at: UtcDateTime
    failure_evidence: ArtifactRef
    cost_amount: Decimal
    cost_currency: CurrencyCode
    cost_complete: bool
    input_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt

    @field_validator("cost_amount", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("rollout cost must use a decimal string")
        return value

    @model_validator(mode="after")
    def _valid_rollout(self) -> AutoRolloutReceipt:
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("rollout cost must be finite and nonnegative")
        if self.source_kind == "fixed_reference_fixture" and (
            self.measurement_kind is not MeasurementKind.SYNTHETIC_OFFLINE
            or self.network_used
        ):
            raise ValueError("fixed rollout receipt must remain offline")
        if self.source_kind == "fresh_develop_run" and (
            self.measurement_kind is not MeasurementKind.LIVE_MEASURED
            or not self.network_used
        ):
            raise ValueError("fresh develop rollout requires live evidence")
        return self


class AutoRoundRecord(ContractModel):
    """One fully gated round; partial work never becomes a round record."""

    round_number: Annotated[int, Field(strict=True, ge=1)]
    parent_skill_sha256: Sha256Digest
    candidate_id: NonEmptyStr
    candidate_skill_sha256: Sha256Digest
    rollout: ArtifactRef
    candidate: ArtifactRef
    gate_decision: ArtifactRef
    gate_outcome: GateOutcome
    promoted: bool
    quality_delta: float = Field(ge=-1, le=1)
    cost_amount: Decimal
    cost_currency: CurrencyCode
    cost_complete: bool
    input_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt
    failure_categories: tuple[FailureCategory, ...]
    patch_targets: tuple[RelativeArtifactPath, ...]

    @field_validator("cost_amount", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("round cost must use a decimal string")
        return value

    @model_validator(mode="after")
    def _complete_round(self) -> AutoRoundRecord:
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("round cost must be finite and nonnegative")
        if self.promoted != (self.gate_outcome is GateOutcome.ACCEPTED):
            raise ValueError("only an accepted GateDecision can promote a round")
        if len(self.failure_categories) != len(set(self.failure_categories)):
            raise ValueError("round failure categories must be unique")
        if not self.patch_targets or len(self.patch_targets) != len(
            set(self.patch_targets)
        ):
            raise ValueError("round patch targets must be nonempty and unique")
        return self


class FinalAggregateReport(VersionedRecord):
    """Aggregate-only result from the final split's single permitted run."""

    record_type: Literal["final_aggregate_report"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    subject_skill_sha256: Sha256Digest
    final_lock_sha256: Sha256Digest
    mode: Literal["fixed", "live"]
    measurement_kind: MeasurementKind
    network_used: bool
    result_source: Literal["fixed_reference", "canonical_live"]
    executed_at: UtcDateTime
    case_count: Literal[12]
    pass_count: Annotated[int, Field(strict=True, ge=0, le=12)]
    pass_rate: float = Field(ge=0, le=1)
    cost_amount: Decimal
    cost_currency: CurrencyCode
    cost_complete: bool
    input_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt
    private_results_sha256: Sha256Digest

    @field_validator("cost_amount", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("final cost must use a decimal string")
        return value

    @model_validator(mode="after")
    def _valid_final(self) -> FinalAggregateReport:
        if self.pass_rate != self.pass_count / self.case_count:
            raise ValueError("final pass rate does not match its aggregate count")
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("final cost must be finite and nonnegative")
        expected = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if self.measurement_kind is not expected:
            raise ValueError("final mode and measurement kind do not match")
        if self.mode == "fixed" and (
            self.network_used or self.result_source != "fixed_reference"
        ):
            raise ValueError("fixed final must remain an offline reference")
        if self.mode == "live" and (
            not self.network_used
            or self.result_source != "canonical_live"
            or not self.cost_complete
        ):
            raise ValueError(
                "live final requires canonical network evidence and complete cost"
            )
        return self


class FinalRunReceipt(VersionedRecord):
    """Protocol-locked receipt for the one consumed final run."""

    record_type: Literal["final_run_receipt"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    subject_skill_sha256: Sha256Digest
    final_lock_sha256: Sha256Digest
    mode: Literal["fixed", "live"]
    measurement_kind: MeasurementKind
    network_used: bool
    engine_id: NonEmptyStr
    simulator_id: NonEmptyStr
    judge_id: NonEmptyStr
    provider_id: NonEmptyStr
    model_lock_sha256: Sha256Digest
    evaluation_protocol_sha256: Sha256Digest
    report_protocol_sha256: Sha256Digest
    executed_at: UtcDateTime
    run_set_sha256: Sha256Digest
    private_results_sha256: Sha256Digest
    aggregate_report_sha256: Sha256Digest
    cost_amount: Decimal
    cost_currency: CurrencyCode
    cost_complete: bool
    input_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt

    @field_validator("cost_amount", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("final run cost must use a decimal string")
        return value

    @model_validator(mode="after")
    def _valid_receipt(self) -> FinalRunReceipt:
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("final run cost must be finite and nonnegative")
        expected = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if self.measurement_kind is not expected:
            raise ValueError("final run mode and measurement kind do not match")
        if self.mode == "fixed" and (
            self.network_used or self.provider_id != _FIXED_FINAL_PROVIDER_ID
        ):
            raise ValueError("fixed final receipt must remain offline")
        if self.mode == "live" and (
            not self.network_used
            or not self.cost_complete
            or self.provider_id == _FIXED_FINAL_PROVIDER_ID
        ):
            raise ValueError("live final receipt requires complete canonical evidence")
        return self


class FinalConsumedCheckpoint(VersionedRecord):
    """Independent one-time marker bound to every final output."""

    record_type: Literal["final_consumed_checkpoint"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    subject_skill_sha256: Sha256Digest
    final_lock_sha256: Sha256Digest
    consumed: Literal[True]
    final_run_receipt_sha256: Sha256Digest
    aggregate_report_sha256: Sha256Digest
    private_results_sha256: Sha256Digest


class AutoEvolveState(VersionedRecord):
    """Replayable aggregate state for one bounded experiment."""

    record_type: Literal["auto_evolve_state"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    config_sha256: Sha256Digest
    status: AutoLoopStatus
    current_accepted_skill_sha256: Sha256Digest
    completed_rounds: StrictNonNegativeInt
    rounds: tuple[AutoRoundRecord, ...]
    total_cost_amount: Decimal
    cost_currency: CurrencyCode
    cost_complete: bool
    total_input_tokens: StrictNonNegativeInt
    total_output_tokens: StrictNonNegativeInt
    pending_cost_amount: Decimal = Decimal(0)
    pending_cost_complete: bool = True
    pending_input_tokens: StrictNonNegativeInt = 0
    pending_output_tokens: StrictNonNegativeInt = 0
    final_cost_amount: Decimal = Decimal(0)
    final_cost_complete: bool = True
    final_input_tokens: StrictNonNegativeInt = 0
    final_output_tokens: StrictNonNegativeInt = 0
    consecutive_rejections: StrictNonNegativeInt
    stopped_at: UtcDateTime | None = None
    stop_reason: AutoStopReason | None = None
    final_report: ArtifactRef | None = None

    @field_validator(
        "total_cost_amount",
        "pending_cost_amount",
        "final_cost_amount",
        mode="before",
    )
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("loop cost must use a decimal string")
        return value

    @model_validator(mode="after")
    def _valid_state(self) -> AutoEvolveState:
        if self.completed_rounds != len(self.rounds):
            raise ValueError("completed round count does not match round records")
        if tuple(row.round_number for row in self.rounds) != tuple(
            range(1, len(self.rounds) + 1)
        ):
            raise ValueError("round records must be contiguous")
        for value in (
            self.total_cost_amount,
            self.pending_cost_amount,
            self.final_cost_amount,
        ):
            if not value.is_finite() or value < 0:
                raise ValueError("loop cost must be finite and nonnegative")
        expected_cost = (
            sum((row.cost_amount for row in self.rounds), Decimal(0))
            + self.pending_cost_amount
            + self.final_cost_amount
        )
        if self.total_cost_amount != expected_cost:
            raise ValueError("loop cost does not match observed step usage")
        expected_input_tokens = (
            sum(row.input_tokens for row in self.rounds)
            + self.pending_input_tokens
            + self.final_input_tokens
        )
        if self.total_input_tokens != expected_input_tokens:
            raise ValueError("loop input tokens do not match observed step usage")
        expected_output_tokens = (
            sum(row.output_tokens for row in self.rounds)
            + self.pending_output_tokens
            + self.final_output_tokens
        )
        if self.total_output_tokens != expected_output_tokens:
            raise ValueError("loop output tokens do not match observed step usage")
        expected_complete = (
            all(row.cost_complete for row in self.rounds)
            and self.pending_cost_complete
            and self.final_cost_complete
        )
        if self.cost_complete != expected_complete:
            raise ValueError("loop cost completeness does not match observed usage")
        if self.status is AutoLoopStatus.RUNNING:
            if self.stop_reason is not None or self.stopped_at is not None:
                raise ValueError("running loop cannot have a stop receipt")
        elif self.stop_reason is None or self.stopped_at is None:
            raise ValueError("stopped loop requires a reason and timestamp")
        if self.status is AutoLoopStatus.FINAL_COMPLETE:
            if self.final_report is None:
                raise ValueError("completed final requires its aggregate report")
        elif self.final_report is not None:
            raise ValueError("final report cannot exist before final completion")
        elif (
            self.final_cost_amount != 0
            or self.final_input_tokens != 0
            or self.final_output_tokens != 0
            or not self.final_cost_complete
        ):
            raise ValueError("final usage cannot exist before final completion")
        return self


class PortfolioFile(ContractModel):
    """One allowlisted, content-addressed portfolio member."""

    path: RelativeArtifactPath
    sha256: Sha256Digest
    kind: Literal[
        "skill",
        "registry",
        "gate",
        "loop_state",
        "l3_report",
        "final_aggregate",
        "architecture",
        "system_summary",
    ]


class PortfolioManifest(VersionedRecord):
    """Safe offline export inventory; hidden split data is never a member."""

    record_type: Literal["portfolio_manifest"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    created_at: UtcDateTime
    files: tuple[PortfolioFile, ...]

    @model_validator(mode="after")
    def _safe_inventory(self) -> PortfolioManifest:
        paths = [row.path for row in self.files]
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("portfolio files must be nonempty and unique")
        forbidden = {"selection", "final", "gold", "private", "credentials"}
        for path in paths:
            components = path.casefold().replace("_", "-").split("/")
            if any(
                token in component for component in components for token in forbidden
            ) and not path.endswith("final-aggregate.json"):
                raise ValueError("portfolio cannot include private split material")
        return self


__all__ = [
    "FINAL_REPORT_PROTOCOL_SHA256",
    "AutoEvolveConfig",
    "AutoEvolveState",
    "AutoLoopStatus",
    "AutoRolloutReceipt",
    "AutoRoundRecord",
    "AutoStopReason",
    "FinalAggregateReport",
    "FinalConsumedCheckpoint",
    "FinalRunReceipt",
    "PortfolioFile",
    "PortfolioManifest",
]
