"""Read-only L1 report data built from append-only runner records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from ses.contracts import ArtifactRef
from ses.contracts.security import validate_public_data
from ses.runner import compute_reliability_metrics, load_run_events


def _verify_artifacts(events_path: Path, attempts: list[dict[str, object]]) -> None:
    run_dir = events_path.parent
    for attempt in attempts:
        value = attempt.get("artifacts")
        if not isinstance(value, Mapping):
            continue
        for candidate in value.values():
            references = candidate if isinstance(candidate, list) else [candidate]
            for item in references:
                if item is None:
                    continue
                reference = ArtifactRef.model_validate(item)
                try:
                    payload = (run_dir / reference.path).read_bytes()
                except OSError as exc:
                    raise ValueError(
                        f"L1 artifact is unavailable: {reference.path}"
                    ) from exc
                reference.verify_bytes(payload)


def build_baseline_report(events_path: Path) -> dict[str, object]:
    """Aggregate recorded outcomes without importing or invoking any Judge."""
    events = load_run_events(events_path)
    started = next(
        (event for event in events if event.get("event_type") == "run_started"), None
    )
    if started is None:
        raise ValueError("baseline event log has no run_started record")
    run_id = started.get("run_id")
    config = started.get("config")
    if not isinstance(run_id, str) or not isinstance(config, Mapping):
        raise ValueError("run_started record is incomplete")
    iterations = config.get("iterations")
    if (
        not isinstance(iterations, int)
        or isinstance(iterations, bool)
        or iterations < 1
    ):
        raise ValueError("run iteration count is invalid")

    attempts: list[dict[str, object]] = []
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for event in events:
        if event.get("event_type") != "attempt":
            continue
        attempts.append(event)
        case_id = event.get("case_id")
        iteration_id = event.get("iteration_id")
        if isinstance(case_id, str) and isinstance(iteration_id, str):
            latest[(case_id, iteration_id)] = event
    results = sorted(
        latest.values(),
        key=lambda value: (str(value.get("case_id")), str(value.get("iteration_id"))),
    )
    _verify_artifacts(events_path, attempts)
    metrics = compute_reliability_metrics(results, k=iterations)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    total_input = 0
    total_output = 0
    total_cost = Decimal(0)
    total_latency = 0
    currencies: set[str] = set()
    for result in attempts:
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            total_input += int(cast(Any, usage.get("input_tokens", 0)))
            total_output += int(cast(Any, usage.get("output_tokens", 0)))
            total_cost += Decimal(str(usage.get("cost_amount", "0")))
            currency = usage.get("cost_currency")
            if isinstance(currency, str):
                currencies.add(currency)
        total_latency += int(cast(Any, result.get("latency_ms", 0)))
    for result in results:
        case_id = cast(str, result["case_id"])
        grouped[case_id].append(result)
    if len(currencies) > 1:
        raise ValueError("L1 report cannot aggregate mixed cost currencies")

    cases: list[dict[str, object]] = []
    for case_id, repetitions in sorted(grouped.items()):
        cases.append(
            {
                "case_id": case_id,
                "first_status": repetitions[0].get("status"),
                "repetitions": repetitions,
            }
        )
    report: dict[str, object] = {
        "schema_version": "v1alpha1",
        "record_type": "l1_baseline_report",
        "formula_version": "l1-baseline-v2",
        "run_id": run_id,
        "config_hash": started.get("config_hash"),
        "metrics": metrics,
        "totals": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost_amount": str(total_cost),
            "cost_currency": next(iter(currencies)) if currencies else None,
            "latency_ms": total_latency,
        },
        "cases": cases,
    }
    validate_public_data(report)
    return report
