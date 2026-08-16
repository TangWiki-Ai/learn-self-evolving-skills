from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ses.contracts import (
    CaseDefinition,
    CaseSplit,
    Money,
    RecordType,
    SchemaVersion,
    ShopSnapshot,
    StateChange,
    StateDiff,
    ToolResult,
    ToolResultStatus,
    content_sha256,
)


def _case() -> CaseDefinition:
    return CaseDefinition(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.CASE_DEFINITION,
        case_id="case-1",
        source_id="state-bench-task-2",
        source_version="5644b183",
        transformation_version="return-case-v1",
        split=CaseSplit.DEVELOP,
        user_prompt="Return the defective headphones.",
        fixture_id="order-fixture-1",
        required_tools=("preview_return", "confirm_return"),
    )


def _snapshot(*, captured_at: str = "2026-08-16T04:00:00Z") -> ShopSnapshot:
    return ShopSnapshot.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "shop_snapshot",
            "snapshot_id": "snapshot-1",
            "case_id": "case-1",
            "captured_at": captured_at,
            "policy_version": "returns-v1",
            "state": {
                "order_id": "order-1",
                "refund": {"amount_minor": 1299, "currency": "USD"},
                "status": "returned",
            },
        }
    )


def _diff(*, summary: str = "Order moved to returned.") -> StateDiff:
    return StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-1",
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        added={"/refund_id": "refund-1"},
        removed={"/return_pending": True},
        changed={
            "/status": StateChange(before="shipped", after="returned"),
        },
        summary=summary,
    )


def test_case_definition_round_trips_with_public_inputs_only() -> None:
    case = _case()

    restored = CaseDefinition.model_validate_json(case.model_dump_json())

    assert restored == case
    assert restored.record_type is RecordType.CASE_DEFINITION
    assert restored.split is CaseSplit.DEVELOP


def test_case_split_only_exposes_the_issue_2_partition() -> None:
    assert list(CaseSplit) == [CaseSplit.DEVELOP]


@pytest.mark.parametrize(
    "split",
    ["creator", "selection", "final", "trigger_eval"],
)
def test_case_definition_rejects_future_partitions(split: str) -> None:
    data = _case().model_dump(mode="json")
    data["split"] = split

    with pytest.raises(ValidationError):
        CaseDefinition.model_validate(data)


@pytest.mark.parametrize(
    "required_tools",
    [("preview_return", "preview_return"), ("",), ("   ",)],
)
def test_case_definition_rejects_invalid_required_tools(
    required_tools: tuple[str, ...],
) -> None:
    data = _case().model_dump()
    data["required_tools"] = required_tools

    with pytest.raises(ValidationError):
        CaseDefinition.model_validate(data)


@pytest.mark.parametrize(
    "private_field",
    ["gold", "hidden_gold", "reference_trace", "selection_answer", "final_answer"],
)
def test_case_definition_rejects_private_answer_fields(private_field: str) -> None:
    data = _case().model_dump(mode="json")
    data[private_field] = {"status": "returned"}

    with pytest.raises(ValidationError):
        CaseDefinition.model_validate(data)


def test_money_uses_strict_minor_units_and_canonical_currency() -> None:
    money = Money(amount_minor=-1299, currency="USD")

    assert Money.model_validate_json(money.model_dump_json()) == money
    assert money.amount_minor == -1299


@pytest.mark.parametrize("amount_minor", [12.99, "1299", True, False])
def test_money_rejects_non_integer_minor_units(amount_minor: object) -> None:
    with pytest.raises(ValidationError):
        Money.model_validate({"amount_minor": amount_minor, "currency": "USD"})


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "123"])
def test_money_rejects_noncanonical_currency(currency: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=1299, currency=currency)


def test_shop_snapshot_round_trips_and_hashes_business_state() -> None:
    snapshot = _snapshot()

    restored = ShopSnapshot.model_validate_json(snapshot.model_dump_json())
    later = _snapshot(captured_at="2026-08-17T04:00:00Z")
    changed_data = snapshot.model_dump(mode="json")
    changed_data["state"]["status"] = "refunded"
    changed = ShopSnapshot.model_validate(changed_data)

    assert restored == snapshot
    assert restored.record_type is RecordType.SHOP_SNAPSHOT
    assert snapshot.captured_at == datetime(2026, 8, 16, 4, 0, tzinfo=UTC)
    assert content_sha256(snapshot) == content_sha256(later)
    assert content_sha256(snapshot) != content_sha256(changed)


@pytest.mark.parametrize(
    "state",
    [
        {"refund": 12.99},
        {"refund": {"amount_minor": 12.99, "currency": "USD"}},
    ],
)
def test_shop_snapshot_rejects_binary_floats(state: dict[str, object]) -> None:
    data = _snapshot().model_dump(mode="json")
    data["state"] = state

    with pytest.raises(ValidationError, match="binary float"):
        ShopSnapshot.model_validate(data)


def test_empty_state_diff_is_valid() -> None:
    diff = StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-empty",
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        added={},
        removed={},
        changed={},
        summary="No business change.",
    )

    assert StateDiff.model_validate_json(diff.model_dump_json()) == diff


def test_state_diff_summary_does_not_change_content_hash() -> None:
    first = _diff(summary="Order moved to returned.")
    second = _diff(summary="Returned.")

    assert first.record_type is RecordType.STATE_DIFF
    assert content_sha256(first) == content_sha256(second)


def test_state_change_distinguishes_json_booleans_from_numbers() -> None:
    change = StateChange(before=1, after=True)

    assert change.before == 1
    assert change.after is True


@pytest.mark.parametrize(
    "updates",
    [
        {"added": {"status": "returned"}},
        {
            "added": {"/status": "returned"},
            "removed": {"/status": "shipped"},
        },
        {"changed": {"/status": {"before": "same", "after": "same"}}},
        {"after_snapshot_id": "snapshot-before"},
    ],
)
def test_state_diff_rejects_ambiguous_changes(updates: dict[str, object]) -> None:
    data = _diff().model_dump(mode="json")
    data.update(updates)

    with pytest.raises(ValidationError):
        StateDiff.model_validate(data)


def test_tool_result_success_and_error_shapes_round_trip() -> None:
    success = ToolResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TOOL_RESULT,
        tool_name="confirm_return",
        status=ToolResultStatus.SUCCESS,
        data={"refund_id": "refund-1"},
    )
    error = ToolResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TOOL_RESULT,
        tool_name="confirm_return",
        status=ToolResultStatus.ERROR,
        data={"retryable": False},
        error_code="policy_denied",
        error_message="The return window has closed.",
    )

    assert ToolResult.model_validate_json(success.model_dump_json()) == success
    assert ToolResult.model_validate_json(error.model_dump_json()) == error
    assert success.record_type is RecordType.TOOL_RESULT


@pytest.mark.parametrize(
    "data",
    [
        {
            "schema_version": "v1alpha1",
            "record_type": "tool_result",
            "tool_name": "confirm_return",
            "status": "success",
            "error_code": "unexpected",
            "error_message": "must not be present",
        },
        {
            "schema_version": "v1alpha1",
            "record_type": "tool_result",
            "tool_name": "confirm_return",
            "status": "error",
        },
        {
            "schema_version": "v1alpha1",
            "record_type": "tool_result",
            "tool_name": "confirm_return",
            "status": "failed",
        },
    ],
)
def test_tool_result_rejects_inconsistent_status_shapes(
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate(data)


@pytest.mark.parametrize(
    "model_data",
    [
        {
            "schema_version": "v1alpha1",
            "record_type": "tool_result",
            "tool_name": "confirm_return",
            "status": "success",
            "data": {"refund": 12.99},
        },
        {
            "schema_version": "v1alpha1",
            "record_type": "state_diff",
            "diff_id": "diff-1",
            "before_snapshot_id": "before",
            "after_snapshot_id": "after",
            "added": {"/refund": 12.99},
        },
    ],
)
def test_shop_json_seams_reject_binary_floats(
    model_data: dict[str, object],
) -> None:
    model = ToolResult if "tool_name" in model_data else StateDiff

    with pytest.raises(ValidationError, match="binary float"):
        model.model_validate(model_data)


def test_shop_json_seams_reject_nested_credentials_and_private_answers() -> None:
    snapshot_data = _snapshot().model_dump(mode="json")
    snapshot_data["state"] = {"nested": [{"apiToken": "not-a-real-value"}]}
    with pytest.raises(ValidationError, match="forbidden field"):
        ShopSnapshot.model_validate(snapshot_data)

    with pytest.raises(ValidationError, match="forbidden field"):
        StateChange.model_validate(
            {"before": {"status": "open"}, "after": {"selection_gold": "private"}}
        )

    diff_data = _diff().model_dump(mode="json")
    diff_data["added"] = {"/refund": {"httpHeaders": "not-a-real-value"}}
    with pytest.raises(ValidationError, match="forbidden field"):
        StateDiff.model_validate(diff_data)

    with pytest.raises(ValidationError, match="forbidden field"):
        ToolResult.model_validate(
            {
                "schema_version": "v1alpha1",
                "record_type": "tool_result",
                "tool_name": "confirm_return",
                "status": "success",
                "data": {"hidden__gold": "private"},
            }
        )
