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
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from ses.contracts.security import validate_public_data

_SAFE_RUN_ID = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVALUATED_STATUSES = frozenset(
    {"pass", "agent_fail", "judge_error", "infrastructure_error"}
)


class IterationStatus(StrEnum):
    """Mutually exclusive baseline iteration outcomes."""

    PASS = "pass"
    AGENT_FAIL = "agent_fail"
    JUDGE_ERROR = "judge_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BUDGET_STOP = "budget_stop"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Independent hard limits applied in documented precedence order."""

    max_cases: int
    max_turns_per_case: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost: Decimal | None = None

    def __post_init__(self) -> None:
        values = (self.max_cases, self.max_turns_per_case)
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("case and turn budgets must be nonnegative integers")
        if self.max_turns_per_case < 1:
            raise ValueError("max_turns_per_case must be at least one")
        for value in (self.max_input_tokens, self.max_output_tokens):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("token budgets must be nonnegative integers")
        if self.max_cost is not None and self.max_cost < 0:
            raise ValueError("cost budget must be nonnegative")

    def to_json(self) -> dict[str, object]:
        return {
            "max_cases": self.max_cases,
            "max_turns_per_case": self.max_turns_per_case,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost": None if self.max_cost is None else str(self.max_cost),
        }


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    """In-memory evaluator output projected into an iteration result event."""

    case_id: str
    iteration_id: str
    status: IterationStatus
    turn_count: int
    input_tokens: int
    output_tokens: int
    cost_amount: Decimal = Decimal(0)
    cost_currency: str = "CNY"
    latency_ms: int = 0
    evidence: tuple[Mapping[str, object], ...] = ()
    tool_timeline: tuple[Mapping[str, object], ...] = ()
    state_diff: Mapping[str, object] = field(default_factory=dict)
    transcript: tuple[Mapping[str, object], ...] = ()
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
        if self.cost_amount < 0:
            raise ValueError("cost must be nonnegative")


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


Evaluator = Callable[[str, str, int], CaseEvaluation]


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported baseline event value: {type(value).__name__}")


def _config_payload(
    case_ids: Sequence[str], iterations: int, budgets: BudgetLimits
) -> dict[str, object]:
    return {
        "case_ids": list(case_ids),
        "iterations": iterations,
        "budgets": budgets.to_json(),
    }


def _config_hash(config: Mapping[str, object]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _append_event(path: Path, event: Mapping[str, object]) -> None:
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


def load_run_events(path: Path) -> list[dict[str, object]]:
    """Load only complete append-only event lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError("baseline event log does not exist") from exc
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid event JSON at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"event at line {line_number} must be an object")
        validate_public_data(value)
        if value.get("sequence") != len(events):
            raise ValueError("event sequences must be contiguous and append-only")
        events.append(cast(dict[str, object], value))
    return events


def _latest_results(
    events: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], Mapping[str, object]]:
    latest: dict[tuple[str, str], Mapping[str, object]] = {}
    for event in events:
        if event.get("event_type") != "iteration_result":
            continue
        case_id = event.get("case_id")
        iteration_id = event.get("iteration_id")
        if isinstance(case_id, str) and isinstance(iteration_id, str):
            latest[(case_id, iteration_id)] = event
    return latest


def compute_reliability_metrics(
    results: Sequence[Mapping[str, object]], *, k: int
) -> dict[str, int | float]:
    """Compute first-attempt pass rate and all-pass reliability over k repeats."""
    if k < 1:
        raise ValueError("k must be at least one")
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for result in results:
        case_id = result.get("case_id")
        status = result.get("status")
        if isinstance(case_id, str) and status in _EVALUATED_STATUSES:
            grouped[case_id].append(result)
    for values in grouped.values():
        values.sort(key=lambda value: str(value.get("iteration_id")))
    first = [values[0] for values in grouped.values() if values]
    reliable = [values[:k] for values in grouped.values() if len(values) >= k]
    return {
        "sample_size": len(first),
        "iteration_sample_size": sum(len(values) for values in grouped.values()),
        "pass_at_1": (
            sum(value.get("status") == "pass" for value in first) / len(first)
            if first
            else 0.0
        ),
        "pass_power_k": (
            sum(
                all(value.get("status") == "pass" for value in values)
                for values in reliable
            )
            / len(reliable)
            if reliable
            else 0.0
        ),
        "k": k,
    }


def _evaluation_event(
    evaluation: CaseEvaluation,
    *,
    supersedes_iteration_id: str | None = None,
) -> dict[str, object]:
    return cast(
        dict[str, object],
        _jsonable(
            {
                "schema_version": "v1alpha1",
                "record_type": "baseline_run_event",
                "event_type": "iteration_result",
                "case_id": evaluation.case_id,
                "iteration_id": evaluation.iteration_id,
                "status": evaluation.status.value,
                "recoverable": evaluation.status
                is IterationStatus.INFRASTRUCTURE_ERROR,
                "turn_count": evaluation.turn_count,
                "usage": {
                    "input_tokens": evaluation.input_tokens,
                    "output_tokens": evaluation.output_tokens,
                    "cost_amount": evaluation.cost_amount,
                    "cost_currency": evaluation.cost_currency,
                },
                "latency_ms": evaluation.latency_ms,
                "evidence": evaluation.evidence,
                "tool_timeline": evaluation.tool_timeline,
                "state_diff": evaluation.state_diff,
                "transcript": evaluation.transcript,
                "error": evaluation.error,
                "stop_reason": evaluation.stop_reason,
                "supersedes_iteration_id": supersedes_iteration_id,
            }
        ),
    )


class BaselineRunner:
    """Run planned case iterations with append-only persistence and safe resume."""

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
        config = _config_payload(case_ids, iterations, budgets)
        config_hash = _config_hash(config)
        if resume:
            events = load_run_events(events_path)
            started = next(
                (event for event in events if event.get("event_type") == "run_started"),
                None,
            )
            if started is None or started.get("config_hash") != config_hash:
                raise ValueError("resume configuration does not match the original run")
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            events = []
            start_event: dict[str, object] = {
                "schema_version": "v1alpha1",
                "record_type": "baseline_run_event",
                "event_type": "run_started",
                "sequence": 0,
                "run_id": run_id,
                "config_hash": config_hash,
                "config": config,
            }
            _append_event(events_path, start_event)
            events.append(start_event)

        latest = _latest_results(events)
        planned: list[tuple[str, str, str | None]] = [
            (case_id, f"iteration-{index}", None)
            for case_id in case_ids
            for index in range(iterations)
        ]
        for case_id in rerun_case_ids:
            existing_indexes = [
                int(iteration_id.removeprefix("iteration-"))
                for existing_case, iteration_id in latest
                if existing_case == case_id and iteration_id.startswith("iteration-")
            ]
            next_index = max(existing_indexes, default=-1) + 1
            previous = (
                f"iteration-{max(existing_indexes)}" if existing_indexes else None
            )
            planned.append((case_id, f"iteration-{next_index}", previous))

        already_slotted = {
            key
            for key, value in latest.items()
            if value.get("status") != IterationStatus.INFRASTRUCTURE_ERROR.value
            or not value.get("recoverable")
        }
        started_slots = len(
            {
                key
                for key, value in latest.items()
                if value.get("status") != IterationStatus.NOT_EVALUATED.value
            }
        )
        total_input = sum(
            int(cast(Mapping[str, Any], value.get("usage", {})).get("input_tokens", 0))
            for value in latest.values()
            if value.get("status") in _EVALUATED_STATUSES
        )
        total_output = sum(
            int(cast(Mapping[str, Any], value.get("usage", {})).get("output_tokens", 0))
            for value in latest.values()
            if value.get("status") in _EVALUATED_STATUSES
        )
        total_cost = sum(
            (
                Decimal(
                    str(
                        cast(Mapping[str, Any], value.get("usage", {})).get(
                            "cost_amount", "0"
                        )
                    )
                )
                for value in latest.values()
                if value.get("status") in _EVALUATED_STATUSES
            ),
            Decimal(0),
        )
        recorded_stop = next(
            (
                value.get("stop_reason")
                for value in reversed(tuple(latest.values()))
                if value.get("status") == IterationStatus.BUDGET_STOP.value
            ),
            None,
        )
        stop_reason = recorded_stop if isinstance(recorded_stop, str) else None
        for case_id, iteration_id, supersedes in planned:
            key = (case_id, iteration_id)
            if key in already_slotted and supersedes is None:
                continue
            previous_result = latest.get(key)
            is_recoverable_retry = bool(
                previous_result
                and previous_result.get("status")
                == IterationStatus.INFRASTRUCTURE_ERROR.value
                and previous_result.get("recoverable")
            )
            if stop_reason is not None:
                event = self._empty_result(
                    case_id, iteration_id, IterationStatus.NOT_EVALUATED, stop_reason
                )
                self._record(events_path, events, event)
                continue
            if not is_recoverable_retry and started_slots >= budgets.max_cases:
                stop_reason = "case_limit"
                event = self._empty_result(
                    case_id, iteration_id, IterationStatus.BUDGET_STOP, stop_reason
                )
                self._record(events_path, events, event)
                continue
            try:
                evaluation = self._evaluator(
                    case_id, iteration_id, budgets.max_turns_per_case
                )
            except Exception as exc:
                evaluation = CaseEvaluation(
                    case_id=case_id,
                    iteration_id=iteration_id,
                    status=IterationStatus.INFRASTRUCTURE_ERROR,
                    turn_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    error=str(exc) or type(exc).__name__,
                )
            if not is_recoverable_retry:
                started_slots += 1
            event = _evaluation_event(evaluation, supersedes_iteration_id=supersedes)
            total_input += evaluation.input_tokens
            total_output += evaluation.output_tokens
            total_cost += evaluation.cost_amount
            budget_reason = (
                evaluation.stop_reason
                if evaluation.status is IterationStatus.BUDGET_STOP
                else None
            )
            if (
                budget_reason is None
                and budgets.max_input_tokens is not None
                and total_input > budgets.max_input_tokens
            ):
                budget_reason = "input_token_limit"
            if (
                budget_reason is None
                and budgets.max_output_tokens is not None
                and total_output > budgets.max_output_tokens
            ):
                budget_reason = "output_token_limit"
            if (
                budget_reason is None
                and budgets.max_cost is not None
                and total_cost > budgets.max_cost
            ):
                budget_reason = "cost_limit"
            if budget_reason is not None:
                stop_reason = budget_reason
                event = {
                    **event,
                    "status": IterationStatus.BUDGET_STOP.value,
                    "recoverable": False,
                    "stop_reason": budget_reason,
                    "partial_result": {
                        "status": evaluation.status.value,
                        "turn_count": evaluation.turn_count,
                        "usage": event["usage"],
                    },
                }
            self._record(events_path, events, event)

        latest = _latest_results(events)
        metrics = compute_reliability_metrics(list(latest.values()), k=iterations)
        summary: dict[str, object] = {
            "schema_version": "v1alpha1",
            "record_type": "baseline_summary",
            "run_id": run_id,
            "config_hash": config_hash,
            "metrics": metrics,
            "stop_reason": stop_reason,
            "result_count": len(latest),
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
    def _record(
        path: Path, events: list[dict[str, object]], event: dict[str, object]
    ) -> None:
        event["sequence"] = len(events)
        _append_event(path, event)
        events.append(event)

    @staticmethod
    def _empty_result(
        case_id: str,
        iteration_id: str,
        status: IterationStatus,
        reason: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "v1alpha1",
            "record_type": "baseline_run_event",
            "event_type": "iteration_result",
            "case_id": case_id,
            "iteration_id": iteration_id,
            "status": status.value,
            "recoverable": False,
            "turn_count": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_amount": "0",
                "cost_currency": "CNY",
            },
            "latency_ms": 0,
            "evidence": [],
            "tool_timeline": [],
            "state_diff": {},
            "transcript": [],
            "error": None,
            "stop_reason": reason,
            "supersedes_iteration_id": None,
        }
