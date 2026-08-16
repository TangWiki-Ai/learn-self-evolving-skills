"""Lesson 4 starter: summarize repeated baseline records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def baseline_reliability(
    records: Sequence[Mapping[str, object]], k: int
) -> tuple[int, int, float, float]:
    """Return first-pass count, case count, pass@1, and all-pass reliability."""
    del records, k
    raise NotImplementedError("Lesson 4: calculate pass@1 and pass^k")
