"""Deterministic StateDiff and snapshot judging."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeAlias

from ses.contracts import (
    ArtifactRef,
    AssertionResult,
    EvidenceRef,
    GradeStatus,
    JudgeKind,
    RecordType,
    SchemaVersion,
    ShopSnapshot,
    StateChange,
    StateDiff,
)

from ..evidence import (
    evidence_ref,
    join_json_pointer,
    state_diff_evidence,
)

StateRecord: TypeAlias = ShopSnapshot | StateDiff


def _semantic_equal(left: object, right: object) -> bool:
    """Compare JSON values without collapsing ``True`` into ``1``."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_semantic_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _semantic_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return left == right


def _display(value: object) -> str:
    if isinstance(value, Mapping):
        value = {str(key): value[key] for key in sorted(value, key=str)}
    elif isinstance(value, (list, tuple)):
        value = list(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return repr(value)


def _flatten(value: object, prefix: tuple[str, ...]) -> dict[tuple[str, ...], object]:
    if isinstance(value, Mapping):
        if not value:
            return {prefix: value}
        result: dict[tuple[str, ...], object] = {}
        for key in sorted(value, key=str):
            result.update(_flatten(value[key], (*prefix, str(key))))
        return result
    if isinstance(value, (list, tuple)):
        if not value:
            return {prefix: value}
        result = {}
        for index, child in enumerate(value):
            result.update(_flatten(child, (*prefix, str(index))))
        return result
    return {prefix: value}


def _existing_snapshot_pointer(actual: ShopSnapshot, path: tuple[str, ...]) -> str:
    """Return the deepest existing ancestor for a missing snapshot path."""

    if path[0] != "state":
        return join_json_pointer(*path)
    current: object = actual.state
    existing = ["state"]
    for token in path[1:]:
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, (list, tuple)) and token.isdigit():
            index = int(token)
            if index >= len(current):
                break
            current = current[index]
        else:
            break
        existing.append(token)
    return join_json_pointer(*existing)


def _result(
    *,
    assertion_id: str,
    required: bool,
    status: GradeStatus,
    reason: str,
    evidence: tuple[EvidenceRef, ...] = (),
    judge_version: str,
) -> AssertionResult:
    return AssertionResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ASSERTION_RESULT,
        assertion_id=assertion_id,
        judge=JudgeKind.STATE,
        judge_version=judge_version,
        required=required,
        status=status,
        reason=reason,
        evidence=evidence,
    )


def _state_snapshot_assertions(
    expected: ShopSnapshot,
    actual: ShopSnapshot,
    *,
    evidence_artifact: ArtifactRef | None,
    required: bool,
    judge_version: str,
) -> tuple[AssertionResult, ...]:
    expected_values = {
        ("case_id",): expected.case_id,
        ("policy_version",): expected.policy_version,
        **_flatten(expected.state, ("state",)),
    }
    actual_values = {
        ("case_id",): actual.case_id,
        ("policy_version",): actual.policy_version,
        **_flatten(actual.state, ("state",)),
    }
    paths = sorted(set(expected_values) | set(actual_values))
    if not paths:
        paths = [("state",)]
    results: list[AssertionResult] = []
    for path in paths:
        expected_present = path in expected_values
        actual_present = path in actual_values
        expected_value = expected_values.get(path)
        actual_value = actual_values.get(path)
        matches = (
            expected_present
            and actual_present
            and _semantic_equal(expected_value, actual_value)
        )
        if evidence_artifact is None:
            status = GradeStatus.NOT_EVALUATED
            reason = "state evidence artifact was not provided"
            evidence: tuple[EvidenceRef, ...] = ()
        else:
            status = GradeStatus.PASS if matches else GradeStatus.FAIL
            if not expected_present:
                reason = (
                    f"unexpected state at {join_json_pointer(*path)}: "
                    f"actual={_display(actual_value)}"
                )
            elif not actual_present:
                reason = (
                    f"missing state at {join_json_pointer(*path)}: "
                    f"expected={_display(expected_value)}"
                )
            else:
                reason = (
                    f"state at {join_json_pointer(*path)}: "
                    f"actual={_display(actual_value)}, "
                    f"expected={_display(expected_value)}"
                )
            pointer = (
                _existing_snapshot_pointer(actual, path)
                if expected_present and not actual_present
                else join_json_pointer(*path)
            )
            evidence = (evidence_ref(evidence_artifact, pointer),)
        results.append(
            _result(
                assertion_id="state:" + join_json_pointer(*path),
                required=required,
                status=status,
                reason=reason,
                evidence=evidence,
                judge_version=judge_version,
            )
        )
    return tuple(results)


def _diff_entries(diff: StateDiff) -> dict[tuple[str, str], object]:
    entries: dict[tuple[str, str], object] = {}
    for path, value in diff.added.items():
        entries[("added", path)] = value
    for path, value in diff.removed.items():
        entries[("removed", path)] = value
    for path, change in diff.changed.items():
        entries[("changed", path)] = change
    return entries


def _change_value(value: object) -> object:
    if isinstance(value, StateChange):
        return {"before": value.before, "after": value.after}
    return value


def _state_diff_assertions(
    expected: StateDiff,
    actual: StateDiff,
    *,
    evidence_artifact: ArtifactRef | None,
    required: bool,
    judge_version: str,
) -> tuple[AssertionResult, ...]:
    expected_values = _diff_entries(expected)
    actual_values = _diff_entries(actual)
    paths = sorted(set(expected_values) | set(actual_values))
    if not paths:
        if evidence_artifact is None:
            return (
                _result(
                    assertion_id="state-diff:/added",
                    required=required,
                    status=GradeStatus.NOT_EVALUATED,
                    reason="StateDiff evidence artifact was not provided",
                    judge_version=judge_version,
                ),
            )
        return (
            _result(
                assertion_id="state-diff:/added",
                required=required,
                status=GradeStatus.PASS,
                reason="StateDiff contains no added, removed, or changed paths",
                evidence=(evidence_ref(evidence_artifact, "/added"),),
                judge_version=judge_version,
            ),
        )
    results: list[AssertionResult] = []
    for bucket, path in paths:
        expected_present = (bucket, path) in expected_values
        actual_present = (bucket, path) in actual_values
        expected_value = _change_value(expected_values.get((bucket, path)))
        actual_value = _change_value(actual_values.get((bucket, path)))
        matches = (
            expected_present
            and actual_present
            and _semantic_equal(expected_value, actual_value)
        )
        if evidence_artifact is None:
            status = GradeStatus.NOT_EVALUATED
            reason = "StateDiff evidence artifact was not provided"
            evidence: tuple[EvidenceRef, ...] = ()
        else:
            status = GradeStatus.PASS if matches else GradeStatus.FAIL
            if not expected_present:
                reason = (
                    f"unexpected StateDiff {bucket} path {path}: "
                    f"actual={_display(actual_value)}"
                )
            elif not actual_present:
                reason = (
                    f"missing StateDiff {bucket} path {path}: "
                    f"expected={_display(expected_value)}"
                )
            else:
                reason = (
                    f"StateDiff {bucket} path {path}: "
                    f"actual={_display(actual_value)}, "
                    f"expected={_display(expected_value)}"
                )
            evidence = (
                evidence_ref(evidence_artifact, join_json_pointer(bucket))
                if expected_present and not actual_present
                else state_diff_evidence(evidence_artifact, bucket, path),
            )
        results.append(
            _result(
                assertion_id="state-diff:" + join_json_pointer(bucket, path),
                required=required,
                status=status,
                reason=reason,
                evidence=evidence,
                judge_version=judge_version,
            )
        )
    return tuple(results)


def judge_state(
    expected: StateRecord | None = None,
    actual: StateRecord | None = None,
    *,
    expected_snapshot: ShopSnapshot | None = None,
    actual_snapshot: ShopSnapshot | None = None,
    expected_diff: StateDiff | None = None,
    actual_diff: StateDiff | None = None,
    evidence_artifact: ArtifactRef | None = None,
    required: bool = True,
    judge_version: str = "state-v1",
) -> tuple[AssertionResult, ...]:
    """Compare state facts one path at a time and attach source evidence."""

    selected_expected = (
        expected
        if expected is not None
        else expected_snapshot
        if expected_snapshot is not None
        else expected_diff
    )
    selected_actual = (
        actual
        if actual is not None
        else actual_snapshot
        if actual_snapshot is not None
        else actual_diff
    )
    if selected_expected is None or selected_actual is None:
        return (
            _result(
                assertion_id="state:input",
                required=required,
                status=GradeStatus.ERROR,
                reason="State Judge requires expected and actual snapshot or StateDiff",
                judge_version=judge_version,
            ),
        )
    if isinstance(selected_expected, ShopSnapshot) and isinstance(
        selected_actual, ShopSnapshot
    ):
        return _state_snapshot_assertions(
            selected_expected,
            selected_actual,
            evidence_artifact=evidence_artifact,
            required=required,
            judge_version=judge_version,
        )
    if isinstance(selected_expected, StateDiff) and isinstance(
        selected_actual, StateDiff
    ):
        return _state_diff_assertions(
            selected_expected,
            selected_actual,
            evidence_artifact=evidence_artifact,
            required=required,
            judge_version=judge_version,
        )
    return (
        _result(
            assertion_id="state:input-type",
            required=required,
            status=GradeStatus.ERROR,
            reason="expected and actual state records must use the same contract type",
            judge_version=judge_version,
        ),
    )


state_judge = judge_state
