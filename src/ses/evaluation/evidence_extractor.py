"""Deterministic, model-free evidence extraction for semantic judges."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, Literal

from pydantic import JsonValue, StrictInt

from ses.contracts import (
    ContractModel,
    JsonPointer,
    Sha256Digest,
    StateDiff,
    Trace,
    content_sha256,
)

from .evidence import escape_json_pointer_token
from .trace import trace_messages, trace_tool_calls

EXTRACTOR_VERSION: Final[Literal["evidence-extractor-v1"]] = "evidence-extractor-v1"
_EXTRACTOR_PROTOCOL = (
    "ses.evaluation.evidence-extractor/v1|"
    "state_diff_facts:bucket,path,before,after|"
    "tool_timeline:sequence,call,arguments,result|"
    "amount_reconciliation:actual_amount_minor_observations|"
    "key_messages:trace_message_order"
)
EXTRACTOR_SHA256: Sha256Digest = hashlib.sha256(
    _EXTRACTOR_PROTOCOL.encode("utf-8")
).hexdigest()


class AmountAgreement(StrEnum):
    """Whether independently observed current amounts agree."""

    AGREES = "agrees"
    DISAGREES = "disagrees"
    INSUFFICIENT = "insufficient"


class StateDiffFact(ContractModel):
    """One stable StateDiff path projected for a model judge."""

    bucket: Literal["added", "removed", "changed"]
    path: JsonPointer
    before: JsonValue = None
    after: JsonValue = None


class ToolTimelineFact(ContractModel):
    """One tool call joined to its result in monotonic trace order."""

    call_sequence: StrictInt
    result_sequence: StrictInt | None = None
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    result_is_error: bool | None = None
    result_content: JsonValue = None


class AmountObservation(ContractModel):
    """One current amount found in state or tool evidence."""

    source: Literal["state_after", "tool_argument", "tool_result"]
    amount_minor: StrictInt
    evidence_pointer: JsonPointer


class AmountReconciliation(ContractModel):
    """Exact integer comparison across independently observed amounts."""

    observations: tuple[AmountObservation, ...]
    distinct_amounts_minor: tuple[StrictInt, ...]
    agreement: AmountAgreement


class KeyMessage(ContractModel):
    """A stable index entry for one assembled assistant message."""

    index: StrictInt
    message_id: str
    text: str
    event_sequences: tuple[StrictInt, ...]
    text_evidence_pointer: JsonPointer


class EvidenceBundle(ContractModel):
    """The complete read-only input available to model-based judges."""

    evidence_version: Literal["evidence-v1"] = "evidence-v1"
    extractor_version: Literal["evidence-extractor-v1"] = EXTRACTOR_VERSION
    extractor_sha256: Sha256Digest = EXTRACTOR_SHA256
    trace_id: str
    diff_id: str
    state_diff_facts: tuple[StateDiffFact, ...]
    tool_timeline: tuple[ToolTimelineFact, ...]
    amount_reconciliation: AmountReconciliation
    key_messages: tuple[KeyMessage, ...]


def _pointer(*tokens: str) -> str:
    return "/" + "/".join(escape_json_pointer_token(token) for token in tokens)


def _state_facts(diff: StateDiff) -> tuple[StateDiffFact, ...]:
    facts: list[StateDiffFact] = []
    for path in sorted(diff.added):
        facts.append(StateDiffFact(bucket="added", path=path, after=diff.added[path]))
    for path in sorted(diff.removed):
        facts.append(
            StateDiffFact(bucket="removed", path=path, before=diff.removed[path])
        )
    for path in sorted(diff.changed):
        change = diff.changed[path]
        facts.append(
            StateDiffFact(
                bucket="changed",
                path=path,
                before=change.before,
                after=change.after,
            )
        )
    return tuple(facts)


def _tool_facts(trace: Trace) -> tuple[ToolTimelineFact, ...]:
    events_by_index = trace.events
    return tuple(
        ToolTimelineFact(
            call_sequence=call.sequence,
            result_sequence=(
                events_by_index[call.result_event_index].sequence
                if call.result_event_index is not None
                else None
            ),
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=call.arguments,
            result_is_error=call.result.is_error if call.result is not None else None,
            result_content=call.result.content if call.result is not None else None,
        )
        for call in trace_tool_calls(trace)
    )


def _amounts_in(
    value: object,
    *,
    source: Literal["state_after", "tool_argument", "tool_result"],
    pointer_tokens: tuple[str, ...],
) -> list[AmountObservation]:
    observations: list[AmountObservation] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = value[key]
            child_tokens = (*pointer_tokens, str(key))
            if (
                key == "amount_minor"
                and isinstance(child, int)
                and not isinstance(child, bool)
            ):
                observations.append(
                    AmountObservation(
                        source=source,
                        amount_minor=child,
                        evidence_pointer=_pointer(*child_tokens),
                    )
                )
            else:
                observations.extend(
                    _amounts_in(
                        child,
                        source=source,
                        pointer_tokens=child_tokens,
                    )
                )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            observations.extend(
                _amounts_in(
                    child,
                    source=source,
                    pointer_tokens=(*pointer_tokens, str(index)),
                )
            )
    return observations


def _amount_reconciliation(
    state_facts: tuple[StateDiffFact, ...],
    tool_facts: tuple[ToolTimelineFact, ...],
) -> AmountReconciliation:
    observations: list[AmountObservation] = []
    for index, fact in enumerate(state_facts):
        observations.extend(
            _amounts_in(
                fact.after,
                source="state_after",
                pointer_tokens=("state_diff_facts", str(index), "after"),
            )
        )
        if (
            fact.path.endswith("/amount_minor")
            and isinstance(fact.after, int)
            and not isinstance(fact.after, bool)
        ):
            observations.append(
                AmountObservation(
                    source="state_after",
                    amount_minor=fact.after,
                    evidence_pointer=_pointer("state_diff_facts", str(index), "after"),
                )
            )
    for index, tool_fact in enumerate(tool_facts):
        observations.extend(
            _amounts_in(
                tool_fact.arguments,
                source="tool_argument",
                pointer_tokens=("tool_timeline", str(index), "arguments"),
            )
        )
        observations.extend(
            _amounts_in(
                tool_fact.result_content,
                source="tool_result",
                pointer_tokens=("tool_timeline", str(index), "result_content"),
            )
        )
    distinct = tuple(sorted({item.amount_minor for item in observations}))
    if len(observations) < 2:
        agreement = AmountAgreement.INSUFFICIENT
    elif len(distinct) == 1:
        agreement = AmountAgreement.AGREES
    else:
        agreement = AmountAgreement.DISAGREES
    return AmountReconciliation(
        observations=tuple(observations),
        distinct_amounts_minor=distinct,
        agreement=agreement,
    )


def _key_messages(trace: Trace) -> tuple[KeyMessage, ...]:
    messages = trace_messages(trace)
    return tuple(
        KeyMessage(
            index=index,
            message_id=message.message_id,
            text=message.text,
            event_sequences=message.sequences,
            text_evidence_pointer=_pointer("key_messages", str(index), "text"),
        )
        for index, message in enumerate(messages)
    )


def extract_evidence(trace: Trace, state_diff: StateDiff) -> EvidenceBundle:
    """Extract a stable, read-only evidence bundle without model inference."""

    state_facts = _state_facts(state_diff)
    tool_facts = _tool_facts(trace)
    return EvidenceBundle(
        trace_id=trace.trace_id,
        diff_id=state_diff.diff_id,
        state_diff_facts=state_facts,
        tool_timeline=tool_facts,
        amount_reconciliation=_amount_reconciliation(state_facts, tool_facts),
        key_messages=_key_messages(trace),
    )


def evidence_sha256(evidence: EvidenceBundle) -> Sha256Digest:
    """Hash the canonical semantic evidence content."""

    return content_sha256(evidence)


def evidence_json_bytes(evidence: EvidenceBundle) -> bytes:
    """Serialize evidence deterministically for a content-addressed artifact."""

    restored = EvidenceBundle.model_validate(evidence)
    return json.dumps(
        restored.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
