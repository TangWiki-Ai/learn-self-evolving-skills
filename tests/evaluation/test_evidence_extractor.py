from __future__ import annotations

from pathlib import Path

from ses.contracts import (
    EngineEvent,
    EngineRequest,
    RecordType,
    SchemaVersion,
    StateChange,
    StateDiff,
    Trace,
)
from ses.evaluation import build_trace
from ses.evaluation.evidence_extractor import (
    EXTRACTOR_SHA256,
    AmountAgreement,
    evidence_sha256,
    extract_evidence,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stream_json" / "normal_flow.jsonl"


def _trace() -> Trace:
    request = EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Return the order.",
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )
    events = tuple(
        EngineEvent.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
    )
    return build_trace(
        events,
        request=request,
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )


def _diff(*, reverse: bool = False) -> StateDiff:
    changed_items = [
        ("/status", StateChange(before="shipped", after="returned")),
        ("/refund/amount_minor", StateChange(before=0, after=1299)),
    ]
    if reverse:
        changed_items.reverse()
    return StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-1",
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        added={"/return/id": "return-1"},
        changed=dict(changed_items),
        summary="Return completed.",
    )


def test_extracts_stable_state_tool_amount_and_message_facts() -> None:
    evidence = extract_evidence(_trace(), _diff())

    assert evidence.extractor_version == "evidence-extractor-v1"
    assert evidence.extractor_sha256 == EXTRACTOR_SHA256
    assert [(fact.bucket, fact.path) for fact in evidence.state_diff_facts] == [
        ("added", "/return/id"),
        ("changed", "/refund/amount_minor"),
        ("changed", "/status"),
    ]
    assert [fact.tool_name for fact in evidence.tool_timeline] == [
        "preview_return",
        "confirm_return",
    ]
    assert evidence.tool_timeline[1].result_is_error is False
    assert [
        item.amount_minor for item in evidence.amount_reconciliation.observations
    ] == [
        1299,
        1299,
    ]
    assert evidence.amount_reconciliation.agreement is AmountAgreement.AGREES
    assert evidence.amount_reconciliation.distinct_amounts_minor == (1299,)
    assert len(evidence.key_messages) == 1
    assert evidence.key_messages[0].message_id == "message-1"
    assert "confirm the return" in evidence.key_messages[0].text
    assert evidence.key_messages[0].text_evidence_pointer == "/key_messages/0/text"


def test_output_and_hash_do_not_depend_on_mapping_insertion_order() -> None:
    first = extract_evidence(_trace(), _diff())
    second = extract_evidence(_trace(), _diff(reverse=True))

    assert first == second
    assert evidence_sha256(first) == evidence_sha256(second)


def test_amount_reconciliation_marks_one_observation_as_insufficient() -> None:
    diff = StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-1",
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        changed={"/refund/fee_minor": StateChange(before=0, after=25)},
    )

    evidence = extract_evidence(_trace(), diff)

    assert evidence.amount_reconciliation.agreement is AmountAgreement.INSUFFICIENT
    assert evidence.amount_reconciliation.distinct_amounts_minor == (1299,)
