from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

import ses.journey.course as course
from ses.journey.course import (
    JourneyCourseError,
    journey_usage_from_reports,
    run_station_0,
    run_station_4,
    run_station_7,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_summary(
    workspace: Path, station: int, metrics: object, **extra: object
) -> None:
    path = workspace / f".ses/evidence/station-{station}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "artifact_paths": [],
                "decision_paths": [],
                "metrics": metrics,
                "mode": "live" if station == 0 else None,
                "record_type": "journey_station_summary",
                "schema_version": "v1alpha1",
                "station": station,
                **extra,
            }
        ),
        encoding="utf-8",
    )


def _assert_index_hashes(workspace: Path) -> None:
    index = _json(workspace / ".ses/deliverables/evidence-index.json")
    artifacts = index["artifacts"]
    assert isinstance(artifacts, list)
    for item in artifacts:
        assert isinstance(item, dict)
        path = workspace / cast(str, item["path"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]


def test_usage_rollup_preserves_unavailable_provider_cost(tmp_path: Path) -> None:
    for run_id, totals in (
        (
            "run-unpriced",
            {
                "input_tokens": 10,
                "output_tokens": 4,
                "cost_amount": "0",
                "cost_currency": "CNY",
                "cost_complete": False,
            },
        ),
        (
            "run-priced",
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "cost_amount": "0.25",
                "cost_currency": "CNY",
            },
        ),
    ):
        path = tmp_path / f".ses/runs/{run_id}/baseline-report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"totals": totals}), encoding="utf-8")

    usage = journey_usage_from_reports(tmp_path)

    assert usage is not None
    assert usage.input_tokens == 13
    assert usage.output_tokens == 6
    assert usage.cost_amount is None
    assert usage.cost_currency is None


def test_station_7_keeps_missing_evidence_null_and_is_idempotent(
    tmp_path: Path,
) -> None:
    first = run_station_7(workspace=tmp_path)

    assert first.status == "completed"
    facts = cast(
        dict[str, object],
        _json(tmp_path / ".ses/deliverables/evidence-facts.json")["facts"],
    )
    assert facts["baseline_case_count"] is None
    assert facts["post_gate_pass_count"] is None
    assert facts["pass_to_fail_count"] is None
    assert facts["portfolio_status"] == "draft_missing_baseline"
    resume = (tmp_path / ".ses/deliverables/resume-zh.md").read_text(encoding="utf-8")
    assert "尚未获得基线" in resume
    assert "0/0" not in resume
    station_summary = _json(tmp_path / ".ses/evidence/station-7.json")
    artifact_paths = station_summary["artifact_paths"]
    assert isinstance(artifact_paths, list)
    assert artifact_paths[:2] == [
        ".ses/deliverables/evidence-facts.json",
        ".ses/deliverables/evidence-index.json",
    ]
    _assert_index_hashes(tmp_path)

    run_station_7(workspace=tmp_path)
    _assert_index_hashes(tmp_path)


def test_station_7_reports_only_a_complete_full_regression(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        0,
        {
            "baseline_case_count": 2,
            "baseline_pass_count": 1,
            "experiment_mode": "live",
            "measurement_kind": "live_measured",
        },
    )
    _write_summary(
        tmp_path,
        4,
        {"candidate_round": 1, "changed_line_count": 3},
    )
    _write_summary(
        tmp_path,
        5,
        {
            "candidate_pass_count": 2,
            "full_regression_ran": True,
            "gate_outcome": "accepted",
            "pass_to_fail_count": 0,
            "regression_case_count": 2,
        },
    )
    _write_summary(tmp_path, 6, {"current_version": "v1", "release_completed": True})

    run_station_7(workspace=tmp_path)

    facts = cast(
        dict[str, object],
        _json(tmp_path / ".ses/deliverables/evidence-facts.json")["facts"],
    )
    assert facts["post_gate_pass_count"] == 2
    assert facts["post_gate_pass_rate"] == 1.0
    assert facts["portfolio_status"] == "verified_released"
    resume = (tmp_path / ".ses/deliverables/resume-zh.md").read_text(encoding="utf-8")
    assert "全量回归结果为 2/2" in resume
    assert "锁定模型的真实评测" in resume


def test_station_4_rejects_credentials_before_persisting_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-sensitive-course-value-1234567890"
    monkeypatch.setenv("SILICONFLOW_API_KEY", secret)
    run_station_0(
        workspace=tmp_path,
        project_root=PROJECT_ROOT,
        mode="fixed",
        timeout=30,
    )

    with pytest.raises(JourneyCourseError, match="credential-like") as error:
        run_station_4(workspace=tmp_path, rationale=secret)

    assert secret not in str(error.value)
    assert not (tmp_path / ".ses/current-candidate.json").exists()
    for path in (tmp_path / ".ses").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()

    working = tmp_path / ".ses/skills/working/SKILL.md"
    working.write_text(
        working.read_text(encoding="utf-8") + f"\n{secret}\n",
        encoding="utf-8",
    )
    with pytest.raises(JourneyCourseError, match="credential-like"):
        run_station_4(workspace=tmp_path, rationale="explain the minimal change")

    assert not (tmp_path / ".ses/current-candidate.json").exists()
    assert not (tmp_path / ".ses/candidates").exists()
    assert not (tmp_path / ".ses/reports/station-4-diff.html").exists()


def test_station_0_does_not_seal_an_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_run_catalog(**kwargs: object) -> tuple[dict[str, object], Path, Path]:
        nonlocal calls
        calls += 1
        run_root = cast(Path, kwargs["run_root"])
        run_id = cast(str, kwargs["run_id"])
        case_ids = cast(Sequence[str], kwargs["case_ids"])
        run_directory = run_root / run_id
        run_directory.mkdir(parents=True, exist_ok=True)
        status = "infrastructure_error" if calls == 1 else "pass"
        report: dict[str, object] = {
            "cases": [
                {"case_id": case_id, "first_status": status} for case_id in case_ids
            ],
            "totals": {
                "cost_amount": "0",
                "cost_currency": "CNY",
                "input_tokens": 0,
                "output_tokens": 0,
            },
        }
        report_path = run_directory / "baseline-report.json"
        html_path = run_directory / "l1.html"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        html_path.write_text("<!doctype html><title>run</title>", encoding="utf-8")
        return report, report_path, html_path

    monkeypatch.setattr(course, "_run_catalog", fake_run_catalog)

    failed = run_station_0(
        workspace=tmp_path,
        project_root=PROJECT_ROOT,
        mode="fixed",
        timeout=30,
    )
    assert failed.status == "needs_attention"
    assert "retry station 0" in cast(str, failed.reason)
    assert not (tmp_path / ".ses/evidence/station-0.json").exists()

    recovered = run_station_0(
        workspace=tmp_path,
        project_root=PROJECT_ROOT,
        mode="fixed",
        timeout=30,
    )
    assert recovered.status == "completed"
    assert calls == 3
    assert (tmp_path / ".ses/evidence/station-0.json").is_file()
