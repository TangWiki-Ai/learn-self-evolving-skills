from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ses.contracts import StateDiff, ToolResult, ToolResultStatus
from ses.shop import CASE_DEFINITION, CaseEnvironment, ShopRole, state_diff


def _result_data(result: ToolResult) -> Mapping[str, object]:
    assert result.status is ToolResultStatus.SUCCESS
    assert isinstance(result.data, Mapping)
    return result.data


def _error_code(result: ToolResult) -> str:
    assert result.status is ToolResultStatus.ERROR
    assert result.error_code is not None
    return result.error_code


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _order(snapshot_state: Mapping[str, object]) -> Mapping[str, object]:
    orders = _mapping(snapshot_state["orders"])
    return _mapping(orders["ORD-6006"])


def _item(snapshot_state: Mapping[str, object]) -> Mapping[str, object]:
    items = _mapping(snapshot_state["order_items"])
    return _mapping(items["ITEM-9050"])


def _complete_return(environment: CaseEnvironment) -> StateDiff:
    before = environment.snapshot()
    assert (
        environment.execute("get_policies", {"topic": "return"}).status
        is ToolResultStatus.SUCCESS
    )
    preview = environment.execute(
        "process_return", {"item_id": "ITEM-9050", "reason": "defective"}
    )
    assert preview.status is ToolResultStatus.SUCCESS
    confirmed = environment.execute(
        "process_return",
        {
            "item_id": "ITEM-9050",
            "reason": "defective",
            "confirm": True,
            "amount_minor": 129_900,
        },
    )
    assert confirmed.status is ToolResultStatus.SUCCESS
    return state_diff(before, environment.snapshot())


def test_case_fixture_records_the_pinned_upstream_case() -> None:
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "shop" / "pinned_return_case.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture["case_id"] == CASE_DEFINITION.case_id
    assert fixture["source"]["commit"] == CASE_DEFINITION.source_version
    assert fixture["required_tools"] == list(CASE_DEFINITION.required_tools)
    assert fixture["expected_return"]["amount_minor"] == 129_900


def test_reset_recreates_a_stable_seed_snapshot() -> None:
    environment = CaseEnvironment()
    first = environment.snapshot()
    environment.execute("get_policies", {"topic": "return"})
    environment.execute(
        "process_return", {"item_id": "ITEM-9050", "reason": "defective"}
    )

    reset = environment.reset()

    assert reset == first
    assert _order(reset.state)["status"] == "delivered"
    assert _item(reset.state)["refund_amount"] is None


def test_defective_policy_uses_minor_units_and_overrides_the_normal_window() -> None:
    environment = CaseEnvironment()
    environment.execute("get_policies", {"topic": "return"})

    preview = _result_data(
        environment.execute(
            "process_return", {"item_id": "ITEM-9050", "reason": "defective"}
        )
    )

    # The source case is 18 days after delivery despite the normal 15-day
    # electronics window. A defective item remains eligible and has no fees.
    assert preview["return_eligible"] is True
    assert preview["refund_amount"] == {"amount_minor": 129_900, "currency": "USD"}
    assert preview["restocking_fee"] == {"amount_minor": 0, "currency": "USD"}
    assert preview["refund_method"] == "original_payment"
    assert preview["free_return_shipping"] is True


def test_confirm_updates_only_the_expected_business_fields() -> None:
    environment = CaseEnvironment()
    before = environment.snapshot()
    diff = _complete_return(environment)
    after = environment.snapshot()

    assert _order(after.state)["status"] == "fully_returned"
    item = _item(after.state)
    assert item["item_status"] == "returned"
    assert item["return_reason"] == "defective"
    assert item["refund_amount"] == {"amount_minor": 129_900, "currency": "USD"}
    assert item["refund_method"] == "original_payment"
    assert item["restocking_fee"] == {"amount_minor": 0, "currency": "USD"}
    assert item["return_label_issued"] is True
    assert set(diff.changed) == {
        "/order_items/ITEM-9050/item_status",
        "/order_items/ITEM-9050/refund_amount",
        "/order_items/ITEM-9050/refund_method",
        "/order_items/ITEM-9050/restocking_fee",
        "/order_items/ITEM-9050/return_label_issued",
        "/order_items/ITEM-9050/return_reason",
        "/orders/ORD-6006/status",
    }
    assert _order(before.state)["status"] == "delivered"


def test_failed_writes_leave_the_business_snapshot_unchanged() -> None:
    environment = CaseEnvironment()
    baseline = environment.snapshot()
    no_policy = environment.execute(
        "process_return",
        {"item_id": "ITEM-9050", "reason": "defective"},
    )
    assert _error_code(no_policy) == "policy_review_required"
    assert environment.snapshot().state == baseline.state

    environment.execute("get_policies", {"topic": "return"})
    no_preview = environment.execute(
        "process_return",
        {
            "item_id": "ITEM-9050",
            "reason": "defective",
            "confirm": True,
            "amount_minor": 129_900,
        },
    )
    assert _error_code(no_preview) == "preview_required"
    assert environment.snapshot().state == baseline.state

    environment.execute(
        "process_return", {"item_id": "ITEM-9050", "reason": "defective"}
    )
    wrong_amount = environment.execute(
        "process_return",
        {
            "item_id": "ITEM-9050",
            "reason": "defective",
            "confirm": True,
            "amount_minor": 129_899,
        },
    )
    assert _error_code(wrong_amount) == "policy_amount_mismatch"
    assert environment.snapshot().state == baseline.state


def test_repeated_write_is_rejected_without_a_second_mutation() -> None:
    environment = CaseEnvironment()
    _complete_return(environment)
    after_first = environment.snapshot()

    repeated = environment.execute(
        "process_return",
        {
            "item_id": "ITEM-9050",
            "reason": "defective",
            "confirm": True,
            "amount_minor": 129_900,
        },
    )

    assert _error_code(repeated) == "already_processed"
    assert environment.snapshot().state == after_first.state


def test_roles_cannot_see_or_call_shop_tools_without_agent_permission() -> None:
    environment = CaseEnvironment(role=ShopRole.SIMULATOR)

    assert environment.available_tools() == ()
    denied = environment.execute("get_order", {"order_id": "ORD-6006"})

    assert _error_code(denied) == "permission_denied"


def test_parallel_environments_do_not_share_preview_or_order_state() -> None:
    def run_return(_: int) -> StateDiff:
        environment = CaseEnvironment()
        try:
            return _complete_return(environment)
        finally:
            environment.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        diffs = list(executor.map(run_return, range(8)))

    assert len({diff.model_dump_json() for diff in diffs}) == 1
    untouched = CaseEnvironment().snapshot()
    assert _order(untouched.state)["status"] == "delivered"


def test_state_diff_is_stable_and_empty_when_business_state_does_not_change() -> None:
    first_environment = CaseEnvironment()
    second_environment = CaseEnvironment()

    first = _complete_return(first_environment)
    second = _complete_return(second_environment)
    initial = second_environment.reset()
    untouched = second_environment.snapshot()
    empty = state_diff(initial, untouched)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert empty.added == {}
    assert empty.removed == {}
    assert empty.changed == {}
    assert empty.summary == "No business state changed."


def test_close_refuses_later_mutations_and_snapshots() -> None:
    environment = CaseEnvironment()
    environment.close()

    assert (
        _error_code(environment.execute("get_order", {"order_id": "ORD-6006"}))
        == "environment_closed"
    )
    with pytest.raises(RuntimeError, match="closed"):
        environment.snapshot()
