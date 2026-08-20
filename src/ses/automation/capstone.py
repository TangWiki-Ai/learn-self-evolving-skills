"""Build and verify the learner-owned shopping capstone completion index."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from ses.automation.portfolio import portfolio_semantic_sha256
from ses.contracts import (
    AcceptedSkillReleaseManifest,
    ArtifactRef,
    ArtifactRoot,
    AutoEvolveConfig,
    AutoEvolveState,
    AutoLoopStatus,
    AutoRolloutReceipt,
    CandidateArtifact,
    CapstoneFinalReceipt,
    CapstoneIndex,
    CapstoneReviewReceipt,
    FailureCardSet,
    FailureEvidenceFixture,
    FinalAggregateReport,
    FinalConsumedCheckpoint,
    FinalRunReceipt,
    GateDecision,
    GateOutcome,
    MeasurementKind,
    OpaqueProtectedSplitLock,
    PairedComparison,
    Patch,
    PortfolioManifest,
    RegistryEvent,
    RegistryEventType,
    SchemaVersion,
    SkillArtifactManifest,
    TriggerEvalResult,
    artifact_json_bytes,
    content_sha256,
)
from ses.evolution.registry import RegistryError, SkillRegistry
from ses.reporting.l3 import load_l3_inputs, render_l3_html
from ses.shopping.course_workflow import ShoppingLearnerReceipt
from ses.skills.installer import normalized_skill_sha256
from ses.skills.static_gate import (
    DEFAULT_STATIC_GATE_POLICY,
    StaticGatePolicy,
    StaticGateReport,
    StaticGateStatus,
    run_static_gate,
)

_Record = TypeVar("_Record", bound=BaseModel)
_REVIEW_KINDS = frozenset(
    {
        "paired_trace",
        "failure_evidence",
        "failure_card",
        "gate_decision",
        "registry_history",
    }
)


@dataclass(frozen=True, slots=True)
class OpaqueSplitLockPaths:
    """Distinct generated selection and final lock paths."""

    selection: Path
    final: Path


class CapstoneIndexError(ValueError):
    """The supplied artifacts do not prove one complete learner workflow."""


def write_opaque_split_locks(
    *,
    experiment_root: Path,
    experiment_id: str,
    profile_sha256: str,
    mode: Literal["fixed", "live"],
    selection_case_count: int,
    selection_commitment_sha256: str,
    final_commitment_sha256: str,
    generated_at: datetime,
) -> OpaqueSplitLockPaths:
    """Create or verify identity-free, experiment-bound 8/12 split locks."""

    if selection_commitment_sha256 == final_commitment_sha256:
        raise CapstoneIndexError("selection and final commitments must be distinct")
    if ".." in experiment_root.parts or experiment_root.is_symlink():
        raise CapstoneIndexError("split lock experiment root must be canonical")
    root = experiment_root.resolve()
    if experiment_root.absolute() != root:
        raise CapstoneIndexError("split lock experiment root must be canonical")
    root.mkdir(parents=True, exist_ok=True)
    protected = root / "protected"
    if protected.is_symlink():
        raise CapstoneIndexError("protected split directory cannot be a symlink")
    protected.mkdir(exist_ok=True)
    measurement = (
        MeasurementKind.SYNTHETIC_OFFLINE
        if mode == "fixed"
        else MeasurementKind.LIVE_MEASURED
    )

    def record(split: Literal["selection", "final"]) -> OpaqueProtectedSplitLock:
        count = selection_case_count if split == "selection" else 12
        commitment = (
            selection_commitment_sha256
            if split == "selection"
            else final_commitment_sha256
        )
        return OpaqueProtectedSplitLock(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="opaque_protected_split_lock",
            experiment_id=experiment_id,
            profile_sha256=profile_sha256,
            mode=mode,
            measurement_kind=measurement,
            split=split,
            case_count=count,
            opaque_slots=tuple(
                f"opaque-{split}-{index:03d}" for index in range(1, count + 1)
            ),
            aggregate_commitment_sha256=commitment,
            generated_at=generated_at,
        )

    paths = OpaqueSplitLockPaths(
        selection=protected / "selection-lock.json",
        final=protected / "final-lock.json",
    )
    for path, value in (
        (paths.selection, record("selection")),
        (paths.final, record("final")),
    ):
        payload = artifact_json_bytes(value)
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise CapstoneIndexError("protected split lock changed on resume")
            continue
        try:
            with path.open("xb") as stream:
                stream.write(payload)
        except FileExistsError as exc:
            raise CapstoneIndexError("protected split lock creation raced") from exc
    if (
        hashlib.sha256(paths.selection.read_bytes()).digest()
        == hashlib.sha256(paths.final.read_bytes()).digest()
    ):
        raise CapstoneIndexError("selection and final lock artifacts must be distinct")
    return paths


def _experiment_root(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise CapstoneIndexError("capstone experiment root must be a real directory")
    absolute = path.absolute()
    resolved = path.resolve(strict=True)
    if absolute != resolved:
        raise CapstoneIndexError("capstone experiment root must be canonical")
    return resolved


def _regular_inside(root: Path, path: Path, *, label: str) -> Path:
    if ".." in path.parts:
        raise CapstoneIndexError(f"{label} path must be canonical")
    lexical = path if path.is_absolute() else root / path
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CapstoneIndexError(f"{label} must stay inside one experiment") from exc
    current = lexical
    while current != root.parent:
        if current.is_symlink():
            raise CapstoneIndexError(f"{label} cannot use symlinks")
        if current == root:
            break
        current = current.parent
    if not resolved.is_file():
        raise CapstoneIndexError(f"{label} must be a regular file")
    return resolved


def _ref(root: Path, path: Path, *, label: str) -> ArtifactRef:
    resolved = _regular_inside(root, path, label=label)
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=resolved.relative_to(root).as_posix(),
        sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
    )


def _from_ref(root: Path, reference: ArtifactRef, *, label: str) -> Path:
    path = _regular_inside(root, root / reference.path, label=label)
    try:
        reference.verify_bytes(path.read_bytes())
    except ValueError as exc:
        raise CapstoneIndexError(f"{label} checksum changed") from exc
    return path


def _same_artifact(left: ArtifactRef, right: ArtifactRef) -> bool:
    return left.path == right.path and left.sha256 == right.sha256


def _record(path: Path, model: type[_Record], *, label: str) -> _Record:
    try:
        payload = path.read_bytes()
        record = model.model_validate_json(payload)
    except (OSError, ValueError) as exc:
        raise CapstoneIndexError(f"{label} is invalid") from exc
    if hasattr(record, "schema_version"):
        try:
            if artifact_json_bytes(record) != payload:  # type: ignore[arg-type]
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise CapstoneIndexError(f"{label} is not canonical JSON") from exc
    return record


def _stage_receipt(
    root: Path,
    reference: ArtifactRef,
    *,
    stage: str,
) -> tuple[ShoppingLearnerReceipt, Path]:
    path = _from_ref(root, reference, label=f"{stage} learner receipt")
    try:
        receipt = ShoppingLearnerReceipt.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise CapstoneIndexError(
            f"{stage} requires a fresh learner-created receipt"
        ) from exc
    if (
        receipt.stage != stage
        or receipt.source_kind != "learner_created"
        or receipt.stop_reason != "completed"
    ):
        raise CapstoneIndexError(
            f"{stage} requires a completed learner-created receipt"
        )
    for artifact in (*receipt.inputs, *receipt.outputs):
        _from_ref(root, artifact, label=f"{stage} receipt artifact")
    return receipt, path


def _output_record(
    root: Path,
    receipt: ShoppingLearnerReceipt,
    model: type[_Record],
    *,
    label: str,
) -> tuple[_Record, Path]:
    for reference in receipt.outputs:
        path = _from_ref(root, reference, label=label)
        try:
            return model.model_validate_json(path.read_bytes()), path
        except ValueError:
            continue
    raise CapstoneIndexError(f"{label} is absent from its learner receipt")


def _load_registry(
    events_path: Path,
    *,
    static_gate_policy: StaticGatePolicy,
) -> SkillRegistry:
    try:
        first_line = events_path.read_bytes().splitlines()[0]
        first = RegistryEvent.model_validate_json(first_line)
        registry = SkillRegistry(
            events_path.parent,
            registry_id=first.registry_id,
            initial_static_gate=lambda source: run_static_gate(
                source,
                policy=static_gate_policy,
            ),
        )
        registry.audit()
        return registry
    except (IndexError, OSError, RegistryError, ValueError) as exc:
        raise CapstoneIndexError("Registry replay failed") from exc


def _manual_branch_events(
    events: Sequence[RegistryEvent],
    *,
    candidate_skill_sha256: str,
    gate_decision_sha256: str,
) -> tuple[RegistryEvent, ...]:
    """Select the one decision branch without treating promotion as a decision."""

    return tuple(
        event
        for event in events
        if event.event_type
        in {
            RegistryEventType.CANDIDATE_ACCEPTED,
            RegistryEventType.CANDIDATE_REJECTED,
        }
        and event.version_sha256 == candidate_skill_sha256
        and event.gate_decision is not None
        and event.gate_decision.sha256 == gate_decision_sha256
    )


def _verify_stage_chain(
    root: Path,
    index: CapstoneIndex,
) -> tuple[
    ShoppingLearnerReceipt,
    PairedComparison,
    Path,
    SkillArtifactManifest,
]:
    create, _ = _stage_receipt(root, index.create_receipt, stage="create")
    static, _ = _stage_receipt(root, index.static_receipt, stage="static")
    trigger, _ = _stage_receipt(root, index.trigger_receipt, stage="trigger")
    paired, _ = _stage_receipt(root, index.paired_receipt, stage="paired")
    receipts = (create, static, trigger, paired)
    if any(
        receipt.profile_sha256 != index.profile_sha256
        or receipt.skill_sha256 != create.skill_sha256
        or receipt.measurement_level.value != index.measurement_kind.value
        for receipt in receipts
    ):
        raise CapstoneIndexError("learner stage receipts mix profile, Skill, or mode")
    if index.mode == "fixed" and any(receipt.network_used for receipt in receipts):
        raise CapstoneIndexError("fixed learner receipts cannot use the network")

    manifest, manifest_path = _output_record(
        root,
        create,
        SkillArtifactManifest,
        label="learner-created runtime manifest",
    )
    if manifest.source_kind == "reference_fallback":
        raise CapstoneIndexError("reference fallback cannot prove learner creation")
    try:
        skill_sha256 = normalized_skill_sha256(manifest_path.parent)
    except (OSError, ValueError) as exc:
        raise CapstoneIndexError("learner-created Skill runtime is invalid") from exc
    if skill_sha256 != create.skill_sha256:
        raise CapstoneIndexError("create receipt does not bind its Skill runtime")

    static_report, _ = _output_record(
        root,
        static,
        StaticGateReport,
        label="Static report",
    )
    if (
        static_report.status is not StaticGateStatus.PASS
        or static_report.skill_sha256 != create.skill_sha256
        or static.primary_metrics.get("static_gate") != "pass"
    ):
        raise CapstoneIndexError("Static learner evidence did not pass")

    trigger_result, _ = _output_record(
        root,
        trigger,
        TriggerEvalResult,
        label="Trigger result",
    )
    if (
        trigger_result.skill_sha256 != create.skill_sha256
        or trigger_result.measurement_kind is not index.measurement_kind
        or (trigger_result.tp, trigger_result.fn) != (10, 0)
        or (trigger_result.tn, trigger_result.fp) != (10, 0)
        or trigger_result.precision != 1
        or trigger_result.recall != 1
        or len(trigger_result.prompts) != 20
    ):
        raise CapstoneIndexError("Trigger evidence is not the locked 10/10 suite")

    comparison, comparison_path = _output_record(
        root,
        paired,
        PairedComparison,
        label="fresh paired comparison",
    )
    if (
        comparison.schema_version is not SchemaVersion.V1ALPHA2
        or comparison.skill_sha256 != create.skill_sha256
        or comparison.measurement_kind is not index.measurement_kind
        or comparison.data_version != index.profile_sha256
        or comparison.baseline_run_id == comparison.skill_run_id
        or len(comparison.cases) != 12
        or comparison.shopping_metrics is None
        or paired.primary_metrics.get("paired_case_count") != 12
    ):
        raise CapstoneIndexError("paired evidence is not a fresh shopping pair")
    comparison.baseline_events.verify_bytes(
        _from_ref(
            root, comparison.baseline_events, label="baseline events"
        ).read_bytes()
    )
    comparison.skill_events.verify_bytes(
        _from_ref(root, comparison.skill_events, label="Skill events").read_bytes()
    )
    return create, comparison, comparison_path, manifest


def _verify_manual_chain(
    root: Path,
    index: CapstoneIndex,
    *,
    create: ShoppingLearnerReceipt,
    comparison: PairedComparison,
    comparison_path: Path,
    static_gate_policy: StaticGatePolicy,
) -> tuple[GateDecision, SkillRegistry, FailureEvidenceFixture, FailureCardSet, Patch]:
    evidence_path = _from_ref(root, index.failure_evidence, label="failure evidence")
    evidence = _record(evidence_path, FailureEvidenceFixture, label="failure evidence")
    if (
        evidence.source.comparison_sha256
        != hashlib.sha256(comparison_path.read_bytes()).hexdigest()
        or evidence.source.pair_execution_sha256 != comparison.pair_execution_sha256
        or evidence.source.baseline_events_sha256 != comparison.baseline_events.sha256
        or evidence.source.skill_events_sha256 != comparison.skill_events.sha256
        or evidence.source.skill_sha256 != create.skill_sha256
        or evidence.source.measurement_kind is not index.measurement_kind
    ):
        raise CapstoneIndexError(
            "failure evidence is not derived from the learner's fresh pair"
        )

    cards_path = _from_ref(root, index.failure_cards, label="Failure Cards")
    cards = _record(cards_path, FailureCardSet, label="Failure Cards")
    if (
        cards.evidence_fixture.sha256 != index.failure_evidence.sha256
        or not cards.cards
        or any(card.attribution.value != "Skill" for card in cards.cards)
    ):
        raise CapstoneIndexError("Failure Cards do not bind learner failure evidence")

    patch_path = _from_ref(root, index.patch, label="learner Patch")
    patch = _record(patch_path, Patch, label="learner Patch")
    card_ids = {card.failure_id for card in cards.cards}
    if patch.parent_skill_sha256 != create.skill_sha256 or any(
        not operation.failure_card_ids
        or not set(operation.failure_card_ids).issubset(card_ids)
        for operation in patch.operations
    ):
        raise CapstoneIndexError("learner Patch is not linked to reviewed failures")

    gate_path = _from_ref(root, index.manual_gate_decision, label="manual GateDecision")
    gate = _record(gate_path, GateDecision, label="manual GateDecision")
    if (
        gate.schema_version is not SchemaVersion.V1ALPHA2
        or gate.accepted_skill_sha256 != patch.parent_skill_sha256
        or gate.mode != index.mode
        or gate.measurement_kind is not index.measurement_kind
        or gate.network_used != (index.mode == "live")
    ):
        raise CapstoneIndexError("manual GateDecision mixes profile or lineage inputs")

    events_path = _from_ref(
        root, index.manual_registry_events, label="manual Registry history"
    )
    if events_path.name != "events.jsonl":
        raise CapstoneIndexError("Registry history must reference events.jsonl")
    registry = _load_registry(
        events_path,
        static_gate_policy=static_gate_policy,
    )
    state = registry.audit()
    if gate.lineage_id != state.lineage_id or gate.lineage_id != index.lineage_id:
        raise CapstoneIndexError("manual GateDecision belongs to another lineage")
    try:
        candidate_path = _from_ref(
            registry.root,
            gate.candidate,
            label="manual Gate candidate snapshot",
        )
        candidate = _record(
            candidate_path,
            CandidateArtifact,
            label="manual Gate candidate snapshot",
        )
    except CapstoneIndexError as exc:
        raise CapstoneIndexError(
            "manual Gate candidate evidence is incomplete"
        ) from exc
    if (
        candidate.patch != patch
        or candidate.content_sha256 != gate.candidate_skill_sha256
        or candidate.candidate_id != gate.candidate_id
    ):
        raise CapstoneIndexError("manual Gate did not evaluate the learner Patch")

    branch_events = _manual_branch_events(
        state.events,
        candidate_skill_sha256=gate.candidate_skill_sha256,
        gate_decision_sha256=index.manual_gate_decision.sha256,
    )
    expected_event = (
        RegistryEventType.CANDIDATE_ACCEPTED
        if gate.outcome is GateOutcome.ACCEPTED
        else RegistryEventType.CANDIDATE_REJECTED
    )
    if len(branch_events) != 1 or branch_events[0].event_type is not expected_event:
        raise CapstoneIndexError("manual Gate lacks the correct Registry branch")
    promoted = any(
        event.event_type is RegistryEventType.PROMOTED
        and event.version_sha256 == gate.candidate_skill_sha256
        for event in state.events
    )
    if promoted != (gate.outcome is GateOutcome.ACCEPTED):
        raise CapstoneIndexError("manual Registry promotion disagrees with Gate")
    return gate, registry, evidence, cards, patch


def _verify_auto(
    root: Path,
    index: CapstoneIndex,
    *,
    registry: SkillRegistry,
    manual_gate: GateDecision,
) -> tuple[AutoEvolveState, tuple[GateDecision, ...]]:
    state_path = _from_ref(root, index.auto_evolve_state, label="auto-evolve state")
    state = _record(state_path, AutoEvolveState, label="auto-evolve state")
    config_path = state_path.parent / "config.json"
    config = _record(config_path, AutoEvolveConfig, label="auto-evolve config")
    if (
        state.status is not AutoLoopStatus.FINAL_COMPLETE
        or state.experiment_id != index.experiment_id
        or state.config_sha256 != content_sha256(config)
        or config.mode != index.mode
        or state.completed_rounds < 2
        or state.current_accepted_skill_sha256 != index.current_accepted_skill_sha256
    ):
        raise CapstoneIndexError("auto-evolve state is not a completed capstone loop")
    outcomes = {row.gate_outcome for row in state.rounds}
    registry_state = registry.audit()
    has_rollback = any(
        event.event_type is RegistryEventType.ROLLED_BACK
        for event in registry_state.events
    )
    if GateOutcome.ACCEPTED not in outcomes or (
        GateOutcome.REJECTED not in outcomes and not has_rollback
    ):
        raise CapstoneIndexError("auto-evolve needs an accept and reject or rollback")
    if registry_state.current_accepted_sha256 != state.current_accepted_skill_sha256:
        raise CapstoneIndexError("auto-evolve state and Registry current disagree")

    decisions: list[GateDecision] = []
    for row in state.rounds:
        rollout_path = _from_ref(root, row.rollout, label="auto rollout receipt")
        rollout = _record(
            rollout_path, AutoRolloutReceipt, label="auto rollout receipt"
        )
        expected_source = (
            "fresh_fixed_execution" if index.mode == "fixed" else "fresh_develop_run"
        )
        if (
            rollout.source_kind != expected_source
            or rollout.measurement_kind is not index.measurement_kind
            or rollout.network_used != (index.mode == "live")
        ):
            raise CapstoneIndexError("checked-in reference cannot prove auto-evolve")
        decision_path = _from_ref(root, row.gate_decision, label="auto GateDecision")
        decision = _record(decision_path, GateDecision, label="auto GateDecision")
        if (
            decision.schema_version is not SchemaVersion.V1ALPHA2
            or decision.lineage_id != index.lineage_id
            or decision.mode != index.mode
            or decision.measurement_kind is not index.measurement_kind
            or decision.outcome is not row.gate_outcome
        ):
            raise CapstoneIndexError("auto GateDecision mixes another experiment")
        decisions.append(decision)

    manual_branch = next(
        (
            event
            for event in registry_state.events
            if event.version_sha256 == manual_gate.candidate_skill_sha256
            and event.gate_decision is not None
        ),
        None,
    )
    first_auto = next(
        (
            event
            for event in registry_state.events
            if event.event_type is RegistryEventType.CANDIDATE_REGISTERED
            and event.version_sha256 == state.rounds[0].candidate_skill_sha256
        ),
        None,
    )
    if (
        manual_branch is None
        or first_auto is None
        or manual_branch.sequence >= first_auto.sequence
    ):
        raise CapstoneIndexError("manual Gate must precede auto-evolve")
    return state, tuple(decisions)


def _verify_final(
    root: Path,
    index: CapstoneIndex,
) -> CapstoneFinalReceipt:
    final_path = _from_ref(root, index.final_receipt, label="capstone final receipt")
    final = _record(final_path, CapstoneFinalReceipt, label="capstone final receipt")
    if (
        final.experiment_id != index.experiment_id
        or final.lineage_id != index.lineage_id
        or final.profile_sha256 != index.profile_sha256
        or final.subject_skill_sha256 != index.current_accepted_skill_sha256
        or final.measurement_kind is not index.measurement_kind
        or final.safety_violation_count != 0
    ):
        raise CapstoneIndexError("failed or cross-lineage final blocks CapstoneIndex")
    expected_origin = (
        "fresh_fixed_execution" if index.mode == "fixed" else "live_measured"
    )
    if final.result_origin != expected_origin:
        raise CapstoneIndexError("final must be a fresh learner execution")

    aggregate_path = _from_ref(root, final.aggregate, label="final aggregate")
    run_path = _from_ref(root, final.final_run_receipt, label="final run receipt")
    checkpoint_path = _from_ref(
        root, final.one_time_checkpoint, label="final one-time checkpoint"
    )
    aggregate = _record(aggregate_path, FinalAggregateReport, label="final aggregate")
    run = _record(run_path, FinalRunReceipt, label="final run receipt")
    checkpoint = _record(
        checkpoint_path, FinalConsumedCheckpoint, label="final one-time checkpoint"
    )
    expected_result_source = (
        "fresh_fixed_execution" if index.mode == "fixed" else "canonical_live"
    )
    if (
        aggregate.schema_version is not SchemaVersion.V1ALPHA2
        or aggregate.case_count != 12
        or aggregate.safety_violation_count != final.safety_violation_count
        or not aggregate.scenario_metrics
        or aggregate.result_source != expected_result_source
        or aggregate.subject_skill_sha256 != final.subject_skill_sha256
        or run.subject_skill_sha256 != final.subject_skill_sha256
        or checkpoint.subject_skill_sha256 != final.subject_skill_sha256
        or run.aggregate_report_sha256 != final.aggregate.sha256
        or checkpoint.aggregate_report_sha256 != final.aggregate.sha256
        or checkpoint.final_run_receipt_sha256 != final.final_run_receipt.sha256
        or aggregate.private_results_sha256 != run.private_results_sha256
        or checkpoint.private_results_sha256 != run.private_results_sha256
    ):
        raise CapstoneIndexError("final one-time evidence bundle is incomplete")
    return final


def _verify_reviews(
    root: Path,
    index: CapstoneIndex,
    *,
    learner_skill_sha256: str,
    comparison: PairedComparison,
    registry: SkillRegistry,
) -> None:
    receipts = tuple(
        _record(
            _from_ref(root, reference, label="learner review receipt"),
            CapstoneReviewReceipt,
            label="learner review receipt",
        )
        for reference in index.review_receipts
    )
    if (
        len(receipts) != len(_REVIEW_KINDS)
        or {receipt.review_kind for receipt in receipts} != _REVIEW_KINDS
    ):
        raise CapstoneIndexError("CapstoneIndex requires every learner review kind")
    traces = {
        reference
        for row in comparison.cases
        for reference in (row.baseline_trace, row.skill_trace)
        if reference is not None
    }
    registry_state = registry.audit()
    for receipt in receipts:
        if (
            receipt.experiment_id != index.experiment_id
            or receipt.profile_sha256 != index.profile_sha256
            or receipt.learner_skill_sha256 != learner_skill_sha256
            or receipt.measurement_kind is not index.measurement_kind
            or receipt.source_kind != "learner_review"
        ):
            raise CapstoneIndexError("learner review receipt mixes another experiment")
        reviewed_path = _from_ref(
            root, receipt.reviewed_artifact, label="reviewed learner artifact"
        )
        if receipt.review_kind == "paired_trace" and not any(
            _same_artifact(receipt.reviewed_artifact, trace) for trace in traces
        ):
            raise CapstoneIndexError("paired Trace review is not from the fresh pair")
        if receipt.review_kind == "failure_evidence" and (
            not _same_artifact(receipt.reviewed_artifact, index.failure_evidence)
        ):
            raise CapstoneIndexError("failure evidence review does not match the index")
        if receipt.review_kind == "failure_card" and (
            not _same_artifact(receipt.reviewed_artifact, index.failure_cards)
        ):
            raise CapstoneIndexError("Failure Card review does not match the index")
        if receipt.review_kind == "registry_history" and (
            not _same_artifact(receipt.reviewed_artifact, index.manual_registry_events)
        ):
            raise CapstoneIndexError("Registry review does not match the index")
        if receipt.review_kind == "gate_decision":
            reviewed = _record(
                reviewed_path, GateDecision, label="reviewed rejected GateDecision"
            )
            if reviewed.outcome is not GateOutcome.REJECTED or not any(
                event.gate_decision is not None
                and event.gate_decision.sha256 == receipt.reviewed_artifact.sha256
                for event in registry_state.events
            ):
                raise CapstoneIndexError("learner must review a rejected GateDecision")


def _verify_reports_and_package(
    root: Path,
    index: CapstoneIndex,
    *,
    registry: SkillRegistry,
    state: AutoEvolveState,
    final: CapstoneFinalReceipt,
) -> None:
    l3_path = _from_ref(root, index.l3_report, label="L3 report")
    try:
        expected_l3 = render_l3_html(load_l3_inputs(root, registry=registry)).encode(
            "utf-8"
        )
    except ValueError as exc:
        raise CapstoneIndexError("L3 report inputs are invalid") from exc
    if l3_path.read_bytes() != expected_l3:
        raise CapstoneIndexError("L3 report is not rendered from this experiment")

    portfolio_path = _from_ref(
        root, index.portfolio_manifest, label="portfolio manifest"
    )
    portfolio = _record(portfolio_path, PortfolioManifest, label="portfolio manifest")
    try:
        portfolio_semantic_sha256(portfolio_path.parent)
    except ValueError as exc:
        raise CapstoneIndexError("portfolio inventory is invalid") from exc
    if portfolio.experiment_id != index.experiment_id:
        raise CapstoneIndexError("portfolio belongs to another experiment")

    release_path = _from_ref(root, index.release_manifest, label="release manifest")
    release = _record(
        release_path,
        AcceptedSkillReleaseManifest,
        label="accepted release manifest",
    )
    runtime_path = _from_ref(
        root, index.package_runtime_manifest, label="package runtime manifest"
    )
    if runtime_path.name != "skill-manifest.json" or release_path.name != (
        "release-manifest.json"
    ):
        raise CapstoneIndexError("package refs do not identify an accepted package")
    runtime = _record(runtime_path, SkillArtifactManifest, label="runtime manifest")
    try:
        package_sha256 = normalized_skill_sha256(runtime_path.parent)
    except (OSError, ValueError) as exc:
        raise CapstoneIndexError("packaged runtime is invalid") from exc
    registry_state = registry.audit()
    current = registry_state.versions[registry_state.current_accepted_sha256]
    current_gate_sha256 = (
        None if current.gate_decision is None else current.gate_decision.sha256
    )
    expected_files = {
        "release-manifest.json",
        "skill/skill-manifest.json",
        *(f"skill/{item.path}" for item in release.runtime_files),
    }
    actual_files = {
        path.relative_to(release_path.parent).as_posix()
        for path in release_path.parent.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if (
        state.status is not AutoLoopStatus.FINAL_COMPLETE
        or final.safety_violation_count != 0
        or release.lineage_id != index.lineage_id
        or release.accepted_skill_sha256 != index.current_accepted_skill_sha256
        or release.package_sha256 != index.current_accepted_skill_sha256
        or release.current_event_sha256 != registry_state.events[-1].event_sha256
        or not _same_artifact(release.final_receipt, index.final_receipt)
        or not _same_artifact(release.runtime_manifest, index.package_runtime_manifest)
        or not _same_artifact(release.registry_events, index.manual_registry_events)
        or release.gate_decision.sha256 != current_gate_sha256
        or release.measurement_kind is not index.measurement_kind
        or release.result_origin != final.result_origin
        or runtime.source_kind == "reference_fallback"
        or runtime.files != release.runtime_files
        or package_sha256 != index.current_accepted_skill_sha256
        or actual_files != expected_files
    ):
        raise CapstoneIndexError("package is not the Registry-current accepted Skill")


def _verify_source_learning_index(root: Path, index: CapstoneIndex) -> None:
    if index.source_learning_index is None:
        return
    source_path = _from_ref(
        root, index.source_learning_index, label="source learning index"
    )
    source = _record(source_path, CapstoneIndex, label="source learning index")
    if (
        source.mode != "fixed"
        or source.measurement_kind is not MeasurementKind.SYNTHETIC_OFFLINE
        or source.experiment_id == index.experiment_id
        or source.profile_sha256 == index.profile_sha256
    ):
        raise CapstoneIndexError(
            "live source learning index is not independent fixed work"
        )

    def evidence_hashes(value: CapstoneIndex) -> set[str]:
        hashes: set[str] = set()
        for name in type(value).model_fields:
            if name == "source_learning_index":
                continue
            field = getattr(value, name)
            if isinstance(field, ArtifactRef):
                hashes.add(field.sha256)
            elif isinstance(field, tuple):
                hashes.update(
                    item.sha256 for item in field if isinstance(item, ArtifactRef)
                )
        return hashes

    source_hashes = evidence_hashes(source)
    current_hashes = evidence_hashes(index)
    if source_hashes & current_hashes:
        raise CapstoneIndexError("fixed and live evidence cannot backfill each other")


def _verify(
    root: Path,
    index: CapstoneIndex,
    *,
    static_gate_policy: StaticGatePolicy,
) -> CapstoneIndex:
    create, comparison, comparison_path, _ = _verify_stage_chain(root, index)
    manual_gate, registry, _, _, _ = _verify_manual_chain(
        root,
        index,
        create=create,
        comparison=comparison,
        comparison_path=comparison_path,
        static_gate_policy=static_gate_policy,
    )
    state, _ = _verify_auto(
        root,
        index,
        registry=registry,
        manual_gate=manual_gate,
    )
    final = _verify_final(root, index)
    _verify_reviews(
        root,
        index,
        learner_skill_sha256=create.skill_sha256,
        comparison=comparison,
        registry=registry,
    )
    _verify_reports_and_package(
        root,
        index,
        registry=registry,
        state=state,
        final=final,
    )
    _verify_source_learning_index(root, index)
    if (
        index.lineage_id != registry.audit().lineage_id
        or index.total_cost_amount != state.total_cost_amount
        or index.cost_currency != state.cost_currency
        or index.cost_complete != state.cost_complete
        or index.network_used != (index.mode == "live")
    ):
        raise CapstoneIndexError(
            "CapstoneIndex summary disagrees with canonical evidence"
        )
    return index


def verify_capstone_index(
    experiment_root: Path,
    index: CapstoneIndex | Path,
    *,
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
) -> CapstoneIndex:
    """Replay every learner, Registry, final, report, and package reference."""

    root = _experiment_root(experiment_root)
    if isinstance(index, Path):
        index_path = _regular_inside(root, index, label="CapstoneIndex")
        index_record = _record(index_path, CapstoneIndex, label="CapstoneIndex")
    else:
        index_record = index
    return _verify(
        root,
        index_record,
        static_gate_policy=static_gate_policy,
    )


def build_capstone_index(
    *,
    experiment_root: Path,
    output_path: Path,
    create_receipt: Path,
    static_receipt: Path,
    trigger_receipt: Path,
    paired_receipt: Path,
    review_receipts: Sequence[Path],
    failure_evidence: Path,
    failure_cards: Path,
    patch: Path,
    manual_gate_decision: Path,
    registry_root: Path,
    auto_evolve_state: Path,
    final_receipt: Path,
    l3_report: Path,
    portfolio_manifest: Path,
    release_manifest: Path,
    package_runtime_manifest: Path,
    created_at: datetime,
    source_learning_index: Path | None = None,
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
) -> CapstoneIndex:
    """Create a completion index only after a full mechanical replay succeeds."""

    root = _experiment_root(experiment_root)
    registry_events = registry_root / "events.jsonl"
    create = _stage_receipt(
        root, _ref(root, create_receipt, label="create receipt"), stage="create"
    )[0]
    events_path = _regular_inside(root, registry_events, label="Registry events")
    registry = _load_registry(
        events_path,
        static_gate_policy=static_gate_policy,
    )
    registry_state = registry.audit()
    state_path = _regular_inside(root, auto_evolve_state, label="auto-evolve state")
    state = _record(state_path, AutoEvolveState, label="auto-evolve state")
    final_path = _regular_inside(root, final_receipt, label="capstone final receipt")
    final = _record(final_path, CapstoneFinalReceipt, label="capstone final receipt")
    mode: Literal["fixed", "live"] = (
        "fixed"
        if create.measurement_level.value == MeasurementKind.SYNTHETIC_OFFLINE.value
        else "live"
    )
    record = CapstoneIndex(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="capstone_index",
        experiment_id=state.experiment_id,
        lineage_id=registry_state.lineage_id,
        profile_sha256=create.profile_sha256,
        mode=mode,
        learning_completion="workflow_complete",
        measurement_kind=MeasurementKind(create.measurement_level.value),
        network_used=mode == "live",
        current_accepted_skill_sha256=registry_state.current_accepted_sha256,
        total_cost_amount=state.total_cost_amount,
        cost_currency=state.cost_currency,
        cost_complete=state.cost_complete,
        create_receipt=_ref(root, create_receipt, label="create receipt"),
        static_receipt=_ref(root, static_receipt, label="Static receipt"),
        trigger_receipt=_ref(root, trigger_receipt, label="Trigger receipt"),
        paired_receipt=_ref(root, paired_receipt, label="paired receipt"),
        review_receipts=tuple(
            _ref(root, path, label="review receipt") for path in review_receipts
        ),
        failure_evidence=_ref(root, failure_evidence, label="failure evidence"),
        failure_cards=_ref(root, failure_cards, label="Failure Cards"),
        patch=_ref(root, patch, label="Patch"),
        manual_gate_decision=_ref(
            root, manual_gate_decision, label="manual GateDecision"
        ),
        manual_registry_events=_ref(root, events_path, label="Registry events"),
        auto_evolve_state=_ref(root, state_path, label="auto-evolve state"),
        final_receipt=_ref(root, final_path, label="capstone final receipt"),
        l3_report=_ref(root, l3_report, label="L3 report"),
        portfolio_manifest=_ref(root, portfolio_manifest, label="portfolio manifest"),
        release_manifest=_ref(root, release_manifest, label="release manifest"),
        package_runtime_manifest=_ref(
            root, package_runtime_manifest, label="package runtime manifest"
        ),
        source_learning_index=(
            None
            if source_learning_index is None
            else _ref(root, source_learning_index, label="source learning index")
        ),
        created_at=created_at,
    )
    if final.subject_skill_sha256 != record.current_accepted_skill_sha256:
        raise CapstoneIndexError("final subject is not Registry current accepted")
    _verify(root, record, static_gate_policy=static_gate_policy)
    output = output_path if output_path.is_absolute() else root / output_path
    if output.exists() or output.is_symlink():
        raise CapstoneIndexError("CapstoneIndex output must not exist")
    try:
        output.resolve().relative_to(root)
    except ValueError as exc:
        raise CapstoneIndexError(
            "CapstoneIndex output must stay in the experiment"
        ) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(artifact_json_bytes(record))
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return verify_capstone_index(
        root,
        output,
        static_gate_policy=static_gate_policy,
    )


__all__ = [
    "CapstoneIndexError",
    "OpaqueSplitLockPaths",
    "build_capstone_index",
    "verify_capstone_index",
    "write_opaque_split_locks",
]
