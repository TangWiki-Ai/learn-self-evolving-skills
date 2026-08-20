from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from ses.automation.capstone import CapstoneIndexError, verify_capstone_index
from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CapstoneIndex,
    CapstoneReviewReceipt,
    FinalAggregateReport,
    MeasurementKind,
    SchemaVersion,
    ShoppingFinalScenarioMetrics,
)
from ses.contracts.shopping import ShoppingScenario

SHA = "a" * 64
NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _ref(path: str, digit: str = "a") -> ArtifactRef:
    return ArtifactRef(root=ArtifactRoot.RUN, path=path, sha256=digit * 64)


def _review(
    kind: Literal[
        "paired_trace",
        "failure_evidence",
        "failure_card",
        "gate_decision",
        "registry_history",
    ],
    path: str,
) -> CapstoneReviewReceipt:
    return CapstoneReviewReceipt(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="capstone_review_receipt",
        experiment_id="experiment-shopping-capstone",
        profile_sha256=SHA,
        learner_skill_sha256="b" * 64,
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        network_used=False,
        source_kind="learner_review",
        review_kind=kind,
        reviewed_artifact=_ref(path),
        reviewed_at=NOW,
    )


def _index(**changes: object) -> CapstoneIndex:
    payload: dict[str, object] = {
        "schema_version": SchemaVersion.V1ALPHA1,
        "record_type": "capstone_index",
        "experiment_id": "experiment-shopping-capstone",
        "lineage_id": "lineage-shopping-capstone",
        "profile_sha256": SHA,
        "mode": "fixed",
        "learning_completion": "workflow_complete",
        "measurement_kind": MeasurementKind.SYNTHETIC_OFFLINE,
        "network_used": False,
        "current_accepted_skill_sha256": "c" * 64,
        "total_cost_amount": "0",
        "cost_currency": "CNY",
        "cost_complete": True,
        "create_receipt": _ref("receipts/create.json", "1"),
        "static_receipt": _ref("receipts/static.json", "2"),
        "trigger_receipt": _ref("receipts/trigger.json", "3"),
        "paired_receipt": _ref("receipts/paired.json", "4"),
        "review_receipts": tuple(
            _ref(f"receipts/review-{index}.json", str(index + 4))
            for index in range(1, 6)
        ),
        "failure_evidence": _ref("manual/failure-evidence.json", "a"),
        "failure_cards": _ref("manual/failure-cards.json", "b"),
        "patch": _ref("manual/patch.json", "c"),
        "manual_gate_decision": _ref("registry/gates/manual/gate-decision.json", "d"),
        "manual_registry_events": _ref("registry/events.jsonl", "e"),
        "auto_evolve_state": _ref("state.json", "f"),
        "final_receipt": _ref("final/capstone-final-receipt.json", "0"),
        "l3_report": _ref("l3.html", "1"),
        "portfolio_manifest": _ref("portfolio/manifest.json", "2"),
        "release_manifest": _ref("package/release-manifest.json", "3"),
        "package_runtime_manifest": _ref("package/skill/skill-manifest.json", "4"),
        "created_at": NOW,
    }
    payload.update(changes)
    return CapstoneIndex.model_validate(payload)


def test_capstone_contract_requires_every_learner_review_kind() -> None:
    reviews = (
        _review("paired_trace", "runs/trace.json"),
        _review("failure_evidence", "manual/failure-evidence.json"),
        _review("failure_card", "manual/failure-cards.json"),
        _review("gate_decision", "registry/gates/rejected/gate-decision.json"),
        _review("registry_history", "registry/events.jsonl"),
    )

    assert {receipt.review_kind for receipt in reviews} == {
        "paired_trace",
        "failure_evidence",
        "failure_card",
        "gate_decision",
        "registry_history",
    }
    with pytest.raises(ValidationError):
        CapstoneReviewReceipt.model_validate(
            {
                **reviews[0].model_dump(mode="python"),
                "source_kind": "reference_fallback",
            }
        )


def test_capstone_index_separates_fixed_and_live_measurement() -> None:
    fixed = _index()

    assert fixed.learning_completion == "workflow_complete"
    assert fixed.source_learning_index is None
    with pytest.raises(ValidationError, match="fixed capstone"):
        _index(source_learning_index=_ref("prior/index.json"))
    with pytest.raises(ValidationError, match="live capstone"):
        _index(
            mode="live",
            measurement_kind=MeasurementKind.LIVE_MEASURED,
            network_used=True,
        )


def test_capstone_index_rejects_duplicate_or_private_final_refs() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        _index(static_receipt=_ref("receipts/create.json", "1"))
    with pytest.raises(ValidationError, match="private final"):
        _index(l3_report=_ref("final/private-results.json", "1"))


def test_verifier_rejects_reference_fallback_as_create_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    create_path = root / "receipts/create.json"
    create_path.parent.mkdir(parents=True)
    create_path.write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "record_type": "shopping_learner_receipt",
                "stage": "create",
                "profile_sha256": SHA,
                "skill_sha256": "b" * 64,
                "measurement_level": "synthetic_offline",
                "network_used": False,
                "source_kind": "reference_fallback",
                "inputs": [],
                "outputs": [],
                "primary_metrics": {},
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "stop_reason": "completed",
                "next_command": "ses skill static-gate",
                "recorded_at": "2026-08-20T00:00:00Z",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    create_ref = ArtifactRef(
        root=ArtifactRoot.RUN,
        path="receipts/create.json",
        sha256=hashlib.sha256(create_path.read_bytes()).hexdigest(),
    )

    with pytest.raises(CapstoneIndexError, match="learner-created"):
        verify_capstone_index(root, _index(create_receipt=create_ref))


def test_capstone_final_aggregate_rejects_tampered_scenario_totals() -> None:
    strata = tuple(
        ShoppingFinalScenarioMetrics(
            scenario=scenario,
            case_count=3,
            full_success_count=3,
            mean_strict_reward=Decimal("1"),
            safety_violation_count=0,
        )
        for scenario in ShoppingScenario
    )
    payload: dict[str, object] = {
        "schema_version": SchemaVersion.V1ALPHA2,
        "record_type": "final_aggregate_report",
        "experiment_id": "experiment-shopping-capstone",
        "subject_skill_sha256": "b" * 64,
        "final_lock_sha256": "c" * 64,
        "mode": "fixed",
        "measurement_kind": MeasurementKind.SYNTHETIC_OFFLINE,
        "network_used": False,
        "result_source": "fresh_fixed_execution",
        "executed_at": NOW,
        "case_count": 12,
        "pass_count": 12,
        "pass_rate": 1,
        "cost_amount": Decimal("0"),
        "cost_currency": "CNY",
        "cost_complete": True,
        "input_tokens": 0,
        "output_tokens": 0,
        "private_results_sha256": "d" * 64,
        "full_success_count": 12,
        "mean_strict_reward": Decimal("1"),
        "safety_violation_count": 1,
        "scenario_metrics": strata,
    }

    with pytest.raises(ValidationError, match="aggregate counts"):
        FinalAggregateReport.model_validate(payload)
