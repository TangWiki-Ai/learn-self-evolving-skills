from __future__ import annotations

import importlib.util
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


@pytest.mark.parametrize(
    "function", ["verify_variant", "calibrate_case", "protect_split"]
)
def test_starter_retains_the_three_core_decision_gaps(function: str) -> None:
    starter = _variant("starter")
    target = getattr(starter, function)
    with pytest.raises(NotImplementedError, match="Lesson 6"):
        if function == "verify_variant":
            target(_seed(), _dimensions())
        elif function == "calibrate_case":
            target(object())
        else:
            target([], [])
