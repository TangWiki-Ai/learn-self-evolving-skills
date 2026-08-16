from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ses.reporting.baseline import build_baseline_report
from ses.reporting.html_l1 import render_l1_html, write_l1_html
from ses.runner import BaselineRunner, BudgetLimits, CaseEvaluation, IterationStatus


def _evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
    del max_turns
    status = (
        IterationStatus.PASS
        if iteration_id == "iteration-0"
        else IterationStatus.AGENT_FAIL
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
                "status": "pass" if status is IterationStatus.PASS else "fail",
                "reason": "terminal state matches"
                if status is IterationStatus.PASS
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
    completed = BaselineRunner(tmp_path, _evaluate).run(
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
    assert report["formula_version"] == "l1-baseline-v1"
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
