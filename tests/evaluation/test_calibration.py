from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from ses.cli.judge_calibration import main
from ses.evaluation.calibration import (
    CalibrationFixture,
    execute_fixed_calibration,
    load_calibration_fixture,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "judges" / "calibration.json"


def test_fixed_calibration_set_is_explicitly_human_reviewed() -> None:
    fixture = load_calibration_fixture(FIXTURE)

    assert fixture.dataset_id == "lesson-03-human-review-v2"
    assert fixture.live_model_measured is False
    assert fixture.cases
    assert {case.label.review_status for case in fixture.cases} == {"human_reviewed"}
    assert not hasattr(fixture.cases[0], "llm_status")
    assert not hasattr(fixture.cases[0], "agent_status")


def test_calibration_reports_confusion_matrices_disagreements_and_actual_agreement() -> (
    None
):
    report = asyncio.run(execute_fixed_calibration(load_calibration_fixture(FIXTURE)))
    llm = next(item for item in report.judges if item.judge.value == "llm")
    agent = next(item for item in report.judges if item.judge.value == "agent")

    assert report.measured is True
    assert report.fixed_offline_protocol_executed is True
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
    assert len(report.measurements) == 8
    assert {item.human_label_version for item in report.measurements} == {
        "human-labels-v1"
    }
    assert all(item.raw_fixed_response.startswith("{") for item in report.measurements)
    assert all(len(item.evidence_sha256) == 64 for item in report.measurements)
    assert all(len(item.protocol_sha256) == 64 for item in report.measurements)


def test_calibration_requires_both_raw_fixed_responses() -> None:
    fixture = load_calibration_fixture(FIXTURE)
    data = fixture.model_dump(mode="json")
    del data["cases"][0]["fixed_responses"]["agent"]

    with pytest.raises(ValueError):
        CalibrationFixture.model_validate(data)


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
    data["cases"][0]["label"]["review_status"] = "pending"

    with pytest.raises(ValueError):
        CalibrationFixture.model_validate(data)


def test_cli_executes_both_judges_before_measuring(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ses.evaluation import calibration

    calls = {"llm": 0, "agent": 0}
    real_llm = cast(Callable[..., Awaitable[object]], calibration.judge_llm)
    real_agent = cast(Callable[..., Awaitable[object]], calibration.judge_agent)

    async def tracked_llm(*args: object, **kwargs: object) -> object:
        calls["llm"] += 1
        return await real_llm(*args, **kwargs)

    async def tracked_agent(*args: object, **kwargs: object) -> object:
        calls["agent"] += 1
        return await real_agent(*args, **kwargs)

    monkeypatch.setattr(calibration, "judge_llm", tracked_llm)
    monkeypatch.setattr(calibration, "judge_agent", tracked_agent)

    assert main(["--fixture", str(FIXTURE)]) == 0
    capsys.readouterr()
    assert calls == {"llm": 4, "agent": 4}
