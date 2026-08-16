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


def _variant(name: str) -> ModuleType:
    return _module(LESSON / name / "baseline.py", f"lesson_04_{name}")


def _records() -> list[Mapping[str, object]]:
    artifact = json.loads((LESSON / "baseline-comparison.json").read_text())
    records = artifact["records"]
    assert isinstance(records, list)
    return records


def test_solution_evaluator_resumes_one_session_then_runs_final_judge() -> None:
    received_sessions: list[str | None] = []

    def agent_turn(message: str, session_id: str | None) -> tuple[str, str]:
        received_sessions.append(session_id)
        return f"answer to {message}", "session-a"

    judged: list[object] = []

    def judge(transcript: object) -> str:
        judged.append(transcript)
        return "pass"

    result = _variant("solution").evaluate_case(
        "case-a", ["I need help", "The order is 42"], agent_turn, judge
    )

    assert received_sessions == [None, "session-a"]
    assert judged
    assert result["status"] == "pass"
    assert result["turn_count"] == 2


def test_solution_runner_feeds_records_into_l1_without_dropping_incomplete_k() -> None:
    solution = _variant("solution")
    records = _records()
    planned = solution.run_baseline(["a", "b"], lambda case_id: {"case_id": case_id})
    report = solution.build_l1_report(records, 2)

    assert planned == [{"case_id": "a"}, {"case_id": "b"}]
    assert report["metrics"] == {
        "first_passes": 2,
        "sample_size": 3,
        "pass_at_1": pytest.approx(2 / 3),
        "pass_power_k": pytest.approx(1 / 3),
        "k": 2,
    }
    assert report["records"] == records


def test_solution_counts_a_case_with_only_one_of_two_passes_in_the_denominator() -> (
    None
):
    report = _variant("solution").build_l1_report(
        [
            {"case_id": "a", "iteration": 0, "status": "pass"},
            {"case_id": "a", "iteration": 1, "status": "pass"},
            {"case_id": "b", "iteration": 0, "status": "pass"},
        ],
        2,
    )

    assert report["metrics"]["pass_power_k"] == 0.5


@pytest.mark.parametrize(
    "function", ["evaluate_case", "run_baseline", "build_l1_report"]
)
def test_starter_retains_each_core_pipeline_gap(function: str) -> None:
    starter = _variant("starter")
    target = getattr(starter, function)
    with pytest.raises(NotImplementedError, match="Lesson 4"):
        if function == "evaluate_case":
            target(
                "case-a",
                [],
                lambda message, session: (message, "session"),
                lambda _: "pass",
            )
        elif function == "run_baseline":
            target(["case-a"], lambda case_id: {"case_id": case_id})
        else:
            target(_records(), 2)


def test_comparison_labels_measured_and_estimated_sources_separately() -> None:
    artifact = json.loads((LESSON / "baseline-comparison.json").read_text())
    assert artifact["outcome_source"]["kind"] == "synthetic_fixture"
    assert artifact["outcome_source"]["measured"] is False
    assert artifact["live_provider_projection"]["kind"] == "estimated"
