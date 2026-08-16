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
    AmountComponentKind,
    evidence_sha256,
    extract_evidence,
)
from ses.evaluator import run_pinned_case

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

    assert evidence.extractor_version == "evidence-extractor-v2"
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
        (item.kind, item.amount_minor)
        for item in evidence.amount_reconciliation.components
    ] == [(AmountComponentKind.STATE_REFUND, 1299)]
    assert evidence.amount_reconciliation.agreement is AmountAgreement.INSUFFICIENT
    assert len(evidence.key_messages) == 1
    assert evidence.key_messages[0].message_id == "message-1"
    assert "confirm the return" in evidence.key_messages[0].text
    assert evidence.key_messages[0].text_evidence_pointer == "/key_messages/0/text"


def test_output_and_hash_do_not_depend_on_mapping_insertion_order() -> None:
    first = extract_evidence(_trace(), _diff())
    second = extract_evidence(_trace(), _diff(reverse=True))

    assert first == second
    assert evidence_sha256(first) == evidence_sha256(second)


def test_unrelated_fee_is_not_compared_with_a_confirmed_amount() -> None:
    diff = StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-1",
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        changed={"/restocking_fee/amount_minor": StateChange(before=0, after=25)},
    )

    evidence = extract_evidence(_trace(), diff)

    assert evidence.amount_reconciliation.agreement is AmountAgreement.INSUFFICIENT
    assert [item.kind for item in evidence.amount_reconciliation.components] == [
        AmountComponentKind.STATE_RESTOCKING_FEE
    ]


def test_issue_2_success_trace_reconciles_named_refund_chain(
    tmp_path: Path,
) -> None:
    completed = run_pinned_case(tmp_path, run_id="run-issue-2-regression")
    trace = Trace.model_validate_json((completed.run_dir / "trace.json").read_bytes())
    diff = StateDiff.model_validate_json(
        (completed.run_dir / "state-diff.json").read_bytes()
    )

    reconciliation = extract_evidence(trace, diff).amount_reconciliation
    confirmed = next(
        relation
        for relation in reconciliation.relations
        if relation.relation_id == "confirmed_refund"
    )

    assert reconciliation.agreement is AmountAgreement.AGREES
    assert confirmed.agreement is AmountAgreement.AGREES
    assert confirmed.amounts_minor == (129_900, 129_900, 129_900, 129_900)
    assert confirmed.component_kinds == (
        AmountComponentKind.CONFIRMED_AMOUNT,
        AmountComponentKind.POLICY_COMPUTED_REFUND,
        AmountComponentKind.REFUND,
        AmountComponentKind.STATE_REFUND,
    )
    adjustments = {
        item.kind: item.amount_minor
        for item in reconciliation.components
        if item.phase.value == "final"
        and item.kind
        in {
            AmountComponentKind.RESTOCKING_FEE,
            AmountComponentKind.RESTOCKING_DISCOUNT,
            AmountComponentKind.SHIPPING_CLAWBACK,
            AmountComponentKind.RETURN_SHIPPING_FEE,
        }
    }
    assert adjustments == {
        AmountComponentKind.RESTOCKING_FEE: 0,
        AmountComponentKind.RESTOCKING_DISCOUNT: 0,
        AmountComponentKind.SHIPPING_CLAWBACK: 0,
        AmountComponentKind.RETURN_SHIPPING_FEE: 0,
    }
