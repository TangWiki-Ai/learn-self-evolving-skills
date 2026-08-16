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


def _records() -> list[Mapping[str, object]]:
    payload = json.loads((LESSON / "baseline-results.json").read_text(encoding="utf-8"))
    assert payload["measured"] is False
    records = payload["records"]
    assert isinstance(records, list)
    return records


def _calculator(
    variant: str,
) -> Callable[[Sequence[Mapping[str, object]]], tuple[int, int, float]]:
    module = _module(LESSON / variant / "baseline.py", f"lesson_02_{variant}")
    return module.state_pass_rate  # type: ignore[no-any-return]


def test_solution_calculates_the_six_case_exercise_rate() -> None:
    assert _calculator("solution")(_records()) == (4, 6, pytest.approx(2 / 3))


def test_solution_rejects_an_empty_baseline() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _calculator("solution")([])


def test_starter_retains_the_lesson_gap() -> None:
    with pytest.raises(NotImplementedError, match="Lesson 2"):
        _calculator("starter")(_records())
