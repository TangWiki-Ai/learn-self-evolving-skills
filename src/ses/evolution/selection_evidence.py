"""Canonical private projection and parser for opaque selection event evidence."""

from __future__ import annotations

import json
from typing import Literal

from ses.contracts import SelectionPairCase, SelectionPairEvaluation

_SelectionSide = Literal["accepted", "candidate"]


def _selection_event_payload(
    pair: SelectionPairEvaluation,
    row: SelectionPairCase,
    *,
    side: _SelectionSide,
    sequence: int,
) -> dict[str, object]:
    """Project one side of a private pair into its canonical JSONL payload."""

    accepted = side == "accepted"
    return {
        "cost_amount": str(
            row.accepted_cost_amount if accepted else row.candidate_cost_amount
        ),
        "cost_currency": pair.cost_currency,
        "evaluation_nonce": pair.evaluation_nonce,
        "input_tokens": (
            row.accepted_input_tokens if accepted else row.candidate_input_tokens
        ),
        "iteration_id": pair.iteration_id,
        "measurement_kind": pair.measurement_kind.value,
        "output_tokens": (
            row.accepted_output_tokens if accepted else row.candidate_output_tokens
        ),
        "run_id": pair.accepted_run_id if accepted else pair.candidate_run_id,
        "score": row.accepted_score if accepted else row.candidate_score,
        "sequence": sequence,
        "skill_sha256": (
            pair.accepted_skill_sha256 if accepted else pair.candidate_skill_sha256
        ),
        "slot": row.slot,
        "status": (
            row.accepted_status.value if accepted else row.candidate_status.value
        ),
    }


def _selection_event_bytes(
    pair: SelectionPairEvaluation,
    *,
    side: _SelectionSide,
) -> bytes:
    """Serialize one side of a pair as canonical private JSONL evidence."""

    return b"".join(
        json.dumps(
            _selection_event_payload(pair, row, side=side, sequence=sequence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
        for sequence, row in enumerate(pair.cases)
    )


def _validate_selection_event_bytes(
    content: bytes,
    pair: SelectionPairEvaluation,
    *,
    side: _SelectionSide,
) -> None:
    """Reject JSONL that is not the exact canonical projection of a pair."""

    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("selection event evidence cannot be read") from exc
    lines = text.splitlines()
    if len(lines) != len(pair.cases):
        raise ValueError("selection event evidence is incomplete")
    for sequence, (line, row) in enumerate(zip(lines, pair.cases, strict=True)):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("selection event evidence is invalid JSON") from exc
        if payload != _selection_event_payload(
            pair,
            row,
            side=side,
            sequence=sequence,
        ):
            raise ValueError(
                "selection event evidence disagrees with its paired summary"
            )


__all__: tuple[str, ...] = ()
