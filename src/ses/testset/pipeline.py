"""Pure orchestration and deterministic audit artifacts for Issue #6."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from ses.testset.cluster import (
    ClusterAdapter,
    ClusterAssignment,
    ClusterItem,
    LabelComparison,
    assign_clusters,
    compare_cluster_labels,
)
from ses.testset.difficulty import TauDifficulty, aggregate_tau_difficulty
from ses.testset.manifest import TRANSFORMATION_VERSION
from ses.testset.scrub import ScrubResult, scrub_abcd
from ses.testset.sources import (
    ABCD_COMMIT,
    STATE_BENCH_COMMIT,
    TAU2_COMMIT,
    filter_abcd_product_defect,
    filter_state_return_items,
    match_state_trajectories,
)
from ses.testset.stratify import CandidateRecord, StratifyAdapter, stratify_candidates


class SourceCountDriftError(ValueError):
    """Pinned source counts changed before candidate mining began."""


@dataclass(frozen=True)
class MiningInputs:
    profile: Literal["fixture", "full"]
    state_tasks: Sequence[Mapping[str, object]]
    state_trajectories: Mapping[str, Mapping[str, object]]
    abcd_conversations: Sequence[Mapping[str, object]]
    tau_tasks: Sequence[Mapping[str, object]]
    tau_result_documents: Mapping[str, object]
    upstream_manifest_sha256: str
    input_sha256: Mapping[str, str]


@dataclass(frozen=True)
class MiningConfig:
    candidate_count: int | None = None
    seed: int = 0


@dataclass(frozen=True)
class MiningExpectedCounts:
    state_tasks: int
    state_return_item_tasks: int
    state_trajectories: int
    state_return_item_trajectories: int
    abcd_conversations: int
    abcd_product_defect: int
    tau_tasks: int
    tau_result_files: int
    tau_trajectory_runs: int


@dataclass(frozen=True)
class StateFunnel:
    source_tasks: int
    return_item_tasks: int
    source_trajectories: int
    return_item_trajectories: int


@dataclass(frozen=True)
class AbcdFunnel:
    source_conversations: int
    exact_product_defect: int
    dropped_empty: int
    dropped_misaligned: int
    dropped_invalid: int
    dropped_encoding: int
    dropped_duplicates: int
    scrubbed_unique: int
    clustered: int
    semantic_duplicates_removed: int
    candidate_pool: int
    candidate_cap_removed: int
    candidates: int


@dataclass(frozen=True)
class TauFunnel:
    source_tasks: int
    result_files: int
    trajectory_runs: int
    task_aggregates: int
    hard_tasks: int
    medium_tasks: int
    easy_tasks: int


@dataclass(frozen=True)
class MiningFunnel:
    profile: str
    state: StateFunnel
    abcd: AbcdFunnel
    tau: TauFunnel


@dataclass(frozen=True)
class MiningBundle:
    profile: str
    transformation_version: str
    seed: int
    config: MiningConfig
    cluster_adapter_id: str
    stratify_adapter_id: str
    upstream_manifest_sha256: str
    input_sha256: Mapping[str, str]
    parsed_input_sha256: Mapping[str, str]
    scrub: ScrubResult
    cluster_assignments: tuple[ClusterAssignment, ...]
    label_metrics: tuple[LabelComparison, ...]
    tau_difficulty: tuple[TauDifficulty, ...]
    candidates: tuple[CandidateRecord, ...]
    funnel: MiningFunnel


@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    records: int
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: str
    record_type: str
    transformation_version: str
    profile: str
    seed: int
    mining_config: MiningConfig
    cluster_adapter_id: str
    stratify_adapter_id: str
    upstream_manifest_sha256: str
    input_sha256: Mapping[str, str]
    parsed_input_digest_algorithm: str
    parsed_input_sha256: Mapping[str, str]
    source_commits: Mapping[str, str]
    artifacts: tuple[ArtifactEntry, ...]


PARSED_INPUT_DIGEST_ALGORITHM = (
    "sha256(canonical-json-v1:ascii-escaped,sort-keys,separators=comma-colon,newline)"
)


def _canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourceCountDriftError("mining input is not strict JSON") from exc
    return (encoded + "\n").encode("utf-8")


def _canonical_input_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourceCountDriftError("mining input is not strict JSON") from exc
    return (encoded + "\n").encode("ascii")


def _parsed_input_digests(inputs: MiningInputs) -> dict[str, str]:
    documents: dict[str, object] = {
        "state_tasks": list(inputs.state_tasks),
        "state_trajectories": dict(inputs.state_trajectories),
        "abcd_conversations": list(inputs.abcd_conversations),
        "tau2_tasks": list(inputs.tau_tasks),
        "tau2_result_documents": dict(inputs.tau_result_documents),
    }
    return {
        name: sha256(_canonical_input_json(document)).hexdigest()
        for name, document in sorted(documents.items())
    }


def _snapshot_mining_inputs(inputs: MiningInputs) -> MiningInputs:
    document = {
        "profile": inputs.profile,
        "state_tasks": list(inputs.state_tasks),
        "state_trajectories": dict(inputs.state_trajectories),
        "abcd_conversations": list(inputs.abcd_conversations),
        "tau_tasks": list(inputs.tau_tasks),
        "tau_result_documents": dict(inputs.tau_result_documents),
        "upstream_manifest_sha256": inputs.upstream_manifest_sha256,
        "input_sha256": dict(inputs.input_sha256),
    }
    snapshot = cast(
        dict[str, object],
        json.loads(_canonical_input_json(document).decode("ascii")),
    )
    return MiningInputs(
        profile=cast(Literal["fixture", "full"], snapshot["profile"]),
        state_tasks=cast(Sequence[Mapping[str, object]], snapshot["state_tasks"]),
        state_trajectories=cast(
            Mapping[str, Mapping[str, object]], snapshot["state_trajectories"]
        ),
        abcd_conversations=cast(
            Sequence[Mapping[str, object]], snapshot["abcd_conversations"]
        ),
        tau_tasks=cast(Sequence[Mapping[str, object]], snapshot["tau_tasks"]),
        tau_result_documents=cast(
            Mapping[str, object], snapshot["tau_result_documents"]
        ),
        upstream_manifest_sha256=cast(str, snapshot["upstream_manifest_sha256"]),
        input_sha256=cast(Mapping[str, str], snapshot["input_sha256"]),
    )


def _tau_run_count(documents: Mapping[str, object]) -> int:
    count = 0
    for name, document in documents.items():
        if not isinstance(document, Mapping):
            raise SourceCountDriftError(f"tau2 result {name} is not an object")
        simulations = document.get("simulations")
        if not isinstance(simulations, Sequence) or isinstance(
            simulations, (str, bytes, bytearray)
        ):
            raise SourceCountDriftError(f"tau2 result {name} has no simulations list")
        count += len(simulations)
    return count


def _validate_input_provenance(inputs: MiningInputs) -> None:
    digests = {
        "upstream manifest": inputs.upstream_manifest_sha256,
        **{f"input {name}": digest for name, digest in inputs.input_sha256.items()},
    }
    if not inputs.input_sha256:
        raise SourceCountDriftError("verified input SHA256 mapping is empty")
    if inputs.profile not in {"fixture", "full"}:
        raise SourceCountDriftError(f"unknown mining profile: {inputs.profile}")
    for asset_id in inputs.input_sha256:
        if not isinstance(asset_id, str):
            raise SourceCountDriftError("input SHA256 key must be a string")
        path = PurePosixPath(asset_id)
        if (
            asset_id in {"", "."}
            or "\\" in asset_id
            or path.is_absolute()
            or bool(PureWindowsPath(asset_id).drive)
            or ".." in path.parts
            or asset_id != path.as_posix()
        ):
            raise SourceCountDriftError(
                "input SHA256 key must be a safe relative POSIX path or asset ID"
            )
    for name, digest in digests.items():
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise SourceCountDriftError(f"{name} has no lowercase SHA256")


def _validate_expected_counts(
    inputs: MiningInputs,
    return_tasks: Sequence[Mapping[str, object]],
    return_trajectories: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
    product_defect: Sequence[Mapping[str, object]],
    trajectory_run_count: int,
    expected: MiningExpectedCounts,
) -> None:
    actual = {
        "state_tasks": len(inputs.state_tasks),
        "state_return_item_tasks": len(return_tasks),
        "state_trajectories": len(inputs.state_trajectories),
        "state_return_item_trajectories": len(return_trajectories),
        "abcd_conversations": len(inputs.abcd_conversations),
        "abcd_product_defect": len(product_defect),
        "tau_tasks": len(inputs.tau_tasks),
        "tau_result_files": len(inputs.tau_result_documents),
        "tau_trajectory_runs": trajectory_run_count,
    }
    for field, expected_value in asdict(expected).items():
        actual_value = actual[field]
        if actual_value != expected_value:
            raise SourceCountDriftError(
                f"{field} count drift: {actual_value} != {expected_value}"
            )


def mine_candidates(
    inputs: MiningInputs,
    *,
    cluster_adapter: ClusterAdapter,
    stratify_adapter: StratifyAdapter,
    config: MiningConfig | None = None,
    expected_counts: MiningExpectedCounts | None = None,
) -> MiningBundle:
    """Mine candidate records without creating executable cases or test splits."""

    active_config = config or MiningConfig()
    _validate_input_provenance(inputs)
    inputs = _snapshot_mining_inputs(inputs)
    parsed_input_sha256 = _parsed_input_digests(inputs)
    return_tasks = filter_state_return_items(inputs.state_tasks)
    return_trajectories = match_state_trajectories(
        return_tasks, inputs.state_trajectories
    )
    product_defect = filter_abcd_product_defect(inputs.abcd_conversations)
    trajectory_run_count = _tau_run_count(inputs.tau_result_documents)
    if expected_counts is not None:
        _validate_expected_counts(
            inputs,
            return_tasks,
            return_trajectories,
            product_defect,
            trajectory_run_count,
            expected_counts,
        )

    scrub = scrub_abcd(product_defect)
    cluster_items = tuple(
        ClusterItem(
            item_id=record.source_id,
            text=record.normalized_text,
            source_kind="abcd_roleplay_benchmark",
        )
        for record in scrub.records
    )
    assignments = assign_clusters(cluster_items, cluster_adapter)
    labels = (
        compare_cluster_labels(
            assignments,
            {record.source_id: record.flow for record in scrub.records},
            label_name="flow",
        ),
        compare_cluster_labels(
            assignments,
            {record.source_id: record.subflow for record in scrub.records},
            label_name="subflow",
        ),
    )
    tau_difficulty = aggregate_tau_difficulty(
        inputs.tau_tasks,
        inputs.tau_result_documents,
    )
    stratify = stratify_candidates(
        scrub.records,
        assignments,
        tau_difficulty,
        adapter=stratify_adapter,
        target_count=active_config.candidate_count,
        seed=active_config.seed,
    )
    candidates = stratify.candidates
    bucket_counts = {
        bucket: sum(summary.difficulty_bucket == bucket for summary in tau_difficulty)
        for bucket in ("hard", "medium", "easy")
    }
    funnel = MiningFunnel(
        profile=inputs.profile,
        state=StateFunnel(
            source_tasks=len(inputs.state_tasks),
            return_item_tasks=len(return_tasks),
            source_trajectories=len(inputs.state_trajectories),
            return_item_trajectories=len(return_trajectories),
        ),
        abcd=AbcdFunnel(
            source_conversations=len(inputs.abcd_conversations),
            exact_product_defect=len(product_defect),
            dropped_empty=scrub.funnel.dropped_empty,
            dropped_misaligned=scrub.funnel.dropped_misaligned,
            dropped_invalid=scrub.funnel.dropped_invalid,
            dropped_encoding=scrub.funnel.dropped_encoding,
            dropped_duplicates=scrub.funnel.dropped_duplicates,
            scrubbed_unique=len(scrub.records),
            clustered=len(assignments),
            semantic_duplicates_removed=(stratify.funnel.semantic_duplicates_removed),
            candidate_pool=stratify.funnel.candidate_pool,
            candidate_cap_removed=stratify.funnel.candidate_cap_removed,
            candidates=len(candidates),
        ),
        tau=TauFunnel(
            source_tasks=len(inputs.tau_tasks),
            result_files=len(inputs.tau_result_documents),
            trajectory_runs=trajectory_run_count,
            task_aggregates=len(tau_difficulty),
            hard_tasks=bucket_counts["hard"],
            medium_tasks=bucket_counts["medium"],
            easy_tasks=bucket_counts["easy"],
        ),
    )
    return MiningBundle(
        profile=inputs.profile,
        transformation_version=TRANSFORMATION_VERSION,
        seed=active_config.seed,
        config=active_config,
        cluster_adapter_id=cluster_adapter.adapter_id,
        stratify_adapter_id=stratify_adapter.adapter_id,
        upstream_manifest_sha256=inputs.upstream_manifest_sha256,
        input_sha256=dict(sorted(inputs.input_sha256.items())),
        parsed_input_sha256=parsed_input_sha256,
        scrub=scrub,
        cluster_assignments=assignments,
        label_metrics=labels,
        tau_difficulty=tau_difficulty,
        candidates=candidates,
        funnel=funnel,
    )


def _canonical_jsonl(values: Sequence[object]) -> bytes:
    return b"".join(_canonical_json(value) for value in values)


def _persistent_record(
    record_type: str, fields: Mapping[str, object]
) -> dict[str, object]:
    return {
        "schema_version": "v1alpha1",
        "record_type": record_type,
        **fields,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_mining_bundle_atomic(
    bundle: MiningBundle, output_dir: Path
) -> ArtifactManifest:
    """Write deterministic artifacts and publish their manifest last."""

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, tuple[bytes, int]] = {
        "scrubbed-abcd.jsonl": (
            _canonical_jsonl(
                [
                    _persistent_record("scrubbed_abcd_conversation", asdict(record))
                    for record in bundle.scrub.records
                ]
            ),
            len(bundle.scrub.records),
        ),
        "cluster-assignments.jsonl": (
            _canonical_jsonl(
                [
                    _persistent_record("cluster_assignment", asdict(assignment))
                    for assignment in bundle.cluster_assignments
                ]
            ),
            len(bundle.cluster_assignments),
        ),
        "label-metrics.json": (
            _canonical_json(
                _persistent_record(
                    "cluster_label_comparison_set",
                    {
                        comparison.label_name: asdict(comparison)
                        for comparison in bundle.label_metrics
                    },
                )
            ),
            len(bundle.label_metrics),
        ),
        "tau2-difficulty.jsonl": (
            _canonical_jsonl(
                [
                    _persistent_record("tau2_task_difficulty", asdict(summary))
                    for summary in bundle.tau_difficulty
                ]
            ),
            len(bundle.tau_difficulty),
        ),
        "candidate-list.jsonl": (
            _canonical_jsonl(
                [
                    _persistent_record("testset_candidate", asdict(candidate))
                    for candidate in bundle.candidates
                ]
            ),
            len(bundle.candidates),
        ),
        "funnel-counts.json": (
            _canonical_json(
                _persistent_record("candidate_mining_funnel", asdict(bundle.funnel))
            ),
            1,
        ),
    }
    entries: list[ArtifactEntry] = []
    for filename in sorted(payloads):
        payload, record_count = payloads[filename]
        _atomic_write(output_dir / filename, payload)
        entries.append(
            ArtifactEntry(
                path=filename,
                records=record_count,
                bytes=len(payload),
                sha256=sha256(payload).hexdigest(),
            )
        )
    artifact_manifest = ArtifactManifest(
        schema_version="v1alpha1",
        record_type="candidate_artifact_manifest",
        transformation_version=bundle.transformation_version,
        profile=bundle.profile,
        seed=bundle.seed,
        mining_config=bundle.config,
        cluster_adapter_id=bundle.cluster_adapter_id,
        stratify_adapter_id=bundle.stratify_adapter_id,
        upstream_manifest_sha256=bundle.upstream_manifest_sha256,
        input_sha256=bundle.input_sha256,
        parsed_input_digest_algorithm=PARSED_INPUT_DIGEST_ALGORITHM,
        parsed_input_sha256=bundle.parsed_input_sha256,
        source_commits={
            "state_bench": STATE_BENCH_COMMIT,
            "abcd": ABCD_COMMIT,
            "tau2": TAU2_COMMIT,
        },
        artifacts=tuple(entries),
    )
    _atomic_write(
        output_dir / "artifact-manifest.json",
        _canonical_json(asdict(artifact_manifest)),
    )
    return artifact_manifest
