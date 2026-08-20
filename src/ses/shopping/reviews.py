"""Mechanically verified learner-review receipts for the shopping capstone."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CapstoneReviewReceipt,
    EvolutionPipelineSummary,
    FailureCardSet,
    FailureEvidenceFixture,
    FailureProvenance,
    GateDecision,
    GateOutcome,
    MeasurementKind,
    PairedComparison,
    SchemaVersion,
    TriggerEvalResult,
    artifact_json_bytes,
)
from ses.evolution.registry import RegistryState
from ses.reporting.l2 import render_l2_html
from ses.shopping.course_workflow import ShoppingLearnerReceipt
from ses.shopping.profile import (
    LoadedShoppingProfile,
    shopping_experiment_id,
    shopping_lineage_id,
)
from ses.shopping.registry import open_shopping_registry
from ses.skills.installer import normalized_skill_sha256

ShoppingReviewKind = Literal[
    "paired_trace",
    "failure_evidence",
    "failure_card",
    "gate_decision",
    "registry_history",
]


class ShoppingReviewError(ValueError):
    """A requested review is not canonical evidence from this experiment."""


@dataclass(frozen=True, slots=True)
class ShoppingReviewResult:
    """The canonical receipt and a content-free learner-facing summary."""

    receipt: CapstoneReviewReceipt
    receipt_path: Path
    summary: Mapping[str, object]


def _experiment_root(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ShoppingReviewError("shopping experiment root does not exist") from exc
    if path.is_symlink() or not resolved.is_dir() or path.absolute() != resolved:
        raise ShoppingReviewError("shopping experiment root must be canonical")
    return resolved


def _regular_inside(root: Path, path: Path, *, label: str) -> Path:
    if ".." in path.parts:
        raise ShoppingReviewError(f"{label} path must be canonical")
    lexical = path if path.is_absolute() else root / path
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ShoppingReviewError(f"{label} must stay inside the experiment") from exc
    if lexical.absolute() != resolved or not resolved.is_file():
        raise ShoppingReviewError(f"{label} must be a regular file without symlinks")
    return resolved


def _ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _resolve_ref(root: Path, reference: ArtifactRef, *, label: str) -> Path:
    if reference.root is not ArtifactRoot.RUN:
        raise ShoppingReviewError(f"{label} must use the experiment run root")
    path = _regular_inside(root, root / reference.path, label=label)
    try:
        reference.verify_bytes(path.read_bytes())
    except ValueError as exc:
        raise ShoppingReviewError(f"{label} checksum does not match") from exc
    return path


def _measurement(profile: LoadedShoppingProfile) -> MeasurementKind:
    return MeasurementKind(profile.profile.measurement_level.value)


def _load_stage_receipt(
    root: Path,
    path: Path,
    *,
    profile: LoadedShoppingProfile,
    stage: Literal["create", "trigger", "paired"],
    learner_skill_sha256: str | None = None,
) -> ShoppingLearnerReceipt:
    receipt_path = _regular_inside(root, path, label=f"{stage} receipt")
    try:
        receipt = ShoppingLearnerReceipt.model_validate_json(receipt_path.read_bytes())
    except ValueError as exc:
        raise ShoppingReviewError(f"{stage} receipt is invalid") from exc
    expected_network = profile.profile.mode == "live"
    if (
        receipt.stage != stage
        or receipt.profile_sha256 != profile.profile_sha256
        or receipt.measurement_level is not profile.profile.measurement_level
        or receipt.network_used != expected_network
        or receipt.source_kind != "learner_created"
        or (
            learner_skill_sha256 is not None
            and receipt.skill_sha256 != learner_skill_sha256
        )
    ):
        raise ShoppingReviewError(f"{stage} receipt belongs to another experiment")
    for reference in (*receipt.inputs, *receipt.outputs):
        _resolve_ref(root, reference, label=f"{stage} receipt artifact")
    return receipt


def _learner_skill(
    root: Path,
    profile: LoadedShoppingProfile,
) -> tuple[str, ShoppingLearnerReceipt]:
    create = _load_stage_receipt(
        root,
        root / "receipts" / "create.json",
        profile=profile,
        stage="create",
    )
    skill = root / "skill" / "v0"
    try:
        actual_sha256 = normalized_skill_sha256(skill)
    except (OSError, ValueError) as exc:
        raise ShoppingReviewError("learner-created Skill v0 is invalid") from exc
    manifest = _regular_inside(
        root,
        skill / "skill-manifest.json",
        label="learner Skill manifest",
    )
    manifest_ref = _ref(root, manifest)
    if create.skill_sha256 != actual_sha256 or manifest_ref not in create.outputs:
        raise ShoppingReviewError("create receipt does not bind learner Skill v0")
    return actual_sha256, create


def _fresh_pair(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
) -> tuple[Path, PairedComparison]:
    paired_receipt = _load_stage_receipt(
        root,
        root / "receipts" / "paired.json",
        profile=profile,
        stage="paired",
        learner_skill_sha256=learner_skill_sha256,
    )
    comparisons: list[tuple[Path, PairedComparison]] = []
    for reference in paired_receipt.outputs:
        path = _resolve_ref(root, reference, label="paired output")
        try:
            comparisons.append(
                (path, PairedComparison.model_validate_json(path.read_bytes()))
            )
        except ValueError:
            continue
    if len(comparisons) != 1:
        raise ShoppingReviewError("paired receipt must bind one fresh comparison")
    comparison_path, comparison = comparisons[0]
    if (
        comparison.schema_version is not SchemaVersion.V1ALPHA2
        or comparison.skill_sha256 != learner_skill_sha256
        or comparison.data_version != profile.profile_sha256
        or comparison.model_lock_sha256 != profile.profile.agent_model_sha256
        or comparison.measurement_kind is not _measurement(profile)
    ):
        raise ShoppingReviewError("paired comparison drifted from the shopping profile")

    trigger_receipt = _load_stage_receipt(
        root,
        root / "receipts" / "trigger.json",
        profile=profile,
        stage="trigger",
        learner_skill_sha256=learner_skill_sha256,
    )
    triggers: list[TriggerEvalResult] = []
    for reference in trigger_receipt.outputs:
        path = _resolve_ref(root, reference, label="Trigger output")
        try:
            triggers.append(TriggerEvalResult.model_validate_json(path.read_bytes()))
        except ValueError:
            continue
    if len(triggers) != 1:
        raise ShoppingReviewError("Trigger receipt must bind one evaluation")
    try:
        render_l2_html(comparison, triggers[0], artifact_root=root)
    except ValueError as exc:
        raise ShoppingReviewError("fresh paired comparison failed replay") from exc
    return comparison_path, comparison


def _paired_trace(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
    artifact: ArtifactRef,
) -> None:
    _, comparison = _fresh_pair(root, profile, learner_skill_sha256)

    traces = {
        reference
        for row in comparison.cases
        for reference in (row.baseline_trace, row.skill_trace)
        if reference is not None
    }
    if artifact not in traces:
        raise ShoppingReviewError("paired Trace is not from the fresh comparison")


def _failure_evidence(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
    artifact_path: Path,
) -> None:
    expected = root / "failure-evidence.json"
    if artifact_path != expected:
        raise ShoppingReviewError("review must use the root failure evidence")
    try:
        fixture = FailureEvidenceFixture.model_validate_json(artifact_path.read_bytes())
    except ValueError as exc:
        raise ShoppingReviewError("root failure evidence is invalid") from exc
    if artifact_path.read_bytes() != artifact_json_bytes(fixture):
        raise ShoppingReviewError("root failure evidence is not canonical")
    comparison_path, comparison = _fresh_pair(
        root,
        profile,
        learner_skill_sha256,
    )
    expected_provenance = (
        FailureProvenance.SYNTHETIC
        if profile.profile.mode == "fixed"
        else FailureProvenance.LIVE
    )
    source = fixture.source
    if (
        fixture.provenance is not expected_provenance
        or source.comparison_sha256
        != hashlib.sha256(comparison_path.read_bytes()).hexdigest()
        or source.pair_execution_sha256 != comparison.pair_execution_sha256
        or source.baseline_events_sha256 != comparison.baseline_events.sha256
        or source.skill_events_sha256 != comparison.skill_events.sha256
        or source.skill_sha256 != learner_skill_sha256
        or source.measurement_kind is not _measurement(profile)
        or any(
            case.shopping_subcode is None
            or case.episode_evidence is None
            or case.raw_reward_evidence is None
            or case.metric_evidence is None
            or not case.safety_evidence
            for case in fixture.cases
        )
    ):
        raise ShoppingReviewError(
            "root failure evidence does not match the fresh shopping pair"
        )


def _failure_cards(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
    artifact_path: Path,
) -> None:
    expected = root / "manual-evolution" / "failure-cards.json"
    if artifact_path != expected:
        raise ShoppingReviewError("review must use the manual-evolution Failure Cards")
    try:
        cards = FailureCardSet.model_validate_json(artifact_path.read_bytes())
    except ValueError as exc:
        raise ShoppingReviewError("manual-evolution Failure Cards are invalid") from exc
    if artifact_path.read_bytes() != artifact_json_bytes(cards):
        raise ShoppingReviewError("manual-evolution Failure Cards are not canonical")

    root_evidence = _regular_inside(
        root,
        root / "failure-evidence.json",
        label="root failure evidence",
    )
    _failure_evidence(root, profile, learner_skill_sha256, root_evidence)
    bundled_evidence = _regular_inside(
        root,
        root / "manual-evolution" / "failure-evidence.json",
        label="bundled failure evidence",
    )
    if bundled_evidence.read_bytes() != root_evidence.read_bytes():
        raise ShoppingReviewError("Failure Cards use different failure evidence")
    expected_evidence_ref = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path="failure-evidence.json",
        sha256=hashlib.sha256(bundled_evidence.read_bytes()).hexdigest(),
    )
    if cards.evidence_fixture != expected_evidence_ref or any(
        card.shopping_subcode is None for card in cards.cards
    ):
        raise ShoppingReviewError(
            "manual-evolution Failure Cards do not bind shopping evidence"
        )

    summary_path = _regular_inside(
        root,
        root / "manual-evolution" / "summary.json",
        label="manual evolution summary",
    )
    try:
        summary = EvolutionPipelineSummary.model_validate_json(
            summary_path.read_bytes()
        )
    except ValueError as exc:
        raise ShoppingReviewError("manual evolution summary is invalid") from exc
    expected_cards_ref = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path="failure-cards.json",
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    )
    if (
        summary.failure_cards != expected_cards_ref
        or summary.parent_skill_sha256 != learner_skill_sha256
        or summary.mode != profile.profile.mode
        or summary.updater_measurement is not _measurement(profile)
    ):
        raise ShoppingReviewError(
            "manual evolution summary does not bind the reviewed Failure Cards"
        )


def _registry_state(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
) -> tuple[Path, RegistryState]:
    registry_root = root / "registry"
    try:
        registry = open_shopping_registry(registry_root)
        state = registry.audit()
    except (OSError, ValueError) as exc:
        raise ShoppingReviewError("shopping Registry replay failed") from exc
    if (
        state.lineage_id != shopping_lineage_id(profile)
        or not state.events
        or state.events[0].version_sha256 != learner_skill_sha256
    ):
        raise ShoppingReviewError("shopping Registry lineage or profile does not match")
    return registry_root, state


def _gate_decision(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
    artifact_path: Path,
) -> None:
    try:
        decision = GateDecision.model_validate_json(artifact_path.read_bytes())
    except ValueError as exc:
        raise ShoppingReviewError("reviewed GateDecision is invalid") from exc
    if artifact_path.read_bytes() != artifact_json_bytes(decision):
        raise ShoppingReviewError("reviewed GateDecision is not canonical")
    if (
        decision.outcome is not GateOutcome.REJECTED
        or decision.lineage_id != shopping_lineage_id(profile)
        or decision.mode != profile.profile.mode
        or decision.measurement_kind is not _measurement(profile)
        or decision.network_used != (profile.profile.mode == "live")
    ):
        raise ShoppingReviewError("learner review requires a rejected GateDecision")

    registry_root, state = _registry_state(
        root,
        profile,
        learner_skill_sha256,
    )
    try:
        relative = artifact_path.relative_to(registry_root).as_posix()
    except ValueError as exc:
        raise ShoppingReviewError(
            "rejected GateDecision is not referenced by a Registry event"
        ) from exc
    registry_ref = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=relative,
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    )
    if not any(event.gate_decision == registry_ref for event in state.events):
        raise ShoppingReviewError(
            "rejected GateDecision is not referenced by a Registry event"
        )


def _registry_history(
    root: Path,
    profile: LoadedShoppingProfile,
    learner_skill_sha256: str,
    artifact_path: Path,
) -> None:
    expected = root / "registry" / "events.jsonl"
    if artifact_path != expected:
        raise ShoppingReviewError("review must use the exact Registry events.jsonl")
    registry_root, state = _registry_state(
        root,
        profile,
        learner_skill_sha256,
    )
    if registry_root / "events.jsonl" != artifact_path or not state.events:
        raise ShoppingReviewError("review must use the exact Registry events.jsonl")


def _write_receipt(path: Path, payload: bytes) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise ShoppingReviewError("review receipt directory cannot be a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.absolute() != parent.resolve():
        raise ShoppingReviewError("review receipt directory must be canonical")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ShoppingReviewError("review receipt changed on resume")
        return
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise ShoppingReviewError("review receipt creation raced") from exc


def write_shopping_review(
    profile: LoadedShoppingProfile,
    experiment_root: Path,
    review_kind: ShoppingReviewKind,
    artifact_path: Path,
    reviewed_at: datetime,
) -> ShoppingReviewResult:
    """Verify one learner-opened artifact and write its idempotent receipt."""

    root = _experiment_root(experiment_root)
    artifact = _regular_inside(root, artifact_path, label="reviewed artifact")
    artifact_ref = _ref(root, artifact)
    learner_skill_sha256, _ = _learner_skill(root, profile)
    if review_kind == "paired_trace":
        _paired_trace(root, profile, learner_skill_sha256, artifact_ref)
    elif review_kind == "failure_evidence":
        _failure_evidence(root, profile, learner_skill_sha256, artifact)
    elif review_kind == "failure_card":
        _failure_cards(root, profile, learner_skill_sha256, artifact)
    elif review_kind == "gate_decision":
        _gate_decision(root, profile, learner_skill_sha256, artifact)
    elif review_kind == "registry_history":
        _registry_history(root, profile, learner_skill_sha256, artifact)
    else:
        raise ShoppingReviewError(f"unsupported shopping review kind: {review_kind}")

    experiment_id = shopping_experiment_id(profile)
    receipt = CapstoneReviewReceipt(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="capstone_review_receipt",
        experiment_id=experiment_id,
        profile_sha256=profile.profile_sha256,
        learner_skill_sha256=learner_skill_sha256,
        measurement_kind=_measurement(profile),
        network_used=profile.profile.mode == "live",
        source_kind="learner_review",
        review_kind=review_kind,
        reviewed_artifact=artifact_ref,
        reviewed_at=reviewed_at,
    )
    receipt_path = root / "reviews" / f"{review_kind}.json"
    _write_receipt(receipt_path, artifact_json_bytes(receipt))
    receipt_json = receipt.model_dump(mode="json")
    summary: dict[str, object] = {
        "experiment_id": receipt.experiment_id,
        "learner_skill_sha256": receipt.learner_skill_sha256,
        "profile_sha256": receipt.profile_sha256,
        "receipt": receipt_path.relative_to(root).as_posix(),
        "review_kind": receipt.review_kind,
        "reviewed_artifact": receipt.reviewed_artifact.model_dump(mode="json"),
        "reviewed_at": receipt_json["reviewed_at"],
        "stage": "learner_review",
    }
    return ShoppingReviewResult(
        receipt=receipt,
        receipt_path=receipt_path,
        summary=summary,
    )


__all__ = [
    "ShoppingReviewError",
    "ShoppingReviewKind",
    "ShoppingReviewResult",
    "write_shopping_review",
]
