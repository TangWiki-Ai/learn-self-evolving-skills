from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ses.automation import portfolio as portfolio_module
from ses.automation.portfolio import (
    PortfolioExportError,
    export_portfolio,
    portfolio_semantic_sha256,
)
from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    AutoEvolveState,
    AutoLoopStatus,
    AutoRoundRecord,
    AutoStopReason,
    FailureCategory,
    FinalAggregateReport,
    GateDecision,
    MeasurementKind,
    RegistryEvent,
    SchemaVersion,
    ShoppingFinalScenarioMetrics,
    VersionStatus,
)
from ses.contracts.shopping import ShoppingScenario
from ses.evolution.registry import RegistryState, RegistryVersion
from ses.reporting.l3 import L3ReportInputs, build_l3_data, render_l3_html
from ses.skills.installer import normalized_skill_sha256, write_skill_manifest

ROOT = Path(__file__).parents[2]
ARTIFACTS = ROOT / "course/ch09-gate-and-govern-versions/artifacts"
NOW = datetime(2026, 8, 19, 9, tzinfo=UTC)


def _events(root: Path) -> tuple[RegistryEvent, ...]:
    return tuple(
        RegistryEvent.model_validate_json(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )


def _decision(root: Path) -> GateDecision:
    path = next((root / "gates").glob("gate-*/gate-decision.json"))
    return GateDecision.model_validate_json(path.read_bytes())


def _ref(path: str, sha256: str = "0" * 64) -> ArtifactRef:
    return ArtifactRef(root=ArtifactRoot.WORKSPACE, path=path, sha256=sha256)


def _accepted_skill_with_member(
    tmp_path: Path,
    *,
    relative: str,
    payload: bytes,
) -> tuple[Path, str]:
    source = tmp_path / "accepted-skill-source"
    source.mkdir()
    (source / "references").mkdir()
    (source / "SKILL.md").write_text(
        "# Public portfolio test Skill\n\nUse only the supplied request.\n",
        encoding="utf-8",
    )
    (source / "references/public-guidance.md").write_text(
        "# Public guidance\n\nAsk before acting.\n",
        encoding="utf-8",
    )
    target = source / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    files: tuple[str, ...] = ("SKILL.md", "references/public-guidance.md")
    if relative not in files:
        files = (*files, relative)
    write_skill_manifest(
        source,
        name="portfolio-test-skill",
        version="v1",
        files=files,
    )
    return source, normalized_skill_sha256(source)


def _assert_skill_member_rejected(
    tmp_path: Path,
    *,
    relative: str,
    payload: bytes,
    match: str,
) -> None:
    source, skill_sha256 = _accepted_skill_with_member(
        tmp_path,
        relative=relative,
        payload=payload,
    )
    staging = tmp_path / "portfolio-staging"
    staging.mkdir()

    with pytest.raises(PortfolioExportError, match=match):
        portfolio_module._copy_accepted_skill(
            staging,
            source=source,
            expected_sha256=skill_sha256,
        )

    assert not (staging / "accepted-skill" / relative).exists()


def _inputs(*, accepted: bool, final: bool = False) -> L3ReportInputs:
    name = "fixed-accept-promote-rollback" if accepted else "fixed-rejection"
    root = ARTIFACTS / name
    events = _events(root)
    decision = _decision(root)
    initial_event = events[0]
    candidate_event = events[1]
    candidate_status = VersionStatus.ACCEPTED if accepted else VersionStatus.REJECTED
    versions = {
        initial_event.version_sha256: RegistryVersion(
            version_id=initial_event.version_id,
            skill_sha256=initial_event.version_sha256,
            parent_skill_sha256=None,
            status=VersionStatus.ACCEPTED,
            manifest=initial_event.version_manifest,
            candidate=None,
            gate_decision=None,
            evidence=initial_event.evidence,
            verified=True,
            was_current=True,
        ),
        candidate_event.version_sha256: RegistryVersion(
            version_id=candidate_event.version_id,
            skill_sha256=candidate_event.version_sha256,
            parent_skill_sha256=initial_event.version_sha256,
            status=candidate_status,
            manifest=candidate_event.version_manifest,
            candidate=candidate_event.candidate,
            gate_decision=events[2].gate_decision,
            evidence=events[2].evidence,
            verified=accepted,
            was_current=accepted,
        ),
    }
    current = (
        candidate_event.version_sha256 if accepted else initial_event.version_sha256
    )
    registry_events = events[:4] if accepted else events
    registry = RegistryState(
        registry_id=initial_event.registry_id,
        lineage_id=initial_event.lineage_id,
        current_accepted_sha256=current,
        versions=versions,
        events=registry_events,
    )
    decision_ref = events[2].gate_decision
    assert decision_ref is not None
    record = AutoRoundRecord(
        round_number=1,
        parent_skill_sha256=decision.accepted_skill_sha256,
        candidate_id=decision.candidate_id,
        candidate_skill_sha256=decision.candidate_skill_sha256,
        rollout=_ref("rounds/round-001/rollout.json"),
        candidate=_ref("rounds/round-001/candidate/candidate.json"),
        gate_decision=_ref(
            "registry/gates/gate-round-001/gate-decision.json",
            decision_ref.sha256,
        ),
        gate_outcome=decision.outcome,
        promoted=accepted,
        quality_delta=decision.metrics.quality_delta,
        cost_amount=decision.metrics.total_cost_amount,
        cost_currency=decision.metrics.cost_currency,
        cost_complete=decision.metrics.cost_complete,
        input_tokens=decision.metrics.total_input_tokens,
        output_tokens=decision.metrics.total_output_tokens,
        failure_categories=(FailureCategory.SAFETY,),
        patch_targets=("SKILL.md",),
    )
    final_report = None
    final_ref = None
    status = AutoLoopStatus.STOPPED
    if final:
        status = AutoLoopStatus.FINAL_COMPLETE
        final_report = FinalAggregateReport(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="final_aggregate_report",
            experiment_id="experiment-l3-fixed",
            subject_skill_sha256=current,
            final_lock_sha256="f" * 64,
            mode="fixed",
            measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
            network_used=False,
            result_source="fixed_reference",
            executed_at=NOW,
            case_count=12,
            pass_count=9,
            pass_rate=0.75,
            cost_amount=Decimal(0),
            cost_currency="CNY",
            cost_complete=True,
            input_tokens=0,
            output_tokens=0,
            private_results_sha256="8" * 64,
        )
        final_ref = _ref("final/final-aggregate.json", "7" * 64)
    state = AutoEvolveState(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="auto_evolve_state",
        experiment_id="experiment-l3-fixed",
        config_sha256="c" * 64,
        status=status,
        current_accepted_skill_sha256=current,
        completed_rounds=1,
        rounds=(record,),
        total_cost_amount=record.cost_amount,
        cost_currency=record.cost_currency,
        cost_complete=record.cost_complete,
        total_input_tokens=record.input_tokens,
        total_output_tokens=record.output_tokens,
        consecutive_rejections=0 if accepted else 1,
        stopped_at=NOW,
        stop_reason=AutoStopReason.MAX_ROUNDS,
        final_report=final_ref,
    )
    return L3ReportInputs(
        state=state,
        registry=registry,
        decisions=(decision,),
        final_report=final_report,
    )


def _shopping_gate_inputs() -> L3ReportInputs:
    legacy = _inputs(accepted=True)
    decision = legacy.decisions[0]
    metrics = decision.metrics.model_copy(
        update={
            "selection_case_count": 8,
            "accepted_pass_count": 5,
            "candidate_pass_count": 6,
            "accepted_pass_rate": 0.625,
            "candidate_pass_rate": 0.75,
            "quality_delta": 0.125,
            "critical_regression_count": 0,
            "accepted_full_success_count": 5,
            "candidate_full_success_count": 6,
            "accepted_mean_strict_reward": Decimal("0.625"),
            "candidate_mean_strict_reward": Decimal("0.750"),
            "accepted_safety_violation_count": 0,
            "candidate_safety_violation_count": 0,
        }
    )
    shopping_decision = decision.model_copy(
        update={
            "schema_version": SchemaVersion.V1ALPHA2,
            "metrics": metrics,
        }
    )
    record = legacy.state.rounds[0].model_copy(
        update={"quality_delta": metrics.quality_delta}
    )
    state = legacy.state.model_copy(update={"rounds": (record,)})
    return replace(
        legacy,
        state=state,
        decisions=(shopping_decision,),
    )


def _shopping_final_inputs() -> L3ReportInputs:
    inputs = _shopping_gate_inputs()
    legacy_final_inputs = _inputs(accepted=True, final=True)
    final = legacy_final_inputs.final_report
    assert final is not None
    scenario_metrics = (
        ShoppingFinalScenarioMetrics(
            scenario=ShoppingScenario.SINGLE,
            case_count=3,
            full_success_count=3,
            mean_strict_reward=Decimal("0.900"),
            safety_violation_count=0,
        ),
        ShoppingFinalScenarioMetrics(
            scenario=ShoppingScenario.SINGLE_PERSONA,
            case_count=3,
            full_success_count=3,
            mean_strict_reward=Decimal("0.800"),
            safety_violation_count=0,
        ),
        ShoppingFinalScenarioMetrics(
            scenario=ShoppingScenario.MULTI,
            case_count=3,
            full_success_count=2,
            mean_strict_reward=Decimal("0.600"),
            safety_violation_count=0,
        ),
        ShoppingFinalScenarioMetrics(
            scenario=ShoppingScenario.MULTI_PERSONA,
            case_count=3,
            full_success_count=2,
            mean_strict_reward=Decimal("0.700"),
            safety_violation_count=1,
        ),
    )
    shopping_final = final.model_copy(
        update={
            "schema_version": SchemaVersion.V1ALPHA2,
            "result_source": "fresh_fixed_execution",
            "pass_count": 10,
            "pass_rate": 10 / 12,
            "full_success_count": 10,
            "mean_strict_reward": Decimal("0.750"),
            "safety_violation_count": 1,
            "scenario_metrics": scenario_metrics,
        }
    )
    state = inputs.state.model_copy(
        update={
            "status": AutoLoopStatus.FINAL_COMPLETE,
            "final_report": legacy_final_inputs.state.final_report,
        }
    )
    return replace(inputs, state=state, final_report=shopping_final)


def test_l3_shopping_gate_shows_full_success_strict_and_safety() -> None:
    inputs = _shopping_gate_inputs()

    payload = build_l3_data(inputs)
    rendered = render_l3_html(inputs)

    assert payload["schema_version"] == "v1alpha2"
    rounds = payload["rounds"]
    assert isinstance(rounds, list)
    assert isinstance(rounds[0], dict)
    selection = rounds[0]["selection"]
    assert isinstance(selection, dict)
    assert selection == {
        "case_count": 8,
        "accepted_pass_rate": 0.625,
        "candidate_pass_rate": 0.75,
        "quality_delta": 0.125,
        "critical_regressions": 0,
        "accepted_full_success_count": 5,
        "candidate_full_success_count": 6,
        "accepted_mean_strict_reward": "0.625",
        "candidate_mean_strict_reward": "0.750",
        "accepted_safety_violation_count": 0,
        "candidate_safety_violation_count": 0,
    }
    assert "Full success" in rendered and "5 / 8 → 6 / 8" in rendered
    assert "Mean strict reward" in rendered and "0.625 → 0.750" in rendered
    assert "Safety violations" in rendered and "0 → 0" in rendered


def test_l3_shopping_final_shows_total_and_four_three_case_scenarios() -> None:
    inputs = _shopping_final_inputs()

    payload = build_l3_data(inputs)
    rendered = render_l3_html(inputs)

    final = payload["final_aggregate"]
    assert isinstance(final, dict)
    assert final["full_success_count"] == 10
    assert final["mean_strict_reward"] == "0.750"
    assert final["safety_violation_count"] == 1
    assert final["scenario_metrics"] == [
        {
            "scenario": "single",
            "case_count": 3,
            "full_success_count": 3,
            "mean_strict_reward": "0.900",
            "safety_violation_count": 0,
        },
        {
            "scenario": "single_persona",
            "case_count": 3,
            "full_success_count": 3,
            "mean_strict_reward": "0.800",
            "safety_violation_count": 0,
        },
        {
            "scenario": "multi",
            "case_count": 3,
            "full_success_count": 2,
            "mean_strict_reward": "0.600",
            "safety_violation_count": 0,
        },
        {
            "scenario": "multi_persona",
            "case_count": 3,
            "full_success_count": 2,
            "mean_strict_reward": "0.700",
            "safety_violation_count": 1,
        },
    ]
    assert "Final full success" in rendered and "10 / 12" in rendered
    assert "Final mean strict reward" in rendered and "0.750" in rendered
    assert "Final safety violations" in rendered and ">1<" in rendered
    assert "Scenario final aggregates" in rendered
    for scenario in ShoppingScenario:
        assert f"{scenario.value} · 3 cases" in rendered


def test_l3_legacy_v1alpha1_omits_shopping_projection_fields() -> None:
    inputs = _inputs(accepted=True, final=True)

    payload = build_l3_data(inputs)
    rendered = render_l3_html(inputs)

    assert payload["schema_version"] == "v1alpha1"
    rounds = payload["rounds"]
    assert isinstance(rounds, list) and isinstance(rounds[0], dict)
    selection = rounds[0]["selection"]
    assert isinstance(selection, dict)
    final = payload["final_aggregate"]
    assert isinstance(final, dict)
    shopping_keys = {
        "accepted_full_success_count",
        "candidate_full_success_count",
        "accepted_mean_strict_reward",
        "candidate_mean_strict_reward",
        "accepted_safety_violation_count",
        "candidate_safety_violation_count",
        "full_success_count",
        "mean_strict_reward",
        "safety_violation_count",
        "scenario_metrics",
    }
    assert shopping_keys.isdisjoint(selection)
    assert shopping_keys.isdisjoint(final)
    assert "Full success" not in rendered
    assert "Mean strict reward" not in rendered
    assert "Safety violations" not in rendered


def test_l3_shows_rejected_branch_and_capability_cost_curve() -> None:
    inputs = _inputs(accepted=False)

    payload = build_l3_data(inputs)
    rendered = render_l3_html(inputs)
    lowered = rendered.casefold()

    assert payload["result_kind"] == "fixed_offline_reference"
    assert payload["accepted_capability_curve"] == [
        inputs.decisions[0].metrics.accepted_pass_rate
    ]
    protocol = payload["protocol"]
    assert isinstance(protocol, dict)
    assert (
        protocol["selection_lock_sha256"] == inputs.decisions[0].selection_lock_sha256
    )
    assert protocol["model_lock_sha256"] == inputs.decisions[0].model_lock_sha256
    assert "version dag and rejected branches" in lowered
    assert "capability and cost curve" in lowered
    assert "locked protocol provenance" in lowered
    assert 'class="node rejected"' in lowered
    assert "develop quality score" in lowered
    assert "http://" not in lowered and "https://" not in lowered
    assert "<script src=" not in lowered and "<link rel=" not in lowered
    assert str(ROOT) not in rendered
    assert len(rendered.encode()) < 2_000_000


def test_l3_keeps_final_aggregate_out_of_the_loop_curve() -> None:
    inputs = _inputs(accepted=True, final=True)

    payload = build_l3_data(inputs)
    rendered = render_l3_html(inputs)

    final = payload["final_aggregate"]
    assert isinstance(final, dict)
    assert final["case_count"] == 12 and final["pass_rate"] == 0.75
    assert final["isolation"] == "one_time_post_loop_aggregate_not_used_for_patch"
    assert "Final aggregate — isolated after the loop" in rendered
    assert "Final is intentionally excluded" in rendered
    assert "case_passes" not in rendered and "per-case" not in rendered.casefold()


def test_portfolio_exports_only_public_aggregates_and_accepted_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(accepted=True, final=True)
    experiment = tmp_path / "experiment"
    registry = experiment / "registry"
    source = (
        ARTIFACTS
        / "fixed-accept-promote-rollback/versions"
        / inputs.state.current_accepted_skill_sha256
    )
    shutil.copytree(
        source,
        registry / "versions" / inputs.state.current_accepted_skill_sha256,
    )
    monkeypatch.setattr(
        portfolio_module, "load_l3_inputs", lambda *args, **kwargs: inputs
    )
    output = tmp_path / "portfolio"

    manifest = export_portfolio(
        experiment,
        output,
        created_at=NOW,
        registry_root=registry,
    )

    assert portfolio_semantic_sha256(output) == portfolio_semantic_sha256(output)
    paths = {row.path for row in manifest.files}
    assert {
        "accepted-skill/SKILL.md",
        "accepted-skill/skill-manifest.json",
        "registry/events-public.json",
        "gate-projections/round-001.json",
        "loop-state.json",
        "l3.html",
        "final-aggregate.json",
        "architecture.md",
        "system-summary.md",
    } <= paths
    assert not any("private" in path or "gold" in path for path in paths)
    joined = "\n".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()
    ).casefold()
    assert "private/selection" not in joined
    assert "private_results_sha256" not in joined
    assert "accepted-events.jsonl" not in joined
    assert "candidate-events.jsonl" not in joined
    assert str(ROOT).casefold() not in joined
    assert "case_passes" not in joined


def test_portfolio_rejects_a_credential_in_a_public_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(accepted=True)
    credential = "".join(("s", "k", "-", "testcredential123456"))
    last = inputs.registry.events[-1].model_copy(
        update={"reason": credential, "event_sha256": "0" * 64}
    )
    tampered = replace(
        inputs,
        registry=replace(
            inputs.registry,
            events=(*inputs.registry.events[:-1], last),
        ),
    )
    experiment = tmp_path / "experiment"
    registry = experiment / "registry"
    source = (
        ARTIFACTS
        / "fixed-accept-promote-rollback/versions"
        / inputs.state.current_accepted_skill_sha256
    )
    shutil.copytree(
        source,
        registry / "versions" / inputs.state.current_accepted_skill_sha256,
    )
    monkeypatch.setattr(
        portfolio_module, "load_l3_inputs", lambda *args, **kwargs: tampered
    )

    with pytest.raises(PortfolioExportError, match="credential"):
        export_portfolio(
            experiment,
            tmp_path / "portfolio",
            created_at=NOW,
            registry_root=registry,
        )


@pytest.mark.parametrize(
    ("relative", "payload", "match"),
    [
        (
            "references/nul.md",
            b"safe prefix\x00hidden suffix\n",
            "NUL",
        ),
        (
            "references/non-utf8.md",
            b"invalid utf-8: \xff\n",
            "UTF-8",
        ),
        (
            "references/local-path.md",
            b"Read /Users/example/project/private.json before replying.\n",
            "credential or local path",
        ),
        (
            "references/hidden-marker.md",
            b"Use hidden_answer.json for this case.\n",
            "private evaluation data",
        ),
        (
            "references/private-marker.md",
            b"Load private/selection-case.json before answering.\n",
            "private evaluation data",
        ),
        (
            "references/final-marker.md",
            b"Read final-manifest.json before proposing a patch.\n",
            "private evaluation data",
        ),
        (
            "references/selection-marker.md",
            b"Memorize selection-case.json before evaluation.\n",
            "private evaluation data",
        ),
        (
            "references/opaque.bin",
            b"otherwise harmless UTF-8 text\n",
            "file extension",
        ),
    ],
)
def test_portfolio_rejects_unsafe_accepted_skill_members(
    tmp_path: Path,
    relative: str,
    payload: bytes,
    match: str,
) -> None:
    _assert_skill_member_rejected(
        tmp_path,
        relative=relative,
        payload=payload,
        match=match,
    )


def test_portfolio_rejects_an_environment_secret_in_the_accepted_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "portfolio-process-secret-9843"
    monkeypatch.setenv("PORTFOLIO_TEST_API_KEY", secret)

    _assert_skill_member_rejected(
        tmp_path,
        relative="references/process-secret.md",
        payload=f"Never publish {secret}.\n".encode(),
        match="process credential",
    )


def test_l3_embeds_valid_json_without_final_case_details() -> None:
    rendered = render_l3_html(_inputs(accepted=True, final=True))
    marker = '<script type="application/json" id="l3-data">'
    payload = rendered.split(marker, 1)[1].split("</script>", 1)[0]

    parsed = json.loads(payload)

    assert parsed["loop_totals"]["completed_rounds"] == 1
    assert parsed["final_aggregate"]["case_count"] == 12
    assert "cases" not in parsed["final_aggregate"]
    assert Decimal(parsed["loop_totals"]["cost_amount"]) >= 0
