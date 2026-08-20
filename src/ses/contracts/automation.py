"""Canonical records for bounded auto-evolution and release portfolio exports."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from ses.contracts.artifact import ArtifactRef, RelativeArtifactPath, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.evolution import FailureCategory, GateOutcome
from ses.contracts.primitives import (
    CurrencyCode,
    NonEmptyStr,
    SchemaVersion,
    StrictNonNegativeInt,
    UtcDateTime,
)
from ses.contracts.shopping import ShoppingScenario
from ses.contracts.skill import MeasurementKind

FINAL_REPORT_PROTOCOL_SHA256 = hashlib.sha256(
    b"ses-final-aggregate-report:v1alpha1"
).hexdigest()
CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256 = hashlib.sha256(
    b"ses-shopping-final-aggregate-report:v1alpha2"
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
    FAILED_FINAL = "failed_final"


class FinalLifecycle(StrEnum):
    """Whether final runs inline for legacy lessons or as a capstone command."""

    INLINE_LEGACY = "inline_legacy"
    INDEPENDENT_CAPSTONE = "independent_capstone"


class SplitLockFormat(StrEnum):
    """Wire format used to validate protected selection and final locks."""

    HOLDOUT_MANIFEST = "holdout_manifest"
    CONTENT_ADDRESSED = "content_addressed"


class OpaqueProtectedSplitLock(VersionedRecord):
    """Public, identity-free inventory bound to one protected experiment split."""

    record_type: Literal["opaque_protected_split_lock"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    profile_sha256: Sha256Digest
    mode: Literal["fixed", "live"]
    measurement_kind: MeasurementKind
    split: Literal["selection", "final"]
    case_count: Annotated[int, Field(strict=True, ge=1)]
    opaque_slots: tuple[NonEmptyStr, ...]
    aggregate_commitment_sha256: Sha256Digest
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def _identity_free_inventory(self) -> OpaqueProtectedSplitLock:
        expected_measurement = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if self.measurement_kind is not expected_measurement:
            raise ValueError("protected split mode and measurement do not match")
        expected_slots = tuple(
            f"opaque-{self.split}-{index:03d}"
            for index in range(1, self.case_count + 1)
        )
        if self.opaque_slots != expected_slots:
            raise ValueError("protected split slots must be complete and opaque")
        return self


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

    content_hash_exclude_if_none: ClassVar[frozenset[str]] = frozenset(
        {"final_lifecycle", "profile_sha256", "split_lock_format"}
    )

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
    final_lifecycle: FinalLifecycle | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    profile_sha256: Sha256Digest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    split_lock_format: SplitLockFormat | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

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
        if (self.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE) != (
            self.profile_sha256 is not None
        ):
            raise ValueError(
                "independent capstone lifecycle and profile hash must be locked together"
            )
        if self.final_lifecycle is FinalLifecycle.INLINE_LEGACY:
            raise ValueError("legacy lifecycle uses the absent default contract fields")
        if self.final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE:
            if self.split_lock_format is not SplitLockFormat.CONTENT_ADDRESSED:
                raise ValueError(
                    "independent capstone requires content-addressed split locks"
                )
            if self.selection_lock_sha256 == self.final_lock_sha256:
                raise ValueError("selection and final require distinct split locks")
            if (
                self.final_report_protocol_sha256
                != CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256
            ):
                raise ValueError(
                    "independent capstone requires the v1alpha2 final report protocol"
                )
        elif self.split_lock_format is not None:
            raise ValueError("legacy config uses the default holdout lock format")
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
    source_kind: Literal[
        "fixed_reference_fixture",
        "fresh_fixed_execution",
        "fresh_develop_run",
    ]
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
        if self.source_kind in {
            "fixed_reference_fixture",
            "fresh_fixed_execution",
        } and (
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


class ShoppingFinalScenarioMetrics(ContractModel):
    """Aggregate-safe final metrics for one three-case shopping scenario."""

    scenario: ShoppingScenario
    case_count: Literal[3]
    full_success_count: Annotated[int, Field(strict=True, ge=0, le=3)]
    mean_strict_reward: Decimal
    safety_violation_count: StrictNonNegativeInt

    @field_validator("mean_strict_reward", mode="before")
    @classmethod
    def _decimal_mean(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("shopping final means must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_scenario(self) -> ShoppingFinalScenarioMetrics:
        if not self.mean_strict_reward.is_finite() or not Decimal(
            0
        ) <= self.mean_strict_reward <= Decimal(1):
            raise ValueError("shopping final strict means must be from zero to one")
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
    result_source: Literal[
        "fixed_reference",
        "fresh_fixed_execution",
        "canonical_live",
    ]
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
    full_success_count: Annotated[int, Field(strict=True, ge=0, le=12)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    mean_strict_reward: Decimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    safety_violation_count: StrictNonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    scenario_metrics: tuple[ShoppingFinalScenarioMetrics, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )

    supported_schema_versions: ClassVar[frozenset[SchemaVersion]] = frozenset(
        {SchemaVersion.V1ALPHA1, SchemaVersion.V1ALPHA2}
    )

    @field_validator("cost_amount", "mean_strict_reward", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, Decimal)):
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
            self.network_used
            or self.result_source not in {"fixed_reference", "fresh_fixed_execution"}
        ):
            raise ValueError("fixed final must remain offline")
        if self.mode == "live" and (
            not self.network_used
            or self.result_source != "canonical_live"
            or not self.cost_complete
        ):
            raise ValueError(
                "live final requires canonical network evidence and complete cost"
            )
        shopping_values = (
            self.full_success_count,
            self.mean_strict_reward,
            self.safety_violation_count,
            self.scenario_metrics or None,
        )
        if self.schema_version is SchemaVersion.V1ALPHA1:
            if any(value is not None for value in shopping_values):
                raise ValueError("v1alpha1 final cannot contain shopping aggregates")
            return self
        if any(value is None for value in shopping_values):
            raise ValueError("v1alpha2 final requires complete shopping aggregates")
        assert self.full_success_count is not None
        assert self.mean_strict_reward is not None
        assert self.safety_violation_count is not None
        if tuple(row.scenario for row in self.scenario_metrics) != tuple(
            ShoppingScenario
        ):
            raise ValueError("shopping final requires every scenario exactly once")
        if (
            len(self.scenario_metrics) * 3 != self.case_count
            or sum(row.full_success_count for row in self.scenario_metrics)
            != self.full_success_count
            or sum(row.safety_violation_count for row in self.scenario_metrics)
            != self.safety_violation_count
            or self.pass_count != self.full_success_count
        ):
            raise ValueError("shopping final strata do not match aggregate counts")
        expected_strict = (
            sum(
                (
                    row.mean_strict_reward * row.case_count
                    for row in self.scenario_metrics
                ),
                Decimal(0),
            )
            / self.case_count
        )
        if (
            not self.mean_strict_reward.is_finite()
            or not Decimal(0) <= self.mean_strict_reward <= Decimal(1)
            or self.mean_strict_reward != expected_strict
        ):
            raise ValueError("shopping final strict strata do not match the aggregate")
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


class CapstoneFinalReceipt(VersionedRecord):
    """Public eligibility receipt for one completed capstone final run."""

    record_type: Literal["capstone_final_receipt"]
    experiment_id: str = Field(pattern=r"^experiment-[a-z0-9-]+$")
    lineage_id: str = Field(pattern=r"^lineage-[a-z0-9-]+$")
    profile_sha256: Sha256Digest
    subject_skill_sha256: Sha256Digest
    measurement_kind: MeasurementKind
    completed: Literal[True]
    safety_violation_count: StrictNonNegativeInt
    result_origin: Literal["fresh_fixed_execution", "live_measured"]
    aggregate: ArtifactRef
    final_run_receipt: ArtifactRef
    one_time_checkpoint: ArtifactRef

    @model_validator(mode="after")
    def _origin_matches_measurement(self) -> CapstoneFinalReceipt:
        expected = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.result_origin == "fresh_fixed_execution"
            else MeasurementKind.LIVE_MEASURED
        )
        if self.measurement_kind is not expected:
            raise ValueError("capstone final origin and measurement do not match")
        if (
            len(
                {
                    self.aggregate.path,
                    self.final_run_receipt.path,
                    self.one_time_checkpoint.path,
                }
            )
            != 3
        ):
            raise ValueError("capstone final evidence references must be distinct")
        return self


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
        if self.status in {
            AutoLoopStatus.FINAL_COMPLETE,
            AutoLoopStatus.FAILED_FINAL,
        }:
            if self.final_report is None:
                raise ValueError("terminal final requires its aggregate report")
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
    "CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256",
    "FINAL_REPORT_PROTOCOL_SHA256",
    "AutoEvolveConfig",
    "AutoEvolveState",
    "AutoLoopStatus",
    "AutoRolloutReceipt",
    "AutoRoundRecord",
    "AutoStopReason",
    "CapstoneFinalReceipt",
    "FinalAggregateReport",
    "FinalConsumedCheckpoint",
    "FinalLifecycle",
    "FinalRunReceipt",
    "OpaqueProtectedSplitLock",
    "PortfolioFile",
    "PortfolioManifest",
    "ShoppingFinalScenarioMetrics",
    "SplitLockFormat",
]
