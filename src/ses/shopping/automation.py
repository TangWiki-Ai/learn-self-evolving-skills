"""Assemble the fixed shopping capstone on the shared automation seams."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ses.automation.capstone import write_opaque_split_locks
from ses.automation.fixed import (
    build_fixed_auto_evolve_orchestrator,
    fixed_shopping_updater,
)
from ses.automation.orchestrator import AutoEvolveOrchestrator
from ses.contracts import FinalLifecycle, RegistryEventType, SplitLockFormat
from ses.evolution.diagnosis import SHOPPING_DIAGNOSIS_POLICY
from ses.evolution.updater import SHOPPING_UPDATER_POLICY
from ses.shopping.course_workflow import SHOPPING_STATIC_GATE_POLICY
from ses.shopping.gate import FixedShoppingEpisodeGateAdapter, shopping_gate_policy
from ses.shopping.profile import LoadedShoppingProfile, shopping_experiment_id
from ses.shopping.protected_course import FixedShoppingFinalAdapter
from ses.shopping.registry import open_shopping_registry
from ses.shopping.rollout import FixedShoppingRolloutAdapter

_LOCK_TIME = datetime(2026, 8, 20, tzinfo=UTC)
_AUTO_TIME = datetime(2026, 8, 20, 1, tzinfo=UTC)


def _require_manual_branch(profile: LoadedShoppingProfile, root: Path) -> Path:
    registry = open_shopping_registry(root / "registry")
    state = registry.audit()
    expected_lineage = (
        f"lineage-shopping-{profile.profile.mode}-{profile.profile_sha256[:16]}"
    )
    if state.lineage_id != expected_lineage:
        raise ValueError("shopping auto-evolve cannot cross profile lineages")
    manual_events = tuple(
        event
        for event in state.events
        if event.gate_decision is not None
        and event.gate_decision.path == "gates/gate-shopping-manual/gate-decision.json"
        and event.event_type
        in {
            RegistryEventType.CANDIDATE_ACCEPTED,
            RegistryEventType.CANDIDATE_REJECTED,
        }
    )
    if len(manual_events) != 1:
        raise ValueError(
            "shopping auto-evolve requires one completed manual Gate branch"
        )
    manual = manual_events[0]
    if manual.event_type is RegistryEventType.CANDIDATE_ACCEPTED and not any(
        event.event_type is RegistryEventType.PROMOTED
        and event.version_sha256 == manual.version_sha256
        for event in state.events
    ):
        raise ValueError("accepted manual shopping candidate must be promoted first")
    return registry.version_path(state.current_accepted_sha256)


def build_shopping_capstone_orchestrator(
    *,
    profile: LoadedShoppingProfile,
    project_root: Path,
    experiment_root: Path,
    final_scenario: Literal["safe", "unauthorized"] = "safe",
) -> AutoEvolveOrchestrator:
    """Continue one learner Registry through two rounds and independent final."""

    if profile.profile.mode != "fixed":
        raise ValueError("live shopping automation is no_go")
    root = experiment_root.resolve(strict=True)
    accepted = _require_manual_branch(profile, root)
    failure_fixture = root / "failure-evidence.json"
    initial_evidence = root / "v0-pipeline-summary.json"
    if not failure_fixture.is_file() or not initial_evidence.is_file():
        raise ValueError("shopping automation requires the manual learner evidence")
    experiment_id = shopping_experiment_id(profile)
    locks = write_opaque_split_locks(
        experiment_root=root,
        experiment_id=experiment_id,
        profile_sha256=profile.profile_sha256,
        mode="fixed",
        selection_case_count=profile.profile.episode_slot_counts["selection"],
        selection_commitment_sha256=(
            profile.profile.protected_split_commitments["selection"]
        ),
        final_commitment_sha256=profile.profile.protected_split_commitments["final"],
        generated_at=_LOCK_TIME,
    )
    policy = shopping_gate_policy(
        profile,
        selection_lock=locks.selection,
        experiment_id=experiment_id,
    )

    if final_scenario not in {"safe", "unauthorized"}:
        raise ValueError("unknown fixed shopping final scenario")

    def gate_adapter(round_number: int) -> FixedShoppingEpisodeGateAdapter:
        registry = open_shopping_registry(root / "registry")
        state = registry.audit()
        return FixedShoppingEpisodeGateAdapter(
            profile=profile,
            experiment_root=root,
            selection_lock=locks.selection,
            accepted_skill_source=registry.version_path(state.current_accepted_sha256),
            candidate_skill_source=(
                root / "rounds" / f"round-{round_number:03d}" / "candidate" / "skill"
            ),
            scenario="accept" if round_number == 1 else "tie",
        )

    return build_fixed_auto_evolve_orchestrator(
        project_root=project_root,
        output_root=root,
        experiment_id=experiment_id,
        accepted_skill=accepted,
        initial_evidence=initial_evidence,
        failure_fixture=failure_fixture,
        selection_lock=locks.selection,
        final_lock=locks.final,
        started_at=_AUTO_TIME,
        max_rounds=2,
        max_cost_amount="1.00",
        scenarios=(),
        rollout_adapter=FixedShoppingRolloutAdapter(
            profile=profile,
            experiment_root=root,
        ),
        updater_factory=fixed_shopping_updater,
        final_adapter=FixedShoppingFinalAdapter(
            profile=profile,
            experiment_root=root,
            final_lock=locks.final,
            scenario=final_scenario,
        ),
        final_lifecycle=FinalLifecycle.INDEPENDENT_CAPSTONE,
        profile_sha256=profile.profile_sha256,
        split_lock_format=SplitLockFormat.CONTENT_ADDRESSED,
        gate_policy=policy,
        gate_adapter_factory=gate_adapter,
        static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
    )


__all__ = ["build_shopping_capstone_orchestrator"]
