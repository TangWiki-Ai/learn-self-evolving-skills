"""Trace, evidence, assertion, and case-grade contracts."""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise
from typing import Literal

from pydantic import model_validator

from ses.contracts.artifact import ArtifactRef, JsonPointer, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import (
    CompletedPayload,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    Usage,
    UsagePayload,
)
from ses.contracts.primitives import (
    AssertionId,
    CaseId,
    GradeId,
    IterationId,
    NonEmptyStr,
    RecordType,
    RunId,
    SessionId,
    TraceId,
)


class GradeStatus(StrEnum):
    """Shared assertion and case grading outcomes."""

    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"
    ERROR = "error"


class JudgeKind(StrEnum):
    """Judge implementations that emit canonical assertion results."""

    STATE = "state"
    RULE = "rule"


class Trace(VersionedRecord):
    """An immutable timeline assembled from normalized engine records."""

    record_type: Literal[RecordType.TRACE]
    trace_id: TraceId
    run_id: RunId
    case_id: CaseId
    iteration_id: IterationId
    session_id: SessionId | None = None
    request: EngineRequest
    events: tuple[EngineEvent, ...]
    usage: Usage | None = None
    exit_status: EngineExitStatus
    skill_version: NonEmptyStr | None = None
    skill_sha256: Sha256Digest | None = None

    @model_validator(mode="after")
    def _validate_timeline(self) -> Trace:
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("Trace event IDs must be unique")
        sequences = [event.sequence for event in self.events]
        if any(current >= following for current, following in pairwise(sequences)):
            raise ValueError("Trace event sequences must be strictly increasing")
        if any(event.request_id != self.request.request_id for event in self.events):
            raise ValueError("Trace events must reference the trace request")

        completed = [
            event.payload
            for event in self.events
            if isinstance(event.payload, CompletedPayload)
        ]
        if len(completed) != 1 or not isinstance(
            self.events[-1].payload if self.events else None,
            CompletedPayload,
        ):
            raise ValueError("Trace requires exactly one terminal completed event")
        terminal = completed[0]
        if terminal.exit_status is not self.exit_status:
            raise ValueError("Trace exit_status must match the completed event")
        if terminal.session_id != self.session_id:
            raise ValueError("Trace session_id must match the completed event")
        if self.exit_status is EngineExitStatus.SUCCESS and self.session_id is None:
            raise ValueError("a successful Trace requires a session_id")
        if (
            self.request.resume_session_id is not None
            and self.session_id != self.request.resume_session_id
        ):
            raise ValueError("a resumed Trace must preserve the requested session_id")

        usage_events = [
            event.payload.usage
            for event in self.events
            if isinstance(event.payload, UsagePayload)
        ]
        if not usage_events and self.usage is not None:
            raise ValueError("Trace usage requires a cumulative usage event")
        if usage_events and self.usage != usage_events[-1]:
            raise ValueError("Trace usage must match the last cumulative usage event")

        if (self.skill_version is None) != (self.skill_sha256 is None):
            raise ValueError("skill_version and skill_sha256 must be provided together")
        return self


class EvidenceRef(ContractModel):
    """A precise JSON location inside a content-addressed artifact."""

    artifact: ArtifactRef
    json_pointer: JsonPointer


class AssertionResult(VersionedRecord):
    """One evidence-backed deterministic judge decision."""

    record_type: Literal[RecordType.ASSERTION_RESULT]
    assertion_id: AssertionId
    judge: JudgeKind
    judge_version: NonEmptyStr
    required: bool
    status: GradeStatus
    reason: NonEmptyStr
    evidence: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def _require_evidence_for_a_decision(self) -> AssertionResult:
        if self.status in {GradeStatus.PASS, GradeStatus.FAIL} and not self.evidence:
            raise ValueError("pass and fail assertions require evidence")
        return self


class CaseGrade(VersionedRecord):
    """The aggregate status plus independent assertion-level results."""

    record_type: Literal[RecordType.CASE_GRADE]
    grade_id: GradeId
    run_id: RunId
    case_id: CaseId
    iteration_id: IterationId
    status: GradeStatus
    assertions: tuple[AssertionResult, ...]

    @model_validator(mode="after")
    def _validate_assertions(self) -> CaseGrade:
        assertion_keys = [
            (assertion.judge, assertion.assertion_id) for assertion in self.assertions
        ]
        if len(set(assertion_keys)) != len(assertion_keys):
            raise ValueError("CaseGrade assertion IDs must be unique per judge")
        if self.status in {GradeStatus.PASS, GradeStatus.FAIL} and not self.assertions:
            raise ValueError("pass and fail case grades require assertions")
        required_statuses = {
            assertion.status for assertion in self.assertions if assertion.required
        }
        if self.status is GradeStatus.PASS and any(
            status is not GradeStatus.PASS for status in required_statuses
        ):
            raise ValueError("pass requires every required assertion to pass")
        if (
            self.status is GradeStatus.FAIL
            and GradeStatus.FAIL not in required_statuses
        ):
            raise ValueError("fail requires a failed required assertion")
        return self
