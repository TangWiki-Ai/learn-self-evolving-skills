from __future__ import annotations

import hashlib
import json
from typing import ClassVar, Literal, cast

import pytest
from pydantic import Field, JsonValue, ValidationError

from ses.contracts import (
    AssertionResult,
    CaseDefinition,
    CaseGrade,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    GradeStatus,
    JudgeKind,
    SchemaVersion,
    ShopSnapshot,
    StateDiff,
    ToolResult,
    Trace,
    VersionedRecord,
    artifact_json_bytes,
    content_sha256,
)


class ExampleRecord(VersionedRecord):
    record_type: Literal["example"]
    value: int


class OriginalCompatibleRecord(VersionedRecord):
    record_type: Literal["compatible"]
    value: int


class ExtendedCompatibleRecord(VersionedRecord):
    record_type: Literal["compatible"]
    value: int
    optional_lock: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    content_hash_exclude_if_none: ClassVar[frozenset[str]] = frozenset(
        {"optional_lock"}
    )


def _event(*, occurred_at: str = "2026-08-16T04:00:00Z") -> EngineEvent:
    return EngineEvent.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "engine_event",
            "event_id": "event-1",
            "request_id": "request-1",
            "sequence": 0,
            "occurred_at": occurred_at,
            "payload": {
                "kind": "text_delta",
                "message_id": "message-1",
                "text": "done",
            },
        }
    )


def _snapshot(
    *,
    captured_at: str = "2026-08-16T04:00:00Z",
    state: dict[str, JsonValue] | None = None,
) -> ShopSnapshot:
    return ShopSnapshot.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "shop_snapshot",
            "snapshot_id": "snapshot-1",
            "case_id": "case-1",
            "captured_at": captured_at,
            "policy_version": "returns-v1",
            "state": state
            or {
                "order": {
                    "attributes": {"priority": "normal", "channel": "web"},
                    "items": [{"sku": "sku-1", "quantity": 1}],
                },
                "status": "returned",
            },
        }
    )


def _diff(*, summary: str = "Order returned.") -> StateDiff:
    return StateDiff.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "state_diff",
            "diff_id": "diff-1",
            "before_snapshot_id": "snapshot-before",
            "after_snapshot_id": "snapshot-after",
            "added": {"/refund_id": "refund-1"},
            "removed": {},
            "changed": {},
            "summary": summary,
        }
    )


def _persisted_records() -> tuple[VersionedRecord, ...]:
    request = EngineRequest.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "engine_request",
            "request_id": "request-1",
            "prompt": "Return the order.",
            "timeout_seconds": 30,
        }
    )
    completed = EngineEvent.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "engine_event",
            "event_id": "event-completed",
            "request_id": "request-1",
            "sequence": 0,
            "occurred_at": "2026-08-16T04:00:00Z",
            "payload": {
                "kind": "completed",
                "exit_status": "success",
                "session_id": "session-1",
            },
        }
    )
    case = CaseDefinition.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "case_definition",
            "case_id": "case-1",
            "source_id": "source-1",
            "source_version": "source-v1",
            "transformation_version": "transform-v1",
            "split": "develop",
            "user_prompt": "Return the order.",
            "fixture_id": "fixture-1",
        }
    )
    result = ToolResult.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "tool_result",
            "tool_name": "confirm_return",
            "status": "success",
            "data": {"refund_id": "refund-1"},
        }
    )
    trace = Trace.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "trace",
            "trace_id": "trace-1",
            "run_id": "run-1",
            "case_id": "case-1",
            "iteration_id": "iteration-0",
            "session_id": "session-1",
            "request": request,
            "events": (completed,),
            "usage": None,
            "exit_status": EngineExitStatus.SUCCESS,
        }
    )
    assertion = AssertionResult.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "assertion_result",
            "assertion_id": "assertion-1",
            "judge": JudgeKind.STATE,
            "judge_version": "state-v1",
            "required": True,
            "status": GradeStatus.NOT_EVALUATED,
            "reason": "No snapshot was available.",
        }
    )
    grade = CaseGrade.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "case_grade",
            "grade_id": "grade-1",
            "run_id": "run-1",
            "case_id": "case-1",
            "iteration_id": "iteration-0",
            "status": GradeStatus.ERROR,
            "assertions": (),
        }
    )
    return (
        request,
        _event(),
        case,
        _snapshot(),
        _diff(),
        result,
        trace,
        assertion,
        grade,
    )


@pytest.mark.parametrize(
    "record",
    [_event(), _snapshot(), _diff()],
    ids=["occurred-at", "captured-at", "summary"],
)
def test_artifact_json_bytes_preserve_all_wire_fields_and_round_trip(
    record: VersionedRecord,
) -> None:
    artifact = artifact_json_bytes(record)
    wire = json.loads(artifact)

    assert wire["schema_version"] == "v1alpha1"
    assert "record_type" in wire
    for field in ("occurred_at", "captured_at", "summary"):
        if hasattr(record, field):
            assert field in wire
    assert type(record).model_validate_json(artifact) == record


def test_artifact_json_bytes_emit_utf8_without_ascii_escaping() -> None:
    artifact = artifact_json_bytes(_diff(summary="订单已退货。"))

    assert "订单已退货。".encode() in artifact
    assert b"\\u8ba2" not in artifact


def test_content_hash_can_exclude_an_opted_in_field_only_while_it_is_none() -> None:
    original = OriginalCompatibleRecord(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="compatible",
        value=1,
    )
    absent = ExtendedCompatibleRecord(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="compatible",
        value=1,
    )
    locked = absent.model_copy(update={"optional_lock": "lock-v1"})

    assert artifact_json_bytes(absent) == artifact_json_bytes(original)
    assert content_sha256(absent) == content_sha256(original)
    assert content_sha256(locked) != content_sha256(original)
    assert (
        content_sha256(locked)
        == hashlib.sha256(artifact_json_bytes(locked)).hexdigest()
    )


def test_nested_mapping_order_does_not_change_artifact_or_content_hash() -> None:
    first = _snapshot(
        state={
            "order": {
                "attributes": {"priority": "normal", "channel": "web"},
                "items": [{"quantity": 1, "sku": "sku-1"}],
            },
            "status": "returned",
        }
    )
    second = _snapshot(
        state={
            "status": "returned",
            "order": {
                "items": [{"sku": "sku-1", "quantity": 1}],
                "attributes": {"channel": "web", "priority": "normal"},
            },
        }
    )

    assert artifact_json_bytes(first) == artifact_json_bytes(second)
    assert content_sha256(first) == content_sha256(second)


def test_json_array_order_remains_semantic() -> None:
    first = _snapshot(state={"items": [{"sku": "sku-1"}, {"sku": "sku-2"}]})
    second = _snapshot(state={"items": [{"sku": "sku-2"}, {"sku": "sku-1"}]})

    assert artifact_json_bytes(first) != artifact_json_bytes(second)
    assert content_sha256(first) != content_sha256(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_event(), _event(occurred_at="2026-08-17T04:00:00Z")),
        (_snapshot(), _snapshot(captured_at="2026-08-17T04:00:00Z")),
        (_diff(), _diff(summary="Returned.")),
    ],
    ids=["occurred-at", "captured-at", "summary"],
)
def test_wire_only_fields_change_artifact_but_not_content_hash(
    first: VersionedRecord,
    second: VersionedRecord,
) -> None:
    first_artifact = artifact_json_bytes(first)
    second_artifact = artifact_json_bytes(second)

    assert first_artifact != second_artifact
    assert (
        hashlib.sha256(first_artifact).digest()
        != hashlib.sha256(second_artifact).digest()
    )
    assert content_sha256(first) == content_sha256(second)


@pytest.mark.parametrize(
    "record_type",
    [
        EngineRequest,
        EngineEvent,
        CaseDefinition,
        ShopSnapshot,
        StateDiff,
        ToolResult,
        Trace,
        AssertionResult,
        CaseGrade,
    ],
)
def test_persisted_record_headers_are_required(
    record_type: type[VersionedRecord],
) -> None:
    assert record_type.model_fields["schema_version"].is_required()
    assert record_type.model_fields["record_type"].is_required()


@pytest.mark.parametrize(
    "record", _persisted_records(), ids=lambda item: item.record_type
)
def test_every_persisted_record_requires_valid_wire_headers(
    record: VersionedRecord,
) -> None:
    artifact = artifact_json_bytes(record)
    wire = json.loads(artifact)

    assert type(record).model_validate_json(artifact) == record
    for missing in ("schema_version", "record_type"):
        missing_header = dict(wire)
        del missing_header[missing]
        with pytest.raises(ValidationError, match=f"missing {missing}"):
            type(record).model_validate_json(json.dumps(missing_header))

    future_version = dict(wire, schema_version="v2")
    with pytest.raises(ValidationError, match="unsupported schema_version 'v2'"):
        type(record).model_validate_json(json.dumps(future_version))

    wrong_type = dict(wire, record_type="wrong_type")
    with pytest.raises(ValidationError, match="invalid record_type 'wrong_type'"):
        type(record).model_validate_json(json.dumps(wrong_type))

    if isinstance(record, Trace):
        assert wire["request"]["schema_version"] == "v1alpha1"
        assert wire["request"]["record_type"] == "engine_request"
        assert wire["events"][0]["schema_version"] == "v1alpha1"
        assert wire["events"][0]["record_type"] == "engine_event"


def test_versioned_record_json_requires_schema_version_and_record_type() -> None:
    with pytest.raises(ValidationError, match="missing schema_version"):
        ExampleRecord.model_validate_json(b'{"record_type":"example","value":1}')

    with pytest.raises(ValidationError, match="missing record_type"):
        ExampleRecord.model_validate_json(b'{"schema_version":"v1alpha1","value":1}')


def test_versioned_record_json_reports_unsupported_headers() -> None:
    with pytest.raises(ValidationError, match="unsupported schema_version 'v2'"):
        ExampleRecord.model_validate_json(
            b'{"schema_version":"v2","record_type":"example","value":1}'
        )

    with pytest.raises(ValidationError, match="invalid record_type 'other'"):
        ExampleRecord.model_validate_json(
            b'{"schema_version":"v1alpha1","record_type":"other","value":1}'
        )

    valid = ExampleRecord.model_validate_json(
        b'{"schema_version":"v1alpha1","record_type":"example","value":1}'
    )
    assert valid.schema_version is SchemaVersion.V1ALPHA1
    assert valid.record_type == "example"


def test_validated_json_containers_resist_mutable_base_class_bypasses() -> None:
    snapshot = _snapshot()
    original_hash = content_sha256(snapshot)
    state = snapshot.state
    order = cast(dict[str, JsonValue], state["order"])
    items = cast(list[JsonValue], order["items"])

    assert not isinstance(state, dict)
    assert not isinstance(items, list)
    with pytest.raises(TypeError):
        dict.__setitem__(cast(dict[str, JsonValue], state), "status", "corrupted")
    with pytest.raises(TypeError):
        list.append(items, {"sku": "sku-2", "quantity": 1})

    assert content_sha256(snapshot) == original_hash
    assert json.loads(artifact_json_bytes(snapshot))["state"]["status"] == "returned"


def test_validation_copies_input_and_round_trip_preserves_deep_immutability() -> None:
    state: dict[str, JsonValue] = {
        "status": "returned",
        "items": [{"sku": "sku-1"}],
    }
    snapshot = _snapshot(state=state)
    original_hash = content_sha256(snapshot)

    state["status"] = "corrupted"
    dumped = snapshot.model_dump(mode="json")
    cast(dict[str, JsonValue], dumped["state"])["status"] = "also-corrupted"
    restored = ShopSnapshot.model_validate_json(artifact_json_bytes(snapshot))

    assert content_sha256(snapshot) == original_hash
    assert snapshot.state["status"] == "returned"
    assert not isinstance(restored.state, dict)
    assert isinstance(restored.state["items"], tuple)


def test_model_validate_revalidates_and_freezes_constructed_instances() -> None:
    snapshot = _snapshot()
    constructed = ShopSnapshot.model_construct(
        **snapshot.model_dump(mode="python", round_trip=True)
    )

    validated = ShopSnapshot.model_validate(constructed)

    assert not isinstance(validated.state, dict)
    assert isinstance(
        cast(dict[str, JsonValue], validated.state["order"])["items"], tuple
    )
