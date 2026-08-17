"""Canonical records shared by Skill creation, evaluation, and reporting."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from ses.contracts.artifact import ArtifactRef, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import Usage
from ses.contracts.primitives import NonEmptyStr, StrictNonNegativeInt, UtcDateTime


class CreatorHumanReview(VersionedRecord):
    """A named, dated decision over one complete creator evidence chain."""

    record_type: Literal["creator_human_review"]
    seed_id: NonEmptyStr
    reviewed_source_sha256: Sha256Digest
    reviewed_trace_sha256: Sha256Digest
    reviewed_replay_sha256: Sha256Digest
    reviewed_state_diff_sha256: Sha256Digest
    reviewed_state_grade_sha256: Sha256Digest
    reviewed_model_evidence_sha256: Sha256Digest
    reviewed_model_grade_sha256: Sha256Digest
    reviewed_model_run_sha256: Sha256Digest
    reviewed_projection_sha256: Sha256Digest
    decision: Literal["approved", "rejected"]
    reason: NonEmptyStr
    reviewed_at: UtcDateTime
    reviewer: NonEmptyStr


class CreatorSourceProvenance(VersionedRecord):
    """Pinned upstream files that produced one creator seed."""

    record_type: Literal["creator_source_provenance"]
    repository: NonEmptyStr
    commit: NonEmptyStr
    task_id: NonEmptyStr
    task_sha256: Sha256Digest
    environment_sha256: Sha256Digest
    trajectory_sha256: Sha256Digest

    @model_validator(mode="after")
    def _uses_full_git_commit(self) -> CreatorSourceProvenance:
        if len(self.commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.commit
        ):
            raise ValueError("creator source commit must be a full lowercase Git SHA")
        return self


class CreatorReplayCall(ContractModel):
    """One upstream tool invocation verified against the recorded result."""

    sequence: StrictNonNegativeInt
    tool_name: NonEmptyStr
    arguments: Mapping[str, JsonValue]
    expected_result_sha256: Sha256Digest
    actual_result_sha256: Sha256Digest
    matched: Literal[True]


class CreatorSourceReplay(VersionedRecord):
    """Deterministic replay receipt for one pinned STATE-Bench trajectory."""

    record_type: Literal["creator_source_replay"]
    seed_id: NonEmptyStr
    source: ArtifactRef
    before_snapshot_sha256: Sha256Digest
    after_snapshot_sha256: Sha256Digest
    upstream_state_diff_sha256: Sha256Digest
    state_score: Literal[1]
    state_reason: NonEmptyStr
    calls: tuple[CreatorReplayCall, ...]

    @model_validator(mode="after")
    def _has_verified_calls(self) -> CreatorSourceReplay:
        if not self.calls:
            raise ValueError("creator replay must contain at least one tool call")
        if tuple(call.sequence for call in self.calls) != tuple(range(len(self.calls))):
            raise ValueError(
                "creator replay calls must use contiguous sequence numbers"
            )
        return self


class DiscoveryStatus(StrEnum):
    """One observed native Skill discovery outcome."""

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    INDETERMINATE = "indeterminate"


class MeasurementKind(StrEnum):
    """Whether metrics came from deterministic fixtures or paid product runs."""

    SYNTHETIC_OFFLINE = "synthetic_offline"
    LIVE_MEASURED = "live_measured"


class TriggerPromptResult(ContractModel):
    """Evidence for one fixed trigger prompt."""

    prompt_id: NonEmptyStr
    prompt: NonEmptyStr
    expected_trigger: bool
    actual: DiscoveryStatus
    evidence: NonEmptyStr


class TriggerEvalResult(VersionedRecord):
    """Canonical confusion matrix consumed by reporting."""

    record_type: Literal["trigger_eval_result"]
    skill_sha256: Sha256Digest
    prompt_set_sha256: Sha256Digest
    engine_version: NonEmptyStr
    model_id: NonEmptyStr
    measurement_kind: MeasurementKind
    measured_at: UtcDateTime
    usage: Usage
    tp: StrictNonNegativeInt
    fp: StrictNonNegativeInt
    tn: StrictNonNegativeInt
    fn: StrictNonNegativeInt
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    indeterminate_count: StrictNonNegativeInt
    prompts: tuple[TriggerPromptResult, ...]

    @model_validator(mode="after")
    def _matrix_matches_prompt_evidence(self) -> TriggerEvalResult:
        tp = fp = tn = fn = indeterminate = 0
        for row in self.prompts:
            if row.actual is DiscoveryStatus.INDETERMINATE:
                indeterminate += 1
            elif row.expected_trigger and row.actual is DiscoveryStatus.TRIGGERED:
                tp += 1
            elif row.expected_trigger:
                fn += 1
            elif row.actual is DiscoveryStatus.TRIGGERED:
                fp += 1
            else:
                tn += 1
        if (self.tp, self.fp, self.tn, self.fn, self.indeterminate_count) != (
            tp,
            fp,
            tn,
            fn,
            indeterminate,
        ):
            raise ValueError("trigger confusion matrix does not match prompt evidence")
        expected_precision = tp / (tp + fp) if tp + fp else 0.0
        expected_recall = tp / (tp + fn) if tp + fn else 0.0
        if self.precision != expected_precision or self.recall != expected_recall:
            raise ValueError("trigger precision or recall does not match the matrix")
        return self


class SkillV0PipelineSummary(VersionedRecord):
    """Canonical handoff from the v0 workflow to CLI and course consumers."""

    record_type: Literal["skill_v0_pipeline_summary"]
    mode: Literal["fixed", "live"]
    seed_count: StrictNonNegativeInt
    skill_sha256: Sha256Digest
    creator_measurement: MeasurementKind
    trigger_measurement: MeasurementKind
    paired_measurement: MeasurementKind
    static_gate: Literal["pass"]
    trigger_precision: float = Field(ge=0, le=1)
    trigger_recall: float = Field(ge=0, le=1)
    paired_case_count: StrictNonNegativeInt
    baseline_pass_rate: float = Field(ge=0, le=1)
    skill_pass_rate: float = Field(ge=0, le=1)
    trigger_result: ArtifactRef
    paired_comparison: ArtifactRef
    l2_html: ArtifactRef
