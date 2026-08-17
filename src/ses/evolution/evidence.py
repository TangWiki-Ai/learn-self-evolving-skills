"""Read paired evidence and export a small, de-identified fixture."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    EvidenceArtifact,
    EvidenceRef,
    EvidenceSource,
    FailureEvidenceCase,
    FailureEvidenceFixture,
    FailureProvenance,
    MeasurementKind,
    PairCategory,
    PairedComparison,
    RunnerStatus,
    SchemaVersion,
)


class EvidenceError(ValueError):
    """The evidence is missing, tampered with, private, or malformed."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}|(?:api[_-]?key|authorization|bearer)[=: ]+\S+)",
    re.IGNORECASE,
)


def sha256_file(path: Path) -> str:
    """Hash a file without persisting or returning its contents."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"cannot read evidence file: {path.name}") from exc


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid evidence JSON: {path.name}") from exc


def _require_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} must be a regular file")


def _check_expected_hash(path: Path, expected: str | None, label: str) -> str:
    actual = sha256_file(path)
    if expected is not None and actual != expected:
        raise EvidenceError(f"{label} hash mismatch")
    return actual


def _read_events(path: Path) -> dict[str, Mapping[str, Any]]:
    _require_file(path, "event log")
    events: dict[str, Mapping[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise EvidenceError("event log record must be an object")
            case_id = value.get("case_id")
            if isinstance(case_id, str):
                events[case_id] = value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("invalid event log") from exc
    return events


def _case_event(
    events: Mapping[str, Mapping[str, Any]], case_id: str
) -> Mapping[str, Any]:
    try:
        return events[case_id]
    except KeyError as exc:
        raise EvidenceError("comparison references a case absent from events") from exc


def _redacted_case(
    *,
    index: int,
    row: Mapping[str, Any],
    event: Mapping[str, Any],
) -> FailureEvidenceCase:
    status = RunnerStatus(str(event.get("status")))
    trace = None
    traces = event.get("artifacts", {}).get("traces", [])
    if isinstance(traces, list) and traces:
        first = traces[0]
        if isinstance(first, Mapping) and isinstance(first.get("sha256"), str):
            trace = EvidenceArtifact(
                kind="trace",
                source_file="skill/trace.json",
                sha256=first["sha256"],
            )
    if trace is None:
        row_trace = row.get("skill_trace")
        if isinstance(row_trace, Mapping) and isinstance(row_trace.get("sha256"), str):
            trace = EvidenceArtifact(
                kind="trace",
                source_file="skill/trace.json",
                sha256=row_trace["sha256"],
            )

    assertion = None
    grade = event.get("artifacts", {}).get("grade")
    if isinstance(grade, Mapping) and isinstance(grade.get("sha256"), str):
        assertion = EvidenceArtifact(
            kind="assertion",
            source_file="skill/assertion.json",
            sha256=grade["sha256"],
        )

    groups: Counter[str] = Counter()
    raw_evidence = event.get("evidence", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, Mapping) and isinstance(item.get("assertion_id"), str):
                groups[item["assertion_id"].split(":", 1)[0]] += 1

    if status is RunnerStatus.INFRASTRUCTURE_ERROR:
        observation = (
            "Skill-side infrastructure_error ended the attempt before Judge "
            "assertion evidence was available."
        )
    elif status is RunnerStatus.AGENT_FAIL:
        observation = (
            "Skill-side agent_fail was observed; the redacted assertion summary "
            f"contains {sum(groups.values())} failed-or-passing evidence entries."
        )
    else:
        observation = "Skill-side attempt completed without a failure classification."

    category_value = row.get("category")
    if not isinstance(category_value, str):
        raise EvidenceError("paired row has no category")
    return FailureEvidenceCase(
        case_key=f"case-{index:03d}",
        pair_category=PairCategory(category_value),
        baseline_status=RunnerStatus(str(row["baseline_status"])),
        skill_status=status,
        trace=trace,
        assertion=assertion,
        failure_kinds=dict(sorted(groups.items())),
        observation=observation,
    )


def export_failure_evidence(
    *,
    comparison_path: Path,
    baseline_events_path: Path,
    skill_events_path: Path,
    output_path: Path,
    expected_comparison_sha256: str | None = None,
    expected_pair_execution_sha256: str | None = None,
    expected_skill_sha256: str | None = None,
    provenance: FailureProvenance = FailureProvenance.LIVE,
) -> FailureEvidenceFixture:
    """Export only stable failure summaries from a paired artifact directory.

    The input event logs are read, reduced, and never copied to ``output_path``.
    """
    comparison_sha256 = _check_expected_hash(
        comparison_path, expected_comparison_sha256, "comparison"
    )
    baseline_sha256 = sha256_file(baseline_events_path)
    skill_sha256_events = sha256_file(skill_events_path)
    raw = _load_json(comparison_path)
    if not isinstance(raw, Mapping):
        raise EvidenceError("comparison must be a JSON object")
    try:
        comparison = PairedComparison.model_validate(raw)
    except ValidationError as exc:
        raise EvidenceError("comparison does not satisfy Ticket 08 schema") from exc
    if (
        expected_pair_execution_sha256 is not None
        and comparison.pair_execution_sha256 != expected_pair_execution_sha256
    ):
        raise EvidenceError("pair execution hash mismatch")
    if (
        expected_skill_sha256 is not None
        and comparison.skill_sha256 != expected_skill_sha256
    ):
        raise EvidenceError("accepted Skill hash mismatch")
    if comparison.baseline_events.sha256 != baseline_sha256:
        raise EvidenceError("baseline event log hash does not match comparison")
    if comparison.skill_events.sha256 != skill_sha256_events:
        raise EvidenceError("Skill event log hash does not match comparison")
    baseline_events = _read_events(baseline_events_path)
    skill_events = _read_events(skill_events_path)
    cases: list[FailureEvidenceCase] = []
    for index, row_model in enumerate(comparison.cases, 1):
        row = row_model.model_dump(mode="json")
        event = _case_event(skill_events, row_model.case_id)
        cases.append(
            _redacted_case(
                index=index,
                row=row,
                event=event,
            )
        )
        _case_event(baseline_events, row_model.case_id)
    fixture = FailureEvidenceFixture(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="failure_evidence_fixture",
        provenance=provenance,
        source=EvidenceSource(
            source_label="ticket08-live-paired-v4",
            comparison_sha256=comparison_sha256,
            pair_execution_sha256=comparison.pair_execution_sha256,
            baseline_events_sha256=baseline_sha256,
            skill_events_sha256=skill_sha256_events,
            skill_sha256=comparison.skill_sha256,
            measurement_kind=(
                comparison.measurement_kind
                if provenance is FailureProvenance.LIVE
                else MeasurementKind.SYNTHETIC_OFFLINE
            ),
        ),
        cases=tuple(cases),
        redaction_notice="provider_streams_paths_gold_and_private_model_content_removed",
    )
    payload = fixture.model_dump(mode="json")
    _assert_safe_fixture(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return fixture


def load_failure_evidence(path: Path) -> FailureEvidenceFixture:
    """Load and validate one evidence fixture."""
    _require_file(path, "failure evidence fixture")
    raw = _load_json(path)
    try:
        fixture = FailureEvidenceFixture.model_validate(raw)
    except ValidationError as exc:
        raise EvidenceError("invalid failure evidence fixture") from exc
    _assert_safe_fixture(fixture.model_dump(mode="json"))
    return fixture


def evidence_ref_for_fixture(path: Path, *, pointer: str) -> ArtifactRef:
    """Build a workspace-relative reference for a fixture file."""
    _require_file(path, "failure evidence fixture")
    if not pointer.startswith("/"):
        raise EvidenceError("evidence pointer must be a JSON pointer")
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path.name,
        sha256=sha256_file(path),
    )


def linked_evidence_ref(path: Path, *, pointer: str) -> EvidenceRef:
    """Build a complete Trace/Assertion link to a fixture JSON pointer."""
    if not pointer.startswith("/"):
        raise EvidenceError("evidence pointer must be a JSON pointer")
    return EvidenceRef(
        artifact=evidence_ref_for_fixture(path, pointer=pointer),
        json_pointer=pointer,
    )


def _assert_safe_fixture(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_safe_fixture(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_fixture(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if _ABSOLUTE.match(value) or "\x00" in value:
            raise EvidenceError(f"absolute path or NUL in fixture at {path}")
        if _SECRET.search(value):
            raise EvidenceError(f"credential-like text in fixture at {path}")
