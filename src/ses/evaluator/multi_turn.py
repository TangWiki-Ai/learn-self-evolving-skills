"""Multi-turn case evaluation with case-local session ownership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from time import monotonic

from ses.contracts import (
    EngineExitStatus,
    EngineRequest,
    RecordType,
    SchemaVersion,
    Trace,
    Usage,
)
from ses.engines.base import Engine
from ses.evaluation import build_trace, trace_messages
from ses.simulation import ConstrainedUserSimulator, SimulatorTurnKind


class MultiTurnOutcome(StrEnum):
    """Evaluation outcomes before deterministic grading is attached."""

    AGENT_FAIL = "agent_fail"
    COMPLETED = "completed"
    SIMULATOR_ERROR = "simulator_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BUDGET_STOP = "budget_stop"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True, slots=True)
class MultiTurnResult:
    """Completed and partial traces from one isolated case iteration."""

    run_id: str
    case_id: str
    iteration_id: str
    outcome: MultiTurnOutcome
    traces: tuple[Trace, ...]
    usage: Usage
    latency_ms: int
    stop_reason: str | None = None


def _sum_usage(traces: tuple[Trace, ...]) -> Usage:
    input_tokens = sum(trace.usage.input_tokens for trace in traces if trace.usage)
    output_tokens = sum(trace.usage.output_tokens for trace in traces if trace.usage)
    costs = [
        trace.usage
        for trace in traces
        if trace.usage and trace.usage.cost_amount is not None
    ]
    currencies = {usage.cost_currency for usage in costs}
    if len(currencies) > 1:
        raise ValueError("a case iteration cannot combine usage currencies")
    cost = sum((usage.cost_amount or Decimal(0) for usage in costs), Decimal(0))
    currency = next(iter(currencies)) if currencies else None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=cost if currencies else None,
        cost_currency=currency,
    )


def _usage_budget_reason(
    usage: Usage,
    *,
    max_input_tokens: int | None,
    max_output_tokens: int | None,
    max_cost_amount: Decimal | None,
    cost_currency: str,
) -> str | None:
    if max_input_tokens is not None and usage.input_tokens >= max_input_tokens:
        return "input_token_limit"
    if max_output_tokens is not None and usage.output_tokens >= max_output_tokens:
        return "output_token_limit"
    if max_cost_amount is None:
        return None
    actual_cost = usage.cost_amount or Decimal(0)
    if usage.cost_currency is not None and usage.cost_currency != cost_currency:
        raise ValueError("case usage currency does not match the run budget")
    return "cost_limit" if actual_cost >= max_cost_amount else None


class MultiTurnEvaluator:
    """Drive one simulator and resume only the session created for that case."""

    def __init__(
        self,
        engine: Engine,
        *,
        allowed_tools: tuple[str, ...] = (),
        timeout_seconds: float = 30,
        on_trace: Callable[[Trace], None] | None = None,
    ) -> None:
        self._engine = engine
        self._allowed_tools = allowed_tools
        self._timeout_seconds = timeout_seconds
        self._on_trace = on_trace

    async def evaluate(
        self,
        *,
        run_id: str,
        case_id: str,
        iteration_id: str,
        simulator: ConstrainedUserSimulator,
        max_turns: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_cost_amount: Decimal | None = None,
        cost_currency: str = "CNY",
    ) -> MultiTurnResult:
        """Run fresh then resumed requests until the user ends or budget stops."""
        if max_turns < 1:
            raise ValueError("max_turns must be at least one")
        for value in (max_input_tokens, max_output_tokens):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("token budgets must be nonnegative integers")
        if max_cost_amount is not None and (
            not max_cost_amount.is_finite() or max_cost_amount < 0
        ):
            raise ValueError("cost budget must be finite and nonnegative")
        if not cost_currency.strip():
            raise ValueError("cost currency must not be blank")
        started = monotonic()
        traces: list[Trace] = []
        assistant_messages: list[str] = []
        session_id: str | None = None
        try:
            turn = simulator.next_turn(())
        except Exception as exc:
            return MultiTurnResult(
                run_id=run_id,
                case_id=case_id,
                iteration_id=iteration_id,
                outcome=MultiTurnOutcome.SIMULATOR_ERROR,
                traces=(),
                usage=_sum_usage(()),
                latency_ms=round((monotonic() - started) * 1000),
                stop_reason=f"simulator raised {type(exc).__name__}",
            )
        initial_reason = _usage_budget_reason(
            _sum_usage(()),
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_cost_amount=max_cost_amount,
            cost_currency=cost_currency,
        )
        if initial_reason is not None:
            return MultiTurnResult(
                run_id=run_id,
                case_id=case_id,
                iteration_id=iteration_id,
                outcome=MultiTurnOutcome.BUDGET_STOP,
                traces=(),
                usage=_sum_usage(()),
                latency_ms=round((monotonic() - started) * 1000),
                stop_reason=initial_reason,
            )
        while turn.kind is SimulatorTurnKind.MESSAGE:
            if len(traces) >= max_turns:
                completed = tuple(traces)
                return MultiTurnResult(
                    run_id=run_id,
                    case_id=case_id,
                    iteration_id=iteration_id,
                    outcome=MultiTurnOutcome.BUDGET_STOP,
                    traces=completed,
                    usage=_sum_usage(completed),
                    latency_ms=round((monotonic() - started) * 1000),
                    stop_reason="turn_limit",
                )
            request = EngineRequest(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type=RecordType.ENGINE_REQUEST,
                request_id=f"{run_id}:{case_id}:{iteration_id}:turn-{len(traces)}",
                prompt=turn.message or "User ended the conversation.",
                resume_session_id=session_id,
                allowed_tools=self._allowed_tools,
                timeout_seconds=self._timeout_seconds,
            )
            try:
                events = tuple([event async for event in self._engine.stream(request)])
            except Exception as exc:
                completed = tuple(traces)
                return MultiTurnResult(
                    run_id=run_id,
                    case_id=case_id,
                    iteration_id=iteration_id,
                    outcome=MultiTurnOutcome.INFRASTRUCTURE_ERROR,
                    traces=completed,
                    usage=_sum_usage(completed),
                    latency_ms=round((monotonic() - started) * 1000),
                    stop_reason=f"engine raised {type(exc).__name__}",
                )
            trace = build_trace(
                events,
                request=request,
                run_id=run_id,
                case_id=case_id,
                iteration_id=f"{iteration_id}:turn-{len(traces)}",
            )
            traces.append(trace)
            if self._on_trace is not None:
                try:
                    self._on_trace(trace)
                except Exception as exc:
                    completed = tuple(traces)
                    return MultiTurnResult(
                        run_id=run_id,
                        case_id=case_id,
                        iteration_id=iteration_id,
                        outcome=MultiTurnOutcome.INFRASTRUCTURE_ERROR,
                        traces=completed,
                        usage=_sum_usage(completed),
                        latency_ms=round((monotonic() - started) * 1000),
                        stop_reason=f"trace persistence raised {type(exc).__name__}",
                    )
            if trace.exit_status is not EngineExitStatus.SUCCESS:
                completed = tuple(traces)
                return MultiTurnResult(
                    run_id=run_id,
                    case_id=case_id,
                    iteration_id=iteration_id,
                    outcome=MultiTurnOutcome.INFRASTRUCTURE_ERROR,
                    traces=completed,
                    usage=_sum_usage(completed),
                    latency_ms=round((monotonic() - started) * 1000),
                    stop_reason=trace.exit_status.value,
                )
            if session_id is None:
                session_id = trace.session_id
            elif trace.session_id != session_id:
                raise RuntimeError("engine changed session within one case")
            assistant_messages.extend(message.text for message in trace_messages(trace))
            try:
                turn = simulator.next_turn(tuple(assistant_messages))
            except Exception as exc:
                completed = tuple(traces)
                return MultiTurnResult(
                    run_id=run_id,
                    case_id=case_id,
                    iteration_id=iteration_id,
                    outcome=MultiTurnOutcome.SIMULATOR_ERROR,
                    traces=completed,
                    usage=_sum_usage(completed),
                    latency_ms=round((monotonic() - started) * 1000),
                    stop_reason=f"simulator raised {type(exc).__name__}",
                )
            if turn.kind is SimulatorTurnKind.MESSAGE:
                completed = tuple(traces)
                reason = _usage_budget_reason(
                    _sum_usage(completed),
                    max_input_tokens=max_input_tokens,
                    max_output_tokens=max_output_tokens,
                    max_cost_amount=max_cost_amount,
                    cost_currency=cost_currency,
                )
                if reason is not None:
                    return MultiTurnResult(
                        run_id=run_id,
                        case_id=case_id,
                        iteration_id=iteration_id,
                        outcome=MultiTurnOutcome.BUDGET_STOP,
                        traces=completed,
                        usage=_sum_usage(completed),
                        latency_ms=round((monotonic() - started) * 1000),
                        stop_reason=reason,
                    )
        completed = tuple(traces)
        return MultiTurnResult(
            run_id=run_id,
            case_id=case_id,
            iteration_id=iteration_id,
            outcome=MultiTurnOutcome.COMPLETED,
            traces=completed,
            usage=_sum_usage(completed),
            latency_ms=round((monotonic() - started) * 1000),
        )
