from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ses.cli.judge_calibration import main
from ses.evaluation.calibration import (
    CalibrationFixture,
    load_calibration_fixture,
    run_fixture_calibration,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "judges" / "calibration.json"


def test_fixed_calibration_set_is_explicitly_human_reviewed() -> None:
    fixture = load_calibration_fixture(FIXTURE)

    assert fixture.dataset_id == "lesson-03-human-review-v1"
    assert fixture.live_model_measured is False
    assert fixture.labels
    assert {label.review_status for label in fixture.labels} == {"human_reviewed"}


def test_calibration_reports_confusion_matrices_disagreements_and_actual_agreement() -> (
    None
):
    report = run_fixture_calibration(load_calibration_fixture(FIXTURE))
    llm = next(item for item in report.judges if item.judge.value == "llm")
    agent = next(item for item in report.judges if item.judge.value == "agent")

    assert report.measured is True
    assert report.live_model_measured is False
    assert (llm.agreements, llm.total, llm.agreement) == (2, 4, Decimal("0.5"))
    assert (agent.agreements, agent.total, agent.agreement) == (
        3,
        4,
        Decimal("0.75"),
    )
    assert llm.confusion_matrix["not_evaluated"]["pass"] == 1
    assert agent.confusion_matrix["not_evaluated"]["not_evaluated"] == 1
    assert [(item.judge.value, item.case_id) for item in report.disagreements] == [
        ("llm", "cal-003"),
        ("llm", "cal-004"),
        ("agent", "cal-004"),
    ]


def test_calibration_rejects_missing_predictions_instead_of_guessing() -> None:
    fixture = load_calibration_fixture(FIXTURE)
    incomplete = fixture.model_copy(update={"predictions": fixture.predictions[:-1]})

    with pytest.raises(ValueError, match="missing prediction"):
        run_fixture_calibration(incomplete)


def test_calibration_cli_emits_measured_results_without_target_claims(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--fixture", str(FIXTURE)]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["measured"] is True
    assert payload["live_model_measured"] is False
    assert "target" not in output.lower()
    assert "accuracy_gain" not in output.lower()


def test_fixture_rejects_unreviewed_human_labels() -> None:
    fixture = load_calibration_fixture(FIXTURE)
    data = fixture.model_dump(mode="json")
    data["labels"][0]["review_status"] = "pending"

    with pytest.raises(ValueError):
        CalibrationFixture.model_validate(data)
