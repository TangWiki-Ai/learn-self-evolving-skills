from __future__ import annotations

from decimal import Decimal

import pytest

from ses.contracts.primitives import SchemaVersion
from ses.contracts.runner import (
    PairCategory,
    PairedCaseResult,
    PairedComparison,
    RunnerStatus,
)


def _row() -> PairedCaseResult:
    return PairedCaseResult(
        case_id="case-1",
        category=PairCategory.FAIL_TO_PASS,
        baseline_status=RunnerStatus.AGENT_FAIL,
        skill_status=RunnerStatus.PASS,
        baseline_score=0.0,
        skill_score=1.0,
        score_delta=1.0,
        baseline_input_tokens=10,
        skill_input_tokens=12,
        baseline_output_tokens=3,
        skill_output_tokens=3,
        baseline_cost_amount=Decimal("0.01"),
        skill_cost_amount=Decimal("0.02"),
        baseline_latency_ms=20,
        skill_latency_ms=22,
        baseline_trace="baseline/trace.json",
        skill_trace="skill/trace.json",
        baseline_state_diff="baseline/diff.json",
        skill_state_diff="skill/diff.json",
        baseline_grade="baseline/grade.json",
        skill_grade="skill/grade.json",
    )


def test_paired_contract_round_trips_and_rejects_unknown_fields() -> None:
    value = PairedComparison(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="paired_comparison",
        baseline_run_id="run-baseline",
        skill_run_id="run-skill",
        skill_sha256="a" * 64,
        protocol_sha256="b" * 64,
        compatible=True,
        fresh_baseline=True,
        fresh_skill=True,
        category_counts={
            PairCategory.FAIL_TO_PASS: 1,
            PairCategory.PASS_TO_FAIL: 0,
            PairCategory.BOTH_PASS: 0,
            PairCategory.BOTH_FAIL: 0,
        },
        baseline_pass_rate=0.0,
        skill_pass_rate=1.0,
        baseline_input_tokens=10,
        skill_input_tokens=12,
        baseline_output_tokens=3,
        skill_output_tokens=3,
        baseline_cost_amount=Decimal("0.01"),
        skill_cost_amount=Decimal("0.02"),
        baseline_latency_ms=20,
        skill_latency_ms=22,
        cases=(_row(),),
    )

    assert PairedComparison.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValueError, match="Extra inputs"):
        PairedComparison.model_validate({**value.model_dump(), "future_alias": True})
