"""Canonical records shared by Skill creation, evaluation, and reporting."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ses.contracts.artifact import (
    ArtifactRef,
    RelativeArtifactPath,
    Sha256Digest,
)
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import Usage
from ses.contracts.primitives import NonEmptyStr, StrictNonNegativeInt, UtcDateTime


class SkillManifestFile(ContractModel):
    """One content-addressed runtime file in a Skill artifact."""

    path: RelativeArtifactPath
    sha256: Sha256Digest

    @field_validator("path")
    @classmethod
    def _installable_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if any(part.startswith(".") for part in path.parts):
            raise ValueError("manifest file path cannot contain hidden segments")
        if value != "SKILL.md" and (
            len(path.parts) < 2 or path.parts[0] != "references"
        ):
            raise ValueError("manifest may declare only SKILL.md and references files")
        return value


class SkillArtifactManifest(VersionedRecord):
    """Canonical inventory and identity of one installable Skill artifact."""

    record_type: Literal["skill_artifact_manifest"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: NonEmptyStr
    source_version: NonEmptyStr = "unspecified"
    content_sha256: Sha256Digest | None = None
    provider_compatibility: tuple[NonEmptyStr, ...] = ("claude-code-native",)
    files: tuple[SkillManifestFile, ...]

    @field_validator("provider_compatibility")
    @classmethod
    def _provider_compatibility_not_empty(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("provider compatibility must be nonempty and unique")
        return value

    @field_validator("files")
    @classmethod
    def _complete_unique_inventory(
        cls, value: tuple[SkillManifestFile, ...]
    ) -> tuple[SkillManifestFile, ...]:
        paths = [item.path for item in value]
        if paths.count("SKILL.md") != 1:
            raise ValueError("manifest must declare SKILL.md exactly once")
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique")
        return value


class CreatorSeedAttestation(VersionedRecord):
    """Course-authored evidence binding that explicitly awaits human review."""

    record_type: Literal["creator_seed_attestation"]
    seed_id: NonEmptyStr
    status: Literal["course_authored_pending_human_review"]
    source_sha256: Sha256Digest
    trace_sha256: Sha256Digest
    replay_sha256: Sha256Digest
    state_diff_sha256: Sha256Digest
    state_grade_sha256: Sha256Digest
    model_evidence_sha256: Sha256Digest
    model_grade_sha256: Sha256Digest
    model_run_sha256: Sha256Digest
    projection_sha256: Sha256Digest
    review_packet: Literal["docs/release/human-review-packet.md"]


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
    seed_review_status: Literal["course_authored_pending_human_review"]
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
    static_gate_result: ArtifactRef
    trigger_result: ArtifactRef
    paired_comparison: ArtifactRef
    l2_html: ArtifactRef
