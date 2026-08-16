from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from ses.shop import ReturnReason
from ses.testset.verified import CandidateSeed, VariantDimensions

LESSON = Path(__file__).parents[1]


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _variant(name: str) -> ModuleType:
    return _module(LESSON / name / "qualification.py", f"lesson_06_{name}")


def _seed() -> CandidateSeed:
    return CandidateSeed(
        candidate_id="candidate:lesson-6",
        source_id="abcd:lesson-6",
        semantic_group_id="semantic:lesson-6",
        flow="product_defect",
        subflow="return_size",
        difficulty_bucket="medium",
        public_intent="Return an item with a policy question.",
    )


def _dimensions() -> VariantDimensions:
    return VariantDimensions(
        membership_tier="gold",
        has_prime_shipping=False,
        days_since_delivery=30,
        return_window_days=15,
        return_reason=ReturnReason.CHANGED_MIND,
        price_minor=20000,
        order_subtotal_minor=20000,
        restocking_fee_pct=15,
    )


def test_solution_uses_the_production_variant_protocol() -> None:
    solution = _variant("solution")
    first = solution.verify_variant(_seed(), _dimensions())
    second = solution.verify_variant(_seed(), _dimensions())

    assert first.fixture.case_id == second.fixture.case_id
    assert first.lineage_hash == second.lineage_hash
    assert first.fixture.user_prompt.find("20000") == -1


def test_solution_runs_model_assisted_curation_from_fixed_responses() -> None:
    solution = _variant("solution")
    source_ids = (
        "abcd:6b8700ce67c6b37b062dd7a60abc76d7ef832a97:train:3592",
        "abcd:6b8700ce67c6b37b062dd7a60abc76d7ef832a97:train:9489",
    )

    bundle = solution.curate_candidate_sources(
        source_ids,
        LESSON.parents[1]
        / "data"
        / "upstream"
        / "abcd"
        / "fixture"
        / "conversations.json",
        LESSON.parents[1] / "data" / "testset" / "ticket07" / "curation-responses.json",
    )

    assert bundle.by_source_id[source_ids[0]].selected is True
    assert bundle.by_source_id[source_ids[1]].selected is False
    assert bundle.live_provider_used is False


def test_judge_meta_eval_covers_cross_layer_failure_modes() -> None:
    matrix = json.loads((LESSON / "judge-meta-eval.json").read_text())
    scenarios = {item["scenario_id"]: item for item in matrix["scenarios"]}

    assert scenarios["correct-state-correct-explanation"]["expected_case_status"] == (
        "pass"
    )
    assert (
        scenarios["correct-state-incorrect-explanation"]["expected_case_status"]
        == "fail"
    )
    assert (
        scenarios["incorrect-state-polished-explanation"]["expected_case_status"]
        == "fail"
    )
    assert scenarios["insufficient-evidence"]["expected_case_status"] == (
        "not_evaluated"
    )


def test_expanded_baseline_is_bound_to_current_catalog_and_artifacts() -> None:
    baseline = json.loads((LESSON / "expanded-baseline.json").read_text())
    manifest = json.loads(
        (
            LESSON.parents[1]
            / "data"
            / "testset"
            / "ticket07"
            / "generated"
            / "develop-manifest.json"
        ).read_text()
    )
    assert baseline["catalog_manifest_data_version"] == manifest["data_version"]
    for artifact in baseline["artifacts"].values():
        path = LESSON / artifact["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    events_path = LESSON / baseline["artifacts"]["events"]["path"]
    started = json.loads(events_path.read_text().splitlines()[0])
    assert started["config"]["data_version"] == baseline["data_version"]


@pytest.mark.parametrize(
    "function",
    [
        "curate_candidate_sources",
        "verify_variant",
        "calibrate_case",
        "protect_split",
    ],
)
def test_starter_retains_the_four_core_decision_gaps(function: str) -> None:
    starter = _variant("starter")
    target = getattr(starter, function)
    with pytest.raises(NotImplementedError, match="Lesson 6"):
        if function == "curate_candidate_sources":
            target([], Path("source.json"), Path("responses.json"))
        elif function == "verify_variant":
            target(_seed(), _dimensions())
        elif function == "calibrate_case":
            target(object())
        else:
            target([], [])
