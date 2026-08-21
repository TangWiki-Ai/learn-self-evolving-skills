"""Append-only, budgeted baseline orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import JsonValue

from ses.contracts.engine import Usage
from ses.contracts.primitives import SchemaVersion
from ses.contracts.runner import (
    BudgetState,
    RunArtifacts,
    RunConfig,
    RunEventType,
    RunnerStatus,
    RunRecord,
)
from ses.contracts.security import validate_public_data

_SAFE_RUN_ID = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_EVALUATED_STATUSES = frozenset(
    {
        RunnerStatus.PASS.value,
        RunnerStatus.AGENT_FAIL.value,
        RunnerStatus.SIMULATOR_ERROR.value,
        RunnerStatus.JUDGE_ERROR.value,
        RunnerStatus.INFRASTRUCTURE_ERROR.value,
    }
)


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Independent hard limits applied in documented precedence order."""

    max_cases: int
    max_turns_per_case: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost: Decimal | None = None
    cost_currency: str = "CNY"

    def __post_init__(self) -> None:
        values = (self.max_cases, self.max_turns_per_case)
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("case and turn budgets must be nonnegative integers")
        if self.max_turns_per_case < 1:
            raise ValueError("max_turns_per_case must be at least one")
        for value in (self.max_input_tokens, self.max_output_tokens):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("token budgets must be nonnegative integers")
        if self.max_cost is not None and (
            not self.max_cost.is_finite() or self.max_cost < 0
        ):
            raise ValueError("cost budget must be finite and nonnegative")

    def to_state(
        self,
        *,
        consumed_cases: int = 0,
        consumed_turns: int = 0,
        consumed_input_tokens: int = 0,
        consumed_output_tokens: int = 0,
        consumed_cost_amount: Decimal = Decimal(0),
        consumed_latency_ms: int = 0,
        stop_reason: str | None = None,
    ) -> BudgetState:
        return BudgetState(
            max_cases=self.max_cases,
            max_turns_per_case=self.max_turns_per_case,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_cost_amount=self.max_cost,
            cost_currency=self.cost_currency,
            consumed_cases=consumed_cases,
            consumed_turns=consumed_turns,
            consumed_input_tokens=consumed_input_tokens,
            consumed_output_tokens=consumed_output_tokens,
            consumed_cost_amount=consumed_cost_amount,
            consumed_latency_ms=consumed_latency_ms,
            stop_reason=stop_reason,
        )


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Attempt-local paths and limits passed to a pipeline evaluator."""

    run_id: str
    run_dir: Path
    case_id: str
    iteration_id: str
    attempt_id: str
    max_turns: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_amount: Decimal | None = None
    cost_currency: str = "CNY"


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    """Evaluator output projected into one append-only attempt record."""

    case_id: str
    iteration_id: str
    status: RunnerStatus
    turn_count: int
    input_tokens: int
    output_tokens: int
    cost_amount: Decimal = Decimal(0)
    cost_currency: str = "CNY"
    cost_complete: bool = True
    latency_ms: int = 0
    artifacts: RunArtifacts = field(default_factory=RunArtifacts)
    session_resumed: bool = False
    evidence: tuple[Mapping[str, JsonValue], ...] = ()
    tool_timeline: tuple[Mapping[str, JsonValue], ...] = ()
    state_diff: Mapping[str, JsonValue] = field(default_factory=dict)
    transcript: tuple[Mapping[str, JsonValue], ...] = ()
    error: str | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        counts = (
            self.turn_count,
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
        )
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("usage and latency values must be nonnegative integers")
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("cost must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class BaselineRun:
    """Paths and derived latest-state view for one runner invocation."""

    run_id: str
    run_dir: Path
    events_path: Path
    summary_path: Path
    latest_results: Mapping[tuple[str, str], Mapping[str, object]]
    metrics: Mapping[str, int | float]
    stop_reason: str | None


LegacyEvaluator = Callable[[str, str, int], CaseEvaluation]


class AttemptEvaluator(Protocol):
    def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation: ...


Evaluator = LegacyEvaluator | AttemptEvaluator


def _config_hash(config: RunConfig, budget: BudgetState) -> str:
    payload = {
        "config": config.model_dump(mode="json"),
        "budget": budget.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append_record(path: Path, record: RunRecord) -> dict[str, object]:
    event = cast(dict[str, object], record.model_dump(mode="json"))
    validate_public_data(event)
    payload = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return event


def load_run_events(path: Path) -> list[dict[str, object]]:
    """Load and validate complete canonical append-only event lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError("baseline event log does not exist") from exc
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            record = RunRecord.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(
                f"invalid run record at line {line_number}: {exc}"
            ) from exc
        if record.sequence != len(events):
            raise ValueError("event sequences must be contiguous and append-only")
        events.append(cast(dict[str, object], record.model_dump(mode="json")))
    return events


def _attempts(events: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [
        event
        for event in events
        if event.get("event_type") == RunEventType.ATTEMPT.value
    ]


def _latest_results(
    events: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], Mapping[str, object]]:
    latest: dict[tuple[str, str], Mapping[str, object]] = {}
    for event in _attempts(events):
        case_id = event.get("case_id")
        iteration_id = event.get("iteration_id")
        if isinstance(case_id, str) and isinstance(iteration_id, str):
            latest[(case_id, iteration_id)] = event
    return latest


def compute_reliability_metrics(
    results: Sequence[Mapping[str, object]], *, k: int
) -> dict[str, int | float]:
    """Compute pass@1 and all-k reliability over every sampled case."""
    if k < 1:
        raise ValueError("k must be at least one")
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for result in results:
        case_id = result.get("case_id")
        status = result.get("status")
        if not isinstance(case_id, str):
            continue
        grouped.setdefault(case_id, [])
        if status in _EVALUATED_STATUSES:
            grouped[case_id].append(result)
    for values in grouped.values():
        values.sort(key=lambda value: str(value.get("iteration_id")))
    first = [values[0] for values in grouped.values() if values]
    reliable_count = sum(
        len(values) >= k
        and all(value.get("status") == RunnerStatus.PASS.value for value in values[:k])
        for values in grouped.values()
    )
    return {
        "sample_size": len(first),
        "iteration_sample_size": sum(len(values) for values in grouped.values()),
        "pass_at_1": (
            sum(value.get("status") == RunnerStatus.PASS.value for value in first)
            / len(first)
            if first
            else 0.0
        ),
        "pass_power_k": reliable_count / len(grouped) if grouped else 0.0,
        "k": k,
    }


def _usage_totals(
    events: Sequence[Mapping[str, object]],
) -> tuple[int, int, Decimal, int, int, int]:
    input_tokens = 0
    output_tokens = 0
    cost = Decimal(0)
    latency_ms = 0
    turns = 0
    slots: set[tuple[str, str]] = set()
    for event in _attempts(events):
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            input_tokens += int(cast(Any, usage.get("input_tokens", 0)))
            output_tokens += int(cast(Any, usage.get("output_tokens", 0)))
            cost += Decimal(str(usage.get("cost_amount") or "0"))
        latency_ms += int(cast(Any, event.get("latency_ms", 0)))
        turns += int(cast(Any, event.get("turn_count", 0)))
        case_id = event.get("case_id")
        iteration_id = event.get("iteration_id")
        if isinstance(case_id, str) and isinstance(iteration_id, str):
            slots.add((case_id, iteration_id))
    return input_tokens, output_tokens, cost, latency_ms, turns, len(slots)


def _budget_reason(budget: BudgetState) -> str | None:
    if (
        budget.max_input_tokens is not None
        and budget.consumed_input_tokens >= budget.max_input_tokens
    ):
        return "input_token_limit"
    if (
        budget.max_output_tokens is not None
        and budget.consumed_output_tokens >= budget.max_output_tokens
    ):
        return "output_token_limit"
    if (
        budget.max_cost_amount is not None
        and budget.consumed_cost_amount >= budget.max_cost_amount
    ):
        return "cost_limit"
    return None


def _remaining(limit: int | None, consumed: int) -> int | None:
    return None if limit is None else max(limit - consumed, 0)


def _remaining_cost(limit: Decimal | None, consumed: Decimal) -> Decimal | None:
    return None if limit is None else max(limit - consumed, Decimal(0))


class BaselineRunner:
    """Run planned case iterations with append-only attempts and safe resume."""

    def __init__(self, output_root: Path, evaluator: Evaluator) -> None:
        self._output_root = output_root.resolve()
        self._evaluator = evaluator

    def run(
        self,
        *,
        run_id: str,
        case_ids: Sequence[str],
        iterations: int,
        budgets: BudgetLimits,
        resume: bool = False,
        rerun_case_ids: Sequence[str] = (),
        data_version: str = "unversioned-data",
        model_lock_hash: str = _EMPTY_HASH,
        skill_hash: str = _EMPTY_HASH,
        protocol_version: str = "ses-runner-v1",
    ) -> BaselineRun:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must be a safe run-prefixed identifier")
        if iterations < 1:
            raise ValueError("iterations must be at least one")
        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise ValueError("case_ids must be nonempty and unique")
        if not set(rerun_case_ids).issubset(case_ids):
            raise ValueError("rerun cases must belong to the run plan")

        run_dir = self._output_root / run_id
        events_path = run_dir / "events.jsonl"
        summary_path = run_dir / "summary.json"
        case_plan = tuple(
            f"{case_id}:iteration-{index}"
            for case_id in case_ids
            for index in range(iterations)
        )
        config = RunConfig(
            data_version=data_version,
            model_lock_hash=model_lock_hash,
            skill_hash=skill_hash,
            protocol_version=protocol_version,
            case_ids=tuple(case_ids),
            case_plan=case_plan,
            iterations=iterations,
        )
        initial_budget = budgets.to_state()
        config_hash = _config_hash(config, initial_budget)
        if resume:
            events = load_run_events(events_path)
            started = events[0] if events else None
            if started is None or started.get("config_hash") != config_hash:
                raise ValueError("resume configuration does not match the original run")
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            start = RunRecord(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="run_record",
                event_type=RunEventType.RUN_STARTED,
                sequence=0,
                run_id=run_id,
                config_hash=config_hash,
                config=config,
                budget=initial_budget,
            )
            events = [_append_record(events_path, start)]

        latest = _latest_results(events)
        planned: list[tuple[str, str, str | None]] = [
            (case_id, f"iteration-{index}", None)
            for case_id in case_ids
            for index in range(iterations)
        ]
        for case_id in rerun_case_ids:
            indexes = [
                int(iteration_id.removeprefix("iteration-"))
                for existing_case, iteration_id in latest
                if existing_case == case_id and iteration_id.startswith("iteration-")
            ]
            next_index = max(indexes, default=-1) + 1
            planned.append(
                (
                    case_id,
                    f"iteration-{next_index}",
                    f"iteration-{max(indexes)}" if indexes else None,
                )
            )

        stop_reason = next(
            (
                str(event["stop_reason"])
                for event in reversed(events)
                if event.get("event_type") == RunEventType.BUDGET_STOP.value
                and event.get("stop_reason")
            ),
            None,
        )
        for case_id, iteration_id, supersedes in planned:
            key = (case_id, iteration_id)
            previous = latest.get(key)
            recoverable = bool(
                previous
                and previous.get("status") == RunnerStatus.INFRASTRUCTURE_ERROR.value
                and previous.get("recoverable")
            )
            if previous is not None and not recoverable and supersedes is None:
                continue
            if stop_reason is not None:
                continue

            totals = _usage_totals(events)
            budget = budgets.to_state(
                consumed_input_tokens=totals[0],
                consumed_output_tokens=totals[1],
                consumed_cost_amount=totals[2],
                consumed_latency_ms=totals[3],
                consumed_turns=totals[4],
                consumed_cases=totals[5],
            )
            reason = _budget_reason(budget)
            if (
                reason is None
                and not recoverable
                and budget.consumed_cases >= budgets.max_cases
            ):
                reason = "case_limit"
            if reason is not None:
                stop_reason = reason
                self._record_stop(
                    events_path,
                    events,
                    run_id,
                    config_hash,
                    case_id,
                    iteration_id,
                    budget,
                    reason,
                )
                continue

            prior_attempts = sum(
                event.get("case_id") == case_id
                and event.get("iteration_id") == iteration_id
                for event in _attempts(events)
            )
            attempt_id = f"attempt-{prior_attempts}"
            context = EvaluationContext(
                run_id=run_id,
                run_dir=run_dir,
                case_id=case_id,
                iteration_id=iteration_id,
                attempt_id=attempt_id,
                max_turns=budgets.max_turns_per_case,
                max_input_tokens=_remaining(budgets.max_input_tokens, totals[0]),
                max_output_tokens=_remaining(budgets.max_output_tokens, totals[1]),
                max_cost_amount=_remaining_cost(budgets.max_cost, totals[2]),
                cost_currency=budgets.cost_currency,
            )
            try:
                method = getattr(self._evaluator, "evaluate_attempt", None)
                evaluation = (
                    method(context)
                    if callable(method)
                    else cast(LegacyEvaluator, self._evaluator)(
                        case_id, iteration_id, budgets.max_turns_per_case
                    )
                )
            except Exception as exc:
                evaluation = CaseEvaluation(
                    case_id=case_id,
                    iteration_id=iteration_id,
                    status=RunnerStatus.INFRASTRUCTURE_ERROR,
                    turn_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    error=f"evaluator raised {type(exc).__name__}",
                )
            post = (
                totals[0] + evaluation.input_tokens,
                totals[1] + evaluation.output_tokens,
                totals[2] + evaluation.cost_amount,
                totals[3] + evaluation.latency_ms,
                totals[4] + evaluation.turn_count,
                totals[5] + (0 if recoverable else 1),
            )
            attempt_budget = budgets.to_state(
                consumed_input_tokens=post[0],
                consumed_output_tokens=post[1],
                consumed_cost_amount=post[2],
                consumed_latency_ms=post[3],
                consumed_turns=post[4],
                consumed_cases=post[5],
            )
            record = RunRecord(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="run_record",
                event_type=RunEventType.ATTEMPT,
                sequence=len(events),
                run_id=run_id,
                config_hash=config_hash,
                case_id=case_id,
                iteration_id=iteration_id,
                attempt_id=attempt_id,
                status=evaluation.status,
                recoverable=evaluation.status is RunnerStatus.INFRASTRUCTURE_ERROR,
                turn_count=evaluation.turn_count,
                session_resumed=evaluation.session_resumed,
                usage=Usage(
                    input_tokens=evaluation.input_tokens,
                    output_tokens=evaluation.output_tokens,
                    cost_amount=evaluation.cost_amount,
                    cost_currency=evaluation.cost_currency,
                ),
                cost_complete=evaluation.cost_complete,
                latency_ms=evaluation.latency_ms,
                budget=attempt_budget,
                artifacts=evaluation.artifacts,
                evidence=evaluation.evidence,
                tool_timeline=evaluation.tool_timeline,
                state_diff=evaluation.state_diff,
                transcript=evaluation.transcript,
                error=evaluation.error,
                stop_reason=evaluation.stop_reason,
                supersedes_iteration_id=supersedes,
            )
            events.append(_append_record(events_path, record))
            latest[key] = events[-1]
            stop_reason = (
                evaluation.stop_reason
                if evaluation.status is RunnerStatus.BUDGET_STOP
                else _budget_reason(attempt_budget)
            )
            if stop_reason is not None:
                self._record_stop(
                    events_path,
                    events,
                    run_id,
                    config_hash,
                    case_id,
                    iteration_id,
                    attempt_budget,
                    stop_reason,
                )

        latest = _latest_results(events)
        metric_results: list[Mapping[str, object]] = list(latest.values())
        metric_results.extend(
            {
                "case_id": case_id,
                "iteration_id": f"iteration-{index}",
                "status": RunnerStatus.NOT_EVALUATED.value,
            }
            for case_id in case_ids
            for index in range(iterations)
            if (case_id, f"iteration-{index}") not in latest
        )
        metrics = compute_reliability_metrics(metric_results, k=iterations)
        totals = _usage_totals(events)
        final_budget = budgets.to_state(
            consumed_input_tokens=totals[0],
            consumed_output_tokens=totals[1],
            consumed_cost_amount=totals[2],
            consumed_latency_ms=totals[3],
            consumed_turns=totals[4],
            consumed_cases=totals[5],
            stop_reason=stop_reason,
        )
        summary: dict[str, object] = {
            "schema_version": "v1alpha1",
            "record_type": "baseline_summary",
            "run_id": run_id,
            "config_hash": config_hash,
            "metrics": metrics,
            "budget": final_budget.model_dump(mode="json"),
            "stop_reason": stop_reason,
            "result_count": len(latest),
            "attempt_count": len(_attempts(events)),
        }
        validate_public_data(summary)
        summary_path.write_text(
            json.dumps(summary, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return BaselineRun(
            run_id=run_id,
            run_dir=run_dir,
            events_path=events_path,
            summary_path=summary_path,
            latest_results=latest,
            metrics=metrics,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _record_stop(
        path: Path,
        events: list[dict[str, object]],
        run_id: str,
        config_hash: str,
        case_id: str,
        iteration_id: str,
        budget: BudgetState,
        reason: str,
    ) -> None:
        count = sum(
            event.get("event_type") == RunEventType.BUDGET_STOP.value
            for event in events
        )
        record = RunRecord(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="run_record",
            event_type=RunEventType.BUDGET_STOP,
            sequence=len(events),
            run_id=run_id,
            config_hash=config_hash,
            case_id=case_id,
            iteration_id=iteration_id,
            attempt_id=f"budget-stop-{count}",
            status=RunnerStatus.BUDGET_STOP,
            budget=budget.model_copy(update={"stop_reason": reason}),
            stop_reason=reason,
        )
        events.append(_append_record(path, record))
