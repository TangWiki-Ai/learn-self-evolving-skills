from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ses.contracts.runner import RunnerStatus
from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    CaseEvaluation,
    compute_reliability_metrics,
    load_run_events,
)
from ses.runner.baseline import EvaluationContext


class _TestEvaluator:
    def __init__(self, evaluate: Callable[[str, str, int], CaseEvaluation]) -> None:
        self._evaluate = evaluate

    def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
        return self._evaluate(context.case_id, context.iteration_id, context.max_turns)


def _evaluation(
    case_id: str,
    iteration_id: str,
    max_turns: int,
    *,
    status: RunnerStatus = RunnerStatus.PASS,
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
        status = RunnerStatus.PASS if case_id == "case-a" else RunnerStatus.AGENT_FAIL
        return _evaluation(case_id, iteration_id, max_turns, status=status)

    completed = BaselineRunner(tmp_path, _TestEvaluator(evaluate)).run(
        run_id="run-repeat",
        case_ids=("case-a", "case-b"),
        iterations=2,
        budgets=BudgetLimits(max_cases=4, max_turns_per_case=3),
    )
    events = load_run_events(completed.events_path)

    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert len([event for event in events if event["event_type"] == "attempt"]) == 4
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

    runner = BaselineRunner(tmp_path, _TestEvaluator(evaluate))
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
    runner = BaselineRunner(tmp_path, _TestEvaluator(_evaluation))
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
        if event["event_type"] == "attempt"
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
    completed = BaselineRunner(tmp_path, _TestEvaluator(_evaluation)).run(
        run_id=f"run-budget-{expected_reason}",
        case_ids=("case-a", "case-b"),
        iterations=1,
        budgets=limits,
    )

    events = load_run_events(completed.events_path)
    assert any(event["status"] == "budget_stop" for event in events[1:])
    assert all(
        value["status"] != "not_evaluated"
        for value in completed.latest_results.values()
    )
    assert completed.stop_reason == expected_reason


def test_token_and_cost_overrun_uses_documented_budget_precedence(
    tmp_path: Path,
) -> None:
    runner = BaselineRunner(tmp_path, _TestEvaluator(_evaluation))
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
    assert result["status"] == "pass"
    events = load_run_events(completed.events_path)
    assert events[-1]["status"] == "budget_stop"
    assert events[-1]["event_type"] == "budget_stop"

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
            {"case_id": "c", "iteration_id": "iteration-0", "status": "not_evaluated"},
        ],
        k=2,
    )

    assert metrics["sample_size"] == 2
    assert metrics["iteration_sample_size"] == 3
    assert metrics["pass_at_1"] == 0.5
    assert metrics["pass_power_k"] == 0.0


def test_pass_power_k_counts_wholly_unevaluated_planned_cases() -> None:
    metrics = compute_reliability_metrics(
        [
            {"case_id": "a", "iteration_id": "iteration-0", "status": "pass"},
            {"case_id": "b", "iteration_id": "iteration-0", "status": "not_evaluated"},
        ],
        k=1,
    )

    assert metrics["sample_size"] == 1
    assert metrics["iteration_sample_size"] == 1
    assert metrics["pass_at_1"] == 1.0
    assert metrics["pass_power_k"] == 0.5


def test_pass_power_k_counts_sampled_cases_with_missing_repetitions_as_unreliable() -> (
    None
):
    metrics = compute_reliability_metrics(
        [
            {"case_id": "a", "iteration_id": "iteration-0", "status": "pass"},
            {"case_id": "a", "iteration_id": "iteration-1", "status": "pass"},
            {"case_id": "b", "iteration_id": "iteration-0", "status": "pass"},
        ],
        k=2,
    )

    assert metrics["sample_size"] == 2
    assert metrics["pass_power_k"] == 0.5


def test_retry_usage_and_latency_are_counted_for_every_append_only_attempt(
    tmp_path: Path,
) -> None:
    calls = 0

    def evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _evaluation(
                case_id,
                iteration_id,
                max_turns,
                status=RunnerStatus.INFRASTRUCTURE_ERROR,
                input_tokens=7,
                output_tokens=3,
                cost="0.02",
            )
        return _evaluation(
            case_id,
            iteration_id,
            max_turns,
            input_tokens=11,
            output_tokens=5,
            cost="0.03",
        )

    runner = BaselineRunner(tmp_path, _TestEvaluator(evaluate))
    first = runner.run(
        run_id="run-attempt-usage",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
    )
    resumed = runner.run(
        run_id="run-attempt-usage",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
        resume=True,
    )

    events = load_run_events(resumed.events_path)
    attempts = [event for event in events if event["event_type"] == "attempt"]
    assert first.latest_results[("case-a", "iteration-0")]["status"] == (
        "infrastructure_error"
    )
    assert [attempt["attempt_id"] for attempt in attempts] == ["attempt-0", "attempt-1"]
    summary = json.loads(resumed.summary_path.read_text())
    assert summary["budget"]["consumed_input_tokens"] == 18
    assert summary["budget"]["consumed_output_tokens"] == 8
    assert summary["budget"]["consumed_cost_amount"] == "0.05"
    assert summary["budget"]["consumed_latency_ms"] == 24


def test_resume_rejects_a_different_plan(tmp_path: Path) -> None:
    runner = BaselineRunner(tmp_path, _TestEvaluator(_evaluation))
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


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("data_version", "data-v2"),
        ("model_lock_hash", "b" * 64),
        ("skill_hash", "c" * 64),
        ("protocol_version", "ses-runner-v2"),
    ],
)
def test_resume_rejects_any_reproducibility_identity_change(
    tmp_path: Path, changed: str, value: str
) -> None:
    runner = BaselineRunner(tmp_path, _TestEvaluator(_evaluation))
    identity: dict[str, Any] = {
        "data_version": "data-v1",
        "model_lock_hash": "a" * 64,
        "skill_hash": "d" * 64,
        "protocol_version": "ses-runner-v1",
    }
    runner.run(
        run_id=f"run-identity-{changed.replace('_', '-')}",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
        **identity,
    )

    with pytest.raises(ValueError, match="configuration"):
        runner.run(
            run_id=f"run-identity-{changed.replace('_', '-')}",
            case_ids=("case-a",),
            iterations=1,
            budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
            resume=True,
            **{**identity, changed: value},
        )


def test_events_file_contains_only_complete_json_lines(tmp_path: Path) -> None:
    completed = BaselineRunner(tmp_path, _TestEvaluator(_evaluation)).run(
        run_id="run-jsonl",
        case_ids=("case-a",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=2),
    )

    lines = completed.events_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(isinstance(json.loads(line), dict) for line in lines)
