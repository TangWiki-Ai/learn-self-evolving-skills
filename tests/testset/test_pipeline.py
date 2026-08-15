from __future__ import annotations

import json
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from ses.contracts import artifact_json_bytes
from ses.testset import pipeline as pipeline_module
from ses.testset.artifacts import ArtifactManifestArtifact
from ses.testset.cluster import ClusterAssignment, ClusterItem
from ses.testset.pipeline import (
    MiningConfig,
    MiningInputs,
    SourceCountDriftError,
    mine_candidates,
    write_mining_bundle_atomic,
)
from ses.testset.stratify import (
    AbcdPairSimilarity,
    StratifyText,
    TauTaskMatch,
    TauTaskText,
)


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

    @property
    def semantic_duplicate_similarity(self) -> float:
        return 0.85

    def compare_abcd(
        self,
        conversations: tuple[StratifyText, ...],
        *,
        seed: int,
    ) -> tuple[AbcdPairSimilarity, ...]:
        del seed
        return tuple(
            AbcdPairSimilarity(
                source_id=left.source_id,
                duplicate_source_id=right.source_id,
                similarity=0.0,
            )
            for index, left in enumerate(conversations)
            for right in conversations[index + 1 :]
        )

    def match_tau(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[TauTaskMatch, ...]:
        del seed
        task_ids = {task.task_id for task in tau_tasks}
        assert task_ids == {"0", "1"}
        return tuple(
            TauTaskMatch(
                source_id=item.source_id,
                tau_task_id="0" if "refund" in item.text else "1",
                similarity=0.9,
            )
            for item in conversations
        )


class CollapsingFakeStratifyAdapter(FakeStratifyAdapter):
    @property
    def adapter_id(self) -> str:
        return "deterministic-collapsing-fake-stratify:v1"

    def compare_abcd(
        self,
        conversations: tuple[StratifyText, ...],
        *,
        seed: int,
    ) -> tuple[AbcdPairSimilarity, ...]:
        del seed
        similarities: list[AbcdPairSimilarity] = []
        for index, left in enumerate(conversations):
            for right in conversations[index + 1 :]:
                same_kind = ("refund" in left.text) == ("refund" in right.text)
                similarities.append(
                    AbcdPairSimilarity(
                        source_id=left.source_id,
                        duplicate_source_id=right.source_id,
                        similarity=(
                            0.95
                            if same_kind
                            and "unique" not in left.text
                            and "unique" not in right.text
                            else 0.0
                        ),
                    )
                )
        return tuple(similarities)


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


def snapshot_files(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def version_directories(output_dir: Path) -> set[str]:
    versions_dir = output_dir.parent / f".{output_dir.name}.versions"
    if not versions_dir.exists():
        return set()
    return {
        path.name
        for path in versions_dir.iterdir()
        if path.is_dir() and not path.is_symlink()
    }


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

    output_dir = tmp_path / "candidate-bundle"
    artifact_manifest = write_mining_bundle_atomic(bundle, output_dir)
    expected_files = {
        "artifact-manifest.json",
        "candidate-list.jsonl",
        "cluster-assignments.jsonl",
        "cluster-summaries.jsonl",
        "funnel-counts.json",
        "label-metrics.json",
        "scrubbed-abcd.jsonl",
        "tau2-difficulty.jsonl",
    }
    assert output_dir.is_symlink()
    assert {path.name for path in output_dir.iterdir()} == expected_files
    candidate_rows = [
        json.loads(line)
        for line in (output_dir / "candidate-list.jsonl").read_text().splitlines()
    ]
    forbidden = {"creator", "selection", "final", "gold", "shop_state"}
    assert all(not (set(row) & forbidden) for row in candidate_rows)
    assert all(row["executable"] is False for row in candidate_rows)
    assert all(row["schema_version"] == "v1alpha1" for row in candidate_rows)
    assert all(row["record_type"] == "testset_candidate" for row in candidate_rows)
    cluster_summaries = [
        json.loads(line)
        for line in (output_dir / "cluster-summaries.jsonl").read_text().splitlines()
    ]
    assert sum(row["member_count"] for row in cluster_summaries) == 2
    assert all(row["representative_samples"] for row in cluster_summaries)
    assert all(row["record_type"] == "cluster_summary" for row in cluster_summaries)
    cluster_assignments = [
        json.loads(line)
        for line in (output_dir / "cluster-assignments.jsonl").read_text().splitlines()
    ]
    assert all(row["confidence"] == 1.0 for row in cluster_assignments)
    label_metrics = json.loads((output_dir / "label-metrics.json").read_text())
    funnel_counts = json.loads((output_dir / "funnel-counts.json").read_text())
    assert label_metrics["record_type"] == "cluster_label_comparison_set"
    assert label_metrics["flow"]["label_name"] == "flow"
    assert label_metrics["subflow"]["label_name"] == "subflow"
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
    assert artifact_manifest.mining_config.candidate_count == 2
    assert artifact_manifest.mining_config.seed == 11
    manifest_payload = (output_dir / "artifact-manifest.json").read_bytes()
    round_tripped_manifest = ArtifactManifestArtifact.model_validate_json(
        manifest_payload
    )
    assert round_tripped_manifest == artifact_manifest
    assert manifest_payload == artifact_json_bytes(round_tripped_manifest) + b"\n"
    for entry in artifact_manifest.artifacts:
        counted_payload = (output_dir / entry.path).read_text()
        actual_records = (
            len(counted_payload.splitlines()) if entry.path.endswith(".jsonl") else 1
        )
        assert entry.records == actual_records
    for artifact in artifact_manifest.artifacts:
        payload = (output_dir / artifact.path).read_bytes()
        assert len(payload) == artifact.bytes
        assert sha256(payload).hexdigest() == artifact.sha256


def test_pipeline_artifacts_are_byte_stable(tmp_path: Path) -> None:
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    output_dir = tmp_path / "candidate-bundle"
    write_mining_bundle_atomic(bundle, output_dir)
    first = {path.name: path.read_bytes() for path in output_dir.iterdir()}
    first_target = os.readlink(output_dir)
    write_mining_bundle_atomic(bundle, output_dir)
    second = {path.name: path.read_bytes() for path in output_dir.iterdir()}

    assert first == second
    assert os.readlink(output_dir) == first_target


def test_bundle_staging_failure_leaves_published_bundle_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "published-bundle"
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, output_dir)
    before = snapshot_files(output_dir)
    before_target = os.readlink(output_dir)
    before_versions = version_directories(output_dir)

    original_atomic_write = pipeline_module._atomic_write
    write_count = 0

    def fail_during_staging(path: Path, payload: bytes) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 3:
            raise OSError("injected staging failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(pipeline_module, "_atomic_write", fail_during_staging)
    replacement_bundle = replace(
        bundle,
        seed=12,
        config=MiningConfig(candidate_count=2, seed=12),
    )

    with pytest.raises(OSError, match="injected staging failure"):
        write_mining_bundle_atomic(replacement_bundle, output_dir)

    assert snapshot_files(output_dir) == before
    assert os.path.lexists(output_dir)
    assert os.readlink(output_dir) == before_target
    assert version_directories(output_dir) == before_versions
    assert not tuple(tmp_path.glob(".published-bundle.staging-*"))
    assert not tuple(tmp_path.glob(".published-bundle.pointer-*"))
    assert not tuple(tmp_path.glob(".published-bundle.rollback-*"))


def test_bundle_checksum_failure_leaves_published_bundle_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "published-bundle"
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, output_dir)
    before = snapshot_files(output_dir)
    before_target = os.readlink(output_dir)
    before_versions = version_directories(output_dir)

    original_atomic_write = pipeline_module._atomic_write

    def corrupt_staged_candidate(path: Path, payload: bytes) -> None:
        if path.name == "candidate-list.jsonl":
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        original_atomic_write(path, payload)

    monkeypatch.setattr(
        pipeline_module,
        "_atomic_write",
        corrupt_staged_candidate,
    )

    with pytest.raises(OSError, match=r"checksum mismatch: candidate-list\.jsonl"):
        write_mining_bundle_atomic(bundle, output_dir)

    assert snapshot_files(output_dir) == before
    assert os.path.lexists(output_dir)
    assert os.readlink(output_dir) == before_target
    assert version_directories(output_dir) == before_versions
    assert not tuple(tmp_path.glob(".published-bundle.staging-*"))
    assert not tuple(tmp_path.glob(".published-bundle.pointer-*"))
    assert not tuple(tmp_path.glob(".published-bundle.rollback-*"))


def test_bundle_pointer_swap_failure_leaves_previous_pointer_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "published-bundle"
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, output_dir)
    before = snapshot_files(output_dir)
    before_target = os.readlink(output_dir)
    before_versions = version_directories(output_dir)
    original_replace = pipeline_module._replace_path

    def fail_pointer_swap(source: Path, destination: Path) -> None:
        if destination == output_dir and source.name == "bundle-pointer":
            assert os.path.lexists(output_dir)
            assert os.readlink(output_dir) == before_target
            raise OSError("injected pointer swap failure")
        original_replace(source, destination)

    monkeypatch.setattr(
        pipeline_module,
        "_replace_path",
        fail_pointer_swap,
    )

    with pytest.raises(OSError, match="injected pointer swap failure"):
        write_mining_bundle_atomic(
            replace(
                bundle,
                seed=12,
                config=MiningConfig(candidate_count=2, seed=12),
            ),
            output_dir,
        )

    assert snapshot_files(output_dir) == before
    assert os.path.lexists(output_dir)
    assert os.readlink(output_dir) == before_target
    assert version_directories(output_dir) == before_versions
    assert not tuple(tmp_path.glob(".published-bundle.staging-*"))
    assert not tuple(tmp_path.glob(".published-bundle.pointer-*"))
    assert not tuple(tmp_path.glob(".published-bundle.rollback-*"))


def test_bundle_replacement_switch_never_removes_stable_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "published-bundle"
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, output_dir)
    before_target = os.readlink(output_dir)
    original_replace = pipeline_module._replace_path
    pointer_visibility: list[tuple[bool, bool]] = []

    def observe_pointer_swap(source: Path, destination: Path) -> None:
        if destination != output_dir:
            original_replace(source, destination)
            return
        visible_before = os.path.lexists(output_dir)
        original_replace(source, destination)
        pointer_visibility.append((visible_before, os.path.lexists(output_dir)))

    monkeypatch.setattr(pipeline_module, "_replace_path", observe_pointer_swap)
    write_mining_bundle_atomic(
        replace(
            bundle,
            seed=12,
            config=MiningConfig(candidate_count=2, seed=12),
        ),
        output_dir,
    )

    assert pointer_visibility == [(True, True)]
    assert output_dir.is_symlink()
    assert os.readlink(output_dir) != before_target
    assert Path(output_dir.readlink()).parts[0] == ".published-bundle.versions"


def test_bundle_post_swap_fsync_failure_atomically_restores_previous_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "published-bundle"
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, output_dir)
    before = snapshot_files(output_dir)
    before_target = os.readlink(output_dir)
    before_versions = version_directories(output_dir)
    original_fsync = pipeline_module._fsync_directory

    def fail_after_visible_switch(directory: Path) -> None:
        if (
            directory == output_dir.parent
            and output_dir.is_symlink()
            and os.readlink(output_dir) != before_target
        ):
            raise OSError("injected post-swap parent fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(pipeline_module, "_fsync_directory", fail_after_visible_switch)

    with pytest.raises(OSError, match="post-swap parent fsync failure"):
        write_mining_bundle_atomic(
            replace(
                bundle,
                seed=12,
                config=MiningConfig(candidate_count=2, seed=12),
            ),
            output_dir,
        )

    assert os.path.lexists(output_dir)
    assert os.readlink(output_dir) == before_target
    assert snapshot_files(output_dir) == before
    assert version_directories(output_dir) == before_versions
    assert not tuple(tmp_path.glob(".published-bundle.staging-*"))
    assert not tuple(tmp_path.glob(".published-bundle.pointer-*"))
    assert not tuple(tmp_path.glob(".published-bundle.rollback-*"))


def test_bundle_reclaims_only_versions_older_than_previous(tmp_path: Path) -> None:
    output_dir = tmp_path / "published-bundle"
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
        config=MiningConfig(candidate_count=2, seed=11),
    )
    write_mining_bundle_atomic(bundle, output_dir)
    first_target = Path(os.readlink(output_dir)).name
    write_mining_bundle_atomic(
        replace(bundle, seed=12, config=MiningConfig(candidate_count=2, seed=12)),
        output_dir,
    )
    second_target = Path(os.readlink(output_dir)).name
    write_mining_bundle_atomic(
        replace(bundle, seed=13, config=MiningConfig(candidate_count=2, seed=13)),
        output_dir,
    )
    third_target = Path(os.readlink(output_dir)).name

    assert version_directories(output_dir) == {second_target, third_target}
    assert first_target not in version_directories(output_dir)


def test_bundle_rejects_legacy_physical_directory_without_mutation(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "legacy-bundle"
    output_dir.mkdir()
    (output_dir / "artifact-manifest.json").write_bytes(b"legacy manifest\n")
    before = snapshot_files(output_dir)
    bundle = mine_candidates(
        inputs(),
        cluster_adapter=FakeClusterAdapter(),
        stratify_adapter=FakeStratifyAdapter(),
    )

    with pytest.raises(OSError, match="cannot atomically replace legacy directory"):
        write_mining_bundle_atomic(bundle, output_dir)

    assert os.path.lexists(output_dir)
    assert not output_dir.is_symlink()
    assert snapshot_files(output_dir) == before
    assert not (tmp_path / ".legacy-bundle.versions").exists()
    assert not tuple(tmp_path.glob(".legacy-bundle.staging-*"))


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
