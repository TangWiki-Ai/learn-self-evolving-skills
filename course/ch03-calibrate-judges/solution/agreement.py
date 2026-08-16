"""Lesson 3 solution: calibrate one judge against reviewed labels."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

STATUSES = ("pass", "fail", "not_evaluated", "error")


def summarize_agreement(
    cases: Sequence[Mapping[str, object]],
    prediction_field: str,
) -> Mapping[str, object]:
    """Return measured agreement, full confusion matrix, and disagreements."""

    if not cases:
        raise ValueError("agreement experiment requires at least one case")
    matrix = {human: {prediction: 0 for prediction in STATUSES} for human in STATUSES}
    agreements = 0
    disagreements: list[str] = []
    for case in cases:
        case_id = case.get("case_id")
        human = case.get("human_status")
        if prediction_field not in case:
            raise ValueError(f"missing prediction field: {prediction_field}")
        prediction = case[prediction_field]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id must be a non-empty string")
        if human not in STATUSES or prediction not in STATUSES:
            raise ValueError("statuses must use canonical grading values")
        human_status = str(human)
        prediction_status = str(prediction)
        matrix[human_status][prediction_status] += 1
        if human_status == prediction_status:
            agreements += 1
        else:
            disagreements.append(case_id)
    total = len(cases)
    return {
        "agreements": agreements,
        "total": total,
        "agreement": agreements / total,
        "confusion_matrix": matrix,
        "disagreements": disagreements,
    }
