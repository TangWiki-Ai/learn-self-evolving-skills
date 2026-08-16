"""Isolated, deterministic execution for one STATE-Bench return case.

The implementation deliberately models only the facts and tools that the
``2-return_defective_electronics`` case needs.  It does not reuse mutable
module-level state, so every :class:`CaseEnvironment` starts from the same
seed and can run alongside other instances safely.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast

from pydantic import JsonValue

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
)

CASE_ID: Final = "state-bench-customer-support-2-return-defective-electronics"
SOURCE_VERSION: Final = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
TRANSFORMATION_VERSION: Final = "ses-shop-return-defective-electronics-v1"
FIXTURE_ID: Final = "state-bench-5644b183-2-return-defective-electronics"
POLICY_VERSION: Final = "state-bench-customer-support-return-v1"
TOOL_SCHEMA_VERSION: Final = "ses-shop-mcp-v1"
_CURRENCY: Final = "USD"
_SNAPSHOT_AT: Final = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
_ORDER_ID: Final = "ORD-6006"
_ITEM_ID: Final = "ITEM-9050"
_PRODUCT_ID: Final = "PROD-2050"
_REFUND_AMOUNT_MINOR: Final = 129_900


CASE_DEFINITION: Final = CaseDefinition(
    schema_version=SchemaVersion.V1ALPHA1,
    record_type=RecordType.CASE_DEFINITION,
    case_id=CASE_ID,
    source_id="state-bench:customer_support:2-return_defective_electronics",
    source_version=SOURCE_VERSION,
    transformation_version=TRANSFORMATION_VERSION,
    split=CaseSplit.DEVELOP,
    user_prompt=(
        "My laptop from order ORD-6006 has a defective screen — it flickers "
        "constantly. I'd like to return it."
    ),
    fixture_id=FIXTURE_ID,
    required_tools=("get_order", "get_policies", "process_return"),
)


class ShopRole(StrEnum):
    """Roles allowed to attach to this case's shop MCP endpoint."""

    AGENT = "agent"
    SIMULATOR = "simulator"
    JUDGE = "judge"
    CREATOR = "creator"


@dataclass
class _Order:
    status: str = "delivered"


@dataclass
class _OrderItem:
    item_status: str = "delivered"
    return_reason: str | None = None
    refund_amount: Money | None = None
    refund_method: str | None = None
    restocking_fee: Money | None = None
    return_label_issued: bool = False


@dataclass
class _State:
    order: _Order
    item: _OrderItem


@dataclass(frozen=True)
class _ReturnDecision:
    """Internal, non-persisted result of the pinned case's policy oracle."""

    refund_amount: Money
    restocking_fee: Money
    refund_method: str
    free_return_shipping: bool
    return_reason: str


def _seed_state() -> _State:
    return _State(order=_Order(), item=_OrderItem())


def _money_json(money: Money) -> JsonValue:
    return cast(JsonValue, money.model_dump(mode="json"))


def _plain_json(value: object) -> JsonValue:
    """Turn contract-frozen values back into plain JSON-compatible values."""
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            {key: _plain_json(child) for key, child in sorted(value.items())},
        )
    if isinstance(value, tuple | list):
        return cast(JsonValue, [_plain_json(child) for child in value])
    if value is None or isinstance(value, str | int | float | bool):
        return cast(JsonValue, value)
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _json_equal(first: JsonValue, second: JsonValue) -> bool:
    return json.dumps(
        _plain_json(first), sort_keys=True, separators=(",", ":")
    ) == json.dumps(_plain_json(second), sort_keys=True, separators=(",", ":"))


def _json_pointer(parent: str, key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _state_changes(
    before: JsonValue,
    after: JsonValue,
    path: str,
    added: dict[str, JsonValue],
    removed: dict[str, JsonValue],
    changed: dict[str, StateChange],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys):
            removed[_json_pointer(path, key)] = _plain_json(before[key])
        for key in sorted(after_keys - before_keys):
            added[_json_pointer(path, key)] = _plain_json(after[key])
        for key in sorted(before_keys & after_keys):
            _state_changes(
                _plain_json(before[key]),
                _plain_json(after[key]),
                _json_pointer(path, key),
                added,
                removed,
                changed,
            )
        return
    if not _json_equal(before, after):
        changed[path] = StateChange(
            before=_plain_json(before), after=_plain_json(after)
        )


def state_diff(before: ShopSnapshot, after: ShopSnapshot) -> StateDiff:
    """Build a stable, business-field diff between two snapshots."""
    if before.case_id != after.case_id:
        raise ValueError("cannot diff snapshots from different cases")
    if before.policy_version != after.policy_version:
        raise ValueError("cannot diff snapshots with different policy versions")

    added: dict[str, JsonValue] = {}
    removed: dict[str, JsonValue] = {}
    changed: dict[str, StateChange] = {}
    _state_changes(
        _plain_json(before.state),
        _plain_json(after.state),
        "",
        added,
        removed,
        changed,
    )
    if changed:
        summary = "Return state changed for ORD-6006 / ITEM-9050."
    else:
        summary = "No business state changed."
    return StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id=f"{CASE_ID}:diff:{before.snapshot_id}:{after.snapshot_id}",
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
        added=added,
        removed=removed,
        changed=changed,
        summary=summary,
    )


def _tool_result_success(tool_name: str, data: JsonValue) -> ToolResult:
    return ToolResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TOOL_RESULT,
        tool_name=tool_name,
        status=ToolResultStatus.SUCCESS,
        data=data,
    )


def _tool_result_error(
    tool_name: str, error_code: str, error_message: str
) -> ToolResult:
    return ToolResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TOOL_RESULT,
        tool_name=tool_name,
        status=ToolResultStatus.ERROR,
        error_code=error_code,
        error_message=error_message,
    )


def _return_policy(item: _OrderItem, reason: str) -> _ReturnDecision | ToolResult:
    """Evaluate the only return path required by the pinned benchmark case."""
    if item.item_status != "delivered":
        return _tool_result_error(
            "process_return",
            "already_processed",
            f"ITEM-9050 is already {item.item_status}.",
        )
    if reason != "defective":
        return _tool_result_error(
            "process_return",
            "policy_denied",
            "This pinned case accepts only the defective-item return path.",
        )
    return _ReturnDecision(
        refund_amount=Money(amount_minor=_REFUND_AMOUNT_MINOR, currency=_CURRENCY),
        restocking_fee=Money(amount_minor=0, currency=_CURRENCY),
        refund_method="original_payment",
        free_return_shipping=True,
        return_reason="defective",
    )


class CaseEnvironment:
    """Fresh, role-scoped shop state for the pinned STATE-Bench case."""

    def __init__(self, role: ShopRole = ShopRole.AGENT) -> None:
        self._role = role
        self._state = _seed_state()
        self._policy_reviewed = False
        self._previewed_items: set[str] = set()
        self._snapshot_sequence = 0
        self._closed = False

    @property
    def case_definition(self) -> CaseDefinition:
        """Return the public executable-case definition consumed by evaluators."""
        return CASE_DEFINITION

    @property
    def role(self) -> ShopRole:
        """Return the role bound to this environment."""
        return self._role

    def reset(self) -> ShopSnapshot:
        """Restore the known seed and return its deterministic initial snapshot."""
        self._ensure_open()
        self._state = _seed_state()
        self._policy_reviewed = False
        self._previewed_items.clear()
        self._snapshot_sequence = 0
        return self.snapshot()

    def snapshot(self) -> ShopSnapshot:
        """Capture a deterministic, sorted view of business state."""
        self._ensure_open()
        self._snapshot_sequence += 1
        state = self._snapshot_state()
        return ShopSnapshot(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.SHOP_SNAPSHOT,
            snapshot_id=f"{CASE_ID}:snapshot:{self._snapshot_sequence:04d}",
            case_id=CASE_ID,
            captured_at=_SNAPSHOT_AT,
            policy_version=POLICY_VERSION,
            state=state,
        )

    def execute(self, tool_name: str, arguments: object) -> ToolResult:
        """Validate and execute one allowed tool call without partial writes."""
        if self._closed:
            return _tool_result_error(
                tool_name or "unknown_tool",
                "environment_closed",
                "The case environment is closed.",
            )
        if tool_name not in _tool_names():
            return _tool_result_error(
                tool_name or "unknown_tool",
                "unknown_tool",
                f"Unknown shop tool: {tool_name!r}.",
            )
        if self._role is not ShopRole.AGENT:
            return _tool_result_error(
                tool_name,
                "permission_denied",
                f"Role {self._role.value!r} cannot call shop tools.",
            )
        if not isinstance(arguments, Mapping):
            return _tool_result_error(
                tool_name,
                "invalid_input",
                "Tool arguments must be a JSON object.",
            )
        if tool_name == "get_order":
            return self._get_order(arguments)
        if tool_name == "get_policies":
            return self._get_policies(arguments)
        return self._process_return(arguments)

    def close(self) -> None:
        """Release case-local state. Calls after close cannot mutate it."""
        self._previewed_items.clear()
        self._closed = True

    def available_tools(self) -> tuple[dict[str, JsonValue], ...]:
        """Return the MCP schemas visible to the bound role."""
        if self._role is not ShopRole.AGENT:
            return ()
        return tuple(copy.deepcopy(schema) for schema in _TOOL_SCHEMAS)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("The case environment is closed.")

    def _snapshot_state(self) -> dict[str, JsonValue]:
        item = self._state.item
        return {
            "order_items": {
                _ITEM_ID: {
                    "item_id": _ITEM_ID,
                    "item_status": item.item_status,
                    "order_id": _ORDER_ID,
                    "product_id": _PRODUCT_ID,
                    "refund_amount": (
                        None
                        if item.refund_amount is None
                        else _money_json(item.refund_amount)
                    ),
                    "refund_method": item.refund_method,
                    "restocking_fee": (
                        None
                        if item.restocking_fee is None
                        else _money_json(item.restocking_fee)
                    ),
                    "return_label_issued": item.return_label_issued,
                    "return_reason": item.return_reason,
                }
            },
            "orders": {
                _ORDER_ID: {
                    "order_id": _ORDER_ID,
                    "status": self._state.order.status,
                    "total_paid": _money_json(
                        Money(amount_minor=_REFUND_AMOUNT_MINOR, currency=_CURRENCY)
                    ),
                }
            },
        }

    def _get_order(self, arguments: Mapping[object, object]) -> ToolResult:
        validation_error = _require_exact_keys(arguments, {"order_id"}, {"order_id"})
        if validation_error is not None:
            return _tool_result_error("get_order", "invalid_input", validation_error)
        order_id = arguments["order_id"]
        if not isinstance(order_id, str) or not order_id.strip():
            return _tool_result_error(
                "get_order", "invalid_input", "order_id must be a non-empty string."
            )
        if order_id != _ORDER_ID:
            return _tool_result_error(
                "get_order", "order_not_found", f"Order {order_id!r} does not exist."
            )
        state = self._snapshot_state()
        orders = cast(dict[str, dict[str, JsonValue]], state["orders"])
        order_items = cast(dict[str, dict[str, JsonValue]], state["order_items"])
        return _tool_result_success(
            "get_order",
            cast(
                JsonValue,
                {
                    "order": orders[_ORDER_ID],
                    "items": [order_items[_ITEM_ID]],
                    "policy_version": POLICY_VERSION,
                    "tool_schema_version": TOOL_SCHEMA_VERSION,
                },
            ),
        )

    def _get_policies(self, arguments: Mapping[object, object]) -> ToolResult:
        validation_error = _require_exact_keys(arguments, {"topic"}, {"topic"})
        if validation_error is not None:
            return _tool_result_error("get_policies", "invalid_input", validation_error)
        topic = arguments["topic"]
        if topic != "return":
            return _tool_result_error(
                "get_policies",
                "unsupported_topic",
                "This pinned case exposes only the return policy.",
            )
        self._policy_reviewed = True
        return _tool_result_success(
            "get_policies",
            cast(
                JsonValue,
                {
                    "policy_version": POLICY_VERSION,
                    "topic": "return",
                    "rules": {
                        "defective_items": "No return-window restriction.",
                        "refund_method": "original_payment",
                        "refund_amount": _money_json(
                            Money(amount_minor=_REFUND_AMOUNT_MINOR, currency=_CURRENCY)
                        ),
                        "restocking_fee": _money_json(
                            Money(amount_minor=0, currency=_CURRENCY)
                        ),
                        "free_return_shipping": True,
                    },
                    "tool_schema_version": TOOL_SCHEMA_VERSION,
                },
            ),
        )

    def _process_return(self, arguments: Mapping[object, object]) -> ToolResult:
        validation_error = _require_exact_keys(
            arguments,
            {"item_id", "reason", "confirm", "amount_minor"},
            {"item_id", "reason"},
        )
        if validation_error is not None:
            return _tool_result_error(
                "process_return", "invalid_input", validation_error
            )
        item_id = arguments["item_id"]
        reason = arguments["reason"]
        confirm = arguments.get("confirm", False)
        if not isinstance(item_id, str) or item_id != _ITEM_ID:
            return _tool_result_error(
                "process_return", "item_not_found", "ITEM-9050 is the only case item."
            )
        if not isinstance(reason, str):
            return _tool_result_error(
                "process_return", "invalid_input", "reason must be a string."
            )
        if not isinstance(confirm, bool):
            return _tool_result_error(
                "process_return", "invalid_input", "confirm must be a boolean."
            )
        if not self._policy_reviewed:
            return _tool_result_error(
                "process_return",
                "policy_review_required",
                "Call get_policies with topic='return' before process_return.",
            )

        decision = _return_policy(self._state.item, reason)
        if isinstance(decision, ToolResult):
            return decision
        if not confirm:
            self._previewed_items.add(_ITEM_ID)
            return _tool_result_success(
                "process_return",
                cast(
                    JsonValue,
                    {
                        "free_return_shipping": decision.free_return_shipping,
                        "item_id": _ITEM_ID,
                        "policy_version": POLICY_VERSION,
                        "refund_amount": _money_json(decision.refund_amount),
                        "refund_method": decision.refund_method,
                        "restocking_fee": _money_json(decision.restocking_fee),
                        "return_eligible": True,
                        "return_reason": decision.return_reason,
                        "status": "preview",
                        "tool_schema_version": TOOL_SCHEMA_VERSION,
                    },
                ),
            )

        if _ITEM_ID not in self._previewed_items:
            return _tool_result_error(
                "process_return",
                "preview_required",
                "Preview the return before confirming it.",
            )
        amount_minor = arguments.get("amount_minor")
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            return _tool_result_error(
                "process_return",
                "invalid_input",
                "amount_minor must be an integer when confirm is true.",
            )
        if amount_minor != decision.refund_amount.amount_minor:
            return _tool_result_error(
                "process_return",
                "policy_amount_mismatch",
                "amount_minor must equal the deterministic policy refund amount.",
            )

        # All validation is complete. These assignments form the only state mutation.
        item = self._state.item
        item.item_status = "returned"
        item.return_reason = decision.return_reason
        item.refund_amount = decision.refund_amount
        item.refund_method = decision.refund_method
        item.restocking_fee = decision.restocking_fee
        item.return_label_issued = decision.free_return_shipping
        self._state.order.status = "fully_returned"
        self._previewed_items.discard(_ITEM_ID)
        return _tool_result_success(
            "process_return",
            cast(
                JsonValue,
                {
                    "item_id": _ITEM_ID,
                    "policy_version": POLICY_VERSION,
                    "refund_amount": _money_json(decision.refund_amount),
                    "refund_method": decision.refund_method,
                    "restocking_fee": _money_json(decision.restocking_fee),
                    "return_label_issued": decision.free_return_shipping,
                    "status": "returned",
                    "tool_schema_version": TOOL_SCHEMA_VERSION,
                },
            ),
        )


def _require_exact_keys(
    arguments: Mapping[object, object], allowed: set[str], required: set[str]
) -> str | None:
    keys = set(arguments)
    if not all(isinstance(key, str) for key in keys):
        return "Tool argument names must be strings."
    string_keys = cast(set[str], keys)
    unknown = sorted(string_keys - allowed)
    if unknown:
        return f"Unsupported argument(s): {', '.join(unknown)}."
    missing = sorted(required - string_keys)
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}."
    return None


def _tool_names() -> tuple[str, ...]:
    return tuple(cast(str, schema["name"]) for schema in _TOOL_SCHEMAS)


_TOOL_SCHEMAS: Final[tuple[dict[str, JsonValue], ...]] = (
    {
        "name": "get_order",
        "description": "Retrieve the pinned order and its returnable item.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_policies",
        "description": "Read the return policy required before a write operation.",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "enum": ["return"]}},
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_return",
        "description": (
            "Preview, then confirm the defective-item return. Confirm requires the "
            "previewed amount_minor in USD minor units."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "reason": {"type": "string", "enum": ["defective"]},
                "confirm": {"type": "boolean"},
                "amount_minor": {"type": "integer"},
            },
            "required": ["item_id", "reason"],
            "additionalProperties": False,
        },
    },
)
