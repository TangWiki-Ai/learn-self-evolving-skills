from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from ses.contracts import SchemaVersion, VersionedRecord, artifact_json_bytes
from ses.testset.artifacts import (
    AbcdFunnelArtifact,
    ArtifactEntryArtifact,
    ArtifactManifestArtifact,
    CandidateArtifact,
    ClusterAssignmentArtifact,
    ClusterLabelComparisonSetArtifact,
    ClusterSummaryArtifact,
    MiningConfigArtifact,
    MiningFunnelArtifact,
    ScrubbedConversationArtifact,
    StateFunnelArtifact,
    TauDifficultyArtifact,
    TauFunnelArtifact,
)
from ses.testset.cluster import (
    ClusterAssignment,
    ClusterRepresentativeSample,
    ClusterSummary,
    ContingencyCell,
    LabelComparison,
)
from ses.testset.difficulty import PerAssetDifficulty, TauDifficulty
from ses.testset.scrub import DialogueTurn, ScrubbedConversation
from ses.testset.stratify import CandidateRecord

_DIGEST = "a" * 64


def _scrubbed_domain() -> ScrubbedConversation:
    original = (DialogueTurn(speaker="customer", text="My item is defective."),)
    delexed = (
        DialogueTurn(
            speaker="customer",
            text="My [product] is defective.",
            turn_count=1,
        ),
    )
    return ScrubbedConversation(
        source_id="abcd:commit:train:1",
        upstream_id="1",
        source_commit="6b8700ce67c6b37b062dd7a60abc76d7ef832a97",
        source_split="train",
        flow="product_defect",
        subflow="refund",
        original=original,
        delexed=delexed,
        normalized_text="My [product] is defective.",
        pair_sha256="b" * 64,
        dedup_sha256="c" * 64,
        duplicate_source_ids=("abcd:commit:train:2",),
        label_conflict=True,
    )


def _cluster_summary_domain() -> ClusterSummary:
    return ClusterSummary(
        cluster_id="cluster:abc",
        member_count=2,
        representative_selection_method=(
            "assignment_confidence_desc_nulls_last_then_item_id_asc"
        ),
        representative_samples=(
            ClusterRepresentativeSample(
                rank=1,
                item_id="abcd:commit:train:1",
                text="My item is defective.",
                source_kind="abcd_roleplay_benchmark",
                confidence=0.9,
                selection_reason=(
                    "rank=1;method="
                    "assignment_confidence_desc_nulls_last_then_item_id_asc"
                ),
            ),
        ),
    )


def _label_comparison(label_name: str) -> LabelComparison:
    return LabelComparison(
        label_name=label_name,
        evaluated_count=2,
        excluded_missing_label_count=0,
        true_label_count=1,
        cluster_count=1,
        contingency=(
            ContingencyCell(
                reference_label="product_defect" if label_name == "flow" else "refund",
                cluster_id="cluster:abc",
                count=2,
            ),
        ),
        adjusted_rand_index=1.0,
        normalized_mutual_info=1.0,
        homogeneity=1.0,
        completeness=1.0,
        v_measure=1.0,
        informative=False,
        reason="only one reference label is present",
    )


def _tau_domain() -> TauDifficulty:
    per_asset = tuple(
        PerAssetDifficulty(
            result_asset_id=f"result-{index}.json",
            success_count=3,
            run_count=4,
        )
        for index in range(4)
    )
    return TauDifficulty(
        source_id="tau2:commit:1",
        task_id="1",
        task_text="Resolve a defective item request.",
        run_count=16,
        success_count=12,
        pass_rate=0.75,
        pass_rate_decimal="0.75",
        mean_reward=0.75,
        difficulty_score=0.25,
        difficulty_bucket="easy",
        per_asset=per_asset,
        generation_commits=("generation-a",),
    )


def _candidate_domain() -> CandidateRecord:
    return CandidateRecord(
        candidate_id="candidate:abcd:commit:train:1",
        source_id="abcd:commit:train:1",
        duplicate_source_ids=("abcd:commit:train:2",),
        cluster_id="cluster:abc",
        flow="product_defect",
        subflow="refund",
        semantic_group_id="abcd-semantic:abc",
        label_frequency=2,
        long_tail=True,
        label_conflict=True,
        tau_task_id="1",
        tau_run_count=16,
        tau_success_count=12,
        tau_pass_rate="0.75",
        difficulty_bucket="easy",
        similarity=0.8,
        retention_reasons=("preserve_long_tail_label",),
    )


def _funnel_artifact() -> MiningFunnelArtifact:
    return MiningFunnelArtifact(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="candidate_mining_funnel",
        profile="fixture",
        state=StateFunnelArtifact(
            source_tasks=2,
            return_item_tasks=1,
            source_trajectories=1,
            return_item_trajectories=1,
        ),
        abcd=AbcdFunnelArtifact(
            source_conversations=4,
            exact_product_defect=4,
            dropped_empty=0,
            dropped_misaligned=0,
            dropped_invalid=0,
            dropped_encoding=0,
            dropped_duplicates=1,
            scrubbed_unique=3,
            clustered=3,
            semantic_duplicates_removed=1,
            candidate_pool=2,
            candidate_cap_removed=0,
            candidates=2,
        ),
        tau=TauFunnelArtifact(
            source_tasks=1,
            result_files=4,
            trajectory_runs=16,
            task_aggregates=1,
            hard_tasks=0,
            medium_tasks=0,
            easy_tasks=1,
        ),
    )


def _manifest_artifact() -> ArtifactManifestArtifact:
    return ArtifactManifestArtifact(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="candidate_artifact_manifest",
        transformation_version="candidate-mining-v2",
        profile="fixture",
        seed=0,
        mining_config=MiningConfigArtifact(candidate_count=2, seed=0),
        cluster_adapter_id="deterministic-fake-cluster:v1",
        stratify_adapter_id="deterministic-fake-stratify:v1",
        upstream_manifest_sha256=_DIGEST,
        input_sha256={"abcd.json": "b" * 64},
        parsed_input_digest_algorithm="sha256(canonical-json-v1)",
        parsed_input_sha256={"abcd_conversations": "c" * 64},
        source_commits={"abcd": "6b8700ce67c6b37b062dd7a60abc76d7ef832a97"},
        artifacts=(
            ArtifactEntryArtifact(
                path="candidate-list.jsonl",
                records=2,
                bytes=100,
                sha256="d" * 64,
            ),
        ),
    )


def test_domain_conversion_helpers_cover_persisted_mining_records() -> None:
    scrubbed = ScrubbedConversationArtifact.from_domain(_scrubbed_domain())
    assignment = ClusterAssignmentArtifact.from_domain(
        ClusterAssignment(
            item_id="abcd:commit:train:1",
            cluster_id="cluster:abc",
            confidence=0.9,
        )
    )
    summary = ClusterSummaryArtifact.from_domain(_cluster_summary_domain())
    comparisons = ClusterLabelComparisonSetArtifact.from_domain(
        (_label_comparison("flow"), _label_comparison("subflow"))
    )
    difficulty = TauDifficultyArtifact.from_domain(_tau_domain())
    candidate = CandidateArtifact.from_domain(_candidate_domain())

    assert scrubbed.schema_version == SchemaVersion.V1ALPHA1
    assert scrubbed.original[0].text == "My item is defective."
    assert assignment.record_type == "cluster_assignment"
    assert summary.representative_samples[0].rank == 1
    assert comparisons.flow.label_name == "flow"
    assert comparisons.subflow.label_name == "subflow"
    assert difficulty.run_count == 16
    assert candidate.executable is False


def _required_round_trip_records() -> tuple[VersionedRecord, ...]:
    return (
        CandidateArtifact.from_domain(_candidate_domain()),
        ClusterSummaryArtifact.from_domain(_cluster_summary_domain()),
        TauDifficultyArtifact.from_domain(_tau_domain()),
        _funnel_artifact(),
        _manifest_artifact(),
    )


@pytest.mark.parametrize(
    "record",
    _required_round_trip_records(),
    ids=["candidate", "cluster-summary", "tau-difficulty", "funnel", "manifest"],
)
def test_required_artifacts_are_frozen_extra_forbid_and_round_trip(
    record: VersionedRecord,
) -> None:
    payload = artifact_json_bytes(record)

    assert type(record).model_validate_json(payload) == record

    with pytest.raises(ValidationError, match="frozen_instance"):
        record.__setattr__("schema_version", "changed")

    wire = cast(dict[str, object], record.model_dump(mode="python", round_trip=True))
    wire["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        type(record).model_validate(wire)


def test_candidate_cannot_become_executable() -> None:
    candidate = CandidateArtifact.from_domain(_candidate_domain())

    with pytest.raises(ValidationError):
        candidate.model_copy(update={"executable": True})


def test_tau_difficulty_rejects_trajectory_level_record() -> None:
    wire = TauDifficultyArtifact.from_domain(_tau_domain()).model_dump(mode="python")
    wire["run_count"] = 1
    wire["success_count"] = 1
    wire["pass_rate"] = 1.0
    wire["pass_rate_decimal"] = "1"
    wire["mean_reward"] = 1.0
    wire["difficulty_score"] = 0.0

    with pytest.raises(ValidationError, match="requires 16 task runs"):
        TauDifficultyArtifact.model_validate(wire)
