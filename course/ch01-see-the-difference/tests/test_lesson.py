from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

LESSON = Path(__file__).parents[1]


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _comparison() -> Mapping[str, object]:
    payload = json.loads(
        (LESSON / "comparison-artifact.json").read_text(encoding="utf-8")
    )
    assert payload["claim"] == "qualitative_demo_only"
    return payload


def _chooser(variant: str):
    module = _module(LESSON / variant / "skill_choice.py", f"lesson_01_{variant}")
    return module.choose_skill


def test_solution_prefers_a_safe_generated_skill() -> None:
    choose_skill = _chooser("solution")
    generated = {"source": "generated", "installable": True}
    reference = {"source": "reference", "installable": True}

    assert choose_skill(generated, reference) == generated


def test_solution_marks_reference_fallback_when_generation_is_weak() -> None:
    choose_skill = _chooser("solution")
    reference = {"source": "reference", "installable": True}

    assert choose_skill(None, reference) == {
        "source": "reference_fallback",
        "installable": True,
        "reference": True,
    }


def test_solution_comparison_keeps_the_qualitative_boundary_visible() -> None:
    comparison = _comparison()
    assert comparison["measured"] is False
    assert "stable improvement" in comparison["notice"]
    runs = comparison["runs"]
    assert isinstance(runs, Mapping)
    assert runs["without_skill"]["messages"]
    assert runs["without_skill"]["tool_calls"]
    assert runs["without_skill"]["state_result"]
    assert runs["with_skill"]["messages"]
    assert runs["with_skill"]["tool_calls"]
    assert runs["with_skill"]["state_result"]


def test_starter_retains_the_lesson_gap() -> None:
    with pytest.raises(NotImplementedError, match="Lesson 1"):
        _chooser("starter")(
            {"source": "generated", "installable": True},
            {"source": "reference", "installable": True},
        )
