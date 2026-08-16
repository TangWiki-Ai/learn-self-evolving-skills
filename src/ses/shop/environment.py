"""Isolated deterministic execution for one fixture-backed return case."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from pydantic import JsonValue

from ses.contracts import (
    CaseDefinition,
    Money,
    RecordType,
    SchemaVersion,
    ShopSnapshot,
    StateChange,
    StateDiff,
    ToolResult,
    ToolResultStatus,
)
from ses.shop.fixture import PINNED_CASE_FIXTURE, ReturnCaseFixture
from ses.shop.policy import ReturnPolicyDecision, ReturnReason, compute_return_policy

TOOL_SCHEMA_VERSION: Final = "ses-shop-mcp-v1"
CASE_DEFINITION: Final = PINNED_CASE_FIXTURE.case_definition()
CASE_ID: Final = CASE_DEFINITION.case_id
POLICY_VERSION: Final = PINNED_CASE_FIXTURE.policy_version


class ShopRole(StrEnum):
    """Roles allowed to attach to this case's shop MCP endpoint."""

    AGENT = "agent"
    SIMULATOR = "simulator"
    JUDGE = "judge"
    CREATOR = "creator"


@dataclass
class _OrderState:
    status: str


@dataclass
class _OrderItemState:
    item_status: str
    return_reason: str | None = None
    refund_amount: Money | None = None
    refund_method: str | None = None
    restocking_fee: Money | None = None
    return_label_issued: bool = False


@dataclass
class _State:
    order: _OrderState
    item: _OrderItemState


def _seed_state(fixture: ReturnCaseFixture) -> _State:
    return _State(
        order=_OrderState(status=fixture.order.status),
        item=_OrderItemState(item_status=fixture.item.item_status),
    )


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
    if added or removed or changed:
        summary = (
            f"Business state changed: {len(added)} added, {len(removed)} removed, "
            f"{len(changed)} changed."
        )
    else:
        summary = "No business state changed."
    return StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id=f"{before.case_id}:diff:{before.snapshot_id}:{after.snapshot_id}",
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


def _decision_json(decision: ReturnPolicyDecision) -> dict[str, JsonValue]:
    return {
        "days_since_delivery": decision.days_since_delivery,
        "effective_window_days": decision.effective_window_days,
        "free_return_shipping": decision.free_return_shipping,
        "paid_return_shipping_fee": _money_json(decision.paid_return_shipping_fee),
        "policy_computed_amount": _money_json(decision.policy_computed_amount),
        "refund_amount": _money_json(decision.refund_amount),
        "refund_method": decision.refund_method,
        "restocking_discount": _money_json(decision.restocking_discount),
        "restocking_fee": _money_json(decision.restocking_fee),
        "shipping_clawback": _money_json(decision.shipping_clawback),
        "store_credit_only": decision.store_credit_only,
    }


class CaseEnvironment:
    """Fresh, role-scoped shop state deep-cloned from a typed fixture."""

    def __init__(
        self,
        fixture: ReturnCaseFixture = PINNED_CASE_FIXTURE,
        *,
        role: ShopRole = ShopRole.AGENT,
    ) -> None:
        self._source_fixture = fixture.model_copy(deep=True)
        self._fixture = self._source_fixture.model_copy(deep=True)
        self._role = role
        self._state = _seed_state(self._fixture)
        self._policy_reviewed = False
        self._previewed_items: set[str] = set()
        self._snapshot_sequence = 0
        self._closed = False

    @property
    def case_definition(self) -> CaseDefinition:
        """Return the public case definition derived from this fixture."""
        return self._fixture.case_definition()

    @property
    def role(self) -> ShopRole:
        """Return the role bound to this environment."""
        return self._role

    def reset(self) -> ShopSnapshot:
        """Deep-clone the source fixture and restore its initial state."""
        self._ensure_open()
        self._fixture = self._source_fixture.model_copy(deep=True)
        self._state = _seed_state(self._fixture)
        self._policy_reviewed = False
        self._previewed_items.clear()
        self._snapshot_sequence = 0
        return self.snapshot()

    def snapshot(self) -> ShopSnapshot:
        """Capture a deterministic, sorted view of business state."""
        self._ensure_open()
        self._snapshot_sequence += 1
        return ShopSnapshot(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.SHOP_SNAPSHOT,
            snapshot_id=(
                f"{self._fixture.case_id}:snapshot:{self._snapshot_sequence:04d}"
            ),
            case_id=self._fixture.case_id,
            captured_at=self._fixture.task_now,
            policy_version=self._fixture.policy_version,
            state=self._snapshot_state(),
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
        fixture = self._fixture
        item = self._state.item
        return {
            "order_items": {
                fixture.item.item_id: {
                    "item_id": fixture.item.item_id,
                    "item_status": item.item_status,
                    "order_id": fixture.order.order_id,
                    "product_id": fixture.product.product_id,
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
                fixture.order.order_id: {
                    "order_id": fixture.order.order_id,
                    "status": self._state.order.status,
                    "total_paid": _money_json(fixture.order.subtotal),
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
        if order_id != self._fixture.order.order_id:
            return _tool_result_error(
                "get_order", "order_not_found", f"Order {order_id!r} does not exist."
            )
        state = self._snapshot_state()
        orders = cast(dict[str, dict[str, JsonValue]], state["orders"])
        order_items = cast(dict[str, dict[str, JsonValue]], state["order_items"])
        item = order_items[self._fixture.item.item_id]
        item["product"] = {
            "category": self._fixture.product.category,
            "name": self._fixture.product.name,
            "price": _money_json(self._fixture.product.price),
            "return_window_days": self._fixture.product.return_window_days,
        }
        return _tool_result_success(
            "get_order",
            cast(
                JsonValue,
                {
                    "items": [item],
                    "order": orders[self._fixture.order.order_id],
                    "policy_version": self._fixture.policy_version,
                    "tool_schema_version": TOOL_SCHEMA_VERSION,
                },
            ),
        )

    def _get_policies(self, arguments: Mapping[object, object]) -> ToolResult:
        validation_error = _require_exact_keys(arguments, {"topic"}, {"topic"})
        if validation_error is not None:
            return _tool_result_error("get_policies", "invalid_input", validation_error)
        if arguments["topic"] != "return":
            return _tool_result_error(
                "get_policies",
                "unsupported_topic",
                "This case exposes only the return policy.",
            )
        self._policy_reviewed = True
        return _tool_result_success(
            "get_policies",
            cast(
                JsonValue,
                {
                    "policy_version": self._fixture.policy_version,
                    "rules": {
                        "base_window_days": self._fixture.product.return_window_days,
                        "fault_returns": (
                            "Defective, wrong, and transit-damaged items have no "
                            "return-window restriction, no restocking fee, and free "
                            "return shipping."
                        ),
                        "membership_tier": self._fixture.customer.membership_tier,
                        "store_credit_grace_days": 15,
                    },
                    "tool_schema_version": TOOL_SCHEMA_VERSION,
                    "topic": "return",
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
        if not isinstance(item_id, str) or item_id != self._fixture.item.item_id:
            return _tool_result_error(
                "process_return",
                "item_not_found",
                f"{self._fixture.item.item_id} is the only case item.",
            )
        if not isinstance(reason, str):
            return _tool_result_error(
                "process_return", "invalid_input", "reason must be a string."
            )
        try:
            parsed_reason = ReturnReason(reason)
        except ValueError:
            return _tool_result_error(
                "process_return",
                "invalid_input",
                f"Unsupported return reason: {reason!r}.",
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
        if self._state.item.item_status != self._fixture.item.item_status:
            return _tool_result_error(
                "process_return",
                "already_processed",
                f"{item_id} is already {self._state.item.item_status}.",
            )

        decision = compute_return_policy(self._fixture, parsed_reason)
        if not decision.eligible:
            return _tool_result_error(
                "process_return",
                "policy_denied",
                f"Return rejected by policy: {decision.reason_code}.",
            )
        if not confirm:
            self._previewed_items.add(item_id)
            return _tool_result_success(
                "process_return",
                cast(
                    JsonValue,
                    {
                        **_decision_json(decision),
                        "item_id": item_id,
                        "policy_version": self._fixture.policy_version,
                        "return_eligible": True,
                        "return_reason": parsed_reason.value,
                        "status": "preview",
                        "tool_schema_version": TOOL_SCHEMA_VERSION,
                    },
                ),
            )

        if item_id not in self._previewed_items:
            return _tool_result_error(
                "process_return",
                "preview_required",
                "Preview the return before confirming it.",
            )
        amount_minor = arguments.get("amount_minor")
        if (
            isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or amount_minor < 0
        ):
            return _tool_result_error(
                "process_return",
                "invalid_input",
                "amount_minor must be a non-negative integer when confirm is true.",
            )

        # Validation is complete. Preserve upstream scoring semantics by writing
        # the submitted amount verbatim; the State Judge compares it with the
        # separately returned policy_computed_amount.
        submitted_amount = Money(
            amount_minor=amount_minor,
            currency=self._fixture.product.price.currency,
        )
        item = self._state.item
        item.item_status = "returned"
        item.return_reason = parsed_reason.value
        item.refund_amount = submitted_amount
        item.refund_method = decision.refund_method
        item.restocking_fee = decision.restocking_fee
        item.return_label_issued = decision.free_return_shipping
        self._state.order.status = "fully_returned"
        self._previewed_items.discard(item_id)
        result_data = _decision_json(decision)
        result_data["refund_amount"] = _money_json(submitted_amount)
        result_data.update(
            {
                "item_id": item_id,
                "policy_version": self._fixture.policy_version,
                "return_label_issued": decision.free_return_shipping,
                "status": "returned",
                "tool_schema_version": TOOL_SCHEMA_VERSION,
            }
        )
        return _tool_result_success("process_return", cast(JsonValue, result_data))


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
        "description": "Retrieve the pinned order, its item, and product details.",
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
            "Preview, then confirm a return. Confirm writes the supplied non-negative "
            "amount_minor in USD minor units and also reports the policy amount."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": [reason.value for reason in ReturnReason],
                },
                "confirm": {"type": "boolean"},
                "amount_minor": {"type": "integer", "minimum": 0},
            },
            "required": ["item_id", "reason"],
            "additionalProperties": False,
        },
    },
)
