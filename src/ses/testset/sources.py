"""Pinned source loading and exact source-specific slicing rules."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

STATE_BENCH_COMMIT = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
ABCD_COMMIT = "6b8700ce67c6b37b062dd7a60abc76d7ef832a97"
TAU2_COMMIT = "c3398666e6559e3a063da3fc04b5acf7f941464e"


class SourceShapeError(ValueError):
    """A pinned source no longer has the expected JSON shape."""


def load_json_document(path: Path) -> object:
    """Read strict UTF-8 JSON, optionally from a gzip container."""

    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="strict") as stream:
                return cast(object, json.load(stream))
        return cast(object, json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceShapeError(f"cannot read valid JSON from {path}") from exc


def filter_state_return_items(
    tasks: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """Apply the pinned STATE-Bench JSON predicate without fuzzy matching."""

    return tuple(task for task in tasks if task.get("task_type") == "return_item")


def filter_abcd_product_defect(
    conversations: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """Apply the pinned ABCD nested JSON predicate without normalization."""

    selected: list[Mapping[str, object]] = []
    for conversation in conversations:
        scenario = conversation.get("scenario")
        if isinstance(scenario, Mapping) and scenario.get("flow") == "product_defect":
            selected.append(conversation)
    return tuple(selected)


def flatten_abcd_document(
    document: object,
    *,
    profile: Literal["fixture", "full"],
) -> tuple[Mapping[str, object], ...]:
    """Flatten the explicitly selected ABCD profile and retain its partition."""

    if profile == "fixture":
        if not isinstance(document, list):
            raise SourceShapeError("ABCD fixture must be a top-level list")
        records: list[Mapping[str, object]] = []
        for raw in document:
            if not isinstance(raw, Mapping):
                raise SourceShapeError("ABCD fixture contains a non-object record")
            copied = dict(cast(Mapping[str, object], raw))
            source_split = copied.get("source_split")
            if source_split not in {"train", "dev", "test"}:
                raise SourceShapeError(
                    "ABCD fixture must preserve its train/dev/test source_split"
                )
            records.append(copied)
        return tuple(records)

    if not isinstance(document, Mapping):
        raise SourceShapeError("ABCD full data must contain train/dev/test partitions")
    expected = ("train", "dev", "test")
    if set(document) != set(expected):
        raise SourceShapeError("ABCD full data partitions drifted from train/dev/test")
    records = []
    for partition in expected:
        raw_partition = document.get(partition)
        if not isinstance(raw_partition, Sequence) or isinstance(
            raw_partition, (str, bytes, bytearray)
        ):
            raise SourceShapeError(f"ABCD partition {partition} is not a list")
        for raw in raw_partition:
            if not isinstance(raw, Mapping):
                raise SourceShapeError(
                    f"ABCD partition {partition} contains a non-object record"
                )
            copied = dict(cast(Mapping[str, object], raw))
            copied["source_split"] = partition
            records.append(copied)
    return tuple(records)


def match_state_trajectories(
    selected_tasks: Iterable[Mapping[str, object]],
    trajectory_files: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[Mapping[str, object], Mapping[str, object]], ...]:
    """Join STATE trajectories by task filename stem after task filtering."""

    pairs: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for task in selected_tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise SourceShapeError("STATE task has no string task_id")
        trajectory = trajectory_files.get(task_id)
        if trajectory is not None:
            pairs.append((task, trajectory))
    return tuple(pairs)
