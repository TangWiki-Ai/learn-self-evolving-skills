"""Execute fixed offline Judge protocols and compare them with human labels."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    AssertionResult,
    ContractModel,
    GradeStatus,
    JudgeKind,
    RecordType,
    SchemaVersion,
    Sha256Digest,
    TextDeltaPayload,
    UtcDateTime,
    VersionedRecord,
)
from ses.engines.fake import FakeEngine, FakeFixture, FakeStep
from ses.evaluation.evidence_extractor import (
    EvidenceBundle,
    evidence_json_bytes,
    evidence_sha256,
)
from ses.evaluation.judges.agent import AgentJudgeEngine, judge_agent
from ses.evaluation.judges.llm import (
    BoundJudgeEngine,
    JudgeProtocolMetadata,
    JudgeResponseSource,
    Rubric,
    judge_llm,
)

__all__ = [
    "execute_fixed_calibration",
    "judge_agent",
    "judge_llm",
    "load_calibration_fixture",
    "observations_from_assertions",
]

_STATUS_ORDER = (
    GradeStatus.PASS,
    GradeStatus.FAIL,
    GradeStatus.NOT_EVALUATED,
    GradeStatus.ERROR,
)
_JUDGE_ORDER = (JudgeKind.LLM, JudgeKind.AGENT)


class HumanLabel(ContractModel):
    """One reference label whose review status is explicit and auditable."""

    case_id: str
    assertion_id: str
    status: GradeStatus
    review_status: Literal["course_authored_pending_human_review", "human_reviewed"]
    reviewer: str | None = None
    reviewed_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def _human_labels_are_not_infrastructure_errors(self) -> HumanLabel:
        if self.status is GradeStatus.ERROR:
            raise ValueError("human labels cannot use judge error")
        reviewed = self.review_status == "human_reviewed"
        if reviewed != (self.reviewer is not None and self.reviewed_at is not None):
            raise ValueError(
                "human-reviewed labels require reviewer identity and timestamp"
            )
        return self


class FixedJudgeResponses(ContractModel):
    """Original response bytes replayed through each Judge parser."""

    llm: str
    agent: str


class CalibrationCase(ContractModel):
    """One reviewed input plus raw fixed outputs, never predicted statuses."""

    case_id: str
    label: HumanLabel
    rubric: Rubric
    evidence: EvidenceBundle
    fixed_responses: FixedJudgeResponses

    @model_validator(mode="after")
    def _identities_match(self) -> CalibrationCase:
        if self.label.case_id != self.case_id:
            raise ValueError("case and human label IDs must match")
        if self.label.assertion_id != self.rubric.assertion_id:
            raise ValueError("rubric and human assertion IDs must match")
        return self


class CalibrationFixture(VersionedRecord):
    """Reference cases and raw responses for the fixed offline protocol."""

    record_type: Literal[RecordType.CALIBRATION_FIXTURE]
    dataset_id: str
    dataset_version: str
    human_label_version: str
    source: str
    measurement_context: str
    response_source: Literal["course_authored_fixed_response"]
    live_model_measured: Literal[False]
    cases: tuple[CalibrationCase, ...]

    @model_validator(mode="after")
    def _require_unique_rows(self) -> CalibrationFixture:
        keys = [(item.case_id, item.rubric.assertion_id) for item in self.cases]
        if not keys:
            raise ValueError("calibration requires at least one human label")
        if len(set(keys)) != len(keys):
            raise ValueError("human label keys must be unique")
        if len({item.label.review_status for item in self.cases}) != 1:
            raise ValueError("calibration label review status must be uniform")
        return self


class CalibrationObservation(ContractModel):
    """One canonical Judge assertion adapted for agreement calculation."""

    case_id: str
    assertion_id: str
    judge: JudgeKind
    status: GradeStatus
    reason: str

    @model_validator(mode="after")
    def _only_model_judges_are_calibrated(self) -> CalibrationObservation:
        if self.judge not in _JUDGE_ORDER:
            raise ValueError("calibration only supports llm and agent judges")
        return self


class JudgeCalibration(ContractModel):
    """Measured agreement and full confusion matrix for one judge."""

    judge: JudgeKind
    agreements: int
    total: int
    agreement: Decimal
    confusion_matrix: Mapping[str, Mapping[str, int]]


class CalibrationDisagreement(ContractModel):
    """One traceable disagreement with the current reference label."""

    case_id: str
    assertion_id: str
    judge: JudgeKind
    human_status: GradeStatus
    judge_status: GradeStatus
    judge_reason: str


class CalibrationMeasurement(ContractModel):
    """Protocol identities and original response for one executed Judge call."""

    case_id: str
    assertion_id: str
    judge: JudgeKind
    human_label_version: str
    human_status: GradeStatus
    judge_status: GradeStatus
    raw_fixed_response: str
    evidence_sha256: Sha256Digest
    rubric_sha256: Sha256Digest
    prompt_sha256: Sha256Digest
    extractor_sha256: Sha256Digest
    judge_model_id: str
    model_lock_version: str
    response_source: JudgeResponseSource
    model_config_sha256: Sha256Digest
    model_protocol_sha256: Sha256Digest
    protocol_sha256: Sha256Digest


class CalibrationReport(VersionedRecord):
    """Agreement artifact emitted only after both Judge protocols execute."""

    record_type: Literal[RecordType.JUDGE_CALIBRATION]
    dataset_id: str
    dataset_version: str
    human_label_version: str
    source: str
    measurement_context: str
    response_source: Literal["course_authored_fixed_response"]
    label_review_status: Literal[
        "course_authored_pending_human_review", "human_reviewed"
    ]
    measured: Literal[True] = True
    fixed_offline_protocol_executed: Literal[True] = True
    live_model_measured: Literal[False] = False
    sample_size: int
    judges: tuple[JudgeCalibration, ...]
    disagreements: tuple[CalibrationDisagreement, ...]
    measurements: tuple[CalibrationMeasurement, ...]


def load_calibration_fixture(path: Path) -> CalibrationFixture:
    """Load fixed inputs and raw responses without environment or network access."""

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except OSError as exc:
        raise ValueError(f"cannot read calibration fixture {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"calibration fixture is not valid JSON: {path}") from exc
    return CalibrationFixture.model_validate(payload)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"calibration fixture contains duplicate key {key!r}")
        result[key] = value
    return result


def observations_from_assertions(
    results: Mapping[tuple[str, JudgeKind], AssertionResult],
) -> tuple[CalibrationObservation, ...]:
    """Adapt canonical Judge outputs rather than fixture-authored statuses."""

    observations: list[CalibrationObservation] = []
    for (case_id, judge), assertion in sorted(
        results.items(), key=lambda item: (_JUDGE_ORDER.index(item[0][1]), item[0][0])
    ):
        if assertion.judge is not judge:
            raise ValueError("assertion judge does not match its calibration key")
        observations.append(
            CalibrationObservation(
                case_id=case_id,
                assertion_id=assertion.assertion_id,
                judge=judge,
                status=assertion.status,
                reason=assertion.reason,
            )
        )
    return tuple(observations)


def _empty_matrix() -> dict[str, dict[str, int]]:
    return {
        human.value: {predicted.value: 0 for predicted in _STATUS_ORDER}
        for human in _STATUS_ORDER
    }


def _summarize(
    labels: Sequence[HumanLabel],
    predictions: Sequence[CalibrationObservation],
) -> tuple[
    tuple[JudgeCalibration, ...],
    tuple[CalibrationDisagreement, ...],
]:
    prediction_map = {
        (item.judge, item.case_id, item.assertion_id): item for item in predictions
    }
    expected_keys = {
        (judge, label.case_id, label.assertion_id)
        for judge in _JUDGE_ORDER
        for label in labels
    }
    if set(prediction_map) != expected_keys:
        raise ValueError("Judge outputs do not exactly match the human labels")
    reports: list[JudgeCalibration] = []
    disagreements: list[CalibrationDisagreement] = []
    for judge in _JUDGE_ORDER:
        matrix = _empty_matrix()
        agreements = 0
        for label in labels:
            prediction = prediction_map[(judge, label.case_id, label.assertion_id)]
            matrix[label.status.value][prediction.status.value] += 1
            if prediction.status is label.status:
                agreements += 1
            else:
                disagreements.append(
                    CalibrationDisagreement(
                        case_id=label.case_id,
                        assertion_id=label.assertion_id,
                        judge=judge,
                        human_status=label.status,
                        judge_status=prediction.status,
                        judge_reason=prediction.reason,
                    )
                )
        reports.append(
            JudgeCalibration(
                judge=judge,
                agreements=agreements,
                total=len(labels),
                agreement=Decimal(agreements) / Decimal(len(labels)),
                confusion_matrix=matrix,
            )
        )
    return tuple(reports), tuple(disagreements)


def _fake_engine(raw_response: str) -> FakeEngine:
    return FakeEngine(
        FakeFixture(
            events=(
                FakeStep(
                    payload=TextDeltaPayload(
                        message_id="fixed-judge-response", text=raw_response
                    )
                ),
            )
        )
    )


def _artifact(case: CalibrationCase) -> ArtifactRef:
    payload = evidence_json_bytes(case.evidence)
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=f"calibration/{case.case_id}/evidence.json",
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _measurement(
    *,
    case: CalibrationCase,
    judge: JudgeKind,
    raw_response: str,
    protocol: JudgeProtocolMetadata,
    status: GradeStatus,
    human_label_version: str,
) -> CalibrationMeasurement:
    return CalibrationMeasurement(
        case_id=case.case_id,
        assertion_id=case.rubric.assertion_id,
        judge=judge,
        human_label_version=human_label_version,
        human_status=case.label.status,
        judge_status=status,
        raw_fixed_response=raw_response,
        evidence_sha256=evidence_sha256(case.evidence),
        rubric_sha256=protocol.rubric_sha256,
        prompt_sha256=protocol.prompt_sha256,
        extractor_sha256=protocol.extractor_sha256,
        judge_model_id=protocol.judge_model_id,
        model_lock_version=protocol.model_lock_version,
        response_source=protocol.response_source,
        model_config_sha256=protocol.model_config_sha256,
        model_protocol_sha256=protocol.model_protocol_sha256,
        protocol_sha256=protocol.protocol_sha256,
    )


async def execute_fixed_calibration(
    fixture: CalibrationFixture,
) -> CalibrationReport:
    """Actually run both parsers over raw fixed responses, then measure agreement."""

    results: dict[tuple[str, JudgeKind], AssertionResult] = {}
    measurements: list[CalibrationMeasurement] = []
    for case in fixture.cases:
        artifact = _artifact(case)
        llm_run = await judge_llm(
            BoundJudgeEngine.from_fake(_fake_engine(case.fixed_responses.llm)),
            rubric=case.rubric,
            evidence=case.evidence,
            evidence_artifact=artifact,
        )
        agent_run = await judge_agent(
            AgentJudgeEngine.from_fake(_fake_engine(case.fixed_responses.agent)),
            rubric=case.rubric,
            evidence=case.evidence,
            evidence_artifact=artifact,
        )
        for judge, run, raw_response in (
            (JudgeKind.LLM, llm_run, case.fixed_responses.llm),
            (JudgeKind.AGENT, agent_run, case.fixed_responses.agent),
        ):
            results[(case.case_id, judge)] = run.assertion
            measurements.append(
                _measurement(
                    case=case,
                    judge=judge,
                    raw_response=raw_response,
                    protocol=run.protocol,
                    status=run.assertion.status,
                    human_label_version=fixture.human_label_version,
                )
            )
    predictions = observations_from_assertions(results)
    reports, disagreements = _summarize(
        tuple(case.label for case in fixture.cases), predictions
    )
    return CalibrationReport(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.JUDGE_CALIBRATION,
        dataset_id=fixture.dataset_id,
        dataset_version=fixture.dataset_version,
        human_label_version=fixture.human_label_version,
        source=fixture.source,
        measurement_context=fixture.measurement_context,
        response_source=fixture.response_source,
        label_review_status=fixture.cases[0].label.review_status,
        sample_size=len(fixture.cases),
        judges=reports,
        disagreements=disagreements,
        measurements=tuple(measurements),
    )
