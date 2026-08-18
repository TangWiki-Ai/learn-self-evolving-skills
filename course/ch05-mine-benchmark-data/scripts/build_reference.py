#!/usr/bin/env python3
"""Build the compact Lesson 5 reference from a verified full mining bundle."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LESSON_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} contains a non-string key")
    return cast(Mapping[str, object], value)


def _sequence(value: object, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{context} must be a JSON array")
    return cast(Sequence[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context} must be an integer")
    return value


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON: {path}") from exc
    return _mapping(value, str(path))


def _load_jsonl(path: Path) -> Iterator[Mapping[str, object]]:
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    raise ValueError(f"blank JSONL line in {path}:{line_number}")
                value: object = json.loads(line)
                yield _mapping(value, f"{path}:{line_number}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSONL: {path}") from exc


def _source(upstream_manifest: Mapping[str, object], name: str) -> Mapping[str, object]:
    for raw_source in _sequence(upstream_manifest.get("sources"), "manifest sources"):
        source = _mapping(raw_source, "manifest source")
        if source.get("name") == name:
            return source
    raise ValueError(f"upstream manifest has no source named {name}")


def _verify_bundle(
    bundle: Path,
    artifact_manifest: Mapping[str, object],
) -> list[dict[str, object]]:
    verified: list[dict[str, object]] = []
    for raw_entry in _sequence(
        artifact_manifest.get("artifacts"), "artifact manifest entries"
    ):
        entry = _mapping(raw_entry, "artifact manifest entry")
        relative = _string(entry.get("path"), "artifact path")
        if Path(relative).name != relative:
            raise ValueError(f"artifact path must be a filename: {relative}")
        path = bundle / relative
        actual_bytes = path.stat().st_size
        actual_sha256 = _sha256(path)
        expected_bytes = _integer(entry.get("bytes"), f"{relative} bytes")
        expected_sha256 = _string(entry.get("sha256"), f"{relative} sha256")
        if (actual_bytes, actual_sha256) != (expected_bytes, expected_sha256):
            raise ValueError(f"artifact checksum drift: {relative}")
        verified.append(
            {
                "path": relative,
                "records": _integer(entry.get("records"), f"{relative} records"),
                "bytes": actual_bytes,
                "sha256": actual_sha256,
            }
        )
    return verified


def _verify_upstream(
    upstream_manifest: Mapping[str, object],
    data_root: Path,
    input_sha256: Mapping[str, object],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    sources: list[dict[str, object]] = []
    post_run_sha256: dict[str, str] = {}
    for raw_source in _sequence(upstream_manifest.get("sources"), "manifest sources"):
        source = _mapping(raw_source, "manifest source")
        name = _string(source.get("name"), "source name")
        license_data = _mapping(source.get("license"), f"{name} license")
        license_path = _string(license_data.get("path"), f"{name} license path")
        license_file = data_root / license_path
        license_sha256 = _sha256(license_file)
        if license_sha256 != _string(
            license_data.get("sha256"), f"{name} license sha256"
        ):
            raise ValueError(f"license checksum drift: {name}")

        assets: list[dict[str, object]] = []
        for raw_asset in _sequence(source.get("assets"), f"{name} assets"):
            asset = _mapping(raw_asset, f"{name} asset")
            destination = _string(asset.get("destination"), f"{name} asset destination")
            current_sha256 = _sha256(data_root / destination)
            expected_sha256 = _string(asset.get("sha256"), f"{destination} sha256")
            if current_sha256 != expected_sha256:
                raise ValueError(f"post-run input checksum drift: {destination}")
            if input_sha256.get(destination) != current_sha256:
                raise ValueError(f"bundle did not record pinned input: {destination}")
            post_run_sha256[destination] = current_sha256
            asset_summary: dict[str, object] = {
                "name": _string(asset.get("name"), f"{destination} name"),
                "role": _string(asset.get("role"), f"{destination} role"),
                "destination": destination,
                "bytes": _integer(asset.get("bytes"), f"{destination} bytes"),
                "sha256": current_sha256,
            }
            generation_commit = asset.get("generation_commit")
            if generation_commit is not None:
                asset_summary["generation_commit"] = _string(
                    generation_commit, f"{destination} generation commit"
                )
            assets.append(asset_summary)

        sources.append(
            {
                "name": name,
                "kind": _string(source.get("kind"), f"{name} kind"),
                "commit": _string(source.get("commit"), f"{name} commit"),
                "license": {
                    "spdx": _string(license_data.get("spdx"), f"{name} license spdx"),
                    "path": f"data/upstream/{license_path}",
                    "sha256": license_sha256,
                },
                "assets": assets,
            }
        )
    if set(post_run_sha256) != set(input_sha256):
        raise ValueError("full bundle input set differs from pinned source assets")
    return sources, dict(sorted(post_run_sha256.items()))


def _abcd_summary(
    bundle: Path,
    funnel: Mapping[str, object],
    source: Mapping[str, object],
) -> dict[str, object]:
    records = 0
    original_records = 0
    delexed_records = 0
    aligned_records = 0
    original_turns = 0
    delexed_turns = 0
    flow_counts: Counter[str] = Counter()
    subflow_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    source_commit = _string(source.get("commit"), "ABCD commit")

    for record in _load_jsonl(bundle / "scrubbed-abcd.jsonl"):
        records += 1
        if record.get("record_type") != "scrubbed_abcd_conversation":
            raise ValueError("unexpected ABCD artifact record type")
        if record.get("source_commit") != source_commit:
            raise ValueError("ABCD record source commit drifted")
        original = _sequence(record.get("original"), "ABCD original turns")
        delexed = _sequence(record.get("delexed"), "ABCD delexed turns")
        original_records += 1
        delexed_records += 1
        original_turns += len(original)
        delexed_turns += len(delexed)
        aligned = len(original) == len(delexed) and all(
            _mapping(left, "ABCD original turn").get("speaker")
            == _mapping(right, "ABCD delexed turn").get("speaker")
            for left, right in zip(original, delexed, strict=True)
        )
        if aligned:
            aligned_records += 1
        flow_counts[_string(record.get("flow"), "ABCD flow")] += 1
        subflow_counts[_string(record.get("subflow"), "ABCD subflow")] += 1
        split_counts[_string(record.get("source_split"), "ABCD split")] += 1

    abcd_funnel = _mapping(funnel.get("abcd"), "ABCD funnel")
    slice_data = _mapping(source.get("slice"), "ABCD slice")
    expected = _mapping(slice_data.get("expected_counts"), "ABCD expected counts")
    expected_selected = _integer(
        expected.get("selected_conversations"), "ABCD selected conversations"
    )
    if records != expected_selected or records != abcd_funnel.get("scrubbed_unique"):
        raise ValueError("ABCD scrubbed record count drifted")
    if abcd_funnel.get("exact_product_defect") != expected_selected:
        raise ValueError("ABCD exact product_defect count drifted")
    expected_subflows = _mapping(expected.get("subflows"), "ABCD subflows")
    expected_partitions = _mapping(
        expected.get("selected_by_partition"), "ABCD partitions"
    )
    if dict(sorted(subflow_counts.items())) != dict(expected_subflows):
        raise ValueError("ABCD subflow distribution drifted")
    if dict(sorted(split_counts.items())) != dict(expected_partitions):
        raise ValueError("ABCD split distribution drifted")
    if aligned_records != records:
        raise ValueError("ABCD original/delexed alignment drifted")

    return {
        "source_conversations": _integer(
            abcd_funnel.get("source_conversations"), "ABCD source conversations"
        ),
        "exact_product_defect": expected_selected,
        "records_with_original": original_records,
        "records_with_delexed": delexed_records,
        "aligned_original_delexed_records": aligned_records,
        "original_turns": original_turns,
        "delexed_turns": delexed_turns,
        "flow_counts": dict(sorted(flow_counts.items())),
        "subflow_counts": dict(sorted(subflow_counts.items())),
        "partition_counts": dict(sorted(split_counts.items())),
        "scrub": {
            key: _integer(abcd_funnel.get(key), f"ABCD funnel {key}")
            for key in (
                "dropped_empty",
                "dropped_misaligned",
                "dropped_invalid",
                "dropped_encoding",
                "dropped_duplicates",
                "scrubbed_unique",
            )
        },
    }


def _label_summary(bundle: Path) -> dict[str, object]:
    document = _load_json(bundle / "label-metrics.json")
    summary: dict[str, object] = {}
    for label_name in ("flow", "subflow"):
        label = _mapping(document.get(label_name), f"{label_name} metrics")
        summary[label_name] = {
            key: label.get(key)
            for key in (
                "evaluated_count",
                "excluded_missing_label_count",
                "true_label_count",
                "cluster_count",
                "adjusted_rand_index",
                "normalized_mutual_info",
                "homogeneity",
                "completeness",
                "v_measure",
                "informative",
                "reason",
            )
        }
    return summary


def _tau_summary(
    bundle: Path,
    funnel: Mapping[str, object],
    source: Mapping[str, object],
    post_run_sha256: Mapping[str, str],
) -> dict[str, object]:
    task_ids: set[str] = set()
    task_records = 0
    total_runs = 0
    bucket_counts: Counter[str] = Counter()
    generation_commits: set[str] = set()
    for record in _load_jsonl(bundle / "tau2-difficulty.jsonl"):
        task_records += 1
        task_id = _string(record.get("task_id"), "tau2 task id")
        if task_id in task_ids:
            raise ValueError(f"duplicate tau2 task aggregate: {task_id}")
        task_ids.add(task_id)
        run_count = _integer(record.get("run_count"), f"tau2 task {task_id} runs")
        if run_count != 16:
            raise ValueError(f"tau2 task {task_id} was not aggregated over 16 runs")
        per_asset = _sequence(record.get("per_asset"), "tau2 per-asset counts")
        if len(per_asset) != 4 or any(
            _mapping(item, "tau2 per-asset count").get("run_count") != 4
            for item in per_asset
        ):
            raise ValueError(f"tau2 task {task_id} lacks four trials from four assets")
        total_runs += run_count
        bucket_counts[
            _string(record.get("difficulty_bucket"), "tau2 difficulty bucket")
        ] += 1
        generation_commits.update(
            _string(value, "tau2 generation commit")
            for value in _sequence(
                record.get("generation_commits"), "tau2 generation commits"
            )
        )

    tau_funnel = _mapping(funnel.get("tau"), "tau2 funnel")
    slice_data = _mapping(source.get("slice"), "tau2 slice")
    expected = _mapping(slice_data.get("expected_counts"), "tau2 expected counts")
    expected_tasks = _integer(expected.get("tasks"), "tau2 expected tasks")
    expected_runs = _integer(expected.get("trajectories"), "tau2 expected runs")
    if (task_records, total_runs) != (expected_tasks, expected_runs):
        raise ValueError("tau2 task aggregation counts drifted")
    if tau_funnel.get("task_aggregates") != task_records:
        raise ValueError("tau2 funnel aggregate count drifted")

    tau_assets = {
        _string(_mapping(raw, "tau2 asset").get("destination"), "tau2 asset path")
        for raw in _sequence(source.get("assets"), "tau2 assets")
    }
    tau_post_run = {
        path: digest for path, digest in post_run_sha256.items() if path in tau_assets
    }
    if set(tau_post_run) != tau_assets:
        raise ValueError("tau2 post-run checksum set is incomplete")

    return {
        "usage": slice_data.get("usage"),
        "prohibited_usage": slice_data.get("prohibited_usage"),
        "source_tasks": expected_tasks,
        "trajectory_runs": expected_runs,
        "task_aggregates": task_records,
        "runs_per_task": 16,
        "runs_removed_as_separate_candidate_units": expected_runs - task_records,
        "difficulty_buckets": dict(sorted(bucket_counts.items())),
        "generation_commits": sorted(generation_commits),
        "read_only_verification": {
            "all_pinned_assets_match_after_run": True,
            "asset_sha256": dict(sorted(tau_post_run.items())),
        },
    }


def build_reference(
    *,
    bundle: Path,
    upstream_manifest_path: Path,
    data_root: Path,
) -> dict[str, object]:
    """Validate a full bundle and return a compact, path-independent reference."""

    upstream_manifest = _load_json(upstream_manifest_path)
    artifact_manifest_path = bundle / "artifact-manifest.json"
    artifact_manifest = _load_json(artifact_manifest_path)
    if artifact_manifest.get("profile") != "full":
        raise ValueError("Lesson 5 reference requires a full-profile bundle")
    upstream_manifest_sha256 = _sha256(upstream_manifest_path)
    if artifact_manifest.get("upstream_manifest_sha256") != upstream_manifest_sha256:
        raise ValueError("bundle upstream manifest hash drifted")
    if artifact_manifest.get("transformation_version") != upstream_manifest.get(
        "transformation_version"
    ):
        raise ValueError("bundle transformation version drifted")

    input_sha256 = _mapping(
        artifact_manifest.get("input_sha256"), "bundle input sha256"
    )
    sources, post_run_sha256 = _verify_upstream(
        upstream_manifest,
        data_root,
        input_sha256,
    )
    outputs = _verify_bundle(bundle, artifact_manifest)
    funnel = _load_json(bundle / "funnel-counts.json")
    if funnel.get("profile") != "full":
        raise ValueError("funnel profile drifted")

    reference: dict[str, object] = {
        "schema_version": "v1alpha1",
        "record_type": "lesson05_full_mining_reference",
        "profile": "full",
        "upstream": {
            "manifest_path": "data/upstream/manifest.json",
            "manifest_sha256": upstream_manifest_sha256,
            "transformation_version": upstream_manifest.get("transformation_version"),
            "transformation": upstream_manifest.get("transformation"),
            "sources": sources,
            "post_run_asset_sha256": post_run_sha256,
        },
        "pipeline": {
            "seed": artifact_manifest.get("seed"),
            "cluster_adapter_id": artifact_manifest.get("cluster_adapter_id"),
            "stratify_adapter_id": artifact_manifest.get("stratify_adapter_id"),
            "artifact_manifest_sha256": _sha256(artifact_manifest_path),
            "parsed_input_digest_algorithm": artifact_manifest.get(
                "parsed_input_digest_algorithm"
            ),
            "parsed_input_sha256": artifact_manifest.get("parsed_input_sha256"),
            "outputs": outputs,
        },
        "abcd": _abcd_summary(bundle, funnel, _source(upstream_manifest, "abcd")),
        "cluster_label_metrics": _label_summary(bundle),
        "tau2": _tau_summary(
            bundle,
            funnel,
            _source(upstream_manifest, "tau2"),
            post_run_sha256,
        ),
        "funnel": funnel,
    }
    return reference


def _write_atomic(path: Path, document: Mapping[str, object]) -> None:
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the compact Lesson 5 reference from a full bundle."
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "upstream" / "manifest.json",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "upstream",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=LESSON_ROOT / "full-funnel-reference.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reference = build_reference(
            bundle=args.bundle,
            upstream_manifest_path=args.manifest,
            data_root=args.data_root,
        )
        _write_atomic(args.output, reference)
    except (OSError, ValueError) as exc:
        print(f"build_reference: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "sha256": _sha256(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
