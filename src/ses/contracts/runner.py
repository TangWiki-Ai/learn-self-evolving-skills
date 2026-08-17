"""Canonical records owned by the simulation and runner module."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ses.contracts.artifact import ArtifactRef, ArtifactRoot, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import Usage
from ses.contracts.primitives import (
    CaseId,
    CurrencyCode,
    IterationId,
    NonEmptyStr,
    RunId,
    StrictNonNegativeInt,
    UtcDateTime,
)
from ses.contracts.skill import MeasurementKind


class RunnerStatus(StrEnum):
    """Mutually exclusive orchestration outcomes."""

    PASS = "pass"
    AGENT_FAIL = "agent_fail"
    SIMULATOR_ERROR = "simulator_error"
    JUDGE_ERROR = "judge_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BUDGET_STOP = "budget_stop"
    NOT_EVALUATED = "not_evaluated"


class RunEventType(StrEnum):
    """Append-only entries supported by a run manifest."""

    RUN_STARTED = "run_started"
    ATTEMPT = "attempt"
    BUDGET_STOP = "budget_stop"
    NOT_EVALUATED = "not_evaluated"


class PairCategory(StrEnum):
    """Outcome of comparing the same case across a baseline and Skill run."""

    FAIL_TO_PASS = "fail-to-pass"
    PASS_TO_FAIL = "pass-to-fail"
    BOTH_PASS = "both-pass"
    BOTH_FAIL = "both-fail"


class RunArtifacts(ContractModel):
    """Evidence artifacts created by one paid attempt."""

    traces: tuple[ArtifactRef, ...] = ()
    before_snapshot: ArtifactRef | None = None
    after_snapshot: ArtifactRef | None = None
    state_diff: ArtifactRef | None = None
    grade: ArtifactRef | None = None


class RunConfig(ContractModel):
    """Reproducibility identity covered by the resume hash."""

    data_version: NonEmptyStr
    model_lock_hash: Sha256Digest
    skill_hash: Sha256Digest
    protocol_version: NonEmptyStr
    case_ids: tuple[CaseId, ...]
    case_plan: tuple[NonEmptyStr, ...]
    iterations: StrictNonNegativeInt

    @model_validator(mode="after")
    def _valid_plan(self) -> RunConfig:
        if self.iterations < 1:
            raise ValueError("iterations must be at least one")
        if not self.case_ids or len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case_ids must be nonempty and unique")
        if len(self.case_plan) != len(self.case_ids) * self.iterations:
            raise ValueError("case_plan must enumerate every planned iteration")
        return self


class BudgetState(ContractModel):
    """Configured limits and cumulative consumption across every attempt."""

    max_cases: StrictNonNegativeInt
    max_turns_per_case: StrictNonNegativeInt
    max_input_tokens: StrictNonNegativeInt | None = None
    max_output_tokens: StrictNonNegativeInt | None = None
    max_cost_amount: Decimal | None = None
    cost_currency: CurrencyCode
    consumed_cases: StrictNonNegativeInt = 0
    consumed_turns: StrictNonNegativeInt = 0
    consumed_input_tokens: StrictNonNegativeInt = 0
    consumed_output_tokens: StrictNonNegativeInt = 0
    consumed_cost_amount: Decimal = Decimal(0)
    consumed_latency_ms: StrictNonNegativeInt = 0
    stop_reason: NonEmptyStr | None = None

    @field_validator("max_cost_amount", "consumed_cost_amount", mode="before")
    @classmethod
    def _decimal_wire_value(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, Decimal)):
            raise ValueError("runner cost amounts must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_limits(self) -> BudgetState:
        if self.max_turns_per_case < 1:
            raise ValueError("max_turns_per_case must be at least one")
        for value in (self.max_cost_amount, self.consumed_cost_amount):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("runner cost amounts must be finite and nonnegative")
        return self


class RunRecord(VersionedRecord):
    """One canonical append-only run manifest entry."""

    record_type: Literal["run_record"]
    event_type: RunEventType
    sequence: StrictNonNegativeInt
    run_id: RunId
    config_hash: Sha256Digest
    config: RunConfig | None = None
    case_id: CaseId | None = None
    iteration_id: IterationId | None = None
    attempt_id: NonEmptyStr | None = None
    status: RunnerStatus | None = None
    recoverable: bool = False
    turn_count: StrictNonNegativeInt = 0
    session_resumed: bool = False
    usage: Usage | None = None
    latency_ms: StrictNonNegativeInt = 0
    budget: BudgetState
    artifacts: RunArtifacts = Field(default_factory=RunArtifacts)
    evidence: tuple[Mapping[str, JsonValue], ...] = ()
    tool_timeline: tuple[Mapping[str, JsonValue], ...] = ()
    state_diff: Mapping[str, JsonValue] = Field(default_factory=dict)
    transcript: tuple[Mapping[str, JsonValue], ...] = ()
    error: NonEmptyStr | None = None
    stop_reason: NonEmptyStr | None = None
    supersedes_iteration_id: IterationId | None = None

    @model_validator(mode="after")
    def _validate_event_shape(self) -> RunRecord:
        identifiers = (self.case_id, self.iteration_id, self.attempt_id)
        if self.event_type is RunEventType.RUN_STARTED:
            if (
                any(value is not None for value in identifiers)
                or self.status is not None
            ):
                raise ValueError("run_started cannot identify an attempt")
            if self.config is None:
                raise ValueError("run_started requires its reproducibility config")
        elif any(value is None for value in identifiers) or self.status is None:
            raise ValueError(
                "attempt records require case, iteration, attempt, and status"
            )
        elif self.config is not None:
            raise ValueError("only run_started stores the reproducibility config")
        if self.event_type is RunEventType.BUDGET_STOP:
            if self.status is not RunnerStatus.BUDGET_STOP or self.stop_reason is None:
                raise ValueError(
                    "budget_stop requires budget_stop status and a stop reason"
                )
        if self.event_type is RunEventType.NOT_EVALUATED:
            if self.status is not RunnerStatus.NOT_EVALUATED:
                raise ValueError("not_evaluated requires not_evaluated status")
        return self


class PairedCaseResult(ContractModel):
    """One compatible case pair with relative evidence references."""

    case_id: CaseId
    category: PairCategory
    baseline_status: RunnerStatus
    skill_status: RunnerStatus
    baseline_score: float = Field(ge=0, le=1)
    skill_score: float = Field(ge=0, le=1)
    score_delta: float = Field(ge=-1, le=1)
    baseline_input_tokens: StrictNonNegativeInt
    skill_input_tokens: StrictNonNegativeInt
    baseline_output_tokens: StrictNonNegativeInt
    skill_output_tokens: StrictNonNegativeInt
    baseline_cost_amount: Decimal
    skill_cost_amount: Decimal
    baseline_latency_ms: StrictNonNegativeInt
    skill_latency_ms: StrictNonNegativeInt
    baseline_trace: ArtifactRef | None = None
    skill_trace: ArtifactRef | None = None
    baseline_state_diff: ArtifactRef | None = None
    skill_state_diff: ArtifactRef | None = None
    baseline_grade: ArtifactRef | None = None
    skill_grade: ArtifactRef | None = None

    @field_validator("baseline_cost_amount", "skill_cost_amount", mode="before")
    @classmethod
    def _decimal_pair_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("paired cost amounts must use decimal strings")
        return value

    @model_validator(mode="after")
    def _validate_binary_outcome(self) -> PairedCaseResult:
        baseline_pass = self.baseline_status is RunnerStatus.PASS
        skill_pass = self.skill_status is RunnerStatus.PASS
        expected_category = (
            PairCategory.BOTH_PASS
            if baseline_pass and skill_pass
            else PairCategory.PASS_TO_FAIL
            if baseline_pass
            else PairCategory.FAIL_TO_PASS
            if skill_pass
            else PairCategory.BOTH_FAIL
        )
        if (
            self.category is not expected_category
            or self.baseline_score != float(baseline_pass)
            or self.skill_score != float(skill_pass)
            or self.score_delta != self.skill_score - self.baseline_score
        ):
            raise ValueError("paired row category or score is inconsistent")
        if any(
            not value.is_finite() or value < 0
            for value in (self.baseline_cost_amount, self.skill_cost_amount)
        ):
            raise ValueError("paired row costs must be finite and nonnegative")
        for status, refs in (
            (
                self.baseline_status,
                (self.baseline_trace, self.baseline_state_diff, self.baseline_grade),
            ),
            (
                self.skill_status,
                (self.skill_trace, self.skill_state_diff, self.skill_grade),
            ),
        ):
            if status in (RunnerStatus.PASS, RunnerStatus.AGENT_FAIL) and any(
                ref is None for ref in refs
            ):
                raise ValueError("completed paired outcomes require full evidence")
        return self


def pair_execution_sha256(
    *,
    baseline_events: ArtifactRef,
    skill_events: ArtifactRef,
    protocol_sha256: Sha256Digest,
    measured_at: UtcDateTime,
    measurement_kind: MeasurementKind,
) -> Sha256Digest:
    """Hash the exact event logs and measurement identity used by one pair."""

    payload = {
        "baseline_events": baseline_events.model_dump(mode="json"),
        "skill_events": skill_events.model_dump(mode="json"),
        "protocol_sha256": protocol_sha256,
        "measured_at": measured_at.isoformat(),
        "measurement_kind": measurement_kind.value,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PairedComparison(VersionedRecord):
    """Canonical fresh-run comparison consumed by L2 reporting."""

    record_type: Literal["paired_comparison"]
    baseline_run_id: RunId
    skill_run_id: RunId
    skill_sha256: Sha256Digest
    protocol_sha256: Sha256Digest
    pair_execution_sha256: Sha256Digest
    measurement_kind: MeasurementKind
    measured_at: UtcDateTime
    data_version: NonEmptyStr
    model_lock_sha256: Sha256Digest
    engine_version: NonEmptyStr
    model_id: NonEmptyStr
    baseline_events: ArtifactRef
    skill_events: ArtifactRef
    category_counts: Mapping[PairCategory, StrictNonNegativeInt]
    baseline_pass_rate: float = Field(ge=0, le=1)
    skill_pass_rate: float = Field(ge=0, le=1)
    baseline_input_tokens: StrictNonNegativeInt
    skill_input_tokens: StrictNonNegativeInt
    baseline_output_tokens: StrictNonNegativeInt
    skill_output_tokens: StrictNonNegativeInt
    baseline_cost_amount: Decimal
    skill_cost_amount: Decimal
    cost_currency: CurrencyCode
    baseline_latency_ms: StrictNonNegativeInt
    skill_latency_ms: StrictNonNegativeInt
    cases: tuple[PairedCaseResult, ...]

    @field_validator("baseline_cost_amount", "skill_cost_amount", mode="before")
    @classmethod
    def _decimal_comparison_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("paired cost amounts must use decimal strings")
        return value

    @model_validator(mode="after")
    def _validate_pair_set(self) -> PairedComparison:
        if not self.cases or len({item.case_id for item in self.cases}) != len(
            self.cases
        ):
            raise ValueError("paired comparison cases must be nonempty and unique")
        expected = {category: 0 for category in PairCategory}
        for item in self.cases:
            expected[item.category] += 1
        if dict(self.category_counts) != expected:
            raise ValueError("paired category counts do not match case rows")
        if self.baseline_run_id == self.skill_run_id:
            raise ValueError("paired comparison requires distinct run IDs")
        if self.baseline_events == self.skill_events:
            raise ValueError("paired comparison requires distinct event logs")
        for run_id, events in (
            (self.baseline_run_id, self.baseline_events),
            (self.skill_run_id, self.skill_events),
        ):
            if events.root is not ArtifactRoot.RUN or PurePosixPath(
                events.path
            ).parts != (run_id, "events.jsonl"):
                raise ValueError("paired event log path does not match its run")
        for row in self.cases:
            for run_id, refs in (
                (
                    self.baseline_run_id,
                    (row.baseline_trace, row.baseline_state_diff, row.baseline_grade),
                ),
                (
                    self.skill_run_id,
                    (row.skill_trace, row.skill_state_diff, row.skill_grade),
                ),
            ):
                prefix = (run_id, "artifacts", row.case_id, "iteration-0")
                if any(
                    ref.root is not ArtifactRoot.RUN
                    or PurePosixPath(ref.path).parts[: len(prefix)] != prefix
                    for ref in refs
                    if ref is not None
                ):
                    raise ValueError("paired case evidence path does not match its run")
        total = len(self.cases)
        expected_values = (
            sum(row.baseline_score for row in self.cases) / total,
            sum(row.skill_score for row in self.cases) / total,
            sum(row.baseline_input_tokens for row in self.cases),
            sum(row.skill_input_tokens for row in self.cases),
            sum(row.baseline_output_tokens for row in self.cases),
            sum(row.skill_output_tokens for row in self.cases),
            sum((row.baseline_cost_amount for row in self.cases), Decimal(0)),
            sum((row.skill_cost_amount for row in self.cases), Decimal(0)),
            sum(row.baseline_latency_ms for row in self.cases),
            sum(row.skill_latency_ms for row in self.cases),
        )
        actual_values = (
            self.baseline_pass_rate,
            self.skill_pass_rate,
            self.baseline_input_tokens,
            self.skill_input_tokens,
            self.baseline_output_tokens,
            self.skill_output_tokens,
            self.baseline_cost_amount,
            self.skill_cost_amount,
            self.baseline_latency_ms,
            self.skill_latency_ms,
        )
        if actual_values != expected_values:
            raise ValueError("paired aggregate metrics do not match case rows")
        if any(
            not value.is_finite() or value < 0
            for value in (self.baseline_cost_amount, self.skill_cost_amount)
        ):
            raise ValueError("paired aggregate costs must be finite and nonnegative")
        expected_execution = pair_execution_sha256(
            baseline_events=self.baseline_events,
            skill_events=self.skill_events,
            protocol_sha256=self.protocol_sha256,
            measured_at=self.measured_at,
            measurement_kind=self.measurement_kind,
        )
        if self.pair_execution_sha256 != expected_execution:
            raise ValueError("paired execution hash does not match its evidence")
        return self
