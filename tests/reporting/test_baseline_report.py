from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from ses.contracts.runner import RunnerStatus
from ses.reporting.baseline import build_baseline_report
from ses.reporting.html_l1 import render_l1_html, write_l1_html
from ses.runner import BaselineRunner, BudgetLimits, CaseEvaluation
from ses.runner.baseline import EvaluationContext


class _TestEvaluator:
    def __init__(self, evaluate: Callable[[str, str, int], CaseEvaluation]) -> None:
        self._evaluate = evaluate

    def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
        return self._evaluate(context.case_id, context.iteration_id, context.max_turns)


def _evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
    del max_turns
    status = (
        RunnerStatus.PASS if iteration_id == "iteration-0" else RunnerStatus.AGENT_FAIL
    )
    return CaseEvaluation(
        case_id=case_id,
        iteration_id=iteration_id,
        status=status,
        turn_count=2,
        input_tokens=21,
        output_tokens=13,
        cost_amount=Decimal("0.0123"),
        cost_currency="CNY",
        latency_ms=34,
        evidence=(
            {
                "assertion_id": "state:item-returned",
                "status": "pass" if status is RunnerStatus.PASS else "fail",
                "reason": "terminal state matches"
                if status is RunnerStatus.PASS
                else "item stayed delivered",
            },
        ),
        tool_timeline=(
            {"sequence": 1, "tool_name": "get_order", "is_error": False},
            {"sequence": 2, "tool_name": "process_return", "is_error": False},
        ),
        state_diff={
            "summary": "item status changed",
            "changed": [{"path": "/order_items/ITEM/status", "after": "returned"}],
        },
        transcript=(
            {"role": "user", "content": "I want <a return> & a refund."},
            {"role": "assistant", "content": "I can help."},
        ),
    )


def _run(tmp_path: Path, *, cases: int = 2, iterations: int = 2) -> Path:
    completed = BaselineRunner(tmp_path, _TestEvaluator(_evaluate)).run(
        run_id="run-report",
        case_ids=tuple(f"case-{index}" for index in range(cases)),
        iterations=iterations,
        budgets=BudgetLimits(
            max_cases=cases * iterations,
            max_turns_per_case=3,
        ),
    )
    return completed.events_path


def test_report_aggregates_records_without_regrading(tmp_path: Path) -> None:
    report = build_baseline_report(_run(tmp_path))

    assert report["run_id"] == "run-report"
    assert report["formula_version"] == "l1-baseline-v2"
    assert report["metrics"] == {
        "sample_size": 2,
        "iteration_sample_size": 4,
        "pass_at_1": 1.0,
        "pass_power_k": 0.0,
        "k": 2,
    }
    assert report["totals"] == {
        "input_tokens": 84,
        "output_tokens": 52,
        "cost_amount": "0.0492",
        "cost_currency": "CNY",
        "latency_ms": 136,
    }
    cases = report["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 2
    assert [result["status"] for result in cases[0]["repetitions"]] == [
        "pass",
        "agent_fail",
    ]


def test_report_marks_an_unpriced_provider_attempt_incomplete(tmp_path: Path) -> None:
    def evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
        return replace(
            _evaluate(case_id, iteration_id, max_turns),
            cost_amount=Decimal(0),
            cost_complete=False,
        )

    completed = BaselineRunner(tmp_path, _TestEvaluator(evaluate)).run(
        run_id="run-unpriced",
        case_ids=("case-0",),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=3),
    )

    report = build_baseline_report(completed.events_path)

    assert report["totals"] == {
        "input_tokens": 21,
        "output_tokens": 13,
        "cost_amount": "0",
        "cost_currency": "CNY",
        "cost_complete": False,
        "latency_ms": 34,
    }
    cases = report["cases"]
    assert isinstance(cases, list)
    assert cases[0]["repetitions"][0]["cost_complete"] is False
    html = render_l1_html(report)
    assert html.count("cost=unavailable") == 1
    assert '<div class="metric">unavailable</div>' in html


def test_html_is_self_contained_escaped_and_exposes_required_l1_evidence(
    tmp_path: Path,
) -> None:
    html = render_l1_html(build_baseline_report(_run(tmp_path)))

    assert "pass@1" in html
    assert "pass^k" in html
    assert "Evidence" in html
    assert "Tool timeline" in html
    assert "StateDiff" in html
    assert "Usage / cost / latency" in html
    assert "Repeated results" in html
    assert "&lt;a return&gt; &amp; a refund" in html
    lowered = html.casefold()
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "<script" not in lowered
    assert "@import" not in lowered


def test_representative_html_stays_below_two_megabytes(tmp_path: Path) -> None:
    events_path = _run(tmp_path, cases=40, iterations=5)
    output = tmp_path / "l1.html"

    assert write_l1_html(events_path, output) == output
    assert output.stat().st_size < 2 * 1024 * 1024
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_report_totals_all_attempts_but_displays_latest_iteration_result(
    tmp_path: Path,
) -> None:
    events_path = _run(tmp_path, cases=1, iterations=1)
    events = events_path.read_text(encoding="utf-8").splitlines()
    retry = json.loads(events[-1])
    retry["sequence"] = len(events)
    retry["attempt_id"] = "attempt-1"
    retry["status"] = "pass"
    retry["usage"] = {
        "input_tokens": 2,
        "output_tokens": 3,
        "cost_amount": "0.004",
        "cost_currency": "CNY",
    }
    retry["latency_ms"] = 5
    events_path.write_text("\n".join([*events, json.dumps(retry)]) + "\n")

    report = build_baseline_report(events_path)

    assert report["totals"] == {
        "input_tokens": 23,
        "output_tokens": 16,
        "cost_amount": "0.0163",
        "cost_currency": "CNY",
        "latency_ms": 39,
    }
    cases = report["cases"]
    assert isinstance(cases, list)
    assert len(cases[0]["repetitions"]) == 1


def test_report_keeps_budget_skipped_plan_entries_visible(tmp_path: Path) -> None:
    completed = BaselineRunner(tmp_path, _TestEvaluator(_evaluate)).run(
        run_id="run-partial-report",
        case_ids=("case-a", "case-b"),
        iterations=1,
        budgets=BudgetLimits(max_cases=1, max_turns_per_case=3),
    )

    report = build_baseline_report(completed.events_path)

    assert report["metrics"] == {
        "sample_size": 1,
        "iteration_sample_size": 1,
        "pass_at_1": 1.0,
        "pass_power_k": 0.5,
        "k": 1,
    }
    cases = report["cases"]
    assert isinstance(cases, list)
    assert [case["first_status"] for case in cases] == ["pass", "not_evaluated"]
