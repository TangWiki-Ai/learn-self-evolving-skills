from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from ses.cli import journey as journey_cli
from ses.cli.app import main
from ses.contracts.engine import Usage
from ses.foundation.config import ProviderId
from ses.journey import (
    ExperimentCostSource,
    ExperimentMode,
    JourneyProgressStatus,
    JourneyStateError,
    JourneyStatusStore,
)
from ses.journey.course import StationRun

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    station: int,
    *extra: str,
) -> tuple[int, dict[str, object]]:
    code = main(
        [
            "journey",
            "station",
            str(station),
            "--workspace",
            str(workspace),
            "--project-root",
            str(PROJECT_ROOT),
            "--mode",
            "fixed",
            "--json",
            *extra,
        ]
    )
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert isinstance(payload, dict)
    return code, payload


def test_fixed_ci_seam_runs_the_existing_cases_and_always_allows_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_API_KEY", "sk-do-not-persist-this-value")

    code, baseline = _run(tmp_path, capsys, 0)
    assert code == 0
    assert baseline["metrics"] == {
        "baseline_case_count": 15,
        "baseline_pass_count": 15,
        "baseline_pass_rate": 1.0,
        "experiment_mode": "fixed",
        "measurement_kind": "synthetic_offline",
        "no_skill_case_count": 5,
        "no_skill_pass_count": 5,
        "no_skill_pass_rate": 1.0,
        "no_skill_sample_label": "n=5 stratified sample; not the full develop set",
    }
    first_state = JourneyStatusStore(tmp_path).load()
    first_usage = first_state.experiment_usage
    station_0_paths = {
        reference.path for reference in first_state.stations[0].artifact_refs
    }
    assert any("/artifacts/" in path for path in station_0_paths)
    assert not any("/workspaces/" in path for path in station_0_paths)
    assert _run(tmp_path, capsys, 0)[0] == 0
    assert JourneyStatusStore(tmp_path).load().experiment_usage == first_usage

    assert _run(tmp_path, capsys, 1, "--select", "none")[0] == 0
    assert _run(tmp_path, capsys, 2)[0] == 0
    assert _run(tmp_path, capsys, 3)[0] == 0
    assert _run(tmp_path, capsys, 4, "--rationale", "No Skill issue found")[0] == 0
    gate_code, gate = _run(tmp_path, capsys, 5, "--decision", "hold")
    assert gate_code == 2
    assert gate["metrics"] == {
        "both_pass_count": 0,
        "candidate_changed": False,
        "candidate_pass_count": 0,
        "expected_regression_case_count": 15,
        "fail_to_pass_count": 0,
        "full_regression_ran": False,
        "gate_outcome": "rejected",
        "pass_to_fail_count": 0,
        "regression_case_count": 0,
        "regression_case_set_complete": False,
        "target_count": 0,
        "target_pass_count": 0,
        "target_regression_pass_count": 0,
    }
    assert _run(tmp_path, capsys, 6, "--action", "defer")[0] == 2
    summary_code, summary = _run(tmp_path, capsys, 7)
    assert summary_code == 0
    summary_metrics = cast(dict[str, object], summary["metrics"])
    assert summary_metrics["deliverable_count"] == 6
    assert cast(int, summary_metrics["evidence_index_count"]) > 19
    assert summary_metrics["evidence_status"] == "synthetic_ci_only"

    facts = json.loads(
        (tmp_path / ".ses/deliverables/evidence-facts.json").read_text(encoding="utf-8")
    )["facts"]
    assert facts["baseline_case_count"] == 15
    assert facts["baseline_pass_count"] == 15
    assert facts["measurement_kind"] == "synthetic_offline"
    assert facts["post_gate_pass_count"] is None
    assert facts["post_gate_pass_rate"] is None
    assert "STATE-Bench" in (tmp_path / ".ses/deliverables/resume-zh.md").read_text(
        encoding="utf-8"
    )
    assert "CI 合成证据草稿" in (tmp_path / ".ses/deliverables/resume-zh.md").read_text(
        encoding="utf-8"
    )
    assert "修后通过 15/15" not in (
        tmp_path / ".ses/deliverables/resume-zh.md"
    ).read_text(encoding="utf-8")
    state = JourneyStatusStore(tmp_path).load()
    assert state.experiment_mode.value == "fixed"
    assert state.experiment_provider is None
    assert state.model_lock_sha256 is None
    assert state.cost_source.value == "synthetic_ci"
    assert state.stations[7].status is JourneyProgressStatus.COMPLETED
    assert state.stations[5].status is JourneyProgressStatus.NEEDS_ATTENTION
    assert state.experiment_usage.cost_amount > 0
    for path in (tmp_path / ".ses").rglob("*"):
        if path.is_file():
            assert b"sk-do-not-persist-this-value" not in path.read_bytes()


def test_fixed_evidence_cannot_be_resumed_as_live(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(tmp_path, capsys, 0)[0] == 0

    code = main(
        [
            "journey",
            "station",
            "7",
            "--workspace",
            str(tmp_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--json",
        ]
    )
    output = capsys.readouterr()

    assert code == 1
    assert output.out == ""
    assert "mode differs" in output.err
    state = JourneyStatusStore(tmp_path).load()
    assert state.experiment_mode.value == "fixed"
    assert state.stations[7].status is JourneyProgressStatus.PENDING


def test_new_live_journey_requires_explicit_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "journey",
            "station",
            "0",
            "--workspace",
            str(tmp_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--json",
        ]
    )
    output = capsys.readouterr()

    assert code == 1
    assert output.out == ""
    assert "requires --provider" in output.err
    assert not (tmp_path / ".ses/status.json").exists()


def test_start_uses_configured_default_provider_without_prompt(
    tmp_path: Path,
) -> None:
    assert (
        journey_cli._start_provider(
            workspace=tmp_path,
            project_root=PROJECT_ROOT,
            requested_provider=None,
        )
        is ProviderId.SILICONFLOW
    )


def test_start_resume_keeps_the_persisted_provider(
    tmp_path: Path,
) -> None:
    JourneyStatusStore(tmp_path).initialize(
        experiment_mode=ExperimentMode.LIVE,
        experiment_provider=ProviderId.CHATANYWHERE,
        model_lock_sha256="0" * 64,
        cost_source=ExperimentCostSource.UNAVAILABLE,
    )

    assert (
        journey_cli._start_provider(
            workspace=tmp_path,
            project_root=PROJECT_ROOT,
            requested_provider=None,
        )
        is ProviderId.CHATANYWHERE
    )
    with pytest.raises(JourneyStateError, match="persisted"):
        journey_cli._start_provider(
            workspace=tmp_path,
            project_root=PROJECT_ROOT,
            requested_provider=ProviderId.SILICONFLOW.value,
        )


def test_start_runs_station_zero_with_the_default_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ProviderId] = []

    def fake_station_zero(**kwargs: object) -> StationRun:
        provider = kwargs["provider"]
        assert isinstance(provider, ProviderId)
        seen.append(provider)
        return StationRun(
            number=0,
            status="completed",
            artifacts=(),
            decisions=(),
            usage=Usage(input_tokens=0, output_tokens=0),
            metrics={"provider": provider.value},
        )

    monkeypatch.setattr(journey_cli, "run_station_0", fake_station_zero)
    code = main(
        [
            "journey",
            "start",
            "--workspace",
            str(tmp_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--json",
        ]
    )
    output = capsys.readouterr()

    assert code == 0
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["station"] == 0
    assert seen == [ProviderId.SILICONFLOW]
    assert (
        JourneyStatusStore(tmp_path).load().experiment_provider
        is ProviderId.SILICONFLOW
    )


def test_chatanywhere_selection_ignores_siliconflow_key_and_is_persisted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    siliconflow_secret = "sk-siliconflow-must-not-fallback-123456789"
    monkeypatch.setenv("SILICONFLOW_API_KEY", siliconflow_secret)
    monkeypatch.delenv("CHATANYWHERE_API_KEY", raising=False)

    code = main(
        [
            "journey",
            "station",
            "0",
            "--workspace",
            str(tmp_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--provider",
            "chatanywhere",
            "--json",
        ]
    )
    output = capsys.readouterr()

    assert code == 1
    assert output.out == ""
    assert "CHATANYWHERE_API_KEY" in output.err
    state = JourneyStatusStore(tmp_path).load()
    assert state.experiment_provider is not None
    assert state.experiment_provider.value == "chatanywhere"
    assert state.model_lock_sha256 is not None
    assert state.cost_source.value == "unavailable"
    assert state.experiment_usage.cost_complete is False
    for path in (tmp_path / ".ses").rglob("*"):
        if path.is_file():
            assert siliconflow_secret.encode() not in path.read_bytes()

    code = main(
        [
            "journey",
            "station",
            "7",
            "--workspace",
            str(tmp_path),
            "--project-root",
            str(PROJECT_ROOT),
            "--provider",
            "siliconflow",
            "--json",
        ]
    )
    output = capsys.readouterr()
    assert code == 1
    assert output.out == ""
    assert "provider differs" in output.err
