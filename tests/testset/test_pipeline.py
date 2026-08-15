from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from ses.testset.cluster import ClusterAssignment, ClusterItem
from ses.testset.pipeline import (
    MiningConfig,
    MiningInputs,
    SourceCountDriftError,
    mine_candidates,
    write_mining_bundle_atomic,
)
from ses.testset.stratify import StratifyAnnotation, StratifyText, TauTaskText


class FakeClusterAdapter:
    @property
    def adapter_id(self) -> str:
        return "deterministic-fake-cluster:v1"

    def cluster(self, items: tuple[ClusterItem, ...]) -> tuple[ClusterAssignment, ...]:
        return tuple(
            ClusterAssignment(
                item_id=item.item_id,
                cluster_id="refund" if "refund" in item.text else "return",
                confidence=1.0,
            )
            for item in items
        )


class FakeStratifyAdapter:
    @property
    def adapter_id(self) -> str:
        return "deterministic-fake-stratify:v1"

    def annotate(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[StratifyAnnotation, ...]:
        del seed
        task_ids = {task.task_id for task in tau_tasks}
        assert task_ids == {"0", "1"}
        return tuple(
            StratifyAnnotation(
                source_id=item.source_id,
                semantic_group_id=f"semantic:{item.source_id}",
                tau_task_id="0" if "refund" in item.text else "1",
                similarity=0.9,
            )
            for item in conversations
        )


class CollapsingFakeStratifyAdapter(FakeStratifyAdapter):
    @property
    def adapter_id(self) -> str:
        return "deterministic-collapsing-fake-stratify:v1"

    def annotate(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[StratifyAnnotation, ...]:
        del tau_tasks, seed
        annotations: list[StratifyAnnotation] = []
        for item in conversations:
            is_refund = "refund" in item.text
            if "unique" in item.text:
                semantic_group_id = f"semantic:{item.source_id}"
            else:
                semantic_group_id = (
                    "semantic:refund" if is_refund else "semantic:return"
                )
            annotations.append(
                StratifyAnnotation(
                    source_id=item.source_id,
                    semantic_group_id=semantic_group_id,
                    tau_task_id="0" if is_refund else "1",
                    similarity=0.9,
                )
            )
        return tuple(annotations)


class MutatingFakeClusterAdapter(FakeClusterAdapter):
    def __init__(self, source_inputs: MiningInputs) -> None:
        self.source_inputs = source_inputs

    @property
    def adapter_id(self) -> str:
        return "mutating-fake-cluster:v1"

    def cluster(self, items: tuple[ClusterItem, ...]) -> tuple[ClusterAssignment, ...]:
        documents = self.source_inputs.tau_result_documents
        assert isinstance(documents, dict)
        first = documents["result-0.json"]
        assert isinstance(first, dict)
        simulations = first["simulations"]
        assert isinstance(simulations, list)
        first_run = simulations[0]
        assert isinstance(first_run, dict)
        first_run["reward_info"] = {"reward": 0.0}
        declared_hashes = self.source_inputs.input_sha256
        assert isinstance(declared_hashes, dict)
        declared_hashes.clear()
        declared_hashes["/private/tmp/leak"] = "not-a-digest"
        return super().cluster(items)


def abcd(convo_id: int, flow: str, subflow: str, text: str) -> dict[str, object]:
    return {
        "convo_id": convo_id,
        "source_split": "fixture",
        "scenario": {"flow": flow, "subflow": subflow},
        "original": [["customer", text]],
        "delexed": [{"speaker": "customer", "text": text, "turn_count": 1}],
    }


def tau_results() -> dict[str, object]:
    documents: dict[str, object] = {}
    for asset_index in range(4):
        simulations: list[dict[str, object]] = []
        for task_id in ("0", "1"):
            for trial in range(4):
                success = task_id == "0" or (asset_index == 0 and trial == 0)
                simulations.append(
                    {
                        "id": f"{asset_index}-{task_id}-{trial}",
                        "task_id": task_id,
                        "trial": trial,
                        "reward_info": {"reward": 1.0 if success else 0.0},
                    }
                )
        documents[f"result-{asset_index}.json"] = {
            "info": {"git_commit": f"generation-{asset_index}"},
            "simulations": simulations,
        }
    return documents


def inputs() -> MiningInputs:
    return MiningInputs(
        profile="fixture",
        state_tasks=(
            {"task_id": "return-a", "task_type": "return_item"},
            {"task_id": "other", "task_type": "exchange_item"},
        ),
        state_trajectories={"return-a": {"conversation": []}},
        abcd_conversations=(
            abcd(1, "product_defect", "return_size", "return wrong size"),
            abcd(2, "product_defect", "refund_status", "refund is missing"),
            abcd(3, "storewide_query", "timing", "when are you open"),
        ),
        tau_tasks=(
            {
                "id": "0",
                "user_scenario": {
                    "instructions": {"reason_for_call": "refund benchmark task"}
                },
            },
            {
                "id": "1",
                "user_scenario": {
                    "instructions": {"reason_for_call": "return benchmark task"}
                },
            },
        ),
        tau_result_documents=tau_results(),
        upstream_manifest_sha256=sha256(b"synthetic manifest").hexdigest(),
        input_sha256={"synthetic-inputs": sha256(b"synthetic inputs").hexdigest()},
    )


def test_fixture_pipeline_outputs_only_auditable_candidates(tmp_path: Path) -> None:
    source_inputs = inputs()
    before_results = json.dumps(source_inputs.tau_result_documents, sort_keys=True)
    bundle = mine_candidates(
        source_inputs,
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )

    assert bundle.funnel.state.source_tasks == 2
    assert bundle.funnel.state.return_item_tasks == 1
    assert bundle.funnel.state.return_item_trajectories == 1
    assert bundle.funnel.abcd.source_conversations == 3
    assert bundle.funnel.abcd.exact_product_defect == 2
    assert bundle.funnel.abcd.semantic_duplicates_removed == 0
    assert bundle.funnel.abcd.candidate_pool == 2
    assert bundle.funnel.abcd.candidate_cap_removed == 0
    assert bundle.funnel.abcd.candidates == 2
    assert bundle.funnel.tau.trajectory_runs == 32
    assert bundle.funnel.tau.task_aggregates == 2
    assert len(bundle.candidates) == 2
    assert (
        json.dumps(source_inputs.tau_result_documents, sort_keys=True) == before_results
    )

    artifact_manifest = write_mining_bundle_atomic(bundle, tmp_path)
    expected_files = {
        "artifact-manifest.json",
        "candidate-list.jsonl",
        "cluster-assignments.jsonl",
        "funnel-counts.json",
        "label-metrics.json",
        "scrubbed-abcd.jsonl",
        "tau2-difficulty.jsonl",
    }
    assert {path.name for path in tmp_path.iterdir()} == expected_files
    candidate_rows = [
        json.loads(line)
        for line in (tmp_path / "candidate-list.jsonl").read_text().splitlines()
    ]
    forbidden = {"creator", "selection", "final", "gold", "shop_state"}
    assert all(not (set(row) & forbidden) for row in candidate_rows)
    assert all(row["executable"] is False for row in candidate_rows)
    assert all(row["schema_version"] == "v1alpha1" for row in candidate_rows)
    assert all(row["record_type"] == "testset_candidate" for row in candidate_rows)
    label_metrics = json.loads((tmp_path / "label-metrics.json").read_text())
    funnel_counts = json.loads((tmp_path / "funnel-counts.json").read_text())
    assert label_metrics["record_type"] == "cluster_label_comparison_set"
    assert funnel_counts["record_type"] == "candidate_mining_funnel"
    assert artifact_manifest.upstream_manifest_sha256 == (
        source_inputs.upstream_manifest_sha256
    )
    assert artifact_manifest.input_sha256 == source_inputs.input_sha256
    assert artifact_manifest.parsed_input_sha256 == bundle.parsed_input_sha256
    assert "ascii-escaped" in artifact_manifest.parsed_input_digest_algorithm
    assert set(artifact_manifest.parsed_input_sha256) == {
        "abcd_conversations",
        "state_tasks",
        "state_trajectories",
        "tau2_result_documents",
        "tau2_tasks",
    }
    assert artifact_manifest.mining_config == MiningConfig(candidate_count=2, seed=11)
    for artifact in artifact_manifest.artifacts:
        payload = (tmp_path / artifact.path).read_bytes()
        assert len(payload) == artifact.bytes
        assert sha256(payload).hexdigest() == artifact.sha256


def test_pipeline_artifacts_are_byte_stable(tmp_path: Path) -> None:
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, tmp_path)
    first = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    write_mining_bundle_atomic(bundle, tmp_path)
    second = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    assert first == second


def test_pipeline_funnel_accounts_for_semantic_dedup_and_candidate_cap() -> None:
    source_inputs = replace(
        inputs(),
        abcd_conversations=(
            abcd(10, "product_defect", "return_size", "return alpha"),
            abcd(11, "product_defect", "return_size", "return beta"),
            abcd(20, "product_defect", "refund_status", "refund alpha"),
            abcd(21, "product_defect", "refund_status", "refund beta"),
            abcd(22, "product_defect", "refund_status", "refund unique"),
        ),
    )

    bundle = mine_candidates(
        source_inputs,
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=CollapsingFakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2),
    )

    funnel = bundle.funnel.abcd
    assert funnel.scrubbed_unique == 5
    assert funnel.semantic_duplicates_removed == 2
    assert funnel.candidate_pool == 3
    assert funnel.candidate_cap_removed == 1
    assert funnel.candidates == 2
    assert (
        funnel.scrubbed_unique
        == funnel.semantic_duplicates_removed + funnel.candidate_pool
    )
    assert funnel.candidate_pool == funnel.candidate_cap_removed + funnel.candidates


def test_pipeline_binds_provenance_to_parsed_input_content() -> None:
    original = inputs()
    changed = replace(
        original,
        abcd_conversations=(
            abcd(1, "product_defect", "return_size", "return changed size"),
            *original.abcd_conversations[1:],
        ),
    )

    first = mine_candidates(
        original,
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
    )
    second = mine_candidates(
        changed,
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
    )

    assert first.input_sha256 == second.input_sha256
    assert (
        first.parsed_input_sha256["abcd_conversations"]
        != (second.parsed_input_sha256["abcd_conversations"])
    )


def test_pipeline_rejects_machine_local_input_digest_paths() -> None:
    source_inputs = replace(
        inputs(),
        input_sha256={"/private/tmp/source.json": sha256(b"source").hexdigest()},
    )

    with pytest.raises(SourceCountDriftError, match="safe relative POSIX"):
        mine_candidates(
            source_inputs,
            cluster_adapter=FakeClusterAdapter(),
            stratify_adapter=FakeStratifyAdapter(),
        )


def test_pipeline_snapshots_mutable_inputs_before_calling_adapters() -> None:
    source_inputs = inputs()

    bundle = mine_candidates(
        source_inputs,
        cluster_adapter=MutatingFakeClusterAdapter(source_inputs),
        stratify_adapter=FakeStratifyAdapter(),
    )

    task_zero = next(item for item in bundle.tau_difficulty if item.task_id == "0")
    assert task_zero.success_count == 16
    assert bundle.input_sha256 == {
        "synthetic-inputs": sha256(b"synthetic inputs").hexdigest()
    }
    assert "/private/tmp/leak" not in bundle.input_sha256


def test_pipeline_rejects_windows_drive_input_digest_paths() -> None:
    source_inputs = replace(
        inputs(),
        input_sha256={"C:/Users/alice/source.json": sha256(b"source").hexdigest()},
    )

    with pytest.raises(SourceCountDriftError, match="safe relative POSIX"):
        mine_candidates(
            source_inputs,
            cluster_adapter=FakeClusterAdapter(),
            stratify_adapter=FakeStratifyAdapter(),
        )


def test_pipeline_routes_escaped_invalid_unicode_through_scrub_funnel() -> None:
    source_inputs = replace(
        inputs(),
        abcd_conversations=(
            *inputs().abcd_conversations,
            abcd(
                4,
                "product_defect",
                "return_size",
                "broken surrogate \ud800",
            ),
        ),
    )

    bundle = mine_candidates(
        source_inputs,
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
    )

    assert bundle.funnel.abcd.dropped_encoding == 1
    assert bundle.funnel.abcd.scrubbed_unique == 2
