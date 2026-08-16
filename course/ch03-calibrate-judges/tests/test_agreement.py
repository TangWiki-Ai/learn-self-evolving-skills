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


def _cases() -> list[Mapping[str, object]]:
    payload = json.loads(
        (LESSON / "agreement-experiment.json").read_text(encoding="utf-8")
    )
    assert payload["human_reviewed"] is True
    assert payload["live_model_measured"] is False
    cases = payload["cases"]
    assert isinstance(cases, list)
    return cases


def _summarizer(
    variant: str,
) -> Callable[[Sequence[Mapping[str, object]], str], Mapping[str, object]]:
    module = _module(LESSON / variant / "agreement.py", f"lesson_03_{variant}")
    return module.summarize_agreement  # type: ignore[no-any-return]


def test_solution_reports_actual_llm_and_agent_agreement() -> None:
    llm = _summarizer("solution")(_cases(), "llm_status")
    agent = _summarizer("solution")(_cases(), "agent_status")

    assert (llm["agreements"], llm["total"], llm["agreement"]) == (2, 4, 0.5)
    assert (agent["agreements"], agent["total"], agent["agreement"]) == (
        3,
        4,
        0.75,
    )
    assert llm["confusion_matrix"]["not_evaluated"]["pass"] == 1  # type: ignore[index]
    assert agent["disagreements"] == ["cal-004"]


def test_solution_rejects_missing_predictions() -> None:
    rows = _cases()
    rows[0] = dict(rows[0])
    del rows[0]["llm_status"]

    with pytest.raises(ValueError, match="missing"):
        _summarizer("solution")(rows, "llm_status")


def test_starter_retains_the_lesson_gap() -> None:
    with pytest.raises(NotImplementedError, match="Lesson 3"):
        _summarizer("starter")(_cases(), "llm_status")
