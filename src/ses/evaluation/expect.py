"""Zero-cost checks that run before an Engine receives a request."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

from ses.contracts import CaseDefinition, EngineRequest, Usage

from .errors import EvaluationError, EvaluationErrorCode, PreflightError


class PreflightStatus(StrEnum):
    """Outcome of the local expect gate."""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Internal preflight budget description; no persisted contract is invented."""

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_amount: Decimal | None = None
    cost_currency: str | None = None

    @classmethod
    def from_value(cls, value: object) -> BudgetLimits:
        if isinstance(value, BudgetLimits):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(max_total_tokens=value)
        if not isinstance(value, Mapping):
            raise ValueError("budget must be a mapping or BudgetLimits")

        def integer(*names: str) -> int | None:
            supplied = next((value[name] for name in names if name in value), None)
            if supplied is None:
                return None
            if (
                isinstance(supplied, bool)
                or not isinstance(supplied, int)
                or supplied < 0
            ):
                raise ValueError(f"{names[0]} must be a nonnegative integer")
            return cast(int, supplied)

        max_input = integer("max_input_tokens", "input_tokens")
        max_output = integer("max_output_tokens", "output_tokens")
        max_total = integer("max_total_tokens", "max_tokens", "total_tokens")
        raw_amount = value.get("max_cost_amount", value.get("cost_amount"))
        raw_currency = value.get("cost_currency", value.get("currency"))
        amount: Decimal | None = None
        currency: str | None = None
        if raw_amount is not None or raw_currency is not None:
            if not isinstance(raw_amount, (str, Decimal)):
                raise ValueError("budget cost must be a decimal string")
            if (
                not isinstance(raw_currency, str)
                or len(raw_currency) != 3
                or not raw_currency.isupper()
            ):
                raise ValueError("budget currency must be a three-letter code")
            try:
                amount = Decimal(raw_amount)
            except (ArithmeticError, ValueError) as exc:
                raise ValueError("budget cost must be a decimal") from exc
            if not amount.is_finite() or amount < 0:
                raise ValueError("budget cost must be finite and nonnegative")
            currency = raw_currency
        if all(item is None for item in (max_input, max_output, max_total, amount)):
            raise ValueError("budget must define at least one limit")
        return cls(max_input, max_output, max_total, amount, currency)

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
        total = usage.input_tokens + usage.output_tokens
        if self.max_total_tokens is not None and total > self.max_total_tokens:
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


@dataclass(frozen=True, slots=True)
class ExecutionAfterExpect:
    """The result of an explicitly guarded Engine invocation."""

    preflight: ExpectResult
    value: object | None
    engine_called: bool


def _failure(
    code: EvaluationErrorCode,
    message: str,
) -> EvaluationError:
    return EvaluationError(code, message)


def _load_fixture(fixture: object) -> object:
    if isinstance(fixture, Path):
        try:
            return json.loads(fixture.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("fixture file does not exist") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("fixture file is not valid UTF-8 JSON") from exc
    if isinstance(fixture, str):
        path = Path(fixture)
        if path.exists():
            return _load_fixture(path)
    return fixture


def _fixture_id(fixture: object) -> str | None:
    if isinstance(fixture, Mapping):
        value = fixture.get("fixture_id", fixture.get("id"))
        return value if isinstance(value, str) else None
    value = getattr(fixture, "fixture_id", None)
    return value if isinstance(value, str) else None


def _fixture_tools(fixture: object) -> tuple[str, ...]:
    if not isinstance(fixture, Mapping):
        return ()
    value = fixture.get("available_tools", fixture.get("tools", ()))
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _environment_tools(environment: object) -> tuple[str, ...]:
    if isinstance(environment, Mapping):
        value = environment.get("available_tools", environment.get("tools", ()))
    else:
        value = getattr(
            environment, "available_tools", getattr(environment, "tools", ())
        )
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _environment_failures(environment: object | None) -> tuple[str, ...]:
    if environment is None:
        return ()
    if isinstance(environment, Mapping):
        ready = environment.get("ready", environment.get("available"))
        if ready is False:
            return ("environment is not ready",)
        if environment.get("closed") is True:
            return ("environment is closed",)
        required = environment.get("required", {})
        if isinstance(required, Mapping):
            missing = tuple(
                str(key)
                for key, value in required.items()
                if value is False or value is None
            )
            if missing:
                return ("environment prerequisites missing: " + ", ".join(missing),)
        return ()
    ready = getattr(environment, "ready", None)
    if ready is False:
        return ("environment is not ready",)
    is_ready = getattr(environment, "is_ready", None)
    if callable(is_ready):
        try:
            if is_ready() is False:
                return ("environment is not ready",)
        except Exception as exc:
            return (f"environment readiness check failed: {type(exc).__name__}",)
    return ()


def expect(
    case: CaseDefinition | Mapping[str, object] | object,
    fixture: object | None = None,
    *,
    required_tools: Iterable[str] | None = None,
    available_tools: Iterable[str] | None = None,
    request: EngineRequest | None = None,
    budget: object | None = None,
    usage: Usage | None = None,
    environment: object | None = None,
) -> ExpectResult:
    """Validate all cheap preconditions without touching an Engine.

    ``fixture`` may be a loaded fixture object or a local JSON path.  The
    function never invokes an object passed as ``environment`` or ``request``;
    it only reads their public readiness and allow-list fields.
    """

    failures: list[EvaluationError] = []
    parsed_case: CaseDefinition | None = None
    try:
        parsed_case = CaseDefinition.model_validate(case)
    except Exception:  # Pydantic exposes several validation subclasses.
        failures.append(_failure(EvaluationErrorCode.INVALID_CASE, "case is invalid"))

    loaded_fixture: object | None = None
    if fixture is None:
        failures.append(
            _failure(EvaluationErrorCode.MISSING_FIXTURE, "fixture is required")
        )
    else:
        try:
            loaded_fixture = _load_fixture(fixture)
            if loaded_fixture is None:
                failures.append(
                    _failure(EvaluationErrorCode.MISSING_FIXTURE, "fixture is empty")
                )
        except ValueError as exc:
            failures.append(_failure(EvaluationErrorCode.MISSING_FIXTURE, str(exc)))

    if parsed_case is not None and loaded_fixture is not None:
        fixture_id = _fixture_id(loaded_fixture)
        if fixture_id is not None and fixture_id != parsed_case.fixture_id:
            failures.append(
                _failure(
                    EvaluationErrorCode.MISSING_FIXTURE,
                    "fixture ID does not match case.fixture_id",
                )
            )
        if isinstance(fixture, str) and fixture == parsed_case.fixture_id:
            pass

    required = tuple(
        required_tools or (parsed_case.required_tools if parsed_case else ())
    )
    if available_tools is not None:
        provided_tools = tuple(available_tools)
    else:
        provided_tools = _fixture_tools(loaded_fixture) + _environment_tools(
            environment
        )
        if not provided_tools and request is not None:
            provided_tools = request.allowed_tools
    missing_tools = tuple(tool for tool in required if tool not in set(provided_tools))
    if missing_tools:
        failures.append(
            _failure(
                EvaluationErrorCode.MISSING_TOOL,
                "required tools are unavailable: " + ", ".join(missing_tools),
            )
        )
    if request is not None:
        missing_from_request = tuple(
            tool for tool in required if tool not in set(request.allowed_tools)
        )
        if missing_from_request:
            failures.append(
                _failure(
                    EvaluationErrorCode.MISSING_TOOL,
                    "required tools are not allowed by the request: "
                    + ", ".join(missing_from_request),
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

    failures.extend(
        _failure(EvaluationErrorCode.ENVIRONMENT_NOT_READY, message)
        for message in _environment_failures(environment)
    )

    return ExpectResult(
        status=PreflightStatus.FAIL if failures else PreflightStatus.PASS,
        failures=tuple(failures),
        case=parsed_case,
        budget=parsed_budget,
    )


def run_after_expect(
    engine: object,
    request: EngineRequest,
    case: CaseDefinition | Mapping[str, object] | object,
    fixture: object | None = None,
    *,
    required_tools: Iterable[str] | None = None,
    available_tools: Iterable[str] | None = None,
    budget: object | None = None,
    usage: Usage | None = None,
    environment: object | None = None,
) -> ExecutionAfterExpect:
    """Call an Engine only after ``expect`` passes.

    The narrow Engine seam is intentionally duck-typed here.  A foundation
    adapter can expose ``run(request)`` or be a callable accepting the request.
    """

    preflight = expect(
        case,
        fixture,
        required_tools=required_tools,
        available_tools=available_tools,
        request=request,
        budget=budget,
        usage=usage,
        environment=environment,
    )
    if not preflight.passed:
        return ExecutionAfterExpect(preflight, None, False)
    run = getattr(engine, "run", None)
    if callable(run):
        return ExecutionAfterExpect(preflight, run(request), True)
    if callable(engine):
        callback = cast(Callable[[EngineRequest], object], engine)
        return ExecutionAfterExpect(preflight, callback(request), True)
    raise TypeError("engine must expose run(request) or be callable")


check_expectations = expect
