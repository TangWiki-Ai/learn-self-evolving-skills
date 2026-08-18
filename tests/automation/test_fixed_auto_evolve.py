from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ses.automation.fixed import (
    FixedFinalAdapter,
    FixedRolloutAdapter,
    run_fixed_auto_evolve,
)
from ses.automation.orchestrator import (
    AutoEvolveError,
    FinalExecution,
    FinalProtocolLock,
    RolloutExecution,
)
from ses.cli.app import main as cli_main
from ses.contracts import (
    AutoEvolveState,
    AutoLoopStatus,
    AutoStopReason,
    FinalAggregateReport,
    GateOutcome,
)
from ses.evolution.gate import FixedGateScenario
from ses.evolution.registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
FINAL_LOCK = ROOT / "data/testset/protected/final-manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("option", "source"),
    [
        ("--selection-lock", SELECTION_LOCK),
        ("--final-lock", FINAL_LOCK),
    ],
)
def test_cli_rejects_a_symlinked_holdout_lock_before_creating_an_experiment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    source: Path,
) -> None:
    alias = tmp_path / source.name
    alias.symlink_to(source)
    output = tmp_path / "must-not-exist"

    assert (
        cli_main(
            [
                "auto-evolve",
                "--mode",
                "fixed",
                "--output-root",
                str(output),
                option,
                str(alias),
            ]
        )
        == 1
    )
    assert "path cannot contain symlinks" in capsys.readouterr().err
    assert not output.exists()


def test_fixed_loop_accepts_rejects_and_resumes_without_new_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "fixed-auto"
    rollout = FixedRolloutAdapter(FIXTURE)
    final = FixedFinalAdapter()

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        rollout_adapter=rollout,
        final_adapter=final,
    )

    assert state.status is AutoLoopStatus.FINAL_COMPLETE
    assert state.stop_reason is AutoStopReason.MAX_ROUNDS
    assert [row.gate_outcome for row in state.rounds] == [
        GateOutcome.ACCEPTED,
        GateOutcome.REJECTED,
    ]
    assert [row.promoted for row in state.rounds] == [True, False]
    assert state.current_accepted_skill_sha256 == state.rounds[0].candidate_skill_sha256
    assert state.rounds[1].parent_skill_sha256 == state.rounds[0].candidate_skill_sha256
    assert rollout.calls == 2
    assert final.calls == 1

    registry = SkillRegistry(output / "registry").audit()
    assert registry.current_accepted_sha256 == state.current_accepted_skill_sha256
    assert [event.event_type.value for event in registry.events] == [
        "registry_initialized",
        "candidate_registered",
        "candidate_accepted",
        "promoted",
        "candidate_registered",
        "candidate_rejected",
    ]
    report = FinalAggregateReport.model_validate_json(
        (output / "final/final-aggregate.json").read_bytes()
    )
    assert report.case_count == 12
    assert report.pass_count == 10
    assert report.result_source == "fixed_reference"
    assert report.network_used is False
    assert stat.S_IMODE((output / "final/private-results.json").stat().st_mode) == 0o600

    events = output / "registry/events.jsonl"
    before = (_sha256(events), len(registry.events))
    resumed = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        rollout_adapter=rollout,
        final_adapter=final,
    )
    assert resumed == state
    assert rollout.calls == 2
    assert final.calls == 1
    assert (
        _sha256(events),
        len(SkillRegistry(output / "registry").audit().events),
    ) == before

    assert (
        cli_main(
            [
                "auto-evolve",
                "--mode",
                "fixed",
                "--output-root",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert '"result_kind":"fixed_reference"' in printed
    assert '"network_used":false' in printed
    assert '"provider_cost_amount":"0"' in printed
    assert '"final_case_count":12' in printed
    assert _sha256(events) == before[0]


class _StateCheckingFinalAdapter(FixedFinalAdapter):
    def __init__(self, output_root: Path) -> None:
        super().__init__()
        self._output_root = output_root

    def run(
        self,
        *,
        experiment_id: str,
        subject_skill: Path,
        subject_skill_sha256: str,
        final_manifest: Path,
        executed_at: datetime,
        protocol: FinalProtocolLock,
    ) -> FinalExecution:
        state = AutoEvolveState.model_validate_json(
            (self._output_root / "state.json").read_bytes()
        )
        assert state.status is AutoLoopStatus.STOPPED
        assert state.stop_reason is not None
        return super().run(
            experiment_id=experiment_id,
            subject_skill=subject_skill,
            subject_skill_sha256=subject_skill_sha256,
            final_manifest=final_manifest,
            executed_at=executed_at,
            protocol=protocol,
        )


def test_final_adapter_only_runs_after_stopped_state_is_durable(tmp_path: Path) -> None:
    output = tmp_path / "final-boundary"
    final = _StateCheckingFinalAdapter(output)

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        final_adapter=final,
    )

    assert state.status is AutoLoopStatus.FINAL_COMPLETE
    assert final.calls == 1


def test_completed_experiment_rejects_registry_pointer_drift(tmp_path: Path) -> None:
    output = tmp_path / "pointer-drift"
    state = run_fixed_auto_evolve(project_root=ROOT, output_root=output)

    SkillRegistry(output / "registry").rollback(
        command_id="command-operator-rollback-after-final",
        target_skill_sha256=state.rounds[0].parent_skill_sha256,
        occurred_at=datetime(2026, 8, 19, 10, tzinfo=UTC),
    )

    with pytest.raises(
        AutoEvolveError,
        match="Registry accepted Skill changed; declare a new experiment",
    ):
        run_fixed_auto_evolve(project_root=ROOT, output_root=output)


@pytest.mark.parametrize(
    ("name", "kwargs", "reason", "rounds"),
    [
        (
            "input-budget",
            {"max_input_tokens": 0},
            AutoStopReason.TOKEN_BUDGET,
            0,
        ),
        (
            "cost-budget",
            {"max_cost_amount": "0"},
            AutoStopReason.COST_BUDGET,
            0,
        ),
        (
            "consecutive-rejections",
            {
                "max_rounds": 3,
                "max_consecutive_rejections": 2,
                "cooldown_rounds": 0,
                "convergence_rounds": 10,
                "scenarios": (FixedGateScenario.TIE,),
            },
            AutoStopReason.CONSECUTIVE_REJECTIONS,
            2,
        ),
        (
            "cooldown",
            {
                "max_rounds": 3,
                "max_consecutive_rejections": 5,
                "cooldown_rounds": 1,
                "convergence_rounds": 10,
                "scenarios": (FixedGateScenario.TIE,),
            },
            AutoStopReason.COOLDOWN,
            2,
        ),
        (
            "convergence",
            {
                "max_rounds": 3,
                "max_consecutive_rejections": 5,
                "cooldown_rounds": 0,
                "convergence_rounds": 2,
                "min_quality_improvement": 0.2,
            },
            AutoStopReason.CONVERGED,
            2,
        ),
        (
            "frozen",
            {"frozen": True},
            AutoStopReason.FROZEN,
            0,
        ),
    ],
)
def test_fixed_loop_enforces_each_stop_guardrail(
    tmp_path: Path,
    name: str,
    kwargs: dict[str, object],
    reason: AutoStopReason,
    rounds: int,
) -> None:
    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=tmp_path / name,
        **kwargs,  # type: ignore[arg-type]
    )

    assert state.stop_reason is reason
    assert state.completed_rounds == rounds
    budget_stop = reason in {
        AutoStopReason.TOKEN_BUDGET,
        AutoStopReason.COST_BUDGET,
    }
    assert state.status is (
        AutoLoopStatus.STOPPED if budget_stop else AutoLoopStatus.FINAL_COMPLETE
    )
    assert (state.final_report is None) is budget_stop
    if budget_stop:
        assert state.total_cost_amount == 0
        assert state.total_input_tokens == 0
        assert state.total_output_tokens == 0
        assert not (tmp_path / name / "rounds").exists()
        assert not (tmp_path / name / "final").exists()


class _InterruptedRollout:
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        *,
        experiment_id: str,
        round_number: int,
        parent_skill: Path,
        parent_skill_sha256: str,
        executed_at: datetime,
    ) -> RolloutExecution:
        del (
            experiment_id,
            round_number,
            parent_skill,
            parent_skill_sha256,
            executed_at,
        )
        self.calls += 1
        raise RuntimeError("simulated interruption after write-ahead intent")


def test_interrupted_paid_step_is_not_repeated(tmp_path: Path) -> None:
    output = tmp_path / "interrupted"
    interrupted = _InterruptedRollout()
    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            rollout_adapter=interrupted,
            started_at=datetime(2026, 8, 19, 9, tzinfo=UTC),
        )
    assert interrupted.calls == 1

    replacement = FixedRolloutAdapter(FIXTURE)
    registry_events = output / "registry/events.jsonl"
    intent = output / ".journal/round-001-rollout.json"
    before = (_sha256(registry_events), _sha256(intent))
    resumed = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        rollout_adapter=replacement,
        started_at=datetime(2026, 8, 19, 9, tzinfo=UTC),
    )
    assert resumed.status is AutoLoopStatus.STOPPED
    assert resumed.stop_reason is AutoStopReason.INTERRUPTED_STEP
    assert replacement.calls == 0
    assert (_sha256(registry_events), _sha256(intent)) == before
    assert not (output / ".journal/round-001-rollout.receipt.json").exists()
