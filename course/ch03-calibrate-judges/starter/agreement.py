"""Lesson 3 starter: calibrate one judge against human-reviewed labels."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def summarize_agreement(
    cases: Sequence[Mapping[str, object]],
    prediction_field: str,
) -> Mapping[str, object]:
    """Return agreement, a confusion matrix, and disagreement case IDs."""

    raise NotImplementedError("Lesson 3: implement judge agreement calibration")
