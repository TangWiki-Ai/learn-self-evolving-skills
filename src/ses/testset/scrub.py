"""ABCD alignment validation, normalization, stable IDs, and exact deduplication."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import cast

from ses.testset.sources import ABCD_COMMIT

_WHITESPACE = re.compile(r"\s+")


class ScrubError(ValueError):
    """A source identity conflict makes deterministic scrubbing impossible."""


@dataclass(frozen=True)
class DialogueTurn:
    speaker: str
    text: str
    turn_count: int | None = None


@dataclass(frozen=True)
class ScrubbedConversation:
    source_id: str
    upstream_id: str
    source_commit: str
    source_split: str
    flow: str
    subflow: str
    original: tuple[DialogueTurn, ...]
    delexed: tuple[DialogueTurn, ...]
    normalized_text: str
    pair_sha256: str
    dedup_sha256: str
    duplicate_source_ids: tuple[str, ...] = ()
    label_conflict: bool = False


@dataclass(frozen=True)
class ScrubFunnel:
    input_records: int
    dropped_empty: int
    dropped_misaligned: int
    dropped_invalid: int
    dropped_encoding: int
    dropped_duplicates: int
    output_records: int


@dataclass(frozen=True)
class ScrubResult:
    records: tuple[ScrubbedConversation, ...]
    funnel: ScrubFunnel


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def _normalized_for_storage(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _normalized_for_dedup(text: str) -> str:
    return _WHITESPACE.sub(" ", _normalized_for_storage(text)).strip()


def _parse_original(value: object) -> tuple[DialogueTurn, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    turns: list[DialogueTurn] = []
    for raw in value:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes, bytearray))
            or len(raw) != 2
        ):
            return None
        speaker, text = raw
        if not isinstance(speaker, str) or not isinstance(text, str):
            return None
        turns.append(DialogueTurn(speaker=speaker, text=text))
    return tuple(turns)


def _parse_delexed(value: object) -> tuple[DialogueTurn, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    turns: list[DialogueTurn] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        speaker = raw.get("speaker")
        text = raw.get("text")
        turn_count = raw.get("turn_count")
        if not isinstance(speaker, str) or not isinstance(text, str):
            return None
        if turn_count is not None and (
            not isinstance(turn_count, int) or isinstance(turn_count, bool)
        ):
            return None
        turns.append(DialogueTurn(speaker=speaker, text=text, turn_count=turn_count))
    return tuple(turns)


def _build_source_id(
    conversation: Mapping[str, object], source_commit: str
) -> tuple[str, str, str] | None:
    upstream_id = conversation.get("convo_id")
    split = conversation.get("source_split")
    if (
        not isinstance(upstream_id, int)
        or isinstance(upstream_id, bool)
        or not isinstance(split, str)
        or not split
    ):
        return None
    return (
        f"abcd:{source_commit}:{split}:{upstream_id}",
        str(upstream_id),
        split,
    )


def _build_record(
    conversation: Mapping[str, object], source_commit: str
) -> tuple[ScrubbedConversation | None, str | None]:
    identity = _build_source_id(conversation, source_commit)
    scenario = conversation.get("scenario")
    if identity is None or not isinstance(scenario, Mapping):
        return None, "invalid"
    flow = scenario.get("flow")
    subflow = scenario.get("subflow")
    if flow != "product_defect" or not isinstance(subflow, str) or not subflow:
        return None, "invalid"

    original = _parse_original(conversation.get("original"))
    delexed = _parse_delexed(conversation.get("delexed"))
    if original is None or delexed is None:
        return None, "invalid"
    if not original or not delexed or not any(turn.text.strip() for turn in original):
        return None, "empty"
    if len(original) != len(delexed) or any(
        left.speaker != right.speaker
        for left, right in zip(original, delexed, strict=True)
    ):
        return None, "misaligned"

    try:
        for turn in (*original, *delexed):
            turn.speaker.encode("utf-8", errors="strict")
            turn.text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None, "encoding"

    source_id, upstream_id, source_split = identity
    customer_turns = [
        _normalized_for_dedup(turn.text)
        for turn in delexed
        if turn.speaker == "customer" and turn.text.strip()
    ]
    if not customer_turns:
        return None, "empty"
    normalized_text = "\n".join(customer_turns)
    pair_payload = {
        "original": [(turn.speaker, turn.text) for turn in original],
        "delexed": [(turn.speaker, turn.text, turn.turn_count) for turn in delexed],
    }
    dedup_payload = [
        (turn.speaker, _normalized_for_dedup(turn.text)) for turn in delexed
    ]
    return (
        ScrubbedConversation(
            source_id=source_id,
            upstream_id=upstream_id,
            source_commit=source_commit,
            source_split=source_split,
            flow=cast(str, flow),
            subflow=subflow,
            original=original,
            delexed=delexed,
            normalized_text=normalized_text,
            pair_sha256=sha256(_canonical_json(pair_payload)).hexdigest(),
            dedup_sha256=sha256(_canonical_json(dedup_payload)).hexdigest(),
        ),
        None,
    )


def scrub_abcd(
    conversations: Iterable[Mapping[str, object]],
    *,
    source_commit: str = ABCD_COMMIT,
) -> ScrubResult:
    """Validate paired turns, retain source intent, and deterministically deduplicate."""

    raw_records = tuple(conversations)
    counters = {
        "empty": 0,
        "misaligned": 0,
        "invalid": 0,
        "encoding": 0,
    }
    provisional: list[ScrubbedConversation] = []
    by_source_id: dict[str, ScrubbedConversation] = {}
    for conversation in raw_records:
        record, reason = _build_record(conversation, source_commit)
        if record is None:
            assert reason is not None
            counters[reason] += 1
            continue
        previous = by_source_id.get(record.source_id)
        if previous is not None:
            if previous != record:
                raise ScrubError(f"source identity conflict: {record.source_id}")
            counters["invalid"] += 1
            continue
        by_source_id[record.source_id] = record
        provisional.append(record)

    label_sets_by_hash: dict[str, set[tuple[str, str]]] = {}
    groups: dict[tuple[str, str, str], list[ScrubbedConversation]] = {}
    for record in provisional:
        label_sets_by_hash.setdefault(record.dedup_sha256, set()).add(
            (record.flow, record.subflow)
        )
        groups.setdefault(
            (record.dedup_sha256, record.flow, record.subflow), []
        ).append(record)

    output: list[ScrubbedConversation] = []
    dropped_duplicates = 0
    for group_key in sorted(groups):
        group = sorted(groups[group_key], key=lambda item: item.source_id)
        representative = group[0]
        duplicates = tuple(item.source_id for item in group[1:])
        dropped_duplicates += len(duplicates)
        output.append(
            replace(
                representative,
                duplicate_source_ids=duplicates,
                label_conflict=len(label_sets_by_hash[representative.dedup_sha256]) > 1,
            )
        )
    output.sort(key=lambda item: item.source_id)

    return ScrubResult(
        records=tuple(output),
        funnel=ScrubFunnel(
            input_records=len(raw_records),
            dropped_empty=counters["empty"],
            dropped_misaligned=counters["misaligned"],
            dropped_invalid=counters["invalid"],
            dropped_encoding=counters["encoding"],
            dropped_duplicates=dropped_duplicates,
            output_records=len(output),
        ),
    )
