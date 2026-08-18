"""Deterministic adapters and a zero-cost entry point for Lesson 10."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ses.automation.orchestrator import (
    AutoEvolveCommand,
    AutoEvolveOrchestrator,
    FinalEvaluationAdapter,
    FinalExecution,
    FinalProtocolLock,
    RolloutAdapter,
    RolloutExecution,
    final_execution_run_set_sha256,
    validate_locked_manifest_path,
)
from ses.contracts import (
    AutoEvolveConfig,
    AutoEvolveState,
    FailureCategory,
    FailureEvidenceFixture,
    MeasurementKind,
    Patch,
    SchemaVersion,
    UpdatePatchOperation,
    Usage,
    content_sha256,
)
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    default_gate_policy,
)
from ses.evolution.patches import file_content_sha256
from ses.evolution.updater import FakeUpdater, Updater, UpdaterRequest
from ses.testset.holdout import HoldoutCommitments


class FixedRolloutAdapter:
    """Replay a public teaching fixture while binding it to each round parent."""

    def __init__(self, fixture_path: Path) -> None:
        try:
            self._fixture = FailureEvidenceFixture.model_validate_json(
                fixture_path.read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise ValueError("fixed rollout fixture is invalid") from exc
        if (
            self._fixture.source.measurement_kind
            is not MeasurementKind.SYNTHETIC_OFFLINE
        ):
            raise ValueError("fixed rollout fixture must be synthetic_offline")
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
        del parent_skill, executed_at
        self.calls += 1
        identity = f"{experiment_id}:round:{round_number}:{parent_skill_sha256}"

        def digest(kind: str) -> str:
            return hashlib.sha256(f"{identity}:{kind}".encode()).hexdigest()

        source = self._fixture.source.model_copy(
            update={
                "source_label": f"auto-evolve-fixed-round-{round_number:03d}",
                "skill_sha256": parent_skill_sha256,
                "comparison_sha256": digest("comparison"),
                "pair_execution_sha256": digest("pair-execution"),
                "baseline_events_sha256": digest("baseline-events"),
                "skill_events_sha256": digest("skill-events"),
            }
        )
        evidence = self._fixture.model_copy(update={"source": source})
        return RolloutExecution(
            evidence=evidence,
            measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
            network_used=False,
            source_kind="fixed_reference_fixture",
            usage=Usage(input_tokens=0, output_tokens=0),
            cost_complete=True,
        )


class FixedRoundUpdater:
    """Produce a distinct one-line evidence-linked candidate after round one."""

    measurement_kind = MeasurementKind.SYNTHETIC_OFFLINE

    def __init__(self, round_number: int) -> None:
        if round_number < 2:
            raise ValueError("FixedRoundUpdater is only used after round one")
        self.round_number = round_number
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.latency_ms = 0
        self.last_request: UpdaterRequest | None = None

    def propose(self, request: UpdaterRequest) -> Patch:
        self.last_request = request
        try:
            current = request.parent_files["SKILL.md"]
        except KeyError as exc:
            raise ValueError("fixed updater requires SKILL.md") from exc
        card = next(
            (
                value
                for value in request.cards
                if value.category is FailureCategory.SAFETY
            ),
            request.cards[0],
        )
        addition = (
            "\nIf a preview is ambiguous, stop and re-read the current policy "
            f"before consent (fixed round {self.round_number}).\n"
        )
        operation = UpdatePatchOperation(
            operation="update",
            target="SKILL.md",
            precondition_sha256=file_content_sha256(current),
            content=current.rstrip("\n") + addition,
            trace_evidence=card.trace_evidence,
            assertion_evidence=card.assertion_evidence,
            reason="Clarify the stop condition supported by the safety failure card.",
            risk="The extra guardrail slightly increases instruction length.",
            failure_card_ids=(card.failure_id,),
        )
        identity = hashlib.sha256(
            (request.parent_skill_sha256 + f":fixed-round:{self.round_number}").encode(
                "ascii"
            )
        ).hexdigest()[:16]
        return Patch(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="skill_patch",
            patch_id=f"patch-{identity}",
            parent_skill_sha256=request.parent_skill_sha256,
            operations=(operation,),
        )


class FixedFinalAdapter:
    """Return a deterministic offline reference after the loop has stopped."""

    def __init__(self, case_passes: Sequence[bool] | None = None) -> None:
        values = tuple(
            (True,) * 10 + (False,) * 2 if case_passes is None else case_passes
        )
        if len(values) != 12 or not all(isinstance(value, bool) for value in values):
            raise ValueError("fixed final reference requires exactly 12 outcomes")
        self._case_passes = values
        self.calls = 0

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
        del subject_skill, executed_at
        self.calls += 1
        private_payload = {
            "experiment_id": experiment_id,
            "measurement_kind": "synthetic_offline",
            "result_source": "fixed_reference",
            "subject_skill_sha256": subject_skill_sha256,
            "final_manifest_sha256": _file_sha256(final_manifest),
            "case_passes": list(self._case_passes),
        }
        return FinalExecution(
            case_passes=self._case_passes,
            private_payload=private_payload,
            measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
            network_used=False,
            result_source="fixed_reference",
            usage=Usage(input_tokens=0, output_tokens=0),
            cost_complete=True,
            actual_protocol=protocol,
            run_set_sha256=final_execution_run_set_sha256(
                case_passes=self._case_passes,
                private_payload=private_payload,
            ),
        )


def fixed_updater(round_number: int) -> Updater:
    """Return the deterministic updater for one numbered round."""

    if round_number == 1:
        return FakeUpdater()
    return FixedRoundUpdater(round_number)


def run_fixed_auto_evolve(
    *,
    project_root: Path,
    output_root: Path,
    experiment_id: str = "experiment-fixed-auto-evolve",
    accepted_skill: Path | None = None,
    initial_evidence: Path | None = None,
    failure_fixture: Path | None = None,
    selection_lock: Path | None = None,
    final_lock: Path | None = None,
    final_lock_sha256: str | None = None,
    started_at: datetime | None = None,
    max_rounds: int = 2,
    max_input_tokens: int = 100_000,
    max_output_tokens: int = 100_000,
    max_cost_amount: Decimal | str = "1.00",
    max_consecutive_rejections: int = 2,
    cooldown_rounds: int = 2,
    convergence_rounds: int = 2,
    min_quality_improvement: float = 0.0,
    frozen: bool = False,
    scenarios: Sequence[FixedGateScenario] = (
        FixedGateScenario.ACCEPT,
        FixedGateScenario.TIE,
    ),
    rollout_adapter: RolloutAdapter | None = None,
    updater_factory: Callable[[int], Updater] = fixed_updater,
    final_adapter: FinalEvaluationAdapter | None = None,
) -> AutoEvolveState:
    """Run or exactly resume the bounded two-round offline reference workflow."""

    root = project_root.resolve(strict=True)
    output = output_root.resolve(strict=False)
    accepted = (
        accepted_skill or root / "course/ch07-create-v0/artifacts/skill/v0"
    ).resolve(strict=True)
    evidence = (
        initial_evidence or root / "course/ch07-create-v0/artifacts/summary.json"
    ).resolve(strict=True)
    fixture = (
        failure_fixture
        or root / "tests/fixtures/evolution/synthetic-failure-evidence.json"
    ).resolve(strict=True)
    selection = selection_lock or (
        root / "data/testset/protected/selection-manifest.json"
    )
    final = final_lock or root / "data/testset/protected/final-manifest.json"
    validate_locked_manifest_path(selection)
    validate_locked_manifest_path(final)
    policy = default_gate_policy(root, selection)
    commitments_path = root / "data/testset/protected/holdout-commitments.json"
    try:
        commitments = HoldoutCommitments.model_validate_json(
            commitments_path.read_bytes()
        )
    except (OSError, ValueError) as exc:
        raise ValueError("public holdout commitments are invalid") from exc
    if commitments.selection_manifest_sha256 != policy.selection_lock_sha256:
        raise ValueError("selection manifest differs from its public commitment")
    expected_final_sha256 = final_lock_sha256 or commitments.final_manifest_sha256
    cost_limit = (
        max_cost_amount
        if isinstance(max_cost_amount, Decimal)
        else Decimal(max_cost_amount)
    )
    config = AutoEvolveConfig(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="auto_evolve_config",
        experiment_id=experiment_id,
        mode="fixed",
        max_rounds=max_rounds,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_cost_amount=cost_limit,
        cost_currency=policy.cost_currency,
        max_consecutive_rejections=max_consecutive_rejections,
        cooldown_rounds=cooldown_rounds,
        convergence_rounds=convergence_rounds,
        min_quality_improvement=min_quality_improvement,
        gate_policy_sha256=content_sha256(policy),
        selection_lock_sha256=policy.selection_lock_sha256,
        final_lock_sha256=expected_final_sha256,
        frozen=frozen,
    )
    if not scenarios:
        raise ValueError("fixed auto-evolve requires at least one Gate scenario")

    def gate_adapter_factory(round_number: int) -> FixedGateAdapter:
        index = min(round_number - 1, len(scenarios) - 1)
        return FixedGateAdapter(scenarios[index])

    orchestrator = AutoEvolveOrchestrator(
        AutoEvolveCommand(
            project_root=root,
            output_root=output,
            registry_root=output / "registry",
            accepted_skill=accepted,
            initial_evidence=evidence,
            failure_fixture=fixture,
            selection_lock=selection,
            final_lock=final,
            config=config,
            policy=policy,
            started_at=started_at or datetime(2026, 8, 19, 9, tzinfo=UTC),
        ),
        rollout_adapter=rollout_adapter or FixedRolloutAdapter(fixture),
        updater_factory=updater_factory,
        gate_adapter_factory=gate_adapter_factory,
        final_adapter=final_adapter or FixedFinalAdapter(),
    )
    return orchestrator.run()


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("fixed auto-evolve input must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "FixedFinalAdapter",
    "FixedRolloutAdapter",
    "FixedRoundUpdater",
    "fixed_updater",
    "run_fixed_auto_evolve",
]
