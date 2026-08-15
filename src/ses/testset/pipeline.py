"""Pure orchestration and deterministic audit artifacts for Issue #6."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

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
    ClusterAdapter,
    ClusterAssignment,
    ClusterItem,
    ClusterSummary,
    LabelComparison,
    assign_clusters,
    compare_cluster_labels,
    summarize_clusters,
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
    profile: Literal["fixture", "full"]
    state: StateFunnel
    abcd: AbcdFunnel
    tau: TauFunnel


@dataclass(frozen=True)
class MiningBundle:
    profile: Literal["fixture", "full"]
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
    cluster_summaries: tuple[ClusterSummary, ...]
    label_metrics: tuple[LabelComparison, ...]
    tau_difficulty: tuple[TauDifficulty, ...]
    candidates: tuple[CandidateRecord, ...]
    funnel: MiningFunnel


PARSED_INPUT_DIGEST_ALGORITHM = (
    "sha256(canonical-json-v1:ascii-escaped,sort-keys,separators=comma-colon,newline)"
)


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
    cluster_summaries = summarize_clusters(cluster_items, assignments)
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
        cluster_summaries=cluster_summaries,
        label_metrics=labels,
        tau_difficulty=tau_difficulty,
        candidates=candidates,
        funnel=funnel,
    )


def _artifact_payload(record: VersionedRecord) -> bytes:
    return artifact_json_bytes(record) + b"\n"


def _artifact_jsonl(records: Sequence[VersionedRecord]) -> bytes:
    return b"".join(_artifact_payload(record) for record in records)


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


def _validate_staged_bundle(
    staging_dir: Path,
    artifact_manifest: ArtifactManifestArtifact,
    manifest_payload: bytes,
) -> None:
    """Read back a complete staged bundle before making it visible."""

    expected_names = {
        *(entry.path for entry in artifact_manifest.artifacts),
        "artifact-manifest.json",
    }
    actual_names: set[str] = set()
    for path in staging_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise OSError(f"staged artifact is not a regular file: {path.name}")
        actual_names.add(path.name)
    if actual_names != expected_names:
        raise OSError(
            "staged artifact file set differs from manifest: "
            f"{sorted(actual_names)} != {sorted(expected_names)}"
        )

    for entry in artifact_manifest.artifacts:
        payload = (staging_dir / entry.path).read_bytes()
        if len(payload) != entry.bytes:
            raise OSError(f"staged artifact byte count mismatch: {entry.path}")
        if sha256(payload).hexdigest() != entry.sha256:
            raise OSError(f"staged artifact checksum mismatch: {entry.path}")

    staged_manifest_payload = (staging_dir / "artifact-manifest.json").read_bytes()
    if staged_manifest_payload != manifest_payload:
        raise OSError("staged artifact manifest changed after serialization")
    try:
        staged_manifest = ArtifactManifestArtifact.model_validate_json(
            staged_manifest_payload
        )
    except ValueError as exc:
        raise OSError("staged artifact manifest failed schema validation") from exc
    if staged_manifest != artifact_manifest:
        raise OSError("staged artifact manifest differs from producer model")


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)


_BUNDLE_VERSION_PREFIX = "bundle-"
_BUNDLE_VERSION_DIGEST_LENGTH = 64


def _fsync_directory(directory: Path) -> None:
    """Persist directory entry changes on filesystems that implement fsync."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _version_store(output_dir: Path) -> Path:
    return output_dir.parent / f".{output_dir.name}.versions"


def _is_managed_version_name(name: str) -> bool:
    digest = name.removeprefix(_BUNDLE_VERSION_PREFIX)
    return (
        name.startswith(_BUNDLE_VERSION_PREFIX)
        and len(digest) == _BUNDLE_VERSION_DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in digest)
    )


def _published_target(output_dir: Path, versions_dir: Path) -> str | None:
    """Return a validated relative target for a managed bundle pointer."""

    if not os.path.lexists(output_dir):
        return None
    if not output_dir.is_symlink():
        raise OSError(
            "artifact bundle destination must be a managed symlink; "
            f"cannot atomically replace legacy directory: {output_dir}"
        )
    target = os.readlink(output_dir)
    target_path = Path(target)
    if (
        target_path.is_absolute()
        or len(target_path.parts) != 2
        or target_path.parts[0] != versions_dir.name
        or not _is_managed_version_name(target_path.parts[1])
    ):
        raise OSError(f"artifact bundle pointer is not managed: {output_dir}")
    version_dir = output_dir.parent / target_path
    if version_dir.is_symlink() or not version_dir.is_dir():
        raise OSError(f"artifact bundle pointer target is invalid: {target}")
    return target


def _create_bundle_pointer(
    output_dir: Path, target: str, *, prefix: str
) -> tuple[Path, Path]:
    pointer_dir = Path(
        tempfile.mkdtemp(dir=output_dir.parent, prefix=f".{output_dir.name}.{prefix}-")
    )
    try:
        pointer_path = pointer_dir / "bundle-pointer"
        os.symlink(target, pointer_path)
        _fsync_directory(pointer_dir)
        return pointer_dir, pointer_path
    except BaseException:
        shutil.rmtree(pointer_dir, ignore_errors=True)
        raise


def _restore_bundle_pointer(
    output_dir: Path,
    *,
    failed_target: str,
    previous_target: str | None,
) -> None:
    """Undo a switch that became visible before its durability check failed."""

    if not output_dir.is_symlink() or os.readlink(output_dir) != failed_target:
        return
    if previous_target is None:
        output_dir.unlink()
        _fsync_directory(output_dir.parent)
        return

    rollback_dir: Path | None = None
    try:
        rollback_dir, rollback_pointer = _create_bundle_pointer(
            output_dir,
            previous_target,
            prefix="rollback",
        )
        _replace_path(rollback_pointer, output_dir)
        _fsync_directory(output_dir.parent)
    finally:
        if rollback_dir is not None:
            shutil.rmtree(rollback_dir, ignore_errors=True)


def _switch_bundle_pointer(
    output_dir: Path,
    *,
    target: str,
    previous_target: str | None,
) -> None:
    """Atomically switch one stable path between immutable bundle versions."""

    pointer_dir: Path | None = None
    try:
        pointer_dir, pointer_path = _create_bundle_pointer(
            output_dir,
            target,
            prefix="pointer",
        )
        try:
            _replace_path(pointer_path, output_dir)
            _fsync_directory(output_dir.parent)
        except BaseException:
            try:
                _restore_bundle_pointer(
                    output_dir,
                    failed_target=target,
                    previous_target=previous_target,
                )
            except BaseException as rollback_error:
                raise RuntimeError(
                    f"failed to restore previous artifact bundle at {output_dir}"
                ) from rollback_error
            raise
    finally:
        if pointer_dir is not None:
            shutil.rmtree(pointer_dir, ignore_errors=True)


def _collect_inactive_bundle_versions(
    versions_dir: Path, *, retain_targets: set[str]
) -> None:
    """Keep current and previous generations; ignore all unmanaged entries."""

    retain_names = {Path(target).name for target in retain_targets}
    removed = False
    for candidate in sorted(versions_dir.iterdir()):
        if (
            candidate.name in retain_names
            or not _is_managed_version_name(candidate.name)
            or candidate.is_symlink()
            or not candidate.is_dir()
        ):
            continue
        shutil.rmtree(candidate)
        removed = True
    if removed:
        _fsync_directory(versions_dir)


def _publish_staged_bundle(
    staging_dir: Path,
    output_dir: Path,
    artifact_manifest: ArtifactManifestArtifact,
    manifest_payload: bytes,
) -> None:
    """Install an immutable version and atomically switch its stable pointer."""

    if not output_dir.name:
        raise OSError("artifact bundle destination must have a directory name")
    versions_dir = _version_store(output_dir)
    previous_target = _published_target(output_dir, versions_dir)
    versions_dir_created = False
    if os.path.lexists(versions_dir):
        if versions_dir.is_symlink() or not versions_dir.is_dir():
            raise OSError(f"artifact bundle version store is invalid: {versions_dir}")
    else:
        versions_dir.mkdir()
        versions_dir_created = True
        try:
            _fsync_directory(output_dir.parent)
        except BaseException:
            versions_dir.rmdir()
            raise

    version_name = f"{_BUNDLE_VERSION_PREFIX}{sha256(manifest_payload).hexdigest()}"
    version_dir = versions_dir / version_name
    target = f"{versions_dir.name}/{version_name}"
    version_preexisted = os.path.lexists(version_dir)
    published = False
    try:
        if version_preexisted:
            if version_dir.is_symlink() or not version_dir.is_dir():
                raise OSError(f"artifact bundle version is invalid: {version_dir}")
            _validate_staged_bundle(
                version_dir,
                artifact_manifest,
                manifest_payload,
            )
            shutil.rmtree(staging_dir)
        else:
            _replace_path(staging_dir, version_dir)
            _fsync_directory(versions_dir)
            # The staging entry was removed from this directory by the rename.
            _fsync_directory(output_dir.parent)

        _switch_bundle_pointer(
            output_dir,
            target=target,
            previous_target=previous_target,
        )
        published = True
    finally:
        if (
            not published
            and not version_preexisted
            and (not output_dir.is_symlink() or os.readlink(output_dir) != target)
            and version_dir.is_dir()
            and not version_dir.is_symlink()
        ):
            shutil.rmtree(version_dir)
            _fsync_directory(versions_dir)
        if (
            not published
            and versions_dir_created
            and versions_dir.is_dir()
            and not any(versions_dir.iterdir())
        ):
            versions_dir.rmdir()
            _fsync_directory(output_dir.parent)

    retain_targets = {target}
    if previous_target is not None:
        retain_targets.add(previous_target)
    try:
        _collect_inactive_bundle_versions(
            versions_dir,
            retain_targets=retain_targets,
        )
    except OSError as exc:
        warnings.warn(
            f"could not reclaim inactive artifact bundle version: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )


def write_mining_bundle_atomic(
    bundle: MiningBundle, output_dir: Path
) -> ArtifactManifestArtifact:
    """Stage, validate, and publish a deterministic artifact directory."""

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # A physical directory cannot be replaced by a symlink without a missing-path
    # window using portable filesystem primitives. Reject it before staging bytes.
    _published_target(output_dir, _version_store(output_dir))
    scrubbed_records = tuple(
        ScrubbedConversationArtifact.from_domain(record)
        for record in bundle.scrub.records
    )
    cluster_assignments = tuple(
        ClusterAssignmentArtifact.from_domain(assignment)
        for assignment in bundle.cluster_assignments
    )
    cluster_summaries = tuple(
        ClusterSummaryArtifact.from_domain(summary)
        for summary in bundle.cluster_summaries
    )
    label_metrics = ClusterLabelComparisonSetArtifact.from_domain(bundle.label_metrics)
    tau_difficulty = tuple(
        TauDifficultyArtifact.from_domain(summary) for summary in bundle.tau_difficulty
    )
    candidates = tuple(
        CandidateArtifact.from_domain(candidate) for candidate in bundle.candidates
    )
    funnel = MiningFunnelArtifact(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="candidate_mining_funnel",
        profile=bundle.funnel.profile,
        state=StateFunnelArtifact(
            source_tasks=bundle.funnel.state.source_tasks,
            return_item_tasks=bundle.funnel.state.return_item_tasks,
            source_trajectories=bundle.funnel.state.source_trajectories,
            return_item_trajectories=bundle.funnel.state.return_item_trajectories,
        ),
        abcd=AbcdFunnelArtifact(
            source_conversations=bundle.funnel.abcd.source_conversations,
            exact_product_defect=bundle.funnel.abcd.exact_product_defect,
            dropped_empty=bundle.funnel.abcd.dropped_empty,
            dropped_misaligned=bundle.funnel.abcd.dropped_misaligned,
            dropped_invalid=bundle.funnel.abcd.dropped_invalid,
            dropped_encoding=bundle.funnel.abcd.dropped_encoding,
            dropped_duplicates=bundle.funnel.abcd.dropped_duplicates,
            scrubbed_unique=bundle.funnel.abcd.scrubbed_unique,
            clustered=bundle.funnel.abcd.clustered,
            semantic_duplicates_removed=(
                bundle.funnel.abcd.semantic_duplicates_removed
            ),
            candidate_pool=bundle.funnel.abcd.candidate_pool,
            candidate_cap_removed=bundle.funnel.abcd.candidate_cap_removed,
            candidates=bundle.funnel.abcd.candidates,
        ),
        tau=TauFunnelArtifact(
            source_tasks=bundle.funnel.tau.source_tasks,
            result_files=bundle.funnel.tau.result_files,
            trajectory_runs=bundle.funnel.tau.trajectory_runs,
            task_aggregates=bundle.funnel.tau.task_aggregates,
            hard_tasks=bundle.funnel.tau.hard_tasks,
            medium_tasks=bundle.funnel.tau.medium_tasks,
            easy_tasks=bundle.funnel.tau.easy_tasks,
        ),
    )
    payloads: dict[str, tuple[bytes, int]] = {
        "scrubbed-abcd.jsonl": (
            _artifact_jsonl(scrubbed_records),
            len(scrubbed_records),
        ),
        "cluster-assignments.jsonl": (
            _artifact_jsonl(cluster_assignments),
            len(cluster_assignments),
        ),
        "cluster-summaries.jsonl": (
            _artifact_jsonl(cluster_summaries),
            len(cluster_summaries),
        ),
        "label-metrics.json": (
            _artifact_payload(label_metrics),
            1,
        ),
        "tau2-difficulty.jsonl": (
            _artifact_jsonl(tau_difficulty),
            len(tau_difficulty),
        ),
        "candidate-list.jsonl": (
            _artifact_jsonl(candidates),
            len(candidates),
        ),
        "funnel-counts.json": (
            _artifact_payload(funnel),
            1,
        ),
    }
    staging_dir = Path(
        tempfile.mkdtemp(
            dir=output_dir.parent,
            prefix=f".{output_dir.name}.staging-",
        )
    )
    try:
        entries: list[ArtifactEntryArtifact] = []
        for filename in sorted(payloads):
            payload, record_count = payloads[filename]
            _atomic_write(staging_dir / filename, payload)
            entries.append(
                ArtifactEntryArtifact(
                    path=filename,
                    records=record_count,
                    bytes=len(payload),
                    sha256=sha256(payload).hexdigest(),
                )
            )
        artifact_manifest = ArtifactManifestArtifact(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="candidate_artifact_manifest",
            transformation_version=bundle.transformation_version,
            profile=bundle.profile,
            seed=bundle.seed,
            mining_config=MiningConfigArtifact(
                candidate_count=bundle.config.candidate_count,
                seed=bundle.config.seed,
            ),
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
        manifest_payload = _artifact_payload(artifact_manifest)
        _atomic_write(staging_dir / "artifact-manifest.json", manifest_payload)
        _validate_staged_bundle(staging_dir, artifact_manifest, manifest_payload)
        _fsync_directory(staging_dir)
        _publish_staged_bundle(
            staging_dir,
            output_dir,
            artifact_manifest,
            manifest_payload,
        )
        return artifact_manifest
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
