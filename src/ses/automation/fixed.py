"""Deterministic adapters and a zero-cost entry point for Lesson 10."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

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
    CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256,
    FINAL_REPORT_PROTOCOL_SHA256,
    AutoEvolveConfig,
    AutoEvolveState,
    FailureCategory,
    FailureEvidenceFixture,
    FinalLifecycle,
    GatePolicy,
    MeasurementKind,
    Patch,
    SchemaVersion,
    ShoppingFinalScenarioMetrics,
    SplitLockFormat,
    UpdatePatchOperation,
    Usage,
    content_sha256,
)
from ses.contracts.shopping import ShoppingScenario
from ses.evolution.diagnosis import (
    RETURN_DIAGNOSIS_POLICY,
    FailureDiagnosisPolicy,
)
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateEvaluationAdapter,
    default_gate_policy,
)
from ses.evolution.patches import file_content_sha256
from ses.evolution.updater import (
    RETURN_UPDATER_POLICY,
    FakeUpdater,
    Updater,
    UpdaterPolicy,
    UpdaterRequest,
)
from ses.skills.static_gate import DEFAULT_STATIC_GATE_POLICY, StaticGatePolicy
from ses.testset.holdout import HoldoutCommitments


class FixedRolloutAdapter:
    """Replay a public teaching fixture while binding it to each round parent."""

    def __init__(
        self,
        fixture_path: Path,
        *,
        source_kind: Literal[
            "fixed_reference_fixture", "fresh_fixed_execution"
        ] = "fixed_reference_fixture",
    ) -> None:
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
        self._source_kind = source_kind
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
            source_kind=self._source_kind,
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


class FixedShoppingRoundUpdater:
    """Produce one repeatable shopping-domain refinement per auto round."""

    measurement_kind = MeasurementKind.SYNTHETIC_OFFLINE

    def __init__(self, round_number: int) -> None:
        if round_number < 1:
            raise ValueError("shopping auto-evolve rounds start at one")
        self.round_number = round_number
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.latency_ms = 0
        self.last_request: UpdaterRequest | None = None

    def propose(self, request: UpdaterRequest) -> Patch:
        if request.policy.policy_id != "shopping-v1":
            raise ValueError("shopping round Updater requires the shopping policy")
        self.last_request = request
        try:
            current = request.parent_files["SKILL.md"]
        except KeyError as exc:
            raise ValueError("shopping round Updater requires SKILL.md") from exc
        card = next(
            (
                value
                for value in request.cards
                if value.category is FailureCategory.SAFETY
                and value.shopping_subcode is not None
            ),
            None,
        )
        if card is None:
            card = next(
                (
                    value
                    for value in request.cards
                    if value.shopping_subcode is not None
                ),
                None,
            )
        if card is None:
            raise ValueError(
                "shopping round Updater requires a reviewed shopping failure card"
            )
        safety_patch = card.category is FailureCategory.SAFETY
        addition = (
            "\nBefore any purchase, re-check current authorization and the exact "
            f"offer (fixed shopping round {self.round_number}).\n"
            if safety_patch
            else "\nBefore presenting a candidate, verify the selected option against "
            f"the current request (fixed shopping round {self.round_number}).\n"
        )
        operation = UpdatePatchOperation(
            operation="update",
            target="SKILL.md",
            precondition_sha256=file_content_sha256(current),
            content=current.rstrip("\n") + addition,
            trace_evidence=card.trace_evidence,
            assertion_evidence=card.assertion_evidence,
            reason=(
                "Reinforce the authorization boundary supported by the reviewed "
                "shopping safety failure."
                if safety_patch
                else "Reinforce option verification supported by the reviewed "
                "shopping mismatch failure."
            ),
            risk="The extra guardrail slightly increases instruction length.",
            failure_card_ids=(card.failure_id,),
        )
        identity = hashlib.sha256(
            (
                request.parent_skill_sha256
                + f":fixed-shopping-round:{self.round_number}"
            ).encode("ascii")
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

    def __init__(
        self,
        case_passes: Sequence[bool] | None = None,
        *,
        result_source: Literal[
            "fixed_reference", "fresh_fixed_execution"
        ] = "fixed_reference",
        safety_violation_count: int = 0,
    ) -> None:
        values = tuple(
            (True,) * 10 + (False,) * 2 if case_passes is None else case_passes
        )
        if len(values) != 12 or not all(isinstance(value, bool) for value in values):
            raise ValueError("fixed final reference requires exactly 12 outcomes")
        if result_source not in {"fixed_reference", "fresh_fixed_execution"}:
            raise ValueError("fixed final source must be reference or fresh execution")
        if type(safety_violation_count) is not int or safety_violation_count < 0:
            raise ValueError("fixed final safety count must be a nonnegative integer")
        self._case_passes = values
        self._result_source = result_source
        self._safety_violation_count = safety_violation_count
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
            "result_source": self._result_source,
            "subject_skill_sha256": subject_skill_sha256,
            "final_manifest_sha256": _file_sha256(final_manifest),
            "case_passes": list(self._case_passes),
        }
        scenario_metrics = tuple(
            ShoppingFinalScenarioMetrics(
                scenario=scenario,
                case_count=3,
                full_success_count=sum(self._case_passes[index * 3 : (index + 1) * 3]),
                mean_strict_reward=Decimal("0.75"),
                safety_violation_count=(
                    self._safety_violation_count
                    if index == len(tuple(ShoppingScenario)) - 1
                    else 0
                ),
            )
            for index, scenario in enumerate(ShoppingScenario)
        )
        return FinalExecution(
            case_passes=self._case_passes,
            private_payload=private_payload,
            measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
            network_used=False,
            result_source=self._result_source,
            usage=Usage(input_tokens=0, output_tokens=0),
            cost_complete=True,
            actual_protocol=protocol,
            run_set_sha256=final_execution_run_set_sha256(
                case_passes=self._case_passes,
                private_payload=private_payload,
            ),
            safety_violation_count=self._safety_violation_count,
            scenario_metrics=scenario_metrics,
        )


def fixed_updater(round_number: int) -> Updater:
    """Return the deterministic updater for one numbered round."""

    if round_number == 1:
        return FakeUpdater()
    return FixedRoundUpdater(round_number)


def fixed_shopping_updater(round_number: int) -> Updater:
    """Return the deterministic shopping Updater for one numbered auto round."""

    return FixedShoppingRoundUpdater(round_number)


def build_fixed_auto_evolve_orchestrator(
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
    final_lifecycle: FinalLifecycle = FinalLifecycle.INLINE_LEGACY,
    profile_sha256: str | None = None,
    split_lock_format: SplitLockFormat = SplitLockFormat.HOLDOUT_MANIFEST,
    gate_policy: GatePolicy | None = None,
    gate_adapter_factory: Callable[[int], GateEvaluationAdapter] | None = None,
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
    diagnosis_policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY,
    updater_policy: UpdaterPolicy = RETURN_UPDATER_POLICY,
) -> AutoEvolveOrchestrator:
    """Build the deterministic fixed workflow for rounds or independent final."""

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
    policy = gate_policy or default_gate_policy(root, selection)
    selection_sha256 = _file_sha256(selection)
    if selection_sha256 != policy.selection_lock_sha256:
        raise ValueError("selection lock differs from the Gate policy")
    if split_lock_format is SplitLockFormat.HOLDOUT_MANIFEST:
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
    else:
        expected_final_sha256 = final_lock_sha256 or _file_sha256(final)
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
        final_lifecycle=(
            final_lifecycle
            if final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
            else None
        ),
        profile_sha256=(
            profile_sha256
            if final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
            else None
        ),
        split_lock_format=(
            split_lock_format
            if final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
            else None
        ),
        final_report_protocol_sha256=(
            CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256
            if final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
            else FINAL_REPORT_PROTOCOL_SHA256
        ),
    )
    if gate_adapter_factory is None and not scenarios:
        raise ValueError("fixed auto-evolve requires at least one Gate scenario")

    def default_gate_adapter_factory(round_number: int) -> FixedGateAdapter:
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
            final_lifecycle=final_lifecycle,
            profile_sha256=profile_sha256,
            split_lock_format=split_lock_format,
            static_gate_policy=static_gate_policy,
            diagnosis_policy=diagnosis_policy,
            updater_policy=updater_policy,
        ),
        rollout_adapter=rollout_adapter
        or FixedRolloutAdapter(
            fixture,
            source_kind=(
                "fresh_fixed_execution"
                if final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
                else "fixed_reference_fixture"
            ),
        ),
        updater_factory=updater_factory,
        gate_adapter_factory=gate_adapter_factory or default_gate_adapter_factory,
        final_adapter=final_adapter
        or FixedFinalAdapter(
            result_source=(
                "fresh_fixed_execution"
                if final_lifecycle is FinalLifecycle.INDEPENDENT_CAPSTONE
                else "fixed_reference"
            )
        ),
    )
    return orchestrator


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
    final_lifecycle: FinalLifecycle = FinalLifecycle.INLINE_LEGACY,
    profile_sha256: str | None = None,
    split_lock_format: SplitLockFormat = SplitLockFormat.HOLDOUT_MANIFEST,
    gate_policy: GatePolicy | None = None,
    gate_adapter_factory: Callable[[int], GateEvaluationAdapter] | None = None,
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
    diagnosis_policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY,
    updater_policy: UpdaterPolicy = RETURN_UPDATER_POLICY,
) -> AutoEvolveState:
    """Run or exactly resume the bounded fixed workflow."""

    return build_fixed_auto_evolve_orchestrator(
        project_root=project_root,
        output_root=output_root,
        experiment_id=experiment_id,
        accepted_skill=accepted_skill,
        initial_evidence=initial_evidence,
        failure_fixture=failure_fixture,
        selection_lock=selection_lock,
        final_lock=final_lock,
        final_lock_sha256=final_lock_sha256,
        started_at=started_at,
        max_rounds=max_rounds,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_cost_amount=max_cost_amount,
        max_consecutive_rejections=max_consecutive_rejections,
        cooldown_rounds=cooldown_rounds,
        convergence_rounds=convergence_rounds,
        min_quality_improvement=min_quality_improvement,
        frozen=frozen,
        scenarios=scenarios,
        rollout_adapter=rollout_adapter,
        updater_factory=updater_factory,
        final_adapter=final_adapter,
        final_lifecycle=final_lifecycle,
        profile_sha256=profile_sha256,
        split_lock_format=split_lock_format,
        gate_policy=gate_policy,
        gate_adapter_factory=gate_adapter_factory,
        static_gate_policy=static_gate_policy,
        diagnosis_policy=diagnosis_policy,
        updater_policy=updater_policy,
    ).run()


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("fixed auto-evolve input must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "FixedFinalAdapter",
    "FixedRolloutAdapter",
    "FixedRoundUpdater",
    "FixedShoppingRoundUpdater",
    "build_fixed_auto_evolve_orchestrator",
    "fixed_shopping_updater",
    "fixed_updater",
    "run_fixed_auto_evolve",
]
