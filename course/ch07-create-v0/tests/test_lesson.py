from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from ses.skills.trigger_eval import SyntheticDiscoveryFixture

LESSON = Path(__file__).parents[1]
ROOT = LESSON.parents[1]


def _variant(name: str) -> ModuleType:
    path = LESSON / name / "skill_v0.py"
    spec = importlib.util.spec_from_file_location(f"lesson_07_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_solution_uses_production_seed_gate_and_trigger_protocol() -> None:
    solution = _variant("solution")
    pack = solution.load_seeds(
        ROOT / "data" / "skill-v0" / "creator" / "seed-manifest.json"
    )
    artifact = LESSON / "artifacts" / "skill" / "v0"
    gate = solution.static_gate(artifact)
    trigger = solution.trigger_eval(gate.skill_sha256, SyntheticDiscoveryFixture())

    assert len(pack.records) == 9
    assert gate.status.value == "pass"
    assert (trigger.precision, trigger.recall) == (1.0, 1.0)


def test_fixed_reference_is_quantitative_and_explicitly_offline() -> None:
    summary = json.loads((LESSON / "artifacts" / "summary.json").read_text())
    comparison = json.loads(
        (LESSON / "artifacts" / "paired-comparison.json").read_text()
    )

    assert summary["mode"] == "fixed"
    assert summary["creator_measurement"] == "synthetic_offline"
    assert summary["trigger_measurement"] == "synthetic_offline"
    assert summary["paired_measurement"] == "synthetic_offline"
    assert summary["paired_case_count"] == 15
    assert comparison["category_counts"] == {
        "both-fail": 0,
        "both-pass": 15,
        "fail-to-pass": 0,
        "pass-to-fail": 0,
    }
    assert (LESSON / "artifacts" / "l2.html").stat().st_size < 2_000_000


@pytest.mark.parametrize(
    "function,args",
    [
        ("load_seeds", (Path("manifest.json"),)),
        ("static_gate", (Path("skill"),)),
        ("trigger_eval", ("a" * 64, object())),
        ("paired_compare", (Path("skill"), Path("out"), Path("root"))),
    ],
)
def test_starter_keeps_the_four_decision_gaps(
    function: str, args: tuple[object, ...]
) -> None:
    starter = _variant("starter")
    with pytest.raises(NotImplementedError, match="Lesson 7"):
        getattr(starter, function)(*args)
