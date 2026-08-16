"""Lesson 4 solution: summarize repeated baseline records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

EVALUATED = frozenset({"pass", "agent_fail", "judge_error", "infrastructure_error"})


def _iteration(record: Mapping[str, object]) -> int:
    value = record.get("iteration", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("iteration must be an integer")
    return value


def baseline_reliability(
    records: Sequence[Mapping[str, object]], k: int
) -> tuple[int, int, float, float]:
    """Return first-pass count, case count, pass@1, and all-pass reliability."""
    if k < 1:
        raise ValueError("k must be at least one")
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        case_id = record.get("case_id")
        status = record.get("status")
        if isinstance(case_id, str) and status in EVALUATED:
            grouped[case_id].append(record)
    if not grouped:
        raise ValueError("baseline requires at least one evaluated case")
    for results in grouped.values():
        results.sort(key=_iteration)
    total = len(grouped)
    first_passes = sum(
        results[0].get("status") == "pass" for results in grouped.values()
    )
    reliable = sum(
        len(results) >= k
        and all(result.get("status") == "pass" for result in results[:k])
        for results in grouped.values()
    )
    return first_passes, total, first_passes / total, reliable / total
