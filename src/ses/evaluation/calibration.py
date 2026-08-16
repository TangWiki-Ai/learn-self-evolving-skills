"""Human-label calibration for independent model-based judges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from ses.contracts import AssertionResult, ContractModel, GradeStatus, JudgeKind

_STATUS_ORDER = (
    GradeStatus.PASS,
    GradeStatus.FAIL,
    GradeStatus.NOT_EVALUATED,
    GradeStatus.ERROR,
)
_JUDGE_ORDER = (JudgeKind.LLM, JudgeKind.AGENT)


class HumanLabel(ContractModel):
    """One assertion label explicitly reviewed by a person."""

    case_id: str
    assertion_id: str
    status: GradeStatus
    review_status: Literal["human_reviewed"]

    @model_validator(mode="after")
    def _human_labels_are_not_infrastructure_errors(self) -> HumanLabel:
        if self.status is GradeStatus.ERROR:
            raise ValueError("human labels cannot use judge error")
        return self


class CalibrationObservation(ContractModel):
    """One fixed judge prediction used by an offline experiment."""

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


class CalibrationFixture(ContractModel):
    """Fixed human labels and fixed outputs for the offline course experiment."""

    dataset_id: str
    dataset_version: str
    source: str
    measurement_context: str
    live_model_measured: bool
    labels: tuple[HumanLabel, ...]
    predictions: tuple[CalibrationObservation, ...]

    @model_validator(mode="after")
    def _require_unique_rows(self) -> CalibrationFixture:
        label_keys = [(item.case_id, item.assertion_id) for item in self.labels]
        if not label_keys:
            raise ValueError("calibration requires at least one human label")
        if len(set(label_keys)) != len(label_keys):
            raise ValueError("human label keys must be unique")
        prediction_keys = [
            (item.judge, item.case_id, item.assertion_id) for item in self.predictions
        ]
        if len(set(prediction_keys)) != len(prediction_keys):
            raise ValueError("prediction keys must be unique")
        return self


class JudgeCalibration(ContractModel):
    """Measured agreement and full confusion matrix for one judge."""

    judge: JudgeKind
    agreements: int
    total: int
    agreement: Decimal
    confusion_matrix: Mapping[str, Mapping[str, int]]


class CalibrationDisagreement(ContractModel):
    """One traceable disagreement with the human-reviewed label."""

    case_id: str
    assertion_id: str
    judge: JudgeKind
    human_status: GradeStatus
    judge_status: GradeStatus
    judge_reason: str


class CalibrationReport(ContractModel):
    """Only actual fixture-relative measurements, never PRD targets."""

    dataset_id: str
    dataset_version: str
    source: str
    measurement_context: str
    measured: Literal[True] = True
    live_model_measured: bool
    sample_size: int
    judges: tuple[JudgeCalibration, ...]
    disagreements: tuple[CalibrationDisagreement, ...]


def load_calibration_fixture(path: Path) -> CalibrationFixture:
    """Load a strict fixed experiment fixture without environment or network access."""

    try:
        return CalibrationFixture.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read calibration fixture {path}: {exc}") from exc


def observations_from_assertions(
    results: Mapping[tuple[str, JudgeKind], AssertionResult],
) -> tuple[CalibrationObservation, ...]:
    """Adapt canonical judge outputs to calibration observations."""

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


def calibrate(
    labels: Sequence[HumanLabel],
    predictions: Sequence[CalibrationObservation],
    *,
    dataset_id: str,
    dataset_version: str,
    source: str,
    measurement_context: str,
    live_model_measured: bool,
) -> CalibrationReport:
    """Compare every judge output with every fixed human-reviewed label."""

    label_keys = [(label.case_id, label.assertion_id) for label in labels]
    if not label_keys:
        raise ValueError("calibration requires at least one human label")
    if len(set(label_keys)) != len(label_keys):
        raise ValueError("human label keys must be unique")
    prediction_map = {
        (item.judge, item.case_id, item.assertion_id): item for item in predictions
    }
    if len(prediction_map) != len(predictions):
        raise ValueError("prediction keys must be unique")

    expected_keys = {
        (judge, label.case_id, label.assertion_id)
        for judge in _JUDGE_ORDER
        for label in labels
    }
    missing = expected_keys - set(prediction_map)
    if missing:
        judge, case_id, assertion_id = sorted(
            missing, key=lambda item: (_JUDGE_ORDER.index(item[0]), item[1], item[2])
        )[0]
        raise ValueError(
            f"missing prediction for {judge.value}:{case_id}:{assertion_id}"
        )
    extra = set(prediction_map) - expected_keys
    if extra:
        raise ValueError("prediction does not match a human label")

    judge_reports: list[JudgeCalibration] = []
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
        total = len(labels)
        judge_reports.append(
            JudgeCalibration(
                judge=judge,
                agreements=agreements,
                total=total,
                agreement=Decimal(agreements) / Decimal(total),
                confusion_matrix=matrix,
            )
        )
    return CalibrationReport(
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        source=source,
        measurement_context=measurement_context,
        live_model_measured=live_model_measured,
        sample_size=len(labels),
        judges=tuple(judge_reports),
        disagreements=tuple(disagreements),
    )


def run_fixture_calibration(fixture: CalibrationFixture) -> CalibrationReport:
    """Run the deterministic agreement calculation for a fixed fixture."""

    return calibrate(
        fixture.labels,
        fixture.predictions,
        dataset_id=fixture.dataset_id,
        dataset_version=fixture.dataset_version,
        source=fixture.source,
        measurement_context=fixture.measurement_context,
        live_model_measured=fixture.live_model_measured,
    )
