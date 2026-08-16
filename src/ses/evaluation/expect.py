"""Zero-cost checks that run before an Engine receives a request."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import cast

from ses.contracts import CaseDefinition, Usage

from .errors import EvaluationError, EvaluationErrorCode, PreflightError


class PreflightStatus(StrEnum):
    """Outcome of the local expect gate."""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Validated local limits; this is not a persisted contract."""

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_amount: Decimal | None = None
    cost_currency: str | None = None

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens", "max_total_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer")
        amount = self.max_cost_amount
        currency = self.cost_currency
        if (amount is None) != (currency is None):
            raise ValueError("cost amount and currency must be provided together")
        if amount is not None:
            if not isinstance(amount, Decimal):
                raise ValueError("max_cost_amount must be a Decimal")
            if not amount.is_finite() or amount < 0:
                raise ValueError("budget cost must be finite and nonnegative")
            if (
                not isinstance(currency, str)
                or len(currency) != 3
                or not currency.isupper()
            ):
                raise ValueError("budget currency must be a three-letter code")
        if all(
            value is None
            for value in (
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_total_tokens,
                self.max_cost_amount,
            )
        ):
            raise ValueError("budget must define at least one limit")

    @classmethod
    def from_value(
        cls, value: BudgetLimits | int | Mapping[str, object]
    ) -> BudgetLimits:
        if isinstance(value, BudgetLimits):
            # Revalidate even instances created through an unusual construction path.
            value.__post_init__()
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(max_total_tokens=value)
        if not isinstance(value, Mapping):
            raise ValueError("budget must be an integer, mapping, or BudgetLimits")
        allowed_fields = {
            "max_input_tokens",
            "input_tokens",
            "max_output_tokens",
            "output_tokens",
            "max_total_tokens",
            "max_tokens",
            "total_tokens",
            "max_cost_amount",
            "cost_amount",
            "cost_currency",
            "currency",
        }
        unknown_fields = sorted(repr(key) for key in value if key not in allowed_fields)
        if unknown_fields:
            raise ValueError("unknown budget fields: " + ", ".join(unknown_fields))

        def one(names: tuple[str, ...]) -> object | None:
            supplied = [(name, value[name]) for name in names if name in value]
            if len(supplied) > 1:
                raise ValueError(
                    f"conflicting budget fields: {', '.join(n for n, _ in supplied)}"
                )
            return supplied[0][1] if supplied else None

        def integer(names: tuple[str, ...]) -> int | None:
            supplied = one(names)
            if supplied is None:
                return None
            if isinstance(supplied, bool) or not isinstance(supplied, int):
                raise ValueError(f"{names[0]} must be a nonnegative integer")
            return supplied

        raw_amount = one(("max_cost_amount", "cost_amount"))
        raw_currency = one(("cost_currency", "currency"))
        amount: Decimal | None = None
        if raw_amount is not None:
            if not isinstance(raw_amount, (str, Decimal)):
                raise ValueError("budget cost must be a decimal string or Decimal")
            try:
                amount = Decimal(raw_amount)
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("budget cost must be a decimal") from exc
        return cls(
            max_input_tokens=integer(("max_input_tokens", "input_tokens")),
            max_output_tokens=integer(("max_output_tokens", "output_tokens")),
            max_total_tokens=integer(
                ("max_total_tokens", "max_tokens", "total_tokens")
            ),
            max_cost_amount=amount,
            cost_currency=cast(str | None, raw_currency),
        )

    def validate_usage(self, usage: Usage | None) -> tuple[str, ...]:
        if usage is None:
            return ()
        failures: list[str] = []
        if (
            self.max_input_tokens is not None
            and usage.input_tokens > self.max_input_tokens
        ):
            failures.append("input token budget exceeded")
        if (
            self.max_output_tokens is not None
            and usage.output_tokens > self.max_output_tokens
        ):
            failures.append("output token budget exceeded")
        if (
            self.max_total_tokens is not None
            and usage.input_tokens + usage.output_tokens > self.max_total_tokens
        ):
            failures.append("total token budget exceeded")
        if self.max_cost_amount is not None:
            if usage.cost_amount is None or usage.cost_currency != self.cost_currency:
                failures.append("usage cost is missing or uses a different currency")
            elif usage.cost_amount > self.max_cost_amount:
                failures.append("cost budget exceeded")
        return tuple(failures)


@dataclass(frozen=True, slots=True)
class ExpectResult:
    """Immutable preflight result returned before any Engine call."""

    status: PreflightStatus
    failures: tuple[EvaluationError, ...] = ()
    case: CaseDefinition | None = None
    budget: BudgetLimits | None = None

    @property
    def passed(self) -> bool:
        return self.status is PreflightStatus.PASS

    def __bool__(self) -> bool:
        return self.passed

    def raise_if_failed(self) -> None:
        if not self.passed:
            raise PreflightError(self.failures)


def _failure(code: EvaluationErrorCode, message: str) -> EvaluationError:
    return EvaluationError(code, message)


def _load_fixture(
    fixture: Mapping[str, object] | Path | str | None,
) -> Mapping[str, object] | None:
    if fixture is None:
        return None
    if isinstance(fixture, (Path, str)):
        path = Path(fixture)
        if not path.is_file():
            raise ValueError("fixture file does not exist")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("fixture file is not valid UTF-8 JSON") from exc
        if not isinstance(loaded, Mapping):
            raise ValueError("fixture JSON must be an object")
        return cast(Mapping[str, object], loaded)
    if not isinstance(fixture, Mapping) or not fixture:
        raise ValueError("fixture must be a non-empty mapping or existing JSON path")
    return fixture


def expect(
    case: CaseDefinition,
    fixture: Mapping[str, object] | Path | str | None,
    *,
    fixture_id: str,
    available_tools: Collection[str],
    environment_ready: bool,
    environment_closed: bool,
    budget: BudgetLimits | int | Mapping[str, object] | None = None,
    usage: Usage | None = None,
) -> ExpectResult:
    """Validate explicit, local preconditions without invoking an Engine."""

    failures: list[EvaluationError] = []
    valid_case = isinstance(case, CaseDefinition)
    if not valid_case:
        failures.append(_failure(EvaluationErrorCode.INVALID_CASE, "case is invalid"))

    loaded_fixture: Mapping[str, object] | None = None
    try:
        loaded_fixture = _load_fixture(fixture)
        if loaded_fixture is None:
            raise ValueError("fixture is required")
    except ValueError as exc:
        failures.append(_failure(EvaluationErrorCode.MISSING_FIXTURE, str(exc)))

    if not isinstance(fixture_id, str) or not fixture_id.strip():
        failures.append(
            _failure(EvaluationErrorCode.MISSING_FIXTURE, "fixture_id is required")
        )
    elif valid_case and fixture_id != case.fixture_id:
        failures.append(
            _failure(
                EvaluationErrorCode.MISSING_FIXTURE,
                "fixture_id does not match case.fixture_id",
            )
        )
    if loaded_fixture is not None:
        embedded_id = loaded_fixture.get("fixture_id")
        if not isinstance(embedded_id, str) or embedded_id != fixture_id:
            failures.append(
                _failure(
                    EvaluationErrorCode.MISSING_FIXTURE,
                    "fixture content does not match fixture_id",
                )
            )

    valid_tool_names = bool(available_tools) and all(
        isinstance(name, str) and bool(name.strip()) for name in available_tools
    )
    if not valid_tool_names:
        failures.append(
            _failure(
                EvaluationErrorCode.MISSING_TOOL,
                "environment must expose actual available tool names",
            )
        )
    elif valid_case:
        actual_names = set(available_tools)
        missing = tuple(
            name for name in case.required_tools if name not in actual_names
        )
        if missing:
            failures.append(
                _failure(
                    EvaluationErrorCode.MISSING_TOOL,
                    "required tools are unavailable: " + ", ".join(missing),
                )
            )

    if not isinstance(environment_ready, bool):
        failures.append(
            _failure(
                EvaluationErrorCode.ENVIRONMENT_NOT_READY,
                "environment_ready must be boolean",
            )
        )
    elif not environment_ready:
        failures.append(
            _failure(
                EvaluationErrorCode.ENVIRONMENT_NOT_READY,
                "environment is not ready",
            )
        )
    if not isinstance(environment_closed, bool):
        failures.append(
            _failure(
                EvaluationErrorCode.ENVIRONMENT_NOT_READY,
                "environment_closed must be boolean",
            )
        )
    elif environment_closed:
        failures.append(
            _failure(
                EvaluationErrorCode.ENVIRONMENT_NOT_READY,
                "environment is closed",
            )
        )

    parsed_budget: BudgetLimits | None = None
    if budget is not None:
        try:
            parsed_budget = BudgetLimits.from_value(budget)
            failures.extend(
                _failure(EvaluationErrorCode.INVALID_BUDGET, message)
                for message in parsed_budget.validate_usage(usage)
            )
        except ValueError as exc:
            failures.append(_failure(EvaluationErrorCode.INVALID_BUDGET, str(exc)))

    return ExpectResult(
        status=PreflightStatus.FAIL if failures else PreflightStatus.PASS,
        failures=tuple(failures),
        case=case if valid_case else None,
        budget=parsed_budget,
    )


check_expectations = expect
