from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ses.contracts import MeasurementKind
from ses.contracts.artifact import ArtifactRef, ArtifactRoot
from ses.contracts.primitives import SchemaVersion
from ses.contracts.runner import (
    PairCategory,
    PairedCaseResult,
    PairedComparison,
    RunnerStatus,
    pair_execution_sha256,
)


def _row() -> PairedCaseResult:
    def ref(path: str, digest: str) -> ArtifactRef:
        return ArtifactRef(root=ArtifactRoot.RUN, path=path, sha256=digest * 64)

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
        baseline_trace=ref(
            "run-baseline/artifacts/case-1/iteration-0/attempt-0/trace.json", "1"
        ),
        skill_trace=ref(
            "run-skill/artifacts/case-1/iteration-0/attempt-0/trace.json", "2"
        ),
        baseline_state_diff=ref(
            "run-baseline/artifacts/case-1/iteration-0/attempt-0/diff.json", "3"
        ),
        skill_state_diff=ref(
            "run-skill/artifacts/case-1/iteration-0/attempt-0/diff.json", "4"
        ),
        baseline_grade=ref(
            "run-baseline/artifacts/case-1/iteration-0/attempt-0/grade.json", "5"
        ),
        skill_grade=ref(
            "run-skill/artifacts/case-1/iteration-0/attempt-0/grade.json", "6"
        ),
    )


def test_paired_contract_round_trips_and_rejects_unknown_fields() -> None:
    measured_at = datetime(2026, 8, 17, tzinfo=UTC)
    baseline_events = ArtifactRef(
        root=ArtifactRoot.RUN,
        path="run-baseline/events.jsonl",
        sha256="f" * 64,
    )
    skill_events = ArtifactRef(
        root=ArtifactRoot.RUN,
        path="run-skill/events.jsonl",
        sha256="0" * 64,
    )
    protocol_sha256 = "b" * 64
    value = PairedComparison(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="paired_comparison",
        baseline_run_id="run-baseline",
        skill_run_id="run-skill",
        skill_sha256="a" * 64,
        protocol_sha256=protocol_sha256,
        pair_execution_sha256=pair_execution_sha256(
            baseline_events=baseline_events,
            skill_events=skill_events,
            protocol_sha256=protocol_sha256,
            measured_at=measured_at,
            measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        ),
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=measured_at,
        data_version="d" * 64,
        model_lock_sha256="e" * 64,
        engine_version="fake-engine:1",
        model_id="deterministic-fake",
        baseline_events=baseline_events,
        skill_events=skill_events,
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
        cost_currency="CNY",
        baseline_latency_ms=20,
        skill_latency_ms=22,
        cases=(_row(),),
    )

    wire = value.model_dump(mode="json")
    assert "shopping_metrics" not in wire
    assert "comparable" not in wire["cases"][0]
    assert "baseline_domain_result" not in wire["cases"][0]
    assert "skill_domain_result" not in wire["cases"][0]
    assert PairedComparison.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValueError, match="Extra inputs"):
        PairedComparison.model_validate({**value.model_dump(), "future_alias": True})


def test_paired_evidence_rejects_path_escape_and_requires_sha256() -> None:
    payload = _row().model_dump(mode="json")
    payload["baseline_trace"] = {
        "root": "run",
        "path": "../copied-history/trace.json",
        "sha256": "1" * 64,
    }

    with pytest.raises(ValueError, match="traverse"):
        PairedCaseResult.model_validate(payload)


def test_paired_infrastructure_outcome_allows_partial_evidence() -> None:
    payload = _row().model_dump(mode="json")
    payload.update(
        {
            "category": PairCategory.FAIL_TO_PASS.value,
            "baseline_status": RunnerStatus.INFRASTRUCTURE_ERROR.value,
            "baseline_trace": None,
            "baseline_state_diff": None,
            "baseline_grade": None,
        }
    )

    row = PairedCaseResult.model_validate(payload)

    assert row.baseline_status is RunnerStatus.INFRASTRUCTURE_ERROR
    assert row.baseline_trace is None


def test_paired_completed_outcome_requires_full_evidence() -> None:
    payload = _row().model_dump(mode="json")
    payload["skill_grade"] = None

    with pytest.raises(ValueError, match="full evidence"):
        PairedCaseResult.model_validate(payload)
