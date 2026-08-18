from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.automation.fixed import (
    FixedFinalAdapter,
    FixedRolloutAdapter,
    fixed_updater,
    run_fixed_auto_evolve,
)
from ses.automation.orchestrator import (
    AutoEvolveError,
    FinalExecution,
    FinalProtocolLock,
    RolloutExecution,
    final_execution_run_set_sha256,
)
from ses.automation.state import AutoStateError, AutoStateStore, StepBudgetUsage
from ses.contracts import (
    AutoEvolveConfig,
    AutoEvolveState,
    AutoLoopStatus,
    AutoStopReason,
    FinalAggregateReport,
    FinalConsumedCheckpoint,
    FinalRunReceipt,
    MeasurementKind,
    PairCategory,
    Patch,
    RunnerStatus,
    SchemaVersion,
    Usage,
)
from ses.evolution.updater import UpdaterRequest

ROOT = Path(__file__).resolve().parents[2]
FAILURE_FIXTURE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
ZERO_SHA256 = "0" * 64
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
FINAL_LOCK = ROOT / "data/testset/protected/final-manifest.json"


def _config(*, experiment_id: str = "experiment-resilience") -> AutoEvolveConfig:
    return AutoEvolveConfig(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="auto_evolve_config",
        experiment_id=experiment_id,
        mode="fixed",
        max_rounds=2,
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost_amount=Decimal("1.00"),
        cost_currency="USD",
        max_consecutive_rejections=2,
        cooldown_rounds=1,
        convergence_rounds=2,
        min_quality_improvement=0,
        gate_policy_sha256=ZERO_SHA256,
        selection_lock_sha256=ZERO_SHA256,
        final_lock_sha256=ZERO_SHA256,
    )


@pytest.mark.parametrize(
    ("budget", "value", "reason"),
    [
        ("max_input_tokens", 0, AutoStopReason.TOKEN_BUDGET),
        ("max_output_tokens", 0, AutoStopReason.TOKEN_BUDGET),
        ("max_cost_amount", "0", AutoStopReason.COST_BUDGET),
    ],
)
def test_zero_budget_stops_before_any_potentially_paid_step(
    tmp_path: Path,
    budget: str,
    value: int | str,
    reason: AutoStopReason,
) -> None:
    rollout = FixedRolloutAdapter(FAILURE_FIXTURE)
    final = FixedFinalAdapter()

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=tmp_path / budget,
        rollout_adapter=rollout,
        final_adapter=final,
        **{budget: value},  # type: ignore[arg-type]
    )

    assert state.status is AutoLoopStatus.STOPPED
    assert state.stop_reason is reason
    assert state.completed_rounds == 0
    assert rollout.calls == 0
    assert final.calls == 0
    assert not (tmp_path / budget / "rounds").exists()


@pytest.mark.parametrize(
    ("lock_name", "use_symlinked_parent"),
    [
        ("selection", False),
        ("selection", True),
        ("final", False),
        ("final", True),
    ],
)
def test_lock_symlinks_fail_before_any_auto_evolve_execution(
    tmp_path: Path,
    lock_name: str,
    use_symlinked_parent: bool,
) -> None:
    source = SELECTION_LOCK if lock_name == "selection" else FINAL_LOCK
    if use_symlinked_parent:
        alias = tmp_path / f"{lock_name}-parent-alias"
        alias.symlink_to(source.parent, target_is_directory=True)
        lock = alias / source.name
    else:
        lock = tmp_path / f"{lock_name}-lock-alias.json"
        lock.symlink_to(source)

    rollout = FixedRolloutAdapter(FAILURE_FIXTURE)
    final = FixedFinalAdapter()
    output = tmp_path / f"output-{lock_name}-{use_symlinked_parent}"
    arguments = (
        {"selection_lock": lock} if lock_name == "selection" else {"final_lock": lock}
    )

    with pytest.raises(AutoEvolveError, match="path cannot contain symlinks"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            rollout_adapter=rollout,
            final_adapter=final,
            **arguments,  # type: ignore[arg-type]
        )

    assert rollout.calls == 0
    assert final.calls == 0
    assert not output.exists()


def test_selection_lock_with_a_lexical_final_component_fails_before_execution(
    tmp_path: Path,
) -> None:
    disguised_root = tmp_path / "final"
    disguised_root.mkdir()
    disguised_selection = disguised_root / "selection-manifest.json"
    disguised_selection.write_bytes(SELECTION_LOCK.read_bytes())
    rollout = FixedRolloutAdapter(FAILURE_FIXTURE)
    final = FixedFinalAdapter()
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="cannot name the final split"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            selection_lock=disguised_selection,
            rollout_adapter=rollout,
            final_adapter=final,
        )

    assert rollout.calls == 0
    assert final.calls == 0
    assert not output.exists()


class _IncompleteCostRollout:
    def __init__(self) -> None:
        self._delegate = FixedRolloutAdapter(FAILURE_FIXTURE)

    @property
    def calls(self) -> int:
        return self._delegate.calls

    def run(
        self,
        *,
        experiment_id: str,
        round_number: int,
        parent_skill: Path,
        parent_skill_sha256: str,
        executed_at: datetime,
    ) -> RolloutExecution:
        execution = self._delegate.run(
            experiment_id=experiment_id,
            round_number=round_number,
            parent_skill=parent_skill,
            parent_skill_sha256=parent_skill_sha256,
            executed_at=executed_at,
        )
        return replace(execution, cost_complete=False)


class _WrongCurrencyRollout(_IncompleteCostRollout):
    def run(
        self,
        *,
        experiment_id: str,
        round_number: int,
        parent_skill: Path,
        parent_skill_sha256: str,
        executed_at: datetime,
    ) -> RolloutExecution:
        execution = self._delegate.run(
            experiment_id=experiment_id,
            round_number=round_number,
            parent_skill=parent_skill,
            parent_skill_sha256=parent_skill_sha256,
            executed_at=executed_at,
        )
        return replace(
            execution,
            usage=Usage(
                input_tokens=1,
                output_tokens=1,
                cost_amount=Decimal("0.01"),
                cost_currency="EUR",
            ),
        )


class _NoFailureRollout(FixedRolloutAdapter):
    def run(
        self,
        *,
        experiment_id: str,
        round_number: int,
        parent_skill: Path,
        parent_skill_sha256: str,
        executed_at: datetime,
    ) -> RolloutExecution:
        execution = super().run(
            experiment_id=experiment_id,
            round_number=round_number,
            parent_skill=parent_skill,
            parent_skill_sha256=parent_skill_sha256,
            executed_at=executed_at,
        )
        cases = tuple(
            case.model_copy(
                update={
                    "pair_category": PairCategory.BOTH_PASS,
                    "skill_status": RunnerStatus.PASS,
                    "failure_kinds": {},
                    "failure_categories": (),
                    "observation": "Both baseline and Skill passed.",
                }
            )
            for case in execution.evidence.cases
        )
        return replace(
            execution,
            evidence=execution.evidence.model_copy(update={"cases": cases}),
            usage=Usage(
                input_tokens=2,
                output_tokens=1,
                cost_amount=Decimal("0.05"),
                cost_currency="USD",
            ),
        )


def test_incomplete_cost_stops_before_patch_gate_final_or_next_round(
    tmp_path: Path,
) -> None:
    rollout = _IncompleteCostRollout()
    final = FixedFinalAdapter()
    output = tmp_path / "incomplete-cost"

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=3,
        rollout_adapter=rollout,
        final_adapter=final,
    )

    assert state.status is AutoLoopStatus.STOPPED
    assert state.stop_reason is AutoStopReason.COST_BUDGET
    assert state.completed_rounds == 0
    assert state.cost_complete is False
    assert rollout.calls == 1
    assert final.calls == 0
    assert not (output / "rounds/round-001/candidate").exists()
    assert not (output / "registry/gates/gate-auto-r001").exists()
    assert not (output / "rounds/round-002").exists()


def test_wrong_cost_currency_fails_closed_before_patch_or_final(
    tmp_path: Path,
) -> None:
    rollout = _WrongCurrencyRollout()
    final = FixedFinalAdapter()
    output = tmp_path / "wrong-currency"

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        rollout_adapter=rollout,
        final_adapter=final,
    )

    assert state.status is AutoLoopStatus.STOPPED
    assert state.stop_reason is AutoStopReason.COST_BUDGET
    assert state.cost_complete is False
    assert rollout.calls == 1
    assert final.calls == 0
    assert not (output / "rounds/round-001/candidate").exists()


def test_no_failure_evidence_stops_normally_preserves_rollout_usage_and_runs_final_once(
    tmp_path: Path,
) -> None:
    rollout = _NoFailureRollout(FAILURE_FIXTURE)
    final = FixedFinalAdapter()
    output = tmp_path / "no-failure"

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=3,
        rollout_adapter=rollout,
        final_adapter=final,
    )

    assert state.status is AutoLoopStatus.FINAL_COMPLETE
    assert state.stop_reason is AutoStopReason.NO_FAILURE_EVIDENCE
    assert state.completed_rounds == 0
    assert state.pending_cost_amount == Decimal("0.05")
    assert state.pending_input_tokens == 2
    assert state.pending_output_tokens == 1
    assert state.total_cost_amount == Decimal("0.05")
    assert state.total_input_tokens == 2
    assert state.total_output_tokens == 1
    assert rollout.calls == 1
    assert final.calls == 1
    assert not (output / "rounds/round-001/reflection.json").exists()
    assert not (output / "rounds/round-001/candidate").exists()
    assert not (output / "registry/gates/gate-auto-r001").exists()

    resumed = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=3,
        rollout_adapter=rollout,
        final_adapter=final,
    )
    assert resumed == state
    assert rollout.calls == 1
    assert final.calls == 1


class _OneTokenFinal(FixedFinalAdapter):
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
        execution = super().run(
            experiment_id=experiment_id,
            subject_skill=subject_skill,
            subject_skill_sha256=subject_skill_sha256,
            final_manifest=final_manifest,
            executed_at=executed_at,
            protocol=protocol,
        )
        return replace(execution, usage=Usage(input_tokens=1, output_tokens=0))


class _TamperedFinal(FixedFinalAdapter):
    def __init__(
        self,
        *,
        private_payload: dict[str, object] | None = None,
        wrong_protocol: bool = False,
    ) -> None:
        super().__init__()
        self._private_payload = private_payload
        self._wrong_protocol = wrong_protocol

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
        execution = super().run(
            experiment_id=experiment_id,
            subject_skill=subject_skill,
            subject_skill_sha256=subject_skill_sha256,
            final_manifest=final_manifest,
            executed_at=executed_at,
            protocol=protocol,
        )
        actual_protocol = (
            replace(protocol, judge_id="unexpected-judge")
            if self._wrong_protocol
            else protocol
        )
        private_payload = self._private_payload or dict(execution.private_payload)
        return replace(
            execution,
            actual_protocol=actual_protocol,
            private_payload=private_payload,
            run_set_sha256=final_execution_run_set_sha256(
                case_passes=execution.case_passes,
                private_payload=private_payload,
            ),
        )


def test_final_usage_is_part_of_the_global_budget_and_persisted_total(
    tmp_path: Path,
) -> None:
    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=tmp_path / "final-budget",
        max_rounds=1,
        max_input_tokens=1_201,
        final_adapter=_OneTokenFinal(),
    )

    assert state.status is AutoLoopStatus.FINAL_COMPLETE
    assert state.total_input_tokens == 1_201
    assert state.final_input_tokens == 1
    assert state.total_cost_amount == Decimal("0.01230")
    assert state.final_cost_amount == Decimal("0")


def test_final_receipt_and_consumed_checkpoint_lock_resume_and_detect_tampering(
    tmp_path: Path,
) -> None:
    output = tmp_path / "final-receipt"
    final = FixedFinalAdapter()
    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=1,
        final_adapter=final,
    )
    receipt_path = output / "final/final-run-receipt.json"
    aggregate_path = output / "final/final-aggregate.json"
    private_path = output / "final/private-results.json"
    checkpoint_path = output / "final-consumed.checkpoint.json"
    receipt = FinalRunReceipt.model_validate_json(receipt_path.read_bytes())
    checkpoint = FinalConsumedCheckpoint.model_validate_json(
        checkpoint_path.read_bytes()
    )

    assert receipt.experiment_id == state.experiment_id
    assert receipt.subject_skill_sha256 == state.current_accepted_skill_sha256
    assert receipt.engine_id == "fixed-offline-engine"
    assert receipt.simulator_id == "fixed-offline-simulator"
    assert receipt.judge_id == "fixed-offline-judge"
    assert receipt.provider_id == "none-offline"
    assert (
        receipt.aggregate_report_sha256
        == hashlib.sha256(aggregate_path.read_bytes()).hexdigest()
    )
    assert (
        receipt.private_results_sha256
        == hashlib.sha256(private_path.read_bytes()).hexdigest()
    )
    assert (
        checkpoint.final_run_receipt_sha256
        == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    )
    assert final.calls == 1

    resumed = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=1,
        final_adapter=final,
    )
    assert resumed == state
    assert final.calls == 1

    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["private_results_sha256"] = "f" * 64
    checkpoint_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(AutoEvolveError, match="final bundle"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            max_rounds=1,
            final_adapter=final,
        )
    assert final.calls == 1


def test_final_rejects_an_adapter_that_attests_another_actual_protocol(
    tmp_path: Path,
) -> None:
    output = tmp_path / "wrong-final-protocol"
    with pytest.raises(AutoEvolveError, match="locked mode"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            max_rounds=1,
            final_adapter=_TamperedFinal(wrong_protocol=True),
        )

    assert not (output / "final/private-results.json").exists()
    state = AutoEvolveState.model_validate_json((output / "state.json").read_bytes())
    assert state.stop_reason is AutoStopReason.INTERRUPTED_STEP


@pytest.mark.parametrize(
    ("name", "payload", "marker"),
    [
        (
            "nested-api-key",
            {"nested": {"api_key": "ordinary-secret"}},
            "ordinary-secret",
        ),
        (
            "authorization",
            {"headers": {"authorization": "Bearer ordinary-secret"}},
            "ordinary-secret",
        ),
        ("key-shaped", {"nested": {"value": "sk-12345678"}}, "sk-12345678"),
    ],
)
def test_final_rejects_nested_credential_material_before_persistence(
    tmp_path: Path,
    name: str,
    payload: dict[str, object],
    marker: str,
) -> None:
    output = tmp_path / name
    with pytest.raises(AutoEvolveError, match="contains credentials"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            max_rounds=1,
            final_adapter=_TamperedFinal(private_payload=payload),
        )

    assert not (output / "final/private-results.json").exists()
    assert all(
        marker not in path.read_text(encoding="utf-8", errors="ignore")
        for path in output.rglob("*")
        if path.is_file()
    )


def test_final_rejects_a_known_environment_secret_before_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "environment-only-test-secret"
    monkeypatch.setenv("AUTOMATION_TEST_SECRET_TOKEN", marker)
    output = tmp_path / "environment-secret"

    with pytest.raises(AutoEvolveError, match="contains credentials"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            max_rounds=1,
            final_adapter=_TamperedFinal(
                private_payload={"nested": {"ordinary_value": marker}}
            ),
        )

    assert not (output / "final/private-results.json").exists()
    assert all(
        marker not in path.read_text(encoding="utf-8", errors="ignore")
        for path in output.rglob("*")
        if path.is_file()
    )


class _CapturingRollout(FixedRolloutAdapter):
    def __init__(self, fixture_path: Path) -> None:
        super().__init__(fixture_path)
        self.parent_paths: list[tuple[str, ...]] = []

    def run(
        self,
        *,
        experiment_id: str,
        round_number: int,
        parent_skill: Path,
        parent_skill_sha256: str,
        executed_at: datetime,
    ) -> RolloutExecution:
        self.parent_paths.append(
            tuple(
                sorted(
                    path.relative_to(parent_skill).as_posix()
                    for path in parent_skill.rglob("*")
                    if path.is_file()
                )
            )
        )
        return super().run(
            experiment_id=experiment_id,
            round_number=round_number,
            parent_skill=parent_skill,
            parent_skill_sha256=parent_skill_sha256,
            executed_at=executed_at,
        )


class _CapturingUpdater:
    def __init__(
        self,
        *,
        round_number: int,
        captures: list[dict[str, object]],
    ) -> None:
        self._delegate = fixed_updater(round_number)
        self._captures = captures
        self.measurement_kind = self._delegate.measurement_kind
        self.usage = self._delegate.usage
        self.latency_ms = self._delegate.latency_ms

    def propose(self, request: UpdaterRequest) -> Patch:
        workspace_files = tuple(
            sorted(
                path.relative_to(request.workspace).as_posix()
                for path in request.workspace.rglob("*")
                if path.is_file()
            )
        )
        self._captures.append(
            {
                "visible_files": request.visible_files,
                "workspace_files": workspace_files,
                "parent_files": dict(request.parent_files),
                "cards": [card.model_dump(mode="json") for card in request.cards],
            }
        )
        patch = self._delegate.propose(request)
        self.usage = self._delegate.usage
        self.latency_ms = self._delegate.latency_ms
        return patch


def test_modification_loop_cannot_observe_holdout_bait_or_final_results(
    tmp_path: Path,
) -> None:
    output = tmp_path / "end-to-end-isolation"
    bait_root = output / "visibility-bait"
    bait_root.mkdir(parents=True)
    bait_markers = {
        "selection-manifest.json": "SELECTION-GOLD-BAIT-971b",
        "final-manifest.json": "FINAL-GOLD-BAIT-6d2e",
        "gold.json": "HIDDEN-GOLD-BAIT-a083",
    }
    for name, marker in bait_markers.items():
        (bait_root / name).write_text(marker, encoding="utf-8")

    final_marker = "FINAL-PRIVATE-RESULT-MUST-NOT-FEED-PATCH-4e9c"
    final = _TamperedFinal(private_payload={"heldout_result": final_marker})
    rollout = _CapturingRollout(FAILURE_FIXTURE)
    captures: list[dict[str, object]] = []

    def updater_factory(round_number: int) -> _CapturingUpdater:
        return _CapturingUpdater(round_number=round_number, captures=captures)

    state = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=2,
        rollout_adapter=rollout,
        updater_factory=updater_factory,
        final_adapter=final,
    )

    assert state.status is AutoLoopStatus.FINAL_COMPLETE
    assert len(captures) == 2
    assert len(rollout.parent_paths) == 2
    captured_json = json.dumps(captures, ensure_ascii=False, sort_keys=True)
    captured_paths = json.dumps(rollout.parent_paths, sort_keys=True)
    for name, marker in bait_markers.items():
        assert name not in captured_json
        assert name not in captured_paths
        assert marker not in captured_json
        assert marker not in captured_paths
    assert final_marker not in captured_json
    assert final_marker not in captured_paths
    for patch_path in output.glob("rounds/round-*/candidate/patch.json"):
        assert final_marker not in patch_path.read_text(encoding="utf-8")

    capture_snapshot = json.dumps(captures, ensure_ascii=False, sort_keys=True)
    resumed = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        max_rounds=2,
        rollout_adapter=rollout,
        updater_factory=updater_factory,
        final_adapter=final,
    )
    assert resumed == state
    assert json.dumps(captures, ensure_ascii=False, sort_keys=True) == capture_snapshot
    assert rollout.calls == 2
    assert final.calls == 1


def test_experiment_lock_rejects_a_concurrent_runner(tmp_path: Path) -> None:
    first = AutoStateStore(tmp_path / "locked")
    second = AutoStateStore(tmp_path / "locked")

    with first.experiment_lock():
        with pytest.raises(AutoStateError, match="already running"):
            with second.experiment_lock():
                raise AssertionError("the second runner acquired the lock")


def test_store_rejects_a_symlinked_journal_without_writing_outside(
    tmp_path: Path,
) -> None:
    root = tmp_path / "symlinked-journal"
    outside = tmp_path / "outside-journal"
    root.mkdir()
    outside.mkdir()
    (root / ".journal").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AutoStateError, match="journal"):
        AutoStateStore(root)

    assert tuple(outside.iterdir()) == ()


def test_active_store_rejects_a_replaced_journal_and_keeps_one_root_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "replaced-journal"
    first = AutoStateStore(root)
    second = AutoStateStore(root)
    first.initialize(_config(), accepted_skill_sha256="1" * 64)

    with first.experiment_lock():
        original = root / ".journal-original"
        first.journal_root.rename(original)
        first.journal_root.mkdir()
        with pytest.raises(AutoStateError, match="journal"):
            first.begin_step(
                round_number=1,
                step="rollout",
                expected_outputs=(root / "rounds/round-001/rollout.json",),
                input_hashes={"accepted_skill_sha256": "1" * 64},
            )
        with pytest.raises(AutoStateError, match="already running"):
            with second.experiment_lock():
                raise AssertionError("a replaced journal bypassed the root lock")

    assert not any(first.journal_root.iterdir())
    assert not any(original.iterdir())


def test_active_store_fails_closed_if_experiment_root_is_replaced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "replaced-root"
    replacement = tmp_path / "replacement-root"
    store = AutoStateStore(root)
    store.initialize(_config(), accepted_skill_sha256="1" * 64)
    replacement.mkdir()

    with store.experiment_lock():
        original = tmp_path / "original-root"
        root.rename(original)
        root.symlink_to(replacement, target_is_directory=True)
        with pytest.raises(AutoStateError, match=r"root|symlink"):
            store.step_receipt(round_number=1, step="rollout")

    assert tuple(replacement.iterdir()) == ()
    assert not any(original.joinpath(".journal").iterdir())


def test_step_intent_uses_os_exclusive_creation_under_a_race(tmp_path: Path) -> None:
    store = AutoStateStore(tmp_path / "intent-race")
    state = store.initialize(_config(), accepted_skill_sha256="1" * 64)
    output = store.root / "rounds/round-001/rollout.json"
    barrier = threading.Barrier(2)

    def attempt() -> bool | str:
        barrier.wait()
        try:
            return store.begin_step(
                round_number=1,
                step="rollout",
                expected_outputs=(output,),
                input_hashes={
                    "accepted_skill_sha256": state.current_accepted_skill_sha256
                },
            )
        except AutoStateError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: attempt(), range(2)))

    assert results.count(True) == 1
    assert results.count("rejected") == 1


def test_step_intent_is_exclusive_and_completion_binds_inputs_and_outputs(
    tmp_path: Path,
) -> None:
    store = AutoStateStore(tmp_path / "journal")
    state = store.initialize(_config(), accepted_skill_sha256="1" * 64)
    output = store.root / "rounds/round-001/rollout.json"
    inputs = {"accepted_skill_sha256": state.current_accepted_skill_sha256}

    assert store.begin_step(
        round_number=1,
        step="rollout",
        expected_outputs=(output,),
        input_hashes=inputs,
    )
    with pytest.raises(AutoStateError, match="interrupted"):
        store.begin_step(
            round_number=1,
            step="rollout",
            expected_outputs=(output,),
            input_hashes=inputs,
        )

    output.parent.mkdir(parents=True)
    output.write_bytes(b'{"ok":true}')
    assert not store.begin_step(
        round_number=1,
        step="rollout",
        expected_outputs=(output,),
        input_hashes=inputs,
    )
    store.complete_step(
        round_number=1,
        step="rollout",
        expected_outputs=(output,),
        input_hashes=inputs,
        budget=StepBudgetUsage(
            cost_amount=Decimal("0.25"),
            cost_currency="USD",
            cost_complete=True,
            input_tokens=2,
            output_tokens=3,
        ),
    )

    intent = json.loads(
        store.intent_path(round_number=1, step="rollout").read_text(encoding="utf-8")
    )
    assert intent["config_sha256"] == state.config_sha256
    assert intent["input_hashes"] == inputs
    receipt = store.step_receipt(round_number=1, step="rollout")
    assert receipt.budget is not None
    assert receipt.budget.cost_amount == Decimal("0.25")

    output.write_bytes(b'{"ok":false}')
    with pytest.raises(AutoStateError, match="output hash"):
        store.begin_step(
            round_number=1,
            step="rollout",
            expected_outputs=(output,),
            input_hashes=inputs,
        )


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
        raise RuntimeError("provider disconnected")


def test_interrupted_paid_step_becomes_diagnostic_state_and_is_not_repeated(
    tmp_path: Path,
) -> None:
    output = tmp_path / "interrupted"
    interrupted = _InterruptedRollout()
    with pytest.raises(RuntimeError, match="provider disconnected"):
        run_fixed_auto_evolve(
            project_root=ROOT,
            output_root=output,
            rollout_adapter=interrupted,
            started_at=datetime(2026, 8, 19, 9, tzinfo=UTC),
        )

    persisted = AutoEvolveState.model_validate_json(
        (output / "state.json").read_bytes()
    )
    assert persisted.status is AutoLoopStatus.STOPPED
    assert persisted.stop_reason is AutoStopReason.INTERRUPTED_STEP

    replacement = FixedRolloutAdapter(FAILURE_FIXTURE)
    resumed = run_fixed_auto_evolve(
        project_root=ROOT,
        output_root=output,
        rollout_adapter=replacement,
        started_at=datetime(2026, 8, 19, 9, tzinfo=UTC),
    )
    assert resumed.status is AutoLoopStatus.STOPPED
    assert resumed.stop_reason is AutoStopReason.INTERRUPTED_STEP
    assert replacement.calls == 0


def test_final_contract_rejects_live_evidence_without_a_complete_cost() -> None:
    with pytest.raises(ValidationError, match="complete cost"):
        FinalAggregateReport(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="final_aggregate_report",
            experiment_id="experiment-live-cost",
            subject_skill_sha256="1" * 64,
            final_lock_sha256="2" * 64,
            mode="live",
            measurement_kind=MeasurementKind.LIVE_MEASURED,
            network_used=True,
            result_source="canonical_live",
            executed_at=datetime(2026, 8, 19, 9, tzinfo=UTC),
            case_count=12,
            pass_count=12,
            pass_rate=1,
            cost_amount=Decimal("0"),
            cost_currency="USD",
            cost_complete=False,
            input_tokens=1,
            output_tokens=1,
            private_results_sha256="3" * 64,
        )
