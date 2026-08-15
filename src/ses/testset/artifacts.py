"""Strict producer-owned wire models for candidate-mining artifacts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StrictFloat, StrictInt, model_validator

from ses.contracts import (
    ContractModel,
    NonEmptyStr,
    RelativeArtifactPath,
    SchemaVersion,
    Sha256Digest,
    StrictNonNegativeInt,
    VersionedRecord,
)
from ses.testset.cluster import (
    ClusterAssignment,
    ClusterRepresentativeSample,
    ClusterSummary,
    ContingencyCell,
    LabelComparison,
)
from ses.testset.difficulty import (
    TAU_RESULT_FILES,
    TAU_RUNS_PER_TASK,
    TAU_TRIALS_PER_ASSET,
    PerAssetDifficulty,
    TauDifficulty,
)
from ses.testset.scrub import DialogueTurn, ScrubbedConversation
from ses.testset.stratify import CandidateRecord

PositiveInt = Annotated[StrictInt, Field(gt=0)]
UnitFloat = Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
MetricFloat = Annotated[StrictFloat, Field(ge=-1.0, le=1.0)]
LabelName = Literal["flow", "subflow"]
DifficultyBucket = Literal["hard", "medium", "easy"]


class DialogueTurnArtifact(ContractModel):
    """One aligned role-play dialogue turn."""

    speaker: str
    text: str
    turn_count: StrictNonNegativeInt | None = None

    @classmethod
    def from_domain(cls, value: DialogueTurn) -> Self:
        return cls(
            speaker=value.speaker,
            text=value.text,
            turn_count=value.turn_count,
        )


class ScrubbedConversationArtifact(VersionedRecord):
    """Validated paired ABCD role-play benchmark dialogue."""

    record_type: Literal["scrubbed_abcd_conversation"]
    source_id: NonEmptyStr
    upstream_id: NonEmptyStr
    source_commit: NonEmptyStr
    source_split: NonEmptyStr
    flow: NonEmptyStr
    subflow: NonEmptyStr
    original: tuple[DialogueTurnArtifact, ...]
    delexed: tuple[DialogueTurnArtifact, ...]
    normalized_text: NonEmptyStr
    pair_sha256: Sha256Digest
    dedup_sha256: Sha256Digest
    duplicate_source_ids: tuple[NonEmptyStr, ...] = ()
    label_conflict: bool = False

    @model_validator(mode="after")
    def _validate_alignment_and_lineage(self) -> ScrubbedConversationArtifact:
        if not self.original or len(self.original) != len(self.delexed):
            raise ValueError("original and delexed turns must be non-empty and aligned")
        if any(
            left.speaker != right.speaker
            for left, right in zip(self.original, self.delexed, strict=True)
        ):
            raise ValueError("original and delexed speakers must remain aligned")
        if len(set(self.duplicate_source_ids)) != len(self.duplicate_source_ids):
            raise ValueError("duplicate source IDs must be unique")
        if self.source_id in self.duplicate_source_ids:
            raise ValueError("a record cannot duplicate itself")
        return self

    @classmethod
    def from_domain(cls, value: ScrubbedConversation) -> Self:
        return cls(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="scrubbed_abcd_conversation",
            source_id=value.source_id,
            upstream_id=value.upstream_id,
            source_commit=value.source_commit,
            source_split=value.source_split,
            flow=value.flow,
            subflow=value.subflow,
            original=tuple(
                DialogueTurnArtifact.from_domain(turn) for turn in value.original
            ),
            delexed=tuple(
                DialogueTurnArtifact.from_domain(turn) for turn in value.delexed
            ),
            normalized_text=value.normalized_text,
            pair_sha256=value.pair_sha256,
            dedup_sha256=value.dedup_sha256,
            duplicate_source_ids=value.duplicate_source_ids,
            label_conflict=value.label_conflict,
        )


class ClusterAssignmentArtifact(VersionedRecord):
    """Per-candidate cluster assignment with optional adapter confidence."""

    record_type: Literal["cluster_assignment"]
    item_id: NonEmptyStr
    cluster_id: NonEmptyStr
    confidence: UnitFloat | None = None

    @classmethod
    def from_domain(cls, value: ClusterAssignment) -> Self:
        return cls(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="cluster_assignment",
            item_id=value.item_id,
            cluster_id=value.cluster_id,
            confidence=value.confidence,
        )


class ClusterRepresentativeArtifact(ContractModel):
    """One deterministic audit sample from a cluster."""

    rank: PositiveInt
    item_id: NonEmptyStr
    text: str
    source_kind: NonEmptyStr
    confidence: UnitFloat | None
    selection_reason: NonEmptyStr

    @classmethod
    def from_domain(cls, value: ClusterRepresentativeSample) -> Self:
        return cls(
            rank=value.rank,
            item_id=value.item_id,
            text=value.text,
            source_kind=value.source_kind,
            confidence=value.confidence,
            selection_reason=value.selection_reason,
        )


class ClusterSummaryArtifact(VersionedRecord):
    """Cluster size and deterministic representative samples."""

    record_type: Literal["cluster_summary"]
    cluster_id: NonEmptyStr
    member_count: PositiveInt
    representative_selection_method: NonEmptyStr
    representative_samples: tuple[ClusterRepresentativeArtifact, ...]

    @model_validator(mode="after")
    def _validate_representatives(self) -> ClusterSummaryArtifact:
        if not self.representative_samples:
            raise ValueError("a cluster summary needs at least one representative")
        if len(self.representative_samples) > self.member_count:
            raise ValueError("representative count cannot exceed member count")
        expected_ranks = tuple(range(1, len(self.representative_samples) + 1))
        if (
            tuple(sample.rank for sample in self.representative_samples)
            != expected_ranks
        ):
            raise ValueError("representative ranks must be contiguous and ordered")
        item_ids = tuple(sample.item_id for sample in self.representative_samples)
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("representative item IDs must be unique")
        return self

    @classmethod
    def from_domain(cls, value: ClusterSummary) -> Self:
        return cls(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="cluster_summary",
            cluster_id=value.cluster_id,
            member_count=value.member_count,
            representative_selection_method=value.representative_selection_method,
            representative_samples=tuple(
                ClusterRepresentativeArtifact.from_domain(sample)
                for sample in value.representative_samples
            ),
        )


class ContingencyCellArtifact(ContractModel):
    """One observed label-to-cluster count."""

    reference_label: NonEmptyStr
    cluster_id: NonEmptyStr
    count: PositiveInt

    @classmethod
    def from_domain(cls, value: ContingencyCell) -> Self:
        return cls(
            reference_label=value.reference_label,
            cluster_id=value.cluster_id,
            count=value.count,
        )


class LabelComparisonArtifact(ContractModel):
    """External-label agreement metrics for one label dimension."""

    label_name: LabelName
    evaluated_count: PositiveInt
    excluded_missing_label_count: StrictNonNegativeInt
    true_label_count: PositiveInt
    cluster_count: PositiveInt
    contingency: tuple[ContingencyCellArtifact, ...]
    adjusted_rand_index: MetricFloat
    normalized_mutual_info: UnitFloat
    homogeneity: UnitFloat
    completeness: UnitFloat
    v_measure: UnitFloat
    informative: bool
    reason: str | None

    @model_validator(mode="after")
    def _validate_contingency(self) -> LabelComparisonArtifact:
        if not self.contingency:
            raise ValueError("label comparison contingency cannot be empty")
        if sum(cell.count for cell in self.contingency) != self.evaluated_count:
            raise ValueError("contingency count must equal evaluated count")
        return self

    @classmethod
    def from_domain(cls, value: LabelComparison) -> Self:
        if value.label_name not in {"flow", "subflow"}:
            raise ValueError(f"unsupported label comparison: {value.label_name}")
        return cls(
            label_name=cast(LabelName, value.label_name),
            evaluated_count=value.evaluated_count,
            excluded_missing_label_count=value.excluded_missing_label_count,
            true_label_count=value.true_label_count,
            cluster_count=value.cluster_count,
            contingency=tuple(
                ContingencyCellArtifact.from_domain(cell) for cell in value.contingency
            ),
            adjusted_rand_index=value.adjusted_rand_index,
            normalized_mutual_info=value.normalized_mutual_info,
            homogeneity=value.homogeneity,
            completeness=value.completeness,
            v_measure=value.v_measure,
            informative=value.informative,
            reason=value.reason,
        )


class ClusterLabelComparisonSetArtifact(VersionedRecord):
    """Auditable flow and subflow agreement metrics."""

    record_type: Literal["cluster_label_comparison_set"]
    flow: LabelComparisonArtifact
    subflow: LabelComparisonArtifact

    @model_validator(mode="after")
    def _validate_label_dimensions(self) -> ClusterLabelComparisonSetArtifact:
        if self.flow.label_name != "flow" or self.subflow.label_name != "subflow":
            raise ValueError("comparison set must contain flow and subflow metrics")
        return self

    @classmethod
    def from_domain(cls, values: Sequence[LabelComparison]) -> Self:
        comparisons = {
            value.label_name: LabelComparisonArtifact.from_domain(value)
            for value in values
        }
        if set(comparisons) != {"flow", "subflow"} or len(values) != 2:
            raise ValueError("comparison set requires exactly one flow and one subflow")
        return cls(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="cluster_label_comparison_set",
            flow=comparisons["flow"],
            subflow=comparisons["subflow"],
        )


class PerAssetDifficultyArtifact(ContractModel):
    """Per-result-asset run counts retained for tau2 auditability."""

    result_asset_id: NonEmptyStr
    success_count: StrictNonNegativeInt
    run_count: PositiveInt

    @model_validator(mode="after")
    def _validate_counts(self) -> PerAssetDifficultyArtifact:
        if self.success_count > self.run_count:
            raise ValueError("success count cannot exceed run count")
        return self

    @classmethod
    def from_domain(cls, value: PerAssetDifficulty) -> Self:
        return cls(
            result_asset_id=value.result_asset_id,
            success_count=value.success_count,
            run_count=value.run_count,
        )


class TauDifficultyArtifact(VersionedRecord):
    """Task-level tau2 benchmark difficulty after all runs are aggregated."""

    record_type: Literal["tau2_task_difficulty"]
    source_id: NonEmptyStr
    task_id: NonEmptyStr
    task_text: str
    run_count: PositiveInt
    success_count: StrictNonNegativeInt
    pass_rate: UnitFloat
    pass_rate_decimal: NonEmptyStr
    mean_reward: UnitFloat
    difficulty_score: UnitFloat
    difficulty_bucket: DifficultyBucket
    per_asset: tuple[PerAssetDifficultyArtifact, ...]
    generation_commits: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _validate_aggregate(self) -> TauDifficultyArtifact:
        if self.run_count != TAU_RUNS_PER_TASK:
            raise ValueError(f"tau difficulty requires {TAU_RUNS_PER_TASK} task runs")
        if len(self.per_asset) != TAU_RESULT_FILES:
            raise ValueError(f"tau difficulty requires {TAU_RESULT_FILES} assets")
        if any(asset.run_count != TAU_TRIALS_PER_ASSET for asset in self.per_asset):
            raise ValueError(
                f"each tau result asset requires {TAU_TRIALS_PER_ASSET} trials"
            )
        if self.success_count > self.run_count:
            raise ValueError("success count cannot exceed run count")
        if not self.per_asset:
            raise ValueError("tau difficulty requires per-asset provenance")
        if sum(value.run_count for value in self.per_asset) != self.run_count:
            raise ValueError("per-asset run counts must equal task run count")
        if sum(value.success_count for value in self.per_asset) != self.success_count:
            raise ValueError("per-asset success counts must equal task success count")
        try:
            exact_pass_rate = Decimal(self.pass_rate_decimal)
        except InvalidOperation as exc:
            raise ValueError("pass_rate_decimal must be a decimal string") from exc
        expected_pass_rate = Decimal(self.success_count) / Decimal(self.run_count)
        if exact_pass_rate != expected_pass_rate:
            raise ValueError("pass_rate_decimal must match task counts")
        if not math.isclose(self.pass_rate, float(exact_pass_rate)):
            raise ValueError("pass_rate must match pass_rate_decimal")
        if not math.isclose(self.mean_reward, self.pass_rate):
            raise ValueError("mean reward must match binary pass rate")
        if not math.isclose(self.difficulty_score, 1.0 - self.pass_rate):
            raise ValueError("difficulty score must equal one minus pass rate")
        expected_bucket = (
            "hard"
            if exact_pass_rate <= Decimal("0.25")
            else "easy"
            if exact_pass_rate >= Decimal("0.75")
            else "medium"
        )
        if self.difficulty_bucket != expected_bucket:
            raise ValueError("difficulty bucket must match pass rate")
        return self

    @classmethod
    def from_domain(cls, value: TauDifficulty) -> Self:
        return cls(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="tau2_task_difficulty",
            source_id=value.source_id,
            task_id=value.task_id,
            task_text=value.task_text,
            run_count=value.run_count,
            success_count=value.success_count,
            pass_rate=value.pass_rate,
            pass_rate_decimal=value.pass_rate_decimal,
            mean_reward=value.mean_reward,
            difficulty_score=value.difficulty_score,
            difficulty_bucket=cast(DifficultyBucket, value.difficulty_bucket),
            per_asset=tuple(
                PerAssetDifficultyArtifact.from_domain(asset)
                for asset in value.per_asset
            ),
            generation_commits=value.generation_commits,
        )


class CandidateArtifact(VersionedRecord):
    """Candidate-only mining output; never an executable benchmark case."""

    record_type: Literal["testset_candidate"]
    candidate_id: NonEmptyStr
    source_id: NonEmptyStr
    duplicate_source_ids: tuple[NonEmptyStr, ...]
    cluster_id: NonEmptyStr
    flow: NonEmptyStr
    subflow: NonEmptyStr
    semantic_group_id: NonEmptyStr
    label_frequency: PositiveInt
    long_tail: bool
    label_conflict: bool
    tau_task_id: NonEmptyStr | None
    tau_run_count: PositiveInt | None
    tau_success_count: StrictNonNegativeInt | None
    tau_pass_rate: NonEmptyStr | None
    difficulty_bucket: DifficultyBucket | None
    similarity: UnitFloat | None
    retention_reasons: tuple[NonEmptyStr, ...]
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _validate_candidate(self) -> CandidateArtifact:
        if len(set(self.duplicate_source_ids)) != len(self.duplicate_source_ids):
            raise ValueError("duplicate source IDs must be unique")
        if self.source_id in self.duplicate_source_ids:
            raise ValueError("a candidate cannot duplicate itself")
        tau_fields = (
            self.tau_task_id,
            self.tau_run_count,
            self.tau_success_count,
            self.tau_pass_rate,
            self.difficulty_bucket,
            self.similarity,
        )
        if any(value is not None for value in tau_fields) and any(
            value is None for value in tau_fields
        ):
            raise ValueError("tau difficulty provenance must be complete or absent")
        if (
            self.tau_run_count is not None
            and self.tau_success_count is not None
            and self.tau_success_count > self.tau_run_count
        ):
            raise ValueError("tau success count cannot exceed run count")
        return self

    @classmethod
    def from_domain(cls, value: CandidateRecord) -> Self:
        return cls(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="testset_candidate",
            candidate_id=value.candidate_id,
            source_id=value.source_id,
            duplicate_source_ids=value.duplicate_source_ids,
            cluster_id=value.cluster_id,
            flow=value.flow,
            subflow=value.subflow,
            semantic_group_id=value.semantic_group_id,
            label_frequency=value.label_frequency,
            long_tail=value.long_tail,
            label_conflict=value.label_conflict,
            tau_task_id=value.tau_task_id,
            tau_run_count=value.tau_run_count,
            tau_success_count=value.tau_success_count,
            tau_pass_rate=value.tau_pass_rate,
            difficulty_bucket=cast(DifficultyBucket | None, value.difficulty_bucket),
            similarity=value.similarity,
            retention_reasons=value.retention_reasons,
            executable=value.executable,
        )


class StateFunnelArtifact(ContractModel):
    source_tasks: StrictNonNegativeInt
    return_item_tasks: StrictNonNegativeInt
    source_trajectories: StrictNonNegativeInt
    return_item_trajectories: StrictNonNegativeInt

    @model_validator(mode="after")
    def _validate_counts(self) -> StateFunnelArtifact:
        if self.return_item_tasks > self.source_tasks:
            raise ValueError("filtered task count cannot exceed source task count")
        if self.return_item_trajectories > self.source_trajectories:
            raise ValueError("matched trajectory count cannot exceed source count")
        return self


class AbcdFunnelArtifact(ContractModel):
    source_conversations: StrictNonNegativeInt
    exact_product_defect: StrictNonNegativeInt
    dropped_empty: StrictNonNegativeInt
    dropped_misaligned: StrictNonNegativeInt
    dropped_invalid: StrictNonNegativeInt
    dropped_encoding: StrictNonNegativeInt
    dropped_duplicates: StrictNonNegativeInt
    scrubbed_unique: StrictNonNegativeInt
    clustered: StrictNonNegativeInt
    semantic_duplicates_removed: StrictNonNegativeInt
    candidate_pool: StrictNonNegativeInt
    candidate_cap_removed: StrictNonNegativeInt
    candidates: StrictNonNegativeInt

    @model_validator(mode="after")
    def _validate_counts(self) -> AbcdFunnelArtifact:
        if self.exact_product_defect > self.source_conversations:
            raise ValueError("exact-filter count cannot exceed source count")
        if self.clustered != self.scrubbed_unique:
            raise ValueError("every scrubbed record must have a cluster")
        if self.semantic_duplicates_removed + self.candidate_pool != self.clustered:
            raise ValueError("semantic-dedup funnel counts do not reconcile")
        if self.candidate_cap_removed + self.candidates != self.candidate_pool:
            raise ValueError("candidate-cap funnel counts do not reconcile")
        return self


class TauFunnelArtifact(ContractModel):
    source_tasks: StrictNonNegativeInt
    result_files: StrictNonNegativeInt
    trajectory_runs: StrictNonNegativeInt
    task_aggregates: StrictNonNegativeInt
    hard_tasks: StrictNonNegativeInt
    medium_tasks: StrictNonNegativeInt
    easy_tasks: StrictNonNegativeInt

    @model_validator(mode="after")
    def _validate_counts(self) -> TauFunnelArtifact:
        if self.task_aggregates > self.source_tasks:
            raise ValueError("task aggregate count cannot exceed source task count")
        if (
            self.hard_tasks + self.medium_tasks + self.easy_tasks
            != self.task_aggregates
        ):
            raise ValueError("difficulty buckets must partition task aggregates")
        return self


class MiningFunnelArtifact(VersionedRecord):
    """End-to-end candidate-mining funnel counts."""

    record_type: Literal["candidate_mining_funnel"]
    profile: Literal["fixture", "full"]
    state: StateFunnelArtifact
    abcd: AbcdFunnelArtifact
    tau: TauFunnelArtifact


class MiningConfigArtifact(ContractModel):
    candidate_count: StrictNonNegativeInt | None = None
    seed: StrictInt = 0


class ArtifactEntryArtifact(ContractModel):
    path: RelativeArtifactPath
    records: StrictNonNegativeInt
    bytes: StrictNonNegativeInt
    sha256: Sha256Digest


class ArtifactManifestArtifact(VersionedRecord):
    """Checksummed inventory for one atomically published artifact bundle."""

    record_type: Literal["candidate_artifact_manifest"]
    transformation_version: NonEmptyStr
    profile: Literal["fixture", "full"]
    seed: StrictInt
    mining_config: MiningConfigArtifact
    cluster_adapter_id: NonEmptyStr
    stratify_adapter_id: NonEmptyStr
    upstream_manifest_sha256: Sha256Digest
    input_sha256: Mapping[NonEmptyStr, Sha256Digest]
    parsed_input_digest_algorithm: NonEmptyStr
    parsed_input_sha256: Mapping[NonEmptyStr, Sha256Digest]
    source_commits: Mapping[NonEmptyStr, NonEmptyStr]
    artifacts: tuple[ArtifactEntryArtifact, ...]

    @model_validator(mode="after")
    def _validate_inventory(self) -> ArtifactManifestArtifact:
        if not self.input_sha256:
            raise ValueError("input checksum inventory cannot be empty")
        if not self.parsed_input_sha256:
            raise ValueError("parsed-input checksum inventory cannot be empty")
        if not self.source_commits:
            raise ValueError("source commit inventory cannot be empty")
        paths = tuple(entry.path for entry in self.artifacts)
        if not paths:
            raise ValueError("artifact inventory cannot be empty")
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self
