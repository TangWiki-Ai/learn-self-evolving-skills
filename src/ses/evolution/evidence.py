"""Read paired evidence and export a small, de-identified fixture."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    AssertionResult,
    EvidenceArtifact,
    EvidenceRef,
    EvidenceSource,
    FailureEvidenceCase,
    FailureEvidenceFixture,
    FailureProvenance,
    GradeStatus,
    JudgeSimulatorHealth,
    MeasurementKind,
    PairedCaseResult,
    PairedComparison,
    RunEventType,
    RunnerStatus,
    RunRecord,
    SchemaVersion,
    artifact_json_bytes,
)


class EvidenceError(ValueError):
    """The evidence is missing, tampered with, private, or malformed."""


_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}|(?:api[_-]?key|authorization|bearer)[=: ]+\S+)",
    re.IGNORECASE,
)


def sha256_file(path: Path) -> str:
    """Hash a file without persisting or returning its contents."""
    return hashlib.sha256(_read_regular_bytes(path, "evidence file")).hexdigest()


def _read_regular_bytes(path: Path, label: str) -> bytes:
    _require_file(path, label)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"cannot read {label}: {path.name}") from exc


def _require_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} must be a regular file")


def _read_events(payload: bytes) -> dict[str, RunRecord]:
    events: dict[str, RunRecord] = {}
    try:
        lines = payload.decode("utf-8").splitlines()
        for line in lines:
            event = RunRecord.model_validate_json(line)
            if event.event_type is RunEventType.RUN_STARTED:
                continue
            if event.case_id is None:
                raise EvidenceError("paired event record must identify a case")
            if event.case_id in events:
                raise EvidenceError("paired event log has duplicate case attempts")
            events[event.case_id] = event
    except (UnicodeError, ValidationError) as exc:
        raise EvidenceError("invalid event log") from exc
    return events


def _case_event(events: Mapping[str, RunRecord], case_id: str) -> RunRecord:
    try:
        return events[case_id]
    except KeyError as exc:
        raise EvidenceError("comparison references a case absent from events") from exc


def _redacted_case(
    *,
    index: int,
    row: PairedCaseResult,
    event: RunRecord,
) -> FailureEvidenceCase:
    if event.status is None:
        raise EvidenceError("attempt event has no status")
    status = event.status
    try:
        assertions = tuple(
            AssertionResult.model_validate(item) for item in event.evidence
        )
    except ValidationError as exc:
        raise EvidenceError("attempt contains invalid Assertion evidence") from exc
    failed_assertions = tuple(
        assertion for assertion in assertions if assertion.status is GradeStatus.FAIL
    )
    supporting_trace_hashes = {
        reference.artifact.sha256
        for assertion in failed_assertions
        for reference in assertion.evidence
        if "trace" in Path(reference.artifact.path).name.casefold()
    }
    trace = None
    if event.artifacts.traces:
        selected = next(
            (
                artifact
                for artifact in reversed(event.artifacts.traces)
                if artifact.sha256 in supporting_trace_hashes
            ),
            event.artifacts.traces[-1],
        )
        trace = EvidenceArtifact(
            kind="trace",
            source_file="skill/trace.json",
            sha256=selected.sha256,
        )
    elif row.skill_trace is not None:
        trace = EvidenceArtifact(
            kind="trace",
            source_file="skill/trace.json",
            sha256=row.skill_trace.sha256,
        )

    assertion = None
    if event.artifacts.grade is not None:
        assertion = EvidenceArtifact(
            kind="assertion",
            source_file="skill/assertion.json",
            sha256=event.artifacts.grade.sha256,
        )

    groups: Counter[str] = Counter()
    for result in failed_assertions:
        groups[result.assertion_id.split(":", 1)[0]] += 1

    if status is RunnerStatus.INFRASTRUCTURE_ERROR:
        observation = (
            "Skill-side infrastructure_error ended the attempt before Judge "
            "assertion evidence was available."
        )
    elif status is RunnerStatus.AGENT_FAIL:
        observation = (
            "Skill-side agent_fail was observed; the redacted assertion summary "
            f"contains {sum(groups.values())} failed evidence entries."
        )
    else:
        observation = "Skill-side attempt completed without a failure classification."

    return FailureEvidenceCase(
        case_key=f"case-{index:03d}",
        pair_category=row.category,
        baseline_status=row.baseline_status,
        skill_status=status,
        trace=trace,
        assertion=assertion,
        failure_kinds=dict(sorted(groups.items())),
        judge_simulator_health=JudgeSimulatorHealth.NOT_REVIEWED,
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
) -> FailureEvidenceFixture:
    """Export only stable failure summaries from a paired artifact directory.

    The input event logs are read, reduced, and never copied to ``output_path``.
    """
    comparison_bytes = _read_regular_bytes(comparison_path, "comparison")
    baseline_bytes = _read_regular_bytes(baseline_events_path, "baseline event log")
    skill_bytes = _read_regular_bytes(skill_events_path, "Skill event log")
    comparison_sha256 = hashlib.sha256(comparison_bytes).hexdigest()
    baseline_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    skill_sha256_events = hashlib.sha256(skill_bytes).hexdigest()
    if (
        expected_comparison_sha256 is not None
        and comparison_sha256 != expected_comparison_sha256
    ):
        raise EvidenceError("comparison hash mismatch")
    try:
        comparison = PairedComparison.model_validate_json(comparison_bytes)
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
    provenance = (
        FailureProvenance.LIVE
        if comparison.measurement_kind is MeasurementKind.LIVE_MEASURED
        else FailureProvenance.SYNTHETIC
    )
    baseline_events = _read_events(baseline_bytes)
    skill_events = _read_events(skill_bytes)
    cases: list[FailureEvidenceCase] = []
    for index, row_model in enumerate(comparison.cases, 1):
        event = _case_event(skill_events, row_model.case_id)
        baseline_event = _case_event(baseline_events, row_model.case_id)
        if event.status is not row_model.skill_status:
            raise EvidenceError("Skill event status does not match paired comparison")
        if baseline_event.status is not row_model.baseline_status:
            raise EvidenceError(
                "baseline event status does not match paired comparison"
            )
        cases.append(
            _redacted_case(
                index=index,
                row=row_model,
                event=event,
            )
        )
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
            measurement_kind=(comparison.measurement_kind),
        ),
        cases=tuple(cases),
        redaction_notice="provider_streams_paths_gold_and_private_model_content_removed",
    )
    payload = fixture.model_dump(mode="json")
    _assert_safe_fixture(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as stream:
        stream.write(artifact_json_bytes(fixture))
    return fixture


def load_failure_evidence(path: Path) -> FailureEvidenceFixture:
    """Load and validate one evidence fixture."""
    fixture, _, _ = load_failure_evidence_verified(path)
    return fixture


def load_failure_evidence_verified(
    path: Path,
) -> tuple[FailureEvidenceFixture, bytes, ArtifactRef]:
    """Hash and parse one fixture from the same immutable byte snapshot."""
    payload = _read_regular_bytes(path, "failure evidence fixture")
    try:
        fixture = FailureEvidenceFixture.model_validate_json(payload)
    except ValidationError as exc:
        raise EvidenceError("invalid failure evidence fixture") from exc
    _assert_safe_fixture(fixture.model_dump(mode="json"))
    artifact = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    return fixture, payload, artifact


def evidence_ref_for_fixture(path: Path) -> ArtifactRef:
    """Build a workspace-relative reference for a fixture file."""
    _, _, artifact = load_failure_evidence_verified(path)
    return artifact


def linked_evidence_ref(path: Path, *, pointer: str) -> EvidenceRef:
    """Build a complete Trace/Assertion link to a fixture JSON pointer."""
    if not pointer.startswith("/"):
        raise EvidenceError("evidence pointer must be a JSON pointer")
    return EvidenceRef(
        artifact=evidence_ref_for_fixture(path),
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
