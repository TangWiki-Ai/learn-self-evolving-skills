"""Deterministic transcript and tool-call rules."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias, cast

from pydantic import JsonValue

from ses.contracts import (
    ArtifactRef,
    AssertionResult,
    EvidenceRef,
    GradeStatus,
    JudgeKind,
    RecordType,
    SchemaVersion,
    Trace,
)

from ..evidence import timeline_evidence, trace_event_evidence
from ..trace import TraceToolCall, trace_tool_calls


class RuleKind(StrEnum):
    """Deterministic rule forms supported by Issue #2."""

    TOOL_CALLED = "tool_called"
    TOOL_COUNT = "tool_count"
    TOOL_ARGUMENTS = "tool_arguments"
    TOOL_ORDER = "tool_order"
    FORBIDDEN_CALL = "forbidden_call"


@dataclass(frozen=True, slots=True)
class Rule:
    """An internal immutable rule specification, not a cross-module schema."""

    kind: RuleKind | str
    assertion_id: str
    tool_name: str | None = None
    expected_count: int | None = None
    min_count: int | None = None
    max_count: int | None = None
    expected_arguments: Mapping[str, JsonValue] | None = None
    exact_arguments: bool = False
    order: tuple[str, ...] = ()
    exact_order: bool = False
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RuleKind(self.kind))
        if not isinstance(self.assertion_id, str) or not self.assertion_id.strip():
            raise ValueError("assertion_id must be a non-empty string")
        if not isinstance(self.required, bool):
            raise ValueError("required must be boolean")
        if self.tool_name is not None and (
            not isinstance(self.tool_name, str) or not self.tool_name.strip()
        ):
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.order, tuple) or not all(
            isinstance(name, str) and bool(name.strip()) for name in self.order
        ):
            raise ValueError("order items must be non-empty strings")
        if self.expected_arguments is not None:
            if not isinstance(self.expected_arguments, Mapping):
                raise ValueError("expected_arguments must be a mapping")
            object.__setattr__(
                self,
                "expected_arguments",
                MappingProxyType(dict(self.expected_arguments)),
            )
        for name in ("expected_count", "min_count", "max_count"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.expected_count is not None and (
            self.min_count is not None or self.max_count is not None
        ):
            raise ValueError("expected_count conflicts with min_count/max_count")
        if (
            self.min_count is not None
            and self.max_count is not None
            and self.min_count > self.max_count
        ):
            raise ValueError("min_count must not exceed max_count")
        kind = cast(RuleKind, self.kind)
        if kind is RuleKind.TOOL_ORDER:
            if not self.order:
                raise ValueError("tool_order needs at least one tool")
            if (
                any(
                    value is not None
                    for value in (
                        self.tool_name,
                        self.expected_count,
                        self.min_count,
                        self.max_count,
                        self.expected_arguments,
                    )
                )
                or self.exact_arguments
            ):
                raise ValueError("tool_order contains conflicting fields")
            return
        if self.tool_name is None:
            raise ValueError(f"{kind.value} requires tool_name")
        if self.order or self.exact_order:
            raise ValueError(f"{kind.value} contains conflicting order fields")
        if kind is RuleKind.TOOL_COUNT:
            if self.expected_arguments is not None or self.exact_arguments:
                raise ValueError("tool_count contains conflicting argument fields")
            if all(
                value is None
                for value in (self.expected_count, self.min_count, self.max_count)
            ):
                raise ValueError("tool_count needs a count limit")
            return
        if any(
            value is not None
            for value in (self.expected_count, self.min_count, self.max_count)
        ):
            raise ValueError(f"{kind.value} contains conflicting count fields")
        if kind is RuleKind.TOOL_ARGUMENTS:
            if self.expected_arguments is None:
                raise ValueError("tool_arguments requires expected_arguments")
            return
        if self.expected_arguments is not None or self.exact_arguments:
            raise ValueError(f"{kind.value} contains conflicting argument fields")


RuleInput: TypeAlias = Rule | Mapping[str, object]


def tool_called(
    tool_name: str,
    *,
    assertion_id: str | None = None,
    required: bool = True,
) -> Rule:
    return Rule(
        RuleKind.TOOL_CALLED,
        assertion_id or f"tool-called:{tool_name}",
        tool_name=tool_name,
        required=required,
    )


def tool_count(
    tool_name: str,
    count: int | None = None,
    *,
    min_count: int | None = None,
    max_count: int | None = None,
    assertion_id: str | None = None,
    required: bool = True,
) -> Rule:
    if count is None and min_count is None and max_count is None:
        raise ValueError("tool_count needs count, min_count, or max_count")
    return Rule(
        RuleKind.TOOL_COUNT,
        assertion_id or f"tool-count:{tool_name}",
        tool_name=tool_name,
        expected_count=count,
        min_count=min_count,
        max_count=max_count,
        required=required,
    )


def tool_arguments(
    tool_name: str,
    arguments: Mapping[str, JsonValue],
    *,
    exact: bool = False,
    assertion_id: str | None = None,
    required: bool = True,
) -> Rule:
    return Rule(
        RuleKind.TOOL_ARGUMENTS,
        assertion_id or f"tool-arguments:{tool_name}",
        tool_name=tool_name,
        expected_arguments=arguments,
        exact_arguments=exact,
        required=required,
    )


def tool_order(
    tools: Sequence[str],
    *,
    exact: bool = False,
    assertion_id: str | None = None,
    required: bool = True,
) -> Rule:
    names = tuple(tools)
    if not names:
        raise ValueError("tool_order needs at least one tool")
    return Rule(
        RuleKind.TOOL_ORDER,
        assertion_id or "tool-order:" + "->".join(names),
        order=names,
        exact_order=exact,
        required=required,
    )


def forbidden_call(
    tool_name: str,
    *,
    assertion_id: str | None = None,
    required: bool = True,
) -> Rule:
    return Rule(
        RuleKind.FORBIDDEN_CALL,
        assertion_id or f"forbidden-tool:{tool_name}",
        tool_name=tool_name,
        required=required,
    )


def _as_tool_name(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _one_alias(value: Mapping[str, object], names: tuple[str, ...]) -> object | None:
    supplied = [(name, value[name]) for name in names if name in value]
    if len(supplied) > 1:
        raise ValueError(
            f"conflicting rule fields: {', '.join(n for n, _ in supplied)}"
        )
    return supplied[0][1] if supplied else None


def _coerce_rule(value: RuleInput | Sequence[str]) -> Rule:
    if isinstance(value, Rule):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("tool_order items must be strings")
        return tool_order(value)
    if not isinstance(value, Mapping):
        raise ValueError("rule must be a Rule or mapping")
    raw_kind = _one_alias(value, ("kind", "type"))
    if not isinstance(raw_kind, str):
        raise ValueError("rule is missing kind")
    aliases = {
        "called": RuleKind.TOOL_CALLED.value,
        "count": RuleKind.TOOL_COUNT.value,
        "parameters": RuleKind.TOOL_ARGUMENTS.value,
        "order": RuleKind.TOOL_ORDER.value,
        "forbidden": RuleKind.FORBIDDEN_CALL.value,
    }
    try:
        kind = RuleKind(aliases.get(raw_kind, raw_kind))
    except ValueError as exc:
        raise ValueError(f"unsupported rule kind: {raw_kind!r}") from exc
    assertion_id = _one_alias(value, ("assertion_id", "id"))
    if assertion_id is not None and (
        not isinstance(assertion_id, str) or not assertion_id.strip()
    ):
        raise ValueError("assertion_id must be a non-empty string")
    tool_name = _as_tool_name(_one_alias(value, ("tool_name", "tool", "name")))
    required = value.get("required", True)
    if not isinstance(required, bool):
        raise ValueError("rule required must be boolean")
    common = {"kind", "type", "assertion_id", "id", "required"}
    tool_keys = {"tool_name", "tool", "name"}
    allowed_by_kind = {
        RuleKind.TOOL_CALLED: common | tool_keys,
        RuleKind.FORBIDDEN_CALL: common | tool_keys,
        RuleKind.TOOL_COUNT: common
        | tool_keys
        | {
            "expected_count",
            "count",
            "expected",
            "min_count",
            "minimum",
            "max_count",
            "maximum",
        },
        RuleKind.TOOL_ARGUMENTS: common
        | tool_keys
        | {"expected_arguments", "arguments", "params", "exact", "exact_arguments"},
        RuleKind.TOOL_ORDER: common | {"order", "tools", "exact", "exact_order"},
    }
    unexpected = set(value) - allowed_by_kind[kind]
    if unexpected:
        raise ValueError(
            "unexpected or conflicting rule fields: " + ", ".join(sorted(unexpected))
        )
    if kind is RuleKind.TOOL_CALLED:
        if tool_name is None:
            raise ValueError("tool_called requires tool_name")
        return tool_called(
            tool_name,
            assertion_id=assertion_id if isinstance(assertion_id, str) else None,
            required=required,
        )
    if kind is RuleKind.FORBIDDEN_CALL:
        if tool_name is None:
            raise ValueError("forbidden_call requires tool_name")
        return forbidden_call(
            tool_name,
            assertion_id=assertion_id if isinstance(assertion_id, str) else None,
            required=required,
        )
    if kind is RuleKind.TOOL_COUNT:
        if tool_name is None:
            raise ValueError("tool_count requires tool_name")
        raw_count = _one_alias(value, ("expected_count", "count", "expected"))
        min_count = _one_alias(value, ("min_count", "minimum"))
        max_count = _one_alias(value, ("max_count", "maximum"))
        if raw_count is not None and (min_count is not None or max_count is not None):
            raise ValueError("count conflicts with min_count/max_count")
        if raw_count is not None and (
            isinstance(raw_count, bool) or not isinstance(raw_count, int)
        ):
            raise ValueError("tool_count requires an integer count")
        if min_count is not None and (
            isinstance(min_count, bool) or not isinstance(min_count, int)
        ):
            raise ValueError("min_count must be an integer")
        if max_count is not None and (
            isinstance(max_count, bool) or not isinstance(max_count, int)
        ):
            raise ValueError("max_count must be an integer")
        return tool_count(
            tool_name,
            raw_count,
            min_count=min_count,
            max_count=max_count,
            assertion_id=assertion_id if isinstance(assertion_id, str) else None,
            required=required,
        )
    if kind is RuleKind.TOOL_ARGUMENTS:
        if tool_name is None:
            raise ValueError("tool_arguments requires tool_name")
        arguments = _one_alias(value, ("expected_arguments", "arguments", "params"))
        if not isinstance(arguments, Mapping):
            raise ValueError("tool_arguments requires an arguments mapping")
        exact_value = _one_alias(value, ("exact", "exact_arguments"))
        exact = False if exact_value is None else exact_value
        if not isinstance(exact, bool):
            raise ValueError("exact_arguments must be boolean")
        return tool_arguments(
            tool_name,
            cast(Mapping[str, JsonValue], arguments),
            exact=exact,
            assertion_id=assertion_id if isinstance(assertion_id, str) else None,
            required=required,
        )
    order = _one_alias(value, ("order", "tools"))
    if isinstance(order, str) or not isinstance(order, Sequence):
        raise ValueError("tool_order requires an ordered sequence")
    if not all(isinstance(item, str) for item in order):
        raise ValueError("tool_order items must be strings")
    exact_value = _one_alias(value, ("exact", "exact_order"))
    exact = False if exact_value is None else exact_value
    if not isinstance(exact, bool):
        raise ValueError("exact_order must be boolean")
    return tool_order(
        cast(Sequence[str], order),
        exact=exact,
        assertion_id=assertion_id if isinstance(assertion_id, str) else None,
        required=required,
    )


def _semantic_equal(left: object, right: object) -> bool:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _semantic_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _semantic_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return left == right


def _arguments_match(
    actual: Mapping[str, JsonValue],
    expected: Mapping[str, JsonValue],
    *,
    exact: bool,
) -> bool:
    if exact and set(actual) != set(expected):
        return False
    return all(
        key in actual and _semantic_equal(actual[key], value)
        for key, value in expected.items()
    )


def _display(value: object) -> str:
    if isinstance(value, Mapping):
        value = {str(key): value[key] for key in sorted(value, key=str)}
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return repr(value)


def _result(
    rule: Rule,
    *,
    status: GradeStatus,
    reason: str,
    evidence: tuple[EvidenceRef, ...],
    judge_version: str,
) -> AssertionResult:
    return AssertionResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ASSERTION_RESULT,
        assertion_id=rule.assertion_id,
        judge=JudgeKind.RULE,
        judge_version=judge_version,
        required=rule.required,
        status=status,
        reason=reason,
        evidence=evidence,
    )


def _evidence(
    artifact: ArtifactRef | None,
    calls: Sequence[TraceToolCall],
) -> tuple[EvidenceRef, ...]:
    if artifact is None:
        return ()
    if not calls:
        return (timeline_evidence(artifact),)
    seen: set[str] = set()
    result: list[EvidenceRef] = []
    for call in calls:
        ref = trace_event_evidence(artifact, call.event_index, "payload")
        if ref.json_pointer not in seen:
            seen.add(ref.json_pointer)
            result.append(ref)
    return tuple(result)


def _evaluate_rule(
    rule: Rule,
    calls: tuple[TraceToolCall, ...],
    *,
    evidence_artifact: ArtifactRef | None,
    judge_version: str,
) -> AssertionResult:
    if rule.kind in {
        RuleKind.TOOL_CALLED,
        RuleKind.TOOL_COUNT,
        RuleKind.TOOL_ARGUMENTS,
        RuleKind.FORBIDDEN_CALL,
    }:
        if rule.tool_name is None:
            return _result(
                rule,
                status=GradeStatus.ERROR,
                reason="tool rule is missing tool_name",
                evidence=(),
                judge_version=judge_version,
            )
        matching = tuple(call for call in calls if call.tool_name == rule.tool_name)
    else:
        matching = ()

    status = GradeStatus.FAIL
    reason = "rule did not match"
    evidence_calls: tuple[TraceToolCall, ...] = matching
    if rule.kind is RuleKind.TOOL_CALLED:
        status = GradeStatus.PASS if matching else GradeStatus.FAIL
        reason = (
            f"tool {rule.tool_name!r} was called {len(matching)} time(s)"
            if matching
            else f"tool {rule.tool_name!r} was not called"
        )
    elif rule.kind is RuleKind.TOOL_COUNT:
        expected = rule.expected_count
        count_matches = (
            expected == len(matching)
            if expected is not None
            else (rule.min_count is None or len(matching) >= rule.min_count)
            and (rule.max_count is None or len(matching) <= rule.max_count)
        )
        status = GradeStatus.PASS if count_matches else GradeStatus.FAIL
        expected_text = (
            str(expected)
            if expected is not None
            else f"{rule.min_count if rule.min_count is not None else 0}.."
            f"{rule.max_count if rule.max_count is not None else '∞'}"
        )
        reason = (
            f"tool {rule.tool_name!r} count: actual={len(matching)}, "
            f"expected={expected_text}"
        )
    elif rule.kind is RuleKind.TOOL_ARGUMENTS:
        expected_arguments = rule.expected_arguments
        matched_arguments = tuple(
            call
            for call in matching
            if expected_arguments is not None
            and _arguments_match(
                call.arguments,
                expected_arguments,
                exact=rule.exact_arguments,
            )
        )
        status = GradeStatus.PASS if matched_arguments else GradeStatus.FAIL
        evidence_calls = matched_arguments or matching
        reason = (
            f"tool {rule.tool_name!r} received expected arguments "
            f"{_display(expected_arguments)}"
            if matched_arguments
            else f"tool {rule.tool_name!r} arguments did not match "
            f"expected={_display(expected_arguments)}, "
            f"actual={_display(matching[0].arguments if matching else None)}"
        )
    elif rule.kind is RuleKind.FORBIDDEN_CALL:
        status = GradeStatus.PASS if not matching else GradeStatus.FAIL
        reason = (
            f"forbidden tool {rule.tool_name!r} was not called"
            if not matching
            else f"forbidden tool {rule.tool_name!r} was called {len(matching)} time(s)"
        )
    elif rule.kind is RuleKind.TOOL_ORDER:
        names = tuple(call.tool_name for call in calls)
        if rule.exact_order:
            matches = names == rule.order
        else:
            position = 0
            for name in names:
                if position < len(rule.order) and name == rule.order[position]:
                    position += 1
            matches = position == len(rule.order)
        status = GradeStatus.PASS if matches else GradeStatus.FAIL
        reason = f"tool order actual={_display(names)}, expected={_display(rule.order)}"
        evidence_calls = calls

    if evidence_artifact is None and status in {GradeStatus.PASS, GradeStatus.FAIL}:
        return _result(
            rule,
            status=GradeStatus.NOT_EVALUATED,
            reason="Trace evidence artifact was not provided",
            evidence=(),
            judge_version=judge_version,
        )
    return _result(
        rule,
        status=status,
        reason=reason,
        evidence=_evidence(evidence_artifact, evidence_calls),
        judge_version=judge_version,
    )


def judge_rules(
    trace: Trace,
    rules: Iterable[RuleInput | Sequence[str]],
    *,
    evidence_artifact: ArtifactRef | None = None,
    judge_version: str = "rule-v1",
) -> tuple[AssertionResult, ...]:
    """Evaluate tool-call rules in trace order with one result per rule."""

    calls = trace_tool_calls(trace)
    results: list[AssertionResult] = []
    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(rules):
        try:
            rule = _coerce_rule(raw_rule)
            if rule.assertion_id in seen_ids:
                raise ValueError(f"duplicate assertion_id {rule.assertion_id!r}")
            seen_ids.add(rule.assertion_id)
            results.append(
                _evaluate_rule(
                    rule,
                    calls,
                    evidence_artifact=evidence_artifact,
                    judge_version=judge_version,
                )
            )
        except (TypeError, ValueError) as exc:
            error_rule = tool_called(
                "__invalid_rule__",
                assertion_id=f"rule-error:{index}",
            )
            results.append(
                _result(
                    error_rule,
                    status=GradeStatus.ERROR,
                    reason=f"invalid rule: {exc}",
                    evidence=(),
                    judge_version=judge_version,
                )
            )
    return tuple(results)


rule_judge = judge_rules
