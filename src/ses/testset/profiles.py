"""Load audited fixture or full profiles into the pure mining pipeline."""

from __future__ import annotations

import json
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import cast

from ses.testset.acquisition import acquire_full_manifest
from ses.testset.manifest import (
    AssetSpec,
    FileSpec,
    SourceSpec,
    UpstreamManifest,
    verify_manifest_files,
)
from ses.testset.pipeline import MiningExpectedCounts, MiningInputs
from ses.testset.sources import (
    SourceShapeError,
    flatten_abcd_document,
    load_json_document,
)

STATE_ARCHIVE_PREFIX = "STATE-Bench-5644b1838d96bc4483da29642d058ecaa6f80f7f"
STATE_TASK_DIRECTORY = "state_bench/domains/customer_support/tasks"
STATE_TRAJECTORY_DIRECTORY = "datasets/train_task_trajectories/customer_support"


def expected_counts_for_profile(profile: str) -> MiningExpectedCounts:
    if profile == "fixture":
        return MiningExpectedCounts(
            state_tasks=3,
            state_return_item_tasks=2,
            state_trajectories=1,
            state_return_item_trajectories=1,
            abcd_conversations=3,
            abcd_product_defect=2,
            tau_tasks=2,
            tau_result_files=4,
            tau_trajectory_runs=32,
        )
    if profile == "full":
        return MiningExpectedCounts(
            state_tasks=150,
            state_return_item_tasks=33,
            state_trajectories=100,
            state_return_item_trajectories=21,
            abcd_conversations=10_042,
            abcd_product_defect=1_070,
            tau_tasks=114,
            tau_result_files=4,
            tau_trajectory_runs=1_824,
        )
    raise ValueError(f"unknown profile: {profile}")


def _fixture_path(root: Path, file: FileSpec) -> Path:
    return root / file.path


def _asset_path(root: Path, asset: AssetSpec) -> Path:
    return root / asset.destination


def _only_file(source: SourceSpec, role: str) -> FileSpec:
    matches = [file for file in source.fixture_files if file.role == role]
    if len(matches) != 1:
        raise SourceShapeError(
            f"fixture source {source.name} expected one {role}, got {len(matches)}"
        )
    return matches[0]


def _only_asset(source: SourceSpec, role: str) -> AssetSpec:
    matches = [asset for asset in source.assets if asset.role == role]
    if len(matches) != 1:
        raise SourceShapeError(
            f"full source {source.name} expected one {role}, got {len(matches)}"
        )
    return matches[0]


def _object_sequence(value: object, context: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SourceShapeError(f"{context} must be a JSON list")
    records: list[Mapping[str, object]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SourceShapeError(f"{context} contains a non-object record")
        records.append(cast(Mapping[str, object], raw))
    return tuple(records)


def _object_mapping(value: object, context: str) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise SourceShapeError(f"{context} must be a JSON object")
    records: dict[str, Mapping[str, object]] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            raise SourceShapeError(f"{context} contains an invalid entry")
        records[key] = cast(Mapping[str, object], raw)
    return records


def _load_state_archive(
    archive_path: Path,
) -> tuple[tuple[Mapping[str, object], ...], dict[str, Mapping[str, object]]]:
    tasks: dict[str, Mapping[str, object]] = {}
    trajectories: dict[str, Mapping[str, object]] = {}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = PurePosixPath(member.name)
                if not path.parts or path.parts[0] != STATE_ARCHIVE_PREFIX:
                    raise SourceShapeError("STATE archive root prefix drifted")
                relative = PurePosixPath(*path.parts[1:])
                parent = relative.parent.as_posix()
                if relative.suffix != ".json" or parent not in {
                    STATE_TASK_DIRECTORY,
                    STATE_TRAJECTORY_DIRECTORY,
                }:
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise SourceShapeError(
                        f"cannot read STATE archive member {member.name}"
                    )
                raw = json.loads(stream.read().decode("utf-8", errors="strict"))
                if not isinstance(raw, Mapping):
                    raise SourceShapeError(
                        f"STATE member {member.name} is not an object"
                    )
                record = cast(Mapping[str, object], raw)
                if parent == STATE_TASK_DIRECTORY:
                    task_id = record.get("task_id")
                    if task_id != relative.stem:
                        raise SourceShapeError(
                            f"STATE task ID does not match filename: {member.name}"
                        )
                    tasks[relative.stem] = record
                else:
                    trajectories[relative.stem] = record
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceShapeError(f"cannot parse STATE archive {archive_path}") from exc
    return tuple(tasks[key] for key in sorted(tasks)), {
        key: trajectories[key] for key in sorted(trajectories)
    }


def _load_fixture(manifest: UpstreamManifest, root: Path) -> MiningInputs:
    verify_manifest_files(manifest, root)
    state = manifest.source("state_bench")
    abcd = manifest.source("abcd")
    tau2 = manifest.source("tau2")
    state_tasks = _object_sequence(
        load_json_document(_fixture_path(root, _only_file(state, "state_tasks"))),
        "STATE fixture tasks",
    )
    state_trajectories = _object_mapping(
        load_json_document(
            _fixture_path(root, _only_file(state, "state_trajectories"))
        ),
        "STATE fixture trajectories",
    )
    abcd_document = load_json_document(
        _fixture_path(root, _only_file(abcd, "abcd_conversations"))
    )
    abcd_conversations = flatten_abcd_document(abcd_document, profile="fixture")
    tau_tasks = _object_sequence(
        load_json_document(_fixture_path(root, _only_file(tau2, "tau2_tasks"))),
        "tau2 fixture tasks",
    )
    result_documents: dict[str, object] = {}
    for file in tau2.fixture_files:
        if file.role != "tau2_result":
            continue
        if file.asset_id is None:
            raise SourceShapeError(f"tau2 fixture result {file.path} has no asset_id")
        if file.asset_id in result_documents:
            raise SourceShapeError(f"duplicate tau2 fixture asset_id: {file.asset_id}")
        result_documents[file.asset_id] = load_json_document(_fixture_path(root, file))
    return MiningInputs(
        profile="fixture",
        state_tasks=state_tasks,
        state_trajectories=state_trajectories,
        abcd_conversations=abcd_conversations,
        tau_tasks=tau_tasks,
        tau_result_documents=result_documents,
        upstream_manifest_sha256=manifest.manifest_sha256,
        input_sha256={
            file.path: file.sha256
            for source in manifest.sources
            for file in source.fixture_files
        },
    )


def _load_full(manifest: UpstreamManifest, root: Path) -> MiningInputs:
    # With network disabled this call only reuses already verified local assets.
    acquire_full_manifest(manifest, root, allow_network=False)
    state = manifest.source("state_bench")
    abcd = manifest.source("abcd")
    tau2 = manifest.source("tau2")
    state_tasks, state_trajectories = _load_state_archive(
        _asset_path(root, _only_asset(state, "state_bench_archive"))
    )
    abcd_document = load_json_document(
        _asset_path(root, _only_asset(abcd, "abcd_conversations"))
    )
    abcd_conversations = flatten_abcd_document(abcd_document, profile="full")
    tau_tasks = _object_sequence(
        load_json_document(_asset_path(root, _only_asset(tau2, "tau2_tasks"))),
        "tau2 full tasks",
    )
    result_documents = {
        asset.name: load_json_document(_asset_path(root, asset))
        for asset in tau2.assets
        if asset.role == "tau2_result"
    }
    return MiningInputs(
        profile="full",
        state_tasks=state_tasks,
        state_trajectories=state_trajectories,
        abcd_conversations=abcd_conversations,
        tau_tasks=tau_tasks,
        tau_result_documents=result_documents,
        upstream_manifest_sha256=manifest.manifest_sha256,
        input_sha256={
            asset.destination: asset.sha256
            for source in manifest.sources
            for asset in source.assets
        },
    )


def load_mining_inputs(
    manifest: UpstreamManifest,
    root: Path,
    *,
    profile: str,
) -> MiningInputs:
    """Load a verified profile without mutating any upstream source bytes."""

    if profile == "fixture":
        return _load_fixture(manifest, root)
    if profile == "full":
        return _load_full(manifest, root)
    raise ValueError(f"unknown profile: {profile}")
