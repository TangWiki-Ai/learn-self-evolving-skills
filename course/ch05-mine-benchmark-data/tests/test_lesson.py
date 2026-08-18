from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from ses.testset.cluster import ClusterAssignment, ClusterItem
from ses.testset.manifest import load_manifest
from ses.testset.pipeline import MiningInputs
from ses.testset.profiles import load_mining_inputs

LESSON = Path(__file__).parents[1]
ROOT = LESSON.parents[1]
UPSTREAM = ROOT / "data" / "upstream"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _variant(name: str) -> ModuleType:
    return _module(LESSON / name / "mining.py", f"lesson_05_{name}")


def _fixture_inputs() -> MiningInputs:
    manifest = load_manifest(UPSTREAM / "manifest.json")
    return load_mining_inputs(manifest, UPSTREAM, profile="fixture")


class LabelClusterAdapter:
    def __init__(self, label_by_id: Mapping[str, str]) -> None:
        self._label_by_id = label_by_id

    @property
    def adapter_id(self) -> str:
        return "lesson05-label-cluster:v1"

    def cluster(self, items: tuple[ClusterItem, ...]) -> tuple[ClusterAssignment, ...]:
        return tuple(
            ClusterAssignment(
                item_id=item.item_id,
                cluster_id=self._label_by_id[item.item_id],
                confidence=1.0,
            )
            for item in reversed(items)
        )


@pytest.mark.parametrize(
    "function_name",
    ["scrub_product_defect", "cluster_and_compare_labels", "aggregate_tau2_by_task"],
)
def test_starter_retains_each_core_mining_gap(function_name: str) -> None:
    starter = _variant("starter")
    function = getattr(starter, function_name)

    with pytest.raises(NotImplementedError, match="Lesson 5"):
        if function_name == "scrub_product_defect":
            function(())
        elif function_name == "cluster_and_compare_labels":
            function((), LabelClusterAdapter({}))
        else:
            function((), {})


def test_solution_keeps_exact_slice_and_original_delexed_alignment() -> None:
    solution = _variant("solution")
    inputs = _fixture_inputs()

    result = solution.scrub_product_defect(inputs.abcd_conversations)

    assert len(result.records) == 2
    assert all(record.flow == "product_defect" for record in result.records)
    assert all(record.original and record.delexed for record in result.records)
    assert all(len(record.original) == len(record.delexed) for record in result.records)
    assert all(
        [turn.speaker for turn in record.original]
        == [turn.speaker for turn in record.delexed]
        for record in result.records
    )
    assert {record.subflow for record in result.records} == {
        "return_size",
        "refund_status",
    }


def test_solution_compares_clusters_with_flow_and_subflow_labels() -> None:
    solution = _variant("solution")
    inputs = _fixture_inputs()
    records = solution.scrub_product_defect(inputs.abcd_conversations).records
    adapter = LabelClusterAdapter(
        {record.source_id: record.subflow for record in records}
    )

    assignments, comparisons = solution.cluster_and_compare_labels(records, adapter)
    by_name = {comparison.label_name: comparison for comparison in comparisons}

    assert len(assignments) == len(records)
    assert by_name["flow"].informative is False
    assert by_name["flow"].reason == "single_reference_label"
    assert by_name["subflow"].informative is True
    assert by_name["subflow"].normalized_mutual_info == pytest.approx(1.0)


def test_solution_reads_tau2_without_mutation_and_aggregates_sixteen_runs() -> None:
    solution = _variant("solution")
    inputs = _fixture_inputs()
    tau_paths = sorted((UPSTREAM / "tau2" / "fixture").glob("*.json"))
    before = {path: path.read_bytes() for path in tau_paths}

    difficulty = solution.aggregate_tau2_by_task(
        inputs.tau_tasks, inputs.tau_result_documents
    )

    assert {path: path.read_bytes() for path in tau_paths} == before
    assert [item.task_id for item in difficulty] == ["27", "53"]
    assert [item.run_count for item in difficulty] == [16, 16]
    assert [item.difficulty_bucket for item in difficulty] == ["medium", "easy"]
    assert all(len(item.per_asset) == 4 for item in difficulty)
    assert all(asset.run_count == 4 for item in difficulty for asset in item.per_asset)


def test_full_reference_records_measured_counts_and_pinned_provenance() -> None:
    reference = json.loads(
        (LESSON / "full-funnel-reference.json").read_text(encoding="utf-8")
    )
    upstream = json.loads((UPSTREAM / "manifest.json").read_text(encoding="utf-8"))

    assert reference["record_type"] == "lesson05_full_mining_reference"
    assert reference["profile"] == "full"
    assert (
        reference["upstream"]["manifest_sha256"]
        == sha256((UPSTREAM / "manifest.json").read_bytes()).hexdigest()
    )
    assert reference["upstream"]["transformation"] == upstream["transformation"]

    sources = {item["name"]: item for item in reference["upstream"]["sources"]}
    assert sources["abcd"]["commit"] == ("6b8700ce67c6b37b062dd7a60abc76d7ef832a97")
    assert sources["tau2"]["commit"] == ("c3398666e6559e3a063da3fc04b5acf7f941464e")
    assert sources["abcd"]["license"]["spdx"] == "MIT"
    assert sources["tau2"]["license"]["spdx"] == "MIT"

    abcd = reference["abcd"]
    assert abcd["source_conversations"] == 10_042
    assert abcd["exact_product_defect"] == 1_070
    assert abcd["records_with_original"] == 1_070
    assert abcd["records_with_delexed"] == 1_070
    assert abcd["aligned_original_delexed_records"] == 1_070
    assert abcd["original_turns"] == abcd["delexed_turns"] == 28_535
    assert abcd["flow_counts"] == {"product_defect": 1_070}
    assert sum(abcd["subflow_counts"].values()) == 1_070
    assert abcd["partition_counts"] == {"dev": 102, "test": 105, "train": 863}

    tau2 = reference["tau2"]
    assert tau2["trajectory_runs"] == 1_824
    assert tau2["task_aggregates"] == 114
    assert tau2["runs_per_task"] == 16
    assert tau2["difficulty_buckets"] == {"easy": 70, "hard": 10, "medium": 34}
    assert tau2["read_only_verification"]["all_pinned_assets_match_after_run"] is True
    assert reference["funnel"]["abcd"]["candidates"] == 1_070


def test_readme_exposes_offline_fixture_full_and_test_commands() -> None:
    readme = (LESSON / "README.md").read_text(encoding="utf-8")

    assert "scripts/prepare_data.py --profile fixture" in readme
    assert "scripts/prepare_data.py --profile full" in readme
    assert "course/ch05-mine-benchmark-data/tests" in readme
    assert "full-funnel-reference.json" in readme
