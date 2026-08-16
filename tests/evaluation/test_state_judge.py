from __future__ import annotations

import hashlib

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    GradeStatus,
    RecordType,
    SchemaVersion,
    ShopSnapshot,
    StateChange,
    StateDiff,
    artifact_json_bytes,
)
from ses.evaluation import judge_state


def _snapshot(
    snapshot_id: str,
    *,
    amount_minor: int = 1299,
    case_id: str = "case-1",
    policy_version: str = "returns-v1",
) -> ShopSnapshot:
    return ShopSnapshot.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "shop_snapshot",
            "snapshot_id": snapshot_id,
            "case_id": case_id,
            "captured_at": "2026-08-16T04:00:00Z",
            "policy_version": policy_version,
            "state": {
                "order_id": "order-1",
                "refund": {"amount_minor": amount_minor, "currency": "USD"},
                "status": "returned",
            },
        }
    )


def _snapshot_artifact(snapshot: ShopSnapshot) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=f"snapshots/{snapshot.snapshot_id}.json",
        sha256=hashlib.sha256(artifact_json_bytes(snapshot)).hexdigest(),
    )


def _diff(diff_id: str, *, amount_minor: int = 1299) -> StateDiff:
    return StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id=diff_id,
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        added={"/refund_id": "refund-1"},
        removed={"/return_pending": True},
        changed={
            "/status": StateChange(before="shipped", after="returned"),
            "/refund/amount_minor": StateChange(before=0, after=amount_minor),
        },
        summary="Order returned.",
    )


def _diff_artifact(diff: StateDiff) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=f"diffs/{diff.diff_id}.json",
        sha256=hashlib.sha256(artifact_json_bytes(diff)).hexdigest(),
    )


def test_state_judge_emits_one_evidence_backed_assertion_per_snapshot_leaf() -> None:
    expected = _snapshot("expected")
    actual = _snapshot("actual")

    assertions = judge_state(
        expected,
        actual,
        evidence_artifact=_snapshot_artifact(actual),
    )

    assert assertions
    assert {assertion.status for assertion in assertions} == {GradeStatus.PASS}
    assert all(assertion.evidence for assertion in assertions)
    assert any(
        evidence.json_pointer == "/state/refund/amount_minor"
        for assertion in assertions
        for evidence in assertion.evidence
    )


def test_state_judge_reports_exact_integer_amount_mismatch_and_evidence() -> None:
    expected = _snapshot("expected", amount_minor=1299)
    actual = _snapshot("actual", amount_minor=1199)
    artifact = _snapshot_artifact(actual)

    assertions = judge_state(expected, actual, evidence_artifact=artifact)
    amount_assertion = next(
        assertion
        for assertion in assertions
        if assertion.assertion_id.endswith("refund/amount_minor")
    )

    assert amount_assertion.status is GradeStatus.FAIL
    assert "actual=1199" in amount_assertion.reason
    assert "expected=1299" in amount_assertion.reason
    artifact.verify_bytes(artifact_json_bytes(actual))
    assert amount_assertion.evidence[0].artifact == artifact


def test_state_diff_judge_ignores_human_summary_but_checks_added_removed_changed() -> (
    None
):
    expected = _diff("expected")
    actual = _diff("actual")
    actual_data = actual.model_dump(mode="json")
    actual_data["summary"] = "A different explanation."
    actual = StateDiff.model_validate(actual_data)

    assertions = judge_state(
        expected_diff=expected,
        actual_diff=actual,
        evidence_artifact=_diff_artifact(actual),
    )

    assert len(assertions) == 4
    assert all(assertion.status is GradeStatus.PASS for assertion in assertions)
    assert any(
        evidence.json_pointer == "/changed/~1refund~1amount_minor"
        for assertion in assertions
        for evidence in assertion.evidence
    )


def test_state_judge_marks_missing_evidence_not_evaluated_instead_of_fabricating_a_ref() -> (
    None
):
    assertions = judge_state(_snapshot("expected"), _snapshot("actual"))

    assert assertions
    assert {assertion.status for assertion in assertions} == {GradeStatus.NOT_EVALUATED}
    assert all(not assertion.evidence for assertion in assertions)


def test_state_judge_detects_a_snapshot_from_the_wrong_environment() -> None:
    expected = _snapshot("expected", case_id="case-1", policy_version="returns-v1")
    actual = _snapshot("actual", case_id="case-2", policy_version="returns-v2")

    assertions = judge_state(
        expected,
        actual,
        evidence_artifact=_snapshot_artifact(actual),
    )

    metadata = {
        assertion.assertion_id: assertion.status
        for assertion in assertions
        if assertion.assertion_id in {"state:case_id", "state:policy_version"}
    }
    assert metadata == {
        "state:case_id": GradeStatus.FAIL,
        "state:policy_version": GradeStatus.FAIL,
    }
