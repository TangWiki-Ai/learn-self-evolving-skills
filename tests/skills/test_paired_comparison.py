from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import ses.skills.paired as paired_module
from ses.contracts import RunnerStatus
from ses.foundation.config import ModelRole, ProviderId, load_model_lock
from ses.foundation.credentials import ProviderCredentials, read_siliconflow_credentials
from ses.runner import CaseEvaluation, LiveDevelopConfig, load_develop_catalog
from ses.runner.baseline import EvaluationContext
from ses.skills.paired import PairCategory, compare_run_events, run_fresh_paired

ROOT = Path(__file__).parents[2]


def _v0(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(
            ROOT / "fixtures" / "seed" / "skill" / "v0",
            tmp_path / "v0",
        )
    )


def test_paired_comparison_runs_fresh_isolated_sides_with_skill_as_only_variable(
    tmp_path: Path,
) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )

    assert len(comparison.cases) == 15
    assert {row.category for row in comparison.cases} == {PairCategory.BOTH_PASS}
    assert comparison.baseline_run_id != comparison.skill_run_id
    assert comparison.baseline_events.sha256
    assert comparison.skill_events.sha256
    assert comparison.pair_execution_sha256
    assert comparison.category_counts[PairCategory.FAIL_TO_PASS] == 0
    assert comparison.category_counts[PairCategory.PASS_TO_FAIL] == 0
    assert comparison.category_counts[PairCategory.BOTH_FAIL] == 0
    assert comparison.category_counts[PairCategory.BOTH_PASS] == 15
    assert all(row.baseline_status == row.skill_status for row in comparison.cases)
    assert comparison.skill_input_tokens == comparison.baseline_input_tokens
    assert comparison.skill_cost_amount == comparison.baseline_cost_amount
    assert all(row.baseline_trace and row.skill_trace for row in comparison.cases)
    assert all(
        row.baseline_state_diff and row.skill_state_diff for row in comparison.cases
    )
    assert all(row.baseline_grade and row.skill_grade for row in comparison.cases)
    workspaces = list((tmp_path / "paired").glob("run-*/workspaces/case-*/workspace"))
    assert len(workspaces) == 30


def test_paired_comparison_rejects_protocol_mismatch(tmp_path: Path) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    baseline_events = tmp_path / "paired" / comparison.baseline_run_id / "events.jsonl"
    skill_events = tmp_path / "paired" / comparison.skill_run_id / "events.jsonl"
    lines = skill_events.read_text(encoding="utf-8").splitlines()
    started = json.loads(lines[0])
    started["config"]["protocol_version"] = "incompatible-v2"
    lines[0] = json.dumps(started, sort_keys=True, separators=(",", ":"))
    skill_events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="protocol"):
        compare_run_events(
            baseline_events,
            skill_events,
            output_root=tmp_path / "paired",
            measurement_kind=comparison.measurement_kind,
            measured_at=comparison.measured_at,
            engine_version=comparison.engine_version,
            model_id=comparison.model_id,
        )


def test_paired_comparison_preserves_partial_infrastructure_evidence(
    tmp_path: Path,
) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    baseline_events = tmp_path / "paired" / comparison.baseline_run_id / "events.jsonl"
    skill_events = tmp_path / "paired" / comparison.skill_run_id / "events.jsonl"
    lines = baseline_events.read_text(encoding="utf-8").splitlines()
    attempt = json.loads(lines[1])
    attempt["status"] = "infrastructure_error"
    attempt["artifacts"]["traces"] = []
    attempt["artifacts"]["state_diff"] = None
    attempt["artifacts"]["grade"] = None
    attempt["error"] = "provider timeout"
    lines[1] = json.dumps(attempt, sort_keys=True, separators=(",", ":"))
    baseline_events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rebuilt = compare_run_events(
        baseline_events,
        skill_events,
        output_root=tmp_path / "paired",
        measurement_kind=comparison.measurement_kind,
        measured_at=comparison.measured_at,
        engine_version=comparison.engine_version,
        model_id=comparison.model_id,
    )

    row = rebuilt.cases[0]
    assert row.baseline_status.value == "infrastructure_error"
    assert row.baseline_trace is None
    assert row.baseline_state_diff is None
    assert row.baseline_grade is None


def test_paired_comparison_marks_provider_cost_flag_from_events_as_incomplete(
    tmp_path: Path,
) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    baseline_events = tmp_path / "paired" / comparison.baseline_run_id / "events.jsonl"
    skill_events = tmp_path / "paired" / comparison.skill_run_id / "events.jsonl"
    for events_path in (baseline_events, skill_events):
        lines = events_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            event = json.loads(line)
            if event["event_type"] == "attempt":
                event["cost_complete"] = False
                lines[index] = json.dumps(event, sort_keys=True, separators=(",", ":"))
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rebuilt = compare_run_events(
        baseline_events,
        skill_events,
        output_root=tmp_path / "paired",
        measurement_kind=comparison.measurement_kind,
        measured_at=comparison.measured_at,
        engine_version=comparison.engine_version,
        model_id=comparison.model_id,
    )

    assert rebuilt.baseline_cost_amount == 0
    assert rebuilt.skill_cost_amount == 0
    assert rebuilt.cost_currency == "CNY"
    assert rebuilt.cost_complete is False
    assert rebuilt.model_dump(mode="json")["cost_complete"] is False


def test_fixed_paired_comparison_is_byte_reproducible(tmp_path: Path) -> None:
    first = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "first",
        project_root=ROOT,
    )
    second = run_fresh_paired(
        skill_source=tmp_path / "v0",
        output_root=tmp_path / "second",
        project_root=ROOT,
    )

    assert first == second
    assert (tmp_path / "first" / first.baseline_events.path).read_bytes() == (
        tmp_path / "second" / second.baseline_events.path
    ).read_bytes()
    assert (tmp_path / "first" / first.skill_events.path).read_bytes() == (
        tmp_path / "second" / second.skill_events.path
    ).read_bytes()


def test_live_paired_rejects_pending_catalog_before_provider_call(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "provider-call-count.txt"
    executable = tmp_path / "counting-provider"
    executable.write_text(
        f"#!/bin/sh\nprintf x >> '{marker}'\nexit 99\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    lock = load_model_lock(ROOT / "models.lock.json")

    with pytest.raises(ValueError, match="independent signed human review"):
        run_fresh_paired(
            skill_source=_v0(tmp_path),
            output_root=tmp_path / "live-paired",
            project_root=ROOT,
            live_config=LiveDevelopConfig(
                model=lock.roles[ModelRole.MAIN],
                credentials=read_siliconflow_credentials(
                    {"SILICONFLOW_API_KEY": "must-not-be-used"}
                ),
                executable=str(executable),
                environ={},
                timeout_seconds=0.1,
            ),
        )

    assert not marker.exists()


def test_live_paired_binds_selected_lock_hash_and_currency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_develop_catalog(mode="fixed")

    class OfflineEvaluator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
            return CaseEvaluation(
                case_id=context.case_id,
                iteration_id=context.iteration_id,
                status=RunnerStatus.INFRASTRUCTURE_ERROR,
                turn_count=1,
                input_tokens=0,
                output_tokens=0,
                cost_currency=context.cost_currency,
                cost_complete=False,
            )

    monkeypatch.setattr(
        paired_module,
        "load_develop_catalog",
        lambda *, mode: catalog,
    )
    monkeypatch.setattr(paired_module, "DevelopCatalogEvaluator", OfflineEvaluator)
    lock = load_model_lock(ROOT / "models.chatanywhere.lock.json")
    selected_hash = "f" * 64

    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "live-paired",
        project_root=ROOT,
        live_config=LiveDevelopConfig(
            model=lock.roles[ModelRole.MAIN],
            credentials=ProviderCredentials(
                api_key="not-used",
                provider=ProviderId.CHATANYWHERE,
            ),
            executable="not-used",
            environ={},
            provider=ProviderId.CHATANYWHERE,
            model_lock_sha256=selected_hash,
            cost_currency="CNY",
        ),
    )

    assert comparison.model_lock_sha256 == selected_hash
    assert comparison.cost_currency == "CNY"
    assert comparison.cost_complete is False
    assert comparison.model_dump(mode="json")["cost_complete"] is False
