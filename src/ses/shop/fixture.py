"""Typed loading for the single pinned STATE-Bench shop fixture."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ses.contracts import (
    CaseDefinition,
    CaseSplit,
    Money,
    RecordType,
    SchemaVersion,
    UtcDateTime,
)


class FixtureModel(BaseModel):
    """Strict immutable base for shop fixture records."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ProductFixture(FixtureModel):
    product_id: str
    name: str
    category: str
    price: Money
    return_window_days: int = Field(ge=0)
    restocking_fee_pct: int = Field(ge=0, le=100)


class OrderFixture(FixtureModel):
    order_id: str
    customer_id: str
    status: Literal["delivered"]
    delivery_at: UtcDateTime
    payment_method: str
    subtotal: Money


class OrderItemFixture(FixtureModel):
    item_id: str
    order_id: str
    product_id: str
    item_status: Literal["delivered"]


class CustomerFixture(FixtureModel):
    customer_id: str
    membership_tier: Literal["standard", "silver", "gold", "platinum"]
    has_prime_shipping: bool


class ReturnCaseFixture(FixtureModel):
    """All source and policy facts needed to execute the pinned case."""

    fixture_id: str
    case_id: str
    source_id: str
    source_commit: str
    transformation_version: str
    policy_version: str
    task_id: str
    task_type: Literal["return_item"]
    task_now: UtcDateTime
    user_prompt: str
    required_tools: tuple[str, ...]
    product: ProductFixture
    order: OrderFixture
    item: OrderItemFixture
    customer: CustomerFixture

    @model_validator(mode="after")
    def _validate_links_and_currency(self) -> Self:
        if self.order.customer_id != self.customer.customer_id:
            raise ValueError("order customer_id must reference fixture customer")
        if self.item.order_id != self.order.order_id:
            raise ValueError("item order_id must reference fixture order")
        if self.item.product_id != self.product.product_id:
            raise ValueError("item product_id must reference fixture product")
        if self.order.subtotal.currency != self.product.price.currency:
            raise ValueError("fixture money must use one currency")
        return self

    def case_definition(self) -> CaseDefinition:
        """Derive the public case contract without duplicating source facts."""
        return CaseDefinition(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.CASE_DEFINITION,
            case_id=self.case_id,
            source_id=self.source_id,
            source_version=self.source_commit,
            transformation_version=self.transformation_version,
            split=CaseSplit.DEVELOP,
            user_prompt=self.user_prompt,
            fixture_id=self.fixture_id,
            required_tools=self.required_tools,
        )


def load_case_fixture(path: Path | None = None) -> ReturnCaseFixture:
    """Load and validate the canonical fixture bundled with the shop package."""
    if path is None:
        payload = (
            files("ses.shop")
            .joinpath("fixtures/return_defective_electronics.json")
            .read_text(encoding="utf-8")
        )
    else:
        payload = path.read_text(encoding="utf-8")
    return ReturnCaseFixture.model_validate_json(payload)


PINNED_CASE_FIXTURE = load_case_fixture()
