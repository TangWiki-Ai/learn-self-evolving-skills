"""Strict machine-readable manifest parsing and drift detection."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast

from ses.testset.sources import ABCD_COMMIT, STATE_BENCH_COMMIT, TAU2_COMMIT

TRANSFORMATION_VERSION = "ses-testset-candidates-v1"
_PINNED_COMMITS = {
    "state_bench": STATE_BENCH_COMMIT,
    "abcd": ABCD_COMMIT,
    "tau2": TAU2_COMMIT,
}
_PINNED_REPOSITORIES = {
    "state_bench": "https://github.com/microsoft/STATE-Bench",
    "abcd": "https://github.com/asappresearch/abcd",
    "tau2": "https://github.com/sierra-research/tau2-bench",
}
_EXPECTED_KINDS = {
    "state_bench": "benchmark",
    "abcd": "role_playing_benchmark",
    "tau2": "benchmark",
}
_EXPECTED_SOURCE_IDENTITY_SHA256 = {
    "state_bench": "700b51cb675ff4602ca318e2dd9a808f38638601fd707e8c5aab7426c743b315",
    "abcd": "881485c8166df7404042936dca1cd02d7ad69e683f080edd59c7225ce22372b6",
    "tau2": "50b8a95741136f1bf78ea75fe333b83c0960a50bcdeecb565647fd2d2f6c2ca5",
}
_EXPECTED_LICENSES = {
    "state_bench": (
        "state_bench/LICENSE",
        1081,
        "2e969379b1a7eaeeefe741c576aa64e29099b9629b645e0e938bf2c88e7b5f0b",
    ),
    "abcd": (
        "abcd/LICENSE",
        1071,
        "3ab7e179a7f13027b7bc64293541f0e9beacca3701cade67fb8fce78c2d9317b",
    ),
    "tau2": (
        "tau2/LICENSE",
        1072,
        "e67c5aa0074dfcaefd3c3a1aedb94cb539234aecd15d5a972574e3200e6252fe",
    ),
}
_EXPECTED_ASSETS = {
    "state_bench": (
        (
            "state_bench_source_archive",
            "state_bench_archive",
            "downloads/state_bench/source.tar.gz",
            1_251_382,
            "746646f24ab0ebd713ae28f0e96c1cc81cdbe9598171a2f7ed37953ce7a0b96a",
        ),
    ),
    "abcd": (
        (
            "abcd_v1_1",
            "abcd_conversations",
            "downloads/abcd/abcd_v1.1.json.gz",
            36_985_084,
            "2bdf53ac359543dcdc38d55bc6513e78df120363f8f44870716e909f4606de15",
        ),
    ),
    "tau2": (
        (
            "tau2_retail_tasks",
            "tau2_tasks",
            "downloads/tau2/tasks.json",
            345_982,
            "8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8",
        ),
        (
            "tau2_result_claude",
            "tau2_result",
            "downloads/tau2/results/claude-3-7-sonnet-20250219_retail_default_gpt-4.1-2025-04-14_4trials.json",
            24_908_843,
            "ed41dbd18c080154156484e3a0122c095e324a11367a640d88e15956daed7b9d",
        ),
        (
            "tau2_result_gpt_4_1",
            "tau2_result",
            "downloads/tau2/results/gpt-4.1-2025-04-14_retail_default_gpt-4.1-2025-04-14_4trials.json",
            22_044_059,
            "5fc5b96ada0fe46a463eaed98d1bfed9947fe073bac052290162dae18d71394e",
        ),
        (
            "tau2_result_gpt_4_1_mini",
            "tau2_result",
            "downloads/tau2/results/gpt-4.1-mini-2025-04-14_retail_base_gpt-4.1-2025-04-14_4trials.json",
            23_697_012,
            "6d6badb43b716adca31591b0b40e15fd493b49adddaa8e2c47035bb557549257",
        ),
        (
            "tau2_result_o4_mini",
            "tau2_result",
            "downloads/tau2/results/o4-mini-2025-04-16_retail_default_gpt-4.1-2025-04-14_4trials.json",
            22_066_527,
            "7135f38bbbbd46d6babe830574c8623bb276d8fd6da316580b905cf513853f98",
        ),
    ),
}
_EXPECTED_FIXTURE_ROLES = {
    "state_bench": Counter({("state_tasks", None): 1, ("state_trajectories", None): 1}),
    "abcd": Counter({("abcd_conversations", None): 1}),
    "tau2": Counter(
        {
            ("tau2_tasks", None): 1,
            ("tau2_result", "tau2_result_claude"): 1,
            ("tau2_result", "tau2_result_gpt_4_1"): 1,
            ("tau2_result", "tau2_result_gpt_4_1_mini"): 1,
            ("tau2_result", "tau2_result_o4_mini"): 1,
        }
    ),
}
_EXPECTED_TRANSFORMATION: dict[str, object] = {
    "entrypoint": "scripts/prepare_data.py",
    "stages": [
        "acquire",
        "exact_slice",
        "scrub_deduplicate",
        "cluster_compare_labels",
        "aggregate_tau2_by_task",
        "stratify_candidates",
    ],
    "outputs": [
        "scrubbed-abcd.jsonl",
        "cluster-assignments.jsonl",
        "label-metrics.json",
        "tau2-difficulty.jsonl",
        "candidate-list.jsonl",
        "funnel-counts.json",
        "artifact-manifest.json",
    ],
    "creates_executable_cases": False,
    "writes_test_splits": False,
}
_EXPECTED_SLICE_VALUES: dict[str, tuple[tuple[tuple[str, ...], object], ...]] = {
    "state_bench": (
        (("source_shape",), "individual_json_files"),
        (("filter", "json_path"), "task_type"),
        (("filter", "operator"), "eq"),
        (("filter", "value"), "return_item"),
        (("join", "task_key"), "task_id"),
        (("join", "trajectory_key"), "filename_stem"),
        (("expected_counts", "source_tasks"), 150),
        (("expected_counts", "selected_tasks"), 33),
        (("expected_counts", "source_train_trajectories"), 100),
        (("expected_counts", "selected_train_trajectories"), 21),
    ),
    "abcd": (
        (("source_shape",), "partition_mapping"),
        (("partitions",), ["train", "dev", "test"]),
        (("filter", "json_path"), "scenario.flow"),
        (("filter", "operator"), "eq"),
        (("filter", "value"), "product_defect"),
        (("expected_counts", "source_conversations"), 10_042),
        (("expected_counts", "selected_conversations"), 1_070),
        (("stable_source_id",), "abcd:{commit}:{partition}:{convo_id}"),
        (("alignment",), "original/delexed array position plus exact speaker"),
    ),
    "tau2": (
        (("domain",), "retail"),
        (("usage",), ["deduplication_signal", "difficulty_signal"]),
        (("prohibited_usage",), ["shop_execution"]),
        (("task_id_path",), "id"),
        (("reward_json_path",), "reward_info.reward"),
        (("group_by",), "task_id"),
        (("run_key",), ["result_asset_id", "task_id", "trial"]),
        (("expected_counts", "tasks"), 114),
        (("expected_counts", "result_files"), 4),
        (("expected_counts", "trials_per_result_file"), 4),
        (("expected_counts", "runs_per_task"), 16),
        (("expected_counts", "trajectories"), 1_824),
        (("difficulty", "pass_condition"), "reward == 1"),
        (("difficulty", "hard_max"), "0.25"),
        (("difficulty", "easy_min"), "0.75"),
        (
            ("generation_commits", "tau2_result_claude"),
            "c30d59aaa71c65f9b9eb6a8f8636b48945028fcf",
        ),
        (
            ("generation_commits", "tau2_result_gpt_4_1"),
            "c30d59aaa71c65f9b9eb6a8f8636b48945028fcf",
        ),
        (
            ("generation_commits", "tau2_result_gpt_4_1_mini"),
            "ade39493be54aad326a4c65295f77fe09780329b",
        ),
        (
            ("generation_commits", "tau2_result_o4_mini"),
            "c30d59aaa71c65f9b9eb6a8f8636b48945028fcf",
        ),
    ),
}
_EXPECTED_FIXTURE_PROJECTION_VALUES: dict[
    str, tuple[tuple[tuple[str, ...], object], ...]
] = {
    "state_bench": (
        (("source_records",), 3),
        (("selected_records",), 2),
        (("matching_trajectories",), 1),
        (
            ("trajectory_projection",),
            "first user/assistant role/content pair unchanged",
        ),
    ),
    "abcd": (
        (("upstream_sample_bytes",), 38_934),
        (
            ("upstream_sample_sha256",),
            "151e0c487493ab376bb5115538f3bfd6d2f460c94f9daa5cdf04e55bccdf4808",
        ),
        (("records",), 3),
        (("selected_records",), 2),
        (("source_partition",), "train"),
    ),
    "tau2": (
        (("tasks",), 2),
        (("task_ids",), ["27", "53"]),
        (("result_files",), 4),
        (("runs_per_task",), 16),
        (("successes_by_task",), {"27": 5, "53": 15}),
        (("difficulty_by_task",), {"27": "medium", "53": "easy"}),
    ),
}


class ManifestDriftError(ValueError):
    """The manifest or a pinned local file drifted from its audited value."""


@dataclass(frozen=True)
class FileSpec:
    role: str
    path: str
    bytes: int
    sha256: str
    asset_id: str | None = None


@dataclass(frozen=True)
class AssetSpec:
    name: str
    url: str
    destination: str
    bytes: int
    sha256: str
    role: str
    generation_commit: str | None = None
    compression: str | None = None
    uncompressed_bytes: int | None = None
    uncompressed_sha256: str | None = None


@dataclass(frozen=True)
class LicenseSpec:
    spdx: str
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class SourceSpec:
    name: str
    kind: str
    repository: str
    commit: str
    license: LicenseSpec
    assets: tuple[AssetSpec, ...]
    fixture_files: tuple[FileSpec, ...]
    fixture_projection: Mapping[str, object]
    slice: Mapping[str, object]


@dataclass(frozen=True)
class UpstreamManifest:
    schema_version: str
    record_type: str
    transformation_version: str
    manifest_sha256: str
    sources: tuple[SourceSpec, ...]

    def source(self, name: str) -> SourceSpec:
        for source in self.sources:
            if source.name == name:
                return source
        raise KeyError(name)


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ManifestDriftError(f"{context} must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ManifestDriftError(f"{context} must be a list")
    return cast(Sequence[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestDriftError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestDriftError(f"{context} must be a non-negative integer")
    return value


def _digest(value: object, context: str) -> str:
    digest = _string(value, context)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ManifestDriftError(f"{context} must be a lowercase SHA256")
    return digest


def _relative_path(value: object, context: str) -> str:
    raw = _string(value, context)
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw != path.as_posix():
        raise ManifestDriftError(f"{context} must be a safe relative POSIX path")
    return raw


def _canonical_digest(value: object, context: str) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestDriftError(f"{context} is not strict JSON") from exc
    return sha256(payload).hexdigest()


def _parse_file(raw: object, context: str) -> FileSpec:
    item = _mapping(raw, context)
    raw_asset_id = item.get("asset_id")
    return FileSpec(
        role=_string(item.get("role"), f"{context}.role"),
        path=_relative_path(item.get("path"), f"{context}.path"),
        bytes=_integer(item.get("bytes"), f"{context}.bytes"),
        sha256=_digest(item.get("sha256"), f"{context}.sha256"),
        asset_id=(
            None
            if raw_asset_id is None
            else _string(raw_asset_id, f"{context}.asset_id")
        ),
    )


def _parse_asset(raw: object, context: str, commit: str) -> AssetSpec:
    item = _mapping(raw, context)
    url = _string(item.get("url"), f"{context}.url")
    if not url.startswith("https://") or commit not in url:
        raise ManifestDriftError(f"{context}.url is not pinned to source commit")
    raw_generation_commit = item.get("generation_commit")
    raw_compression = item.get("compression")
    raw_uncompressed_bytes = item.get("uncompressed_bytes")
    raw_uncompressed_sha256 = item.get("uncompressed_sha256")
    if (raw_uncompressed_bytes is None) != (raw_uncompressed_sha256 is None):
        raise ManifestDriftError(
            f"{context} must specify both uncompressed bytes and SHA256"
        )
    return AssetSpec(
        name=_string(item.get("name"), f"{context}.name"),
        url=url,
        destination=_relative_path(item.get("destination"), f"{context}.destination"),
        bytes=_integer(item.get("bytes"), f"{context}.bytes"),
        sha256=_digest(item.get("sha256"), f"{context}.sha256"),
        role=_string(item.get("role"), f"{context}.role"),
        generation_commit=(
            None
            if raw_generation_commit is None
            else _string(raw_generation_commit, f"{context}.generation_commit")
        ),
        compression=(
            None
            if raw_compression is None
            else _string(raw_compression, f"{context}.compression")
        ),
        uncompressed_bytes=(
            None
            if raw_uncompressed_bytes is None
            else _integer(raw_uncompressed_bytes, f"{context}.uncompressed_bytes")
        ),
        uncompressed_sha256=(
            None
            if raw_uncompressed_sha256 is None
            else _digest(raw_uncompressed_sha256, f"{context}.uncompressed_sha256")
        ),
    )


def _parse_source(raw: object, index: int) -> SourceSpec:
    context = f"sources[{index}]"
    item = _mapping(raw, context)
    name = _string(item.get("name"), f"{context}.name")
    commit = _string(item.get("commit"), f"{context}.commit")
    expected_commit = _PINNED_COMMITS.get(name)
    if expected_commit is None:
        raise ManifestDriftError(f"unknown source {name}")
    if commit != expected_commit:
        raise ManifestDriftError(f"{name} commit drift: {commit} != {expected_commit}")
    license_raw = _mapping(item.get("license"), f"{context}.license")
    license_spec = LicenseSpec(
        spdx=_string(license_raw.get("spdx"), f"{context}.license.spdx"),
        path=_relative_path(license_raw.get("path"), f"{context}.license.path"),
        bytes=_integer(license_raw.get("bytes"), f"{context}.license.bytes"),
        sha256=_digest(license_raw.get("sha256"), f"{context}.license.sha256"),
    )
    if license_spec.spdx != "MIT":
        raise ManifestDriftError(f"{name} license drift: {license_spec.spdx}")
    if (
        license_spec.path,
        license_spec.bytes,
        license_spec.sha256,
    ) != _EXPECTED_LICENSES[name]:
        raise ManifestDriftError(f"{name} license identity drifted")
    assets_raw = _sequence(item.get("assets"), f"{context}.assets")
    fixtures_raw = _sequence(item.get("fixture_files"), f"{context}.fixture_files")
    fixture_projection = _mapping(
        item.get("fixture_projection"), f"{context}.fixture_projection"
    )
    slice_spec = _mapping(item.get("slice"), f"{context}.slice")
    repository = _string(item.get("repository"), f"{context}.repository")
    if repository != _PINNED_REPOSITORIES[name]:
        raise ManifestDriftError(f"{name} repository drift: {repository}")
    source = SourceSpec(
        name=name,
        kind=_string(item.get("kind"), f"{context}.kind"),
        repository=repository,
        commit=commit,
        license=license_spec,
        assets=tuple(
            _parse_asset(value, f"{context}.assets[{asset_index}]", commit)
            for asset_index, value in enumerate(assets_raw)
        ),
        fixture_files=tuple(
            _parse_file(value, f"{context}.fixture_files[{file_index}]")
            for file_index, value in enumerate(fixtures_raw)
        ),
        fixture_projection=fixture_projection,
        slice=slice_spec,
    )
    if source.kind != _EXPECTED_KINDS[name]:
        raise ManifestDriftError(f"{name} source kind drifted")
    actual_assets = tuple(
        sorted(
            (
                asset.name,
                asset.role,
                asset.destination,
                asset.bytes,
                asset.sha256,
            )
            for asset in source.assets
        )
    )
    if actual_assets != tuple(sorted(_EXPECTED_ASSETS[name])):
        raise ManifestDriftError(f"{name} full asset inventory drifted")
    fixture_roles = Counter(
        (fixture.role, fixture.asset_id) for fixture in source.fixture_files
    )
    if fixture_roles != _EXPECTED_FIXTURE_ROLES[name]:
        raise ManifestDriftError(f"{name} fixture inventory drifted")
    fixture_paths = [fixture.path for fixture in source.fixture_files]
    if len(fixture_paths) != len(set(fixture_paths)):
        raise ManifestDriftError(f"{name} fixture paths must be unique")
    _validate_slice(source)
    _validate_fixture_projection(source)
    if source.name == "tau2":
        generation_commits = _mapping(
            source.slice.get("generation_commits"),
            f"{context}.slice.generation_commits",
        )
        full_result_ids = {
            asset.name for asset in source.assets if asset.role == "tau2_result"
        }
        fixture_result_ids = {
            file.asset_id
            for file in source.fixture_files
            if file.role == "tau2_result" and file.asset_id is not None
        }
        if fixture_result_ids and fixture_result_ids != full_result_ids:
            raise ManifestDriftError(
                f"{context} fixture result_asset_id mapping drifted"
            )
        for asset in source.assets:
            if (
                asset.role == "tau2_result"
                and asset.generation_commit != generation_commits.get(asset.name)
            ):
                raise ManifestDriftError(
                    f"{context} generation commit drift for {asset.name}"
                )
    if _canonical_digest(item, context) != _EXPECTED_SOURCE_IDENTITY_SHA256[name]:
        raise ManifestDriftError(f"{name} complete source identity drifted")
    return source


def _nested_value(document: Mapping[str, object], path: tuple[str, ...]) -> object:
    current: object = document
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _validate_slice(source: SourceSpec) -> None:
    for path, expected in _EXPECTED_SLICE_VALUES[source.name]:
        actual = _nested_value(source.slice, path)
        if actual != expected:
            dotted = ".".join(path)
            raise ManifestDriftError(
                f"{source.name} slice drift at {dotted}: {actual!r} != {expected!r}"
            )


def _validate_fixture_projection(source: SourceSpec) -> None:
    for path, expected in _EXPECTED_FIXTURE_PROJECTION_VALUES[source.name]:
        actual = _nested_value(source.fixture_projection, path)
        if actual != expected:
            dotted = ".".join(path)
            raise ManifestDriftError(
                f"{source.name} fixture projection drift at {dotted}: "
                f"{actual!r} != {expected!r}"
            )


def load_manifest(path: Path) -> UpstreamManifest:
    """Load the audited manifest and reject pin or schema drift."""

    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestDriftError(f"cannot read manifest {path}") from exc
    root = _mapping(document, "manifest")
    if set(root) != {
        "schema_version",
        "record_type",
        "transformation_version",
        "transformation",
        "sources",
    }:
        raise ManifestDriftError(
            "manifest root fields or transformation declaration drifted"
        )
    schema_version = _string(root.get("schema_version"), "schema_version")
    record_type = _string(root.get("record_type"), "record_type")
    transformation_version = _string(
        root.get("transformation_version"), "transformation_version"
    )
    if schema_version != "v1alpha1":
        raise ManifestDriftError(f"unsupported schema version: {schema_version}")
    if record_type != "upstream_manifest":
        raise ManifestDriftError(f"unexpected record type: {record_type}")
    if transformation_version != TRANSFORMATION_VERSION:
        raise ManifestDriftError(
            f"transformation version drift: {transformation_version}"
        )
    transformation = _mapping(root.get("transformation"), "transformation")
    if dict(transformation) != _EXPECTED_TRANSFORMATION:
        raise ManifestDriftError("transformation history drifted")
    raw_sources = _sequence(root.get("sources"), "sources")
    sources = tuple(_parse_source(raw, index) for index, raw in enumerate(raw_sources))
    names = [source.name for source in sources]
    if len(names) != len(set(names)) or set(names) != set(_PINNED_COMMITS):
        raise ManifestDriftError(
            "manifest must contain each pinned source exactly once"
        )
    return UpstreamManifest(
        schema_version=schema_version,
        record_type=record_type,
        transformation_version=transformation_version,
        manifest_sha256=sha256(payload).hexdigest(),
        sources=sources,
    )


def _verify_file(path: Path, expected_bytes: int | None, expected_hash: str) -> None:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ManifestDriftError(f"manifest file missing: {path}") from exc
    if expected_bytes is not None and len(payload) != expected_bytes:
        raise ManifestDriftError(f"size drift: {path}")
    actual = sha256(payload).hexdigest()
    if actual != expected_hash:
        raise ManifestDriftError(f"checksum drift: {path}")


def verify_manifest_files(manifest: UpstreamManifest, root: Path) -> None:
    """Verify committed licenses and CI fixture bytes without network access."""

    resolved_root = root.resolve()
    for source in manifest.sources:
        license_path = (resolved_root / source.license.path).resolve()
        if not license_path.is_relative_to(resolved_root):
            raise ManifestDriftError("license path escapes manifest root")
        _verify_file(license_path, source.license.bytes, source.license.sha256)
        for fixture in source.fixture_files:
            fixture_path = (resolved_root / fixture.path).resolve()
            if not fixture_path.is_relative_to(resolved_root):
                raise ManifestDriftError("fixture path escapes manifest root")
            _verify_file(fixture_path, fixture.bytes, fixture.sha256)
