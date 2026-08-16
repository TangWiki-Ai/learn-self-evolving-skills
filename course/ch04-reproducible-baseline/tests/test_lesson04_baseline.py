from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Mapping, Sequence
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


def _calculator(
    variant: str,
) -> Callable[[Sequence[Mapping[str, object]], int], tuple[int, int, float, float]]:
    module = _module(LESSON / variant / "baseline.py", f"lesson_04_{variant}")
    return module.baseline_reliability  # type: ignore[no-any-return]


def _records() -> list[Mapping[str, object]]:
    artifact = json.loads((LESSON / "baseline-comparison.json").read_text())
    records = artifact["records"]
    assert isinstance(records, list)
    return records


def test_solution_calculates_pass_at_1_and_all_pass_reliability() -> None:
    passed, total, pass_at_1, reliability = _calculator("solution")(_records(), 2)
    assert (passed, total) == (2, 3)
    assert pass_at_1 == pytest.approx(2 / 3)
    assert reliability == pytest.approx(1 / 3)


def test_solution_excludes_not_evaluated_iterations() -> None:
    records = [
        {"case_id": "a", "iteration": 0, "status": "pass"},
        {"case_id": "a", "iteration": 1, "status": "not_evaluated"},
    ]

    assert _calculator("solution")(records, 2) == (1, 1, 1.0, 0.0)


def test_starter_retains_the_lesson_gap() -> None:
    with pytest.raises(NotImplementedError, match="Lesson 4"):
        _calculator("starter")(_records(), 2)


def test_comparison_labels_measured_and_estimated_sources_separately() -> None:
    artifact = json.loads((LESSON / "baseline-comparison.json").read_text())

    assert artifact["outcome_source"] == {
        "kind": "measured",
        "measured": True,
        "estimated": False,
        "scope": "offline FakeEngine fixture",
    }
    assert artifact["live_provider_projection"] == {
        "kind": "estimated",
        "measured": False,
        "estimated": True,
        "scope": "illustrative only; no live provider run",
    }
