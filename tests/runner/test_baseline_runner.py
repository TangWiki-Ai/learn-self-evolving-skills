from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    CaseEvaluation,
    IterationStatus,
    compute_reliability_metrics,
    load_run_events,
)


def _evaluation(
    case_id: str,
    iteration_id: str,
    max_turns: int,
    *,
    status: IterationStatus = IterationStatus.PASS,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost: str = "0.01",
) -> CaseEvaluation:
    del max_turns
    return CaseEvaluation(
        case_id=case_id,
        iteration_id=iteration_id,
        status=status,
        turn_count=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=Decimal(cost),
        cost_currency="CNY",
        latency_ms=12,
        evidence=({"assertion": "state", "status": status.value},),
        tool_timeline=({"sequence": 1, "tool_name": "get_order"},),
        state_diff={"summary": "one state change", "changed": ["item.status"]},
        transcript=({"role": "user", "content": "I want a return."},),
    )


def test_repeated_run_is_append_only_and_reports_pass_at_1_and_pass_power_k(
    tmp_path: Path,
) -> None:
    def evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
        status = (
            IterationStatus.PASS if case_id == "case-a" else IterationStatus.AGENT_FAIL
        )
        return _evaluation(case_id, iteration_id, max_turns, status=status)

    completed = BaselineRunner(tmp_path, evaluate).run(
        run_id="run-repeat",
        case_ids=("case-a", "case-b"),
        iterations=2,
        budgets=BudgetLimits(max_cases=4, max_turns_per_case=3),
    )
    events = load_run_events(completed.events_path)

    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert (
        len([event for event in events if event["event_type"] == "iteration_result"])
        == 4
    )
    assert completed.metrics == {
        "sample_size": 2,
        "iteration_sample_size": 4,
        "pass_at_1": 0.5,
        "pass_power_k": 0.5,
        "k": 2,
    }


def test_resume_skips_completed_work_and_retries_recoverable_infrastructure_error(
    tmp_path: Path,
) -> None:
    calls: Counter[str] = Counter()

    def evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
        del iteration_id
        calls[case_id] += 1
        if case_id == "case-b" and calls[case_id] == 1:
            raise RuntimeError("temporary worker failure")
        return _evaluation(case_id, "iteration-0", max_turns)

    runner = BaselineRunner(tmp_path, evaluate)
    first = runner.run(
        run_id="run-resume",
        case_ids=("case-a", "case-b"),
        iterations=1,
        budgets=BudgetLimits(max_cases=2, max_turns_per_case=2),
    )
    prefix = first.events_path.read_bytes()
    resumed = runner.run(
        run_id="run-resume",
        case_ids=("case-a", "case-b"),
        iterations=1,
        budgets=BudgetLimits(max_cases=2, max_turns_per_case=2),
        resume=True,
    )

    assert resumed.events_path.read_bytes().startswith(prefix)
    assert calls == Counter({"case-b": 2, "case-a": 1})
    latest = resumed.latest_results
    assert latest[("case-a", "iteration-0")]["status"] == "pass"
    assert latest[("case-b", "iteration-0")]["status"] == "pass"


def test_explicit_rerun_creates_a_new_iteration_without_replacing_the_old_one(
    tmp_path: Path,
) -> None:
    runner = BaselineRunner(tmp_path, _evaluation)
    first = runner.run(
        run_id="run-rerun",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=3, max_turns_per_case=2),
    )
    prefix = first.events_path.read_bytes()
    rerun = runner.run(
        run_id="run-rerun",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=3, max_turns_per_case=2),
        resume=True,
        rerun_case_ids=("case-a",),
    )

    assert rerun.events_path.read_bytes().startswith(prefix)
    results = [
        event
        for event in load_run_events(rerun.events_path)
        if event["event_type"] == "iteration_result"
    ]
    assert [event["iteration_id"] for event in results] == [
        "iteration-0",
        "iteration-1",
    ]
    assert results[1]["supersedes_iteration_id"] == "iteration-0"


@pytest.mark.parametrize(
    ("limits", "expected_reason"),
    [
        (BudgetLimits(max_cases=0, max_turns_per_case=3), "case_limit"),
        (
            BudgetLimits(max_cases=2, max_turns_per_case=3, max_input_tokens=1),
            "input_token_limit",
        ),
        (
            BudgetLimits(max_cases=2, max_turns_per_case=3, max_output_tokens=1),
            "output_token_limit",
        ),
        (
            BudgetLimits(max_cases=2, max_turns_per_case=3, max_cost=Decimal("0.001")),
            "cost_limit",
        ),
    ],
)
def test_budget_stops_preserve_results_and_label_remaining_work(
    tmp_path: Path, limits: BudgetLimits, expected_reason: str
) -> None:
    completed = BaselineRunner(tmp_path, _evaluation).run(
        run_id=f"run-budget-{expected_reason}",
        case_ids=("case-a", "case-b"),
        iterations=1,
        budgets=limits,
    )

    statuses = [value["status"] for value in completed.latest_results.values()]
    assert "budget_stop" in statuses
    assert "not_evaluated" in statuses
    assert completed.stop_reason == expected_reason


def test_token_and_cost_overrun_uses_documented_budget_precedence(
    tmp_path: Path,
) -> None:
    runner = BaselineRunner(tmp_path, _evaluation)
    limits = BudgetLimits(
        max_cases=1,
        max_turns_per_case=3,
        max_input_tokens=1,
        max_output_tokens=1,
        max_cost=Decimal("0.001"),
    )
    completed = runner.run(
        run_id="run-budget-order",
        case_ids=("case-a",),
        iterations=1,
        budgets=limits,
    )

    assert completed.stop_reason == "input_token_limit"
    result = completed.latest_results[("case-a", "iteration-0")]
    partial_result = result["partial_result"]
    assert isinstance(partial_result, Mapping)
    assert partial_result["status"] == "pass"
    assert result["status"] == "budget_stop"

    prefix = completed.events_path.read_bytes()
    resumed = runner.run(
        run_id="run-budget-order",
        case_ids=("case-a",),
        iterations=1,
        budgets=limits,
        resume=True,
    )
    assert resumed.stop_reason == "input_token_limit"
    assert resumed.events_path.read_bytes() == prefix


def test_metrics_exclude_non_evaluated_and_keep_failure_categories_distinct() -> None:
    metrics = compute_reliability_metrics(
        [
            {"case_id": "a", "iteration_id": "iteration-0", "status": "pass"},
            {"case_id": "a", "iteration_id": "iteration-1", "status": "judge_error"},
            {"case_id": "b", "iteration_id": "iteration-0", "status": "agent_fail"},
            {"case_id": "b", "iteration_id": "iteration-1", "status": "not_evaluated"},
        ],
        k=2,
    )

    assert metrics["sample_size"] == 2
    assert metrics["iteration_sample_size"] == 3
    assert metrics["pass_at_1"] == 0.5
    assert metrics["pass_power_k"] == 0.0


def test_resume_rejects_a_different_plan(tmp_path: Path) -> None:
    runner = BaselineRunner(tmp_path, _evaluation)
    runner.run(
        run_id="run-plan",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
    )

    with pytest.raises(ValueError, match="configuration"):
        runner.run(
            run_id="run-plan",
            case_ids=("case-b",),
            iterations=1,
            budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
            resume=True,
        )


def test_events_file_contains_only_complete_json_lines(tmp_path: Path) -> None:
    completed = BaselineRunner(tmp_path, _evaluation).run(
        run_id="run-jsonl",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
    )

    lines = completed.events_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(isinstance(json.loads(line), dict) for line in lines)
