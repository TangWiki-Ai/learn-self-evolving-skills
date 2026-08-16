"""Pure return-policy calculation over a typed case fixture."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ses.contracts import Money
from ses.shop.fixture import ReturnCaseFixture

_TIER_WINDOW_EXTENSION: Final = {
    "standard": 0,
    "silver": 0,
    "gold": 15,
    "platinum": 15,
}
_TIER_RESTOCKING_DISCOUNT_PCT: Final = {
    "standard": 0,
    "silver": 25,
    "gold": 50,
    "platinum": 100,
}
_PRIME_WINDOW_EXTENSION_DAYS: Final = 15
_STORE_CREDIT_GRACE_DAYS: Final = 15
_FREE_SHIPPING_THRESHOLD_MINOR: Final = 10_000
_STANDARD_SHIPPING_COST_MINOR: Final = 800
_LOW_VALUE_RETURN_THRESHOLD_MINOR: Final = 5_000


class ReturnReason(StrEnum):
    DEFECTIVE = "defective"
    WRONG_ITEM = "wrong_item"
    NOT_AS_DESCRIBED = "not_as_described"
    CHANGED_MIND = "changed_mind"
    DAMAGED_IN_TRANSIT = "damaged_in_transit"


_FAULT_REASONS: Final = frozenset(
    {
        ReturnReason.DEFECTIVE,
        ReturnReason.WRONG_ITEM,
        ReturnReason.DAMAGED_IN_TRANSIT,
    }
)


@dataclass(frozen=True)
class ReturnPolicyDecision:
    eligible: bool
    reason_code: str
    refund_amount: Money
    policy_computed_amount: Money
    refund_method: str | None
    restocking_fee: Money
    restocking_discount: Money
    shipping_clawback: Money
    paid_return_shipping_fee: Money
    free_return_shipping: bool
    effective_window_days: int
    days_since_delivery: int
    store_credit_only: bool


def _money(amount_minor: int, fixture: ReturnCaseFixture) -> Money:
    return Money(amount_minor=amount_minor, currency=fixture.product.price.currency)


def compute_return_policy(
    fixture: ReturnCaseFixture, reason: ReturnReason | str
) -> ReturnPolicyDecision:
    """Compute deterministic eligibility and refund terms from fixture facts."""
    parsed_reason = ReturnReason(reason)
    membership = fixture.customer.membership_tier
    tier_extension = _TIER_WINDOW_EXTENSION[membership]
    prime_extension = (
        _PRIME_WINDOW_EXTENSION_DAYS if fixture.customer.has_prime_shipping else 0
    )
    effective_window = (
        fixture.product.return_window_days + tier_extension + prime_extension
    )
    days_since_delivery = (fixture.task_now - fixture.order.delivery_at).days
    zero = _money(0, fixture)

    if fixture.item.item_status != "delivered":
        return ReturnPolicyDecision(
            eligible=False,
            reason_code="already_processed",
            refund_amount=zero,
            policy_computed_amount=zero,
            refund_method=None,
            restocking_fee=zero,
            restocking_discount=zero,
            shipping_clawback=zero,
            paid_return_shipping_fee=zero,
            free_return_shipping=False,
            effective_window_days=effective_window,
            days_since_delivery=days_since_delivery,
            store_credit_only=False,
        )

    is_fault = parsed_reason in _FAULT_REASONS
    in_normal_window = days_since_delivery <= effective_window
    in_grace_window = days_since_delivery <= (
        effective_window + _STORE_CREDIT_GRACE_DAYS
    )
    eligible = is_fault or in_normal_window or in_grace_window
    store_credit_only = not is_fault and not in_normal_window and in_grace_window
    if not eligible:
        return ReturnPolicyDecision(
            eligible=False,
            reason_code="outside_return_window",
            refund_amount=zero,
            policy_computed_amount=zero,
            refund_method=None,
            restocking_fee=zero,
            restocking_discount=zero,
            shipping_clawback=zero,
            paid_return_shipping_fee=zero,
            free_return_shipping=False,
            effective_window_days=effective_window,
            days_since_delivery=days_since_delivery,
            store_credit_only=False,
        )

    price_minor = fixture.product.price.amount_minor
    restocking_fee_minor = 0
    if (
        fixture.product.category == "electronics"
        and parsed_reason is ReturnReason.CHANGED_MIND
    ):
        restocking_fee_minor = price_minor * fixture.product.restocking_fee_pct // 100
    discount_pct = _TIER_RESTOCKING_DISCOUNT_PCT[membership]
    restocking_discount_minor = restocking_fee_minor * discount_pct // 100

    shipping_clawback_minor = 0
    if (
        not is_fault
        and fixture.order.subtotal.amount_minor >= _FREE_SHIPPING_THRESHOLD_MINOR
        and fixture.order.subtotal.amount_minor - price_minor
        < _FREE_SHIPPING_THRESHOLD_MINOR
    ):
        shipping_clawback_minor = _STANDARD_SHIPPING_COST_MINOR

    paid_return_shipping_minor = 0
    if (
        not is_fault
        and fixture.order.subtotal.amount_minor < _LOW_VALUE_RETURN_THRESHOLD_MINOR
    ):
        paid_return_shipping_minor = _STANDARD_SHIPPING_COST_MINOR

    computed_minor = max(
        0,
        price_minor
        - restocking_fee_minor
        + restocking_discount_minor
        - shipping_clawback_minor
        - paid_return_shipping_minor,
    )
    computed = _money(computed_minor, fixture)
    return ReturnPolicyDecision(
        eligible=True,
        reason_code="eligible",
        refund_amount=computed,
        policy_computed_amount=computed,
        refund_method="store_credit" if store_credit_only else "original_payment",
        restocking_fee=_money(restocking_fee_minor, fixture),
        restocking_discount=_money(restocking_discount_minor, fixture),
        shipping_clawback=_money(shipping_clawback_minor, fixture),
        paid_return_shipping_fee=_money(paid_return_shipping_minor, fixture),
        free_return_shipping=is_fault,
        effective_window_days=effective_window,
        days_since_delivery=days_since_delivery,
        store_credit_only=store_credit_only,
    )
