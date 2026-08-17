from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

LESSON = Path(__file__).parents[1]
ROOT = LESSON.parents[1]


def _variant(name: str) -> ModuleType:
    path = LESSON / name / "evolution.py"
    spec = importlib.util.spec_from_file_location(f"lesson_08_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_solution_reads_live_fixture_without_inventing_a_patch() -> None:
    solution = _variant("solution")
    result = solution.analyze(
        ROOT / "tests/fixtures/evolution/live-failure-evidence.json"
    )
    assert result.patch_allowed is False
    assert result.cards == ()
    assert "infrastructure_error" in result.reason


def test_synthetic_lesson_material_explicitly_covers_six_categories_and_three_ops() -> (
    None
):
    cards = json.loads((LESSON / "artifacts/synthetic-failure-cards.json").read_text())[
        "cards"
    ]
    patch = json.loads((LESSON / "artifacts/evidence-linked-patch.json").read_text())
    assert {card["category"] for card in cards} == {
        "trigger",
        "pattern",
        "overload",
        "terminology",
        "timing",
        "safety",
    }
    assert {operation["operation"] for operation in patch["operations"]} == {
        "add",
        "update",
        "delete",
    }
    assert all(card["provenance"] == "synthetic" for card in cards)
    assert (
        patch["parent_skill_sha256"]
        == json.loads(
            (LESSON / "artifacts/evidence-linked-patch-list.json").read_text()
        )["parent_skill_sha256"]
    )


def test_solution_runs_the_complete_offline_evolution(tmp_path: Path) -> None:
    solution = _variant("solution")
    result = solution.evolve(
        parent=ROOT / "course/ch07-create-v0/artifacts/skill/v0",
        evidence=ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json",
        output=tmp_path / "lesson-08",
    )
    assert result.failure_card_count == 6
    assert result.patch_operation_count == 3
    assert (tmp_path / "lesson-08/skill/skill-manifest.json").is_file()


@pytest.mark.parametrize("function", ["analyze", "create_candidate", "evolve"])
def test_starter_keeps_the_three_decision_gaps(function: str) -> None:
    starter = _variant("starter")
    with pytest.raises(NotImplementedError, match="Lesson 8"):
        if function == "analyze":
            starter.analyze(Path("evidence.json"))
        elif function == "create_candidate":
            starter.create_candidate()
        else:
            starter.evolve()
