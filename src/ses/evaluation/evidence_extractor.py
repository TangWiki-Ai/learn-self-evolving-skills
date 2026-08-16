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

EXTRACTOR_VERSION: Final[Literal["evidence-extractor-v2"]] = "evidence-extractor-v2"
_EXTRACTOR_PROTOCOL = (
    "ses.evaluation.evidence-extractor/v2|"
    "state_diff_facts:bucket,path,before,after|"
    "tool_timeline:sequence,call,arguments,result|"
    "amount_reconciliation:named-components,explicit-relations,adjustments-separated|"
    "key_messages:trace_message_order"
)
EXTRACTOR_SHA256: Sha256Digest = hashlib.sha256(
    _EXTRACTOR_PROTOCOL.encode("utf-8")
).hexdigest()


class AmountAgreement(StrEnum):
    """Whether the amounts in one declared business relation agree."""

    AGREES = "agrees"
    DISAGREES = "disagrees"
    INSUFFICIENT = "insufficient"


class AmountComponentKind(StrEnum):
    """Business meaning of one extracted amount."""

    PRODUCT_PRICE = "product_price"
    ORDER_TOTAL = "order_total"
    CONFIRMED_AMOUNT = "confirmed_amount"
    POLICY_COMPUTED_REFUND = "policy_computed_refund"
    REFUND = "refund"
    STATE_REFUND = "state_refund"
    RESTOCKING_FEE = "restocking_fee"
    RESTOCKING_DISCOUNT = "restocking_discount"
    SHIPPING_CLAWBACK = "shipping_clawback"
    RETURN_SHIPPING_FEE = "return_shipping_fee"
    STATE_RESTOCKING_FEE = "state_restocking_fee"


class AmountPhase(StrEnum):
    """Point in the return flow at which an amount was observed."""

    ORDER = "order"
    PREVIEW = "preview"
    FINAL = "final"
    STATE = "state"


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


class AmountComponent(ContractModel):
    """One named amount without an implied equality to unrelated amounts."""

    kind: AmountComponentKind
    phase: AmountPhase
    amount_minor: StrictInt
    currency: str | None = None
    evidence_pointer: JsonPointer


class AmountRelation(ContractModel):
    """One explicit business equality checked across named components."""

    relation_id: Literal["preview_refund", "confirmed_refund"]
    component_kinds: tuple[AmountComponentKind, ...]
    amounts_minor: tuple[StrictInt, ...]
    evidence_pointers: tuple[JsonPointer, ...]
    agreement: AmountAgreement


class AmountReconciliation(ContractModel):
    """Named amounts plus only the business relations that should hold."""

    components: tuple[AmountComponent, ...]
    relations: tuple[AmountRelation, ...]
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

    evidence_version: Literal["evidence-v2"] = "evidence-v2"
    extractor_version: Literal["evidence-extractor-v2"] = EXTRACTOR_VERSION
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
                bucket="changed", path=path, before=change.before, after=change.after
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


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _money(value: object) -> tuple[int, str | None] | None:
    item = _mapping(value)
    if item is None:
        return None
    amount = item.get("amount_minor")
    currency = item.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return None
    return amount, currency if isinstance(currency, str) else None


def _append_money(
    components: list[AmountComponent],
    *,
    kind: AmountComponentKind,
    phase: AmountPhase,
    value: object,
    pointer: JsonPointer,
) -> None:
    parsed = _money(value)
    if parsed is None:
        return
    amount, currency = parsed
    components.append(
        AmountComponent(
            kind=kind,
            phase=phase,
            amount_minor=amount,
            currency=currency,
            evidence_pointer=pointer,
        )
    )


def _state_amount_components(
    state_facts: tuple[StateDiffFact, ...],
) -> list[AmountComponent]:
    components: list[AmountComponent] = []
    for index, fact in enumerate(state_facts):
        pointer = _pointer("state_diff_facts", str(index), "after")
        if fact.path.endswith(("/refund_amount", "/refund/amount_minor")):
            if isinstance(fact.after, int) and not isinstance(fact.after, bool):
                components.append(
                    AmountComponent(
                        kind=AmountComponentKind.STATE_REFUND,
                        phase=AmountPhase.STATE,
                        amount_minor=fact.after,
                        evidence_pointer=pointer,
                    )
                )
                continue
            _append_money(
                components,
                kind=AmountComponentKind.STATE_REFUND,
                phase=AmountPhase.STATE,
                value=fact.after,
                pointer=pointer,
            )
        elif fact.path.endswith(("/restocking_fee", "/restocking_fee/amount_minor")):
            if isinstance(fact.after, int) and not isinstance(fact.after, bool):
                components.append(
                    AmountComponent(
                        kind=AmountComponentKind.STATE_RESTOCKING_FEE,
                        phase=AmountPhase.STATE,
                        amount_minor=fact.after,
                        evidence_pointer=pointer,
                    )
                )
                continue
            _append_money(
                components,
                kind=AmountComponentKind.STATE_RESTOCKING_FEE,
                phase=AmountPhase.STATE,
                value=fact.after,
                pointer=pointer,
            )
    return components


def _tool_amount_components(
    tool_facts: tuple[ToolTimelineFact, ...],
) -> list[AmountComponent]:
    components: list[AmountComponent] = []
    for index, fact in enumerate(tool_facts):
        result = _mapping(fact.result_content)
        data = _mapping(result.get("data")) if result is not None else None
        base = ("tool_timeline", str(index))
        if fact.tool_name == "get_order" and data is not None:
            order = _mapping(data.get("order"))
            if order is not None:
                _append_money(
                    components,
                    kind=AmountComponentKind.ORDER_TOTAL,
                    phase=AmountPhase.ORDER,
                    value=order.get("total_paid"),
                    pointer=_pointer(
                        *base, "result_content", "data", "order", "total_paid"
                    ),
                )
            items = data.get("items")
            if isinstance(items, (list, tuple)):
                for item_index, item_value in enumerate(items):
                    item = _mapping(item_value)
                    product = (
                        _mapping(item.get("product")) if item is not None else None
                    )
                    if product is not None:
                        _append_money(
                            components,
                            kind=AmountComponentKind.PRODUCT_PRICE,
                            phase=AmountPhase.ORDER,
                            value=product.get("price"),
                            pointer=_pointer(
                                *base,
                                "result_content",
                                "data",
                                "items",
                                str(item_index),
                                "product",
                                "price",
                            ),
                        )
        if fact.tool_name != "process_return" or data is None:
            continue
        phase = (
            AmountPhase.FINAL
            if data.get("status") == "returned"
            else AmountPhase.PREVIEW
        )
        fields = (
            ("policy_computed_amount", AmountComponentKind.POLICY_COMPUTED_REFUND),
            ("refund_amount", AmountComponentKind.REFUND),
            ("restocking_fee", AmountComponentKind.RESTOCKING_FEE),
            ("restocking_discount", AmountComponentKind.RESTOCKING_DISCOUNT),
            ("shipping_clawback", AmountComponentKind.SHIPPING_CLAWBACK),
            ("paid_return_shipping_fee", AmountComponentKind.RETURN_SHIPPING_FEE),
        )
        for field, kind in fields:
            _append_money(
                components,
                kind=kind,
                phase=phase,
                value=data.get(field),
                pointer=_pointer(*base, "result_content", "data", field),
            )
        confirmed = fact.arguments.get("amount_minor")
        if (
            phase is AmountPhase.FINAL
            and fact.arguments.get("confirm") is True
            and isinstance(confirmed, int)
            and not isinstance(confirmed, bool)
        ):
            refund = _money(data.get("refund_amount"))
            components.append(
                AmountComponent(
                    kind=AmountComponentKind.CONFIRMED_AMOUNT,
                    phase=AmountPhase.FINAL,
                    amount_minor=confirmed,
                    currency=refund[1] if refund is not None else None,
                    evidence_pointer=_pointer(*base, "arguments", "amount_minor"),
                )
            )
    return components


def _relation(
    components: tuple[AmountComponent, ...],
    *,
    relation_id: Literal["preview_refund", "confirmed_refund"],
    selectors: tuple[tuple[AmountComponentKind, AmountPhase], ...],
) -> AmountRelation:
    selected: list[AmountComponent] = []
    for kind, phase in selectors:
        match = next(
            (
                component
                for component in components
                if component.kind is kind and component.phase is phase
            ),
            None,
        )
        if match is not None:
            selected.append(match)
    if len(selected) != len(selectors):
        agreement = AmountAgreement.INSUFFICIENT
    elif len({item.amount_minor for item in selected}) == 1:
        agreement = AmountAgreement.AGREES
    else:
        agreement = AmountAgreement.DISAGREES
    return AmountRelation(
        relation_id=relation_id,
        component_kinds=tuple(item.kind for item in selected),
        amounts_minor=tuple(item.amount_minor for item in selected),
        evidence_pointers=tuple(item.evidence_pointer for item in selected),
        agreement=agreement,
    )


def _amount_reconciliation(
    state_facts: tuple[StateDiffFact, ...],
    tool_facts: tuple[ToolTimelineFact, ...],
) -> AmountReconciliation:
    components = tuple(
        sorted(
            (
                *_state_amount_components(state_facts),
                *_tool_amount_components(tool_facts),
            ),
            key=lambda item: item.evidence_pointer,
        )
    )
    relations = (
        _relation(
            components,
            relation_id="preview_refund",
            selectors=(
                (AmountComponentKind.POLICY_COMPUTED_REFUND, AmountPhase.PREVIEW),
                (AmountComponentKind.REFUND, AmountPhase.PREVIEW),
            ),
        ),
        _relation(
            components,
            relation_id="confirmed_refund",
            selectors=(
                (AmountComponentKind.CONFIRMED_AMOUNT, AmountPhase.FINAL),
                (AmountComponentKind.POLICY_COMPUTED_REFUND, AmountPhase.FINAL),
                (AmountComponentKind.REFUND, AmountPhase.FINAL),
                (AmountComponentKind.STATE_REFUND, AmountPhase.STATE),
            ),
        ),
    )
    statuses = {item.agreement for item in relations}
    if AmountAgreement.DISAGREES in statuses:
        agreement = AmountAgreement.DISAGREES
    elif relations[1].agreement is AmountAgreement.AGREES:
        agreement = AmountAgreement.AGREES
    else:
        agreement = AmountAgreement.INSUFFICIENT
    return AmountReconciliation(
        components=components, relations=relations, agreement=agreement
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
