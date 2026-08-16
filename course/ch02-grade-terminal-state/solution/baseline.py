"""Lesson 2 solution: calculate a state-only baseline pass rate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def state_pass_rate(records: Sequence[Mapping[str, object]]) -> tuple[int, int, float]:
    """Return passed count, total count, and pass rate."""
    total = len(records)
    if total == 0:
        raise ValueError("baseline requires at least one result")
    passed = sum(record.get("state_grade") == "pass" for record in records)
    return passed, total, passed / total
