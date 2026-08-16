from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ses.contracts.primitives import SchemaVersion
from ses.contracts.runner import (
    BudgetState,
    RunEventType,
    RunnerStatus,
    RunRecord,
)


def test_runner_contracts_are_strict_immutable_and_keep_statuses_distinct() -> None:
    budget = BudgetState(
        max_cases=2,
        max_turns_per_case=3,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_amount=Decimal("0.50"),
        cost_currency="CNY",
        consumed_cases=1,
        consumed_turns=2,
        consumed_input_tokens=20,
        consumed_output_tokens=10,
        consumed_cost_amount=Decimal("0.10"),
        consumed_latency_ms=12,
    )
    record = RunRecord(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="run_record",
        event_type=RunEventType.ATTEMPT,
        sequence=1,
        run_id="run-contract",
        config_hash="a" * 64,
        case_id="case-a",
        iteration_id="iteration-0",
        attempt_id="attempt-0",
        status=RunnerStatus.SIMULATOR_ERROR,
        budget=budget,
    )

    assert record.status is RunnerStatus.SIMULATOR_ERROR
    assert len({RunnerStatus.JUDGE_ERROR, RunnerStatus.INFRASTRUCTURE_ERROR}) == 2
    assert record.model_dump(mode="json")["budget"]["consumed_cost_amount"] == "0.10"
    with pytest.raises(ValidationError):
        RunRecord.model_validate({**record.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        BudgetState(**{**budget.model_dump(), "consumed_cost_amount": "-0.1"})
