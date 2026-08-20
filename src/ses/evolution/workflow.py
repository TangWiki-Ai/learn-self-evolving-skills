"""Atomic Lesson 8 evidence-to-candidate workflow."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    EvolutionPipelineSummary,
    FailureCardSet,
    MeasurementKind,
    Patch,
    SchemaVersion,
    VersionedRecord,
    artifact_json_bytes,
)
from ses.evolution.candidate import (
    create_candidate,
    load_runtime_files,
    write_candidate_record,
)
from ses.evolution.diagnosis import (
    RETURN_DIAGNOSIS_POLICY,
    FailureDiagnosisPolicy,
    build_failure_card_set,
    write_failure_card_set,
)
from ses.evolution.updater import (
    RETURN_UPDATER_POLICY,
    Updater,
    UpdaterPolicy,
    UpdaterRequest,
)
from ses.evolution.workspace import create_updater_workspace
from ses.skills.installer import normalized_skill_sha256
from ses.skills.static_gate import DEFAULT_STATIC_GATE_POLICY, StaticGatePolicy


class EvolutionWorkflowError(ValueError):
    """The complete evidence-to-candidate workflow could not be published."""


def _artifact_ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _write_record(path: Path, value: VersionedRecord) -> None:
    with path.open("xb") as stream:
        stream.write(artifact_json_bytes(value))


def _validate_destination(parent_dir: Path, output_root: Path) -> Path:
    if ".." in output_root.parts:
        raise EvolutionWorkflowError("evolution output path must be canonical")
    if output_root.exists() or output_root.is_symlink():
        raise EvolutionWorkflowError("evolution output must not already exist")
    parent_root = parent_dir.resolve()
    destination = output_root.resolve()
    if destination == parent_root or destination.is_relative_to(parent_root):
        raise EvolutionWorkflowError(
            "evolution output cannot be inside the parent Skill"
        )
    return destination


def _validate_workspace_root(
    parent_dir: Path,
    output_root: Path,
    workspace_root: Path | None,
) -> Path | None:
    if workspace_root is None:
        return None
    if ".." in workspace_root.parts:
        raise EvolutionWorkflowError("Updater workspace root path must be canonical")
    if workspace_root.is_symlink():
        raise EvolutionWorkflowError("Updater workspace root cannot be a symlink")
    if workspace_root.exists() and not workspace_root.is_dir():
        raise EvolutionWorkflowError("Updater workspace root must be a directory")
    root = workspace_root.resolve()
    parent = parent_dir.resolve()
    output = output_root.resolve()
    if root == parent or root.is_relative_to(parent):
        raise EvolutionWorkflowError(
            "Updater workspace root cannot be inside the parent Skill"
        )
    if root == output or root.is_relative_to(output):
        raise EvolutionWorkflowError(
            "Updater workspace root cannot be inside the evolution output"
        )
    return root


def publish_candidate_bundle(
    *,
    parent_dir: Path,
    evidence_path: Path,
    card_set: FailureCardSet,
    patch: Patch,
    output_root: Path,
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
) -> CandidateArtifact:
    """Atomically publish a pre-reviewed Patch and its complete audit bundle."""
    output_root = _validate_destination(parent_dir, output_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".candidate-bundle-", dir=output_root.parent)
    )
    try:
        bundled_evidence = staging / "failure-evidence.json"
        shutil.copyfile(evidence_path, bundled_evidence, follow_symlinks=False)
        expected_ref = _artifact_ref(staging, bundled_evidence)
        if card_set.evidence_fixture != expected_ref:
            raise EvolutionWorkflowError(
                "Failure Card set does not reference the bundled evidence"
            )
        write_failure_card_set(staging / "failure-cards.json", card_set)
        _write_record(staging / "patch.json", patch)
        candidate = create_candidate(
            parent_dir=parent_dir,
            patch=patch,
            cards=card_set.cards,
            evidence_path=bundled_evidence,
            output_dir=staging / "skill",
            expected_parent_sha256=patch.parent_skill_sha256,
            static_gate_policy=static_gate_policy,
        )
        write_candidate_record(staging / "candidate.json", candidate)
        os.replace(staging, output_root)
        return candidate
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def run_evolution_workflow(
    *,
    parent_dir: Path,
    evidence_path: Path,
    output_root: Path,
    updater: Updater,
    mode: Literal["fixed", "live"],
    workspace_root: Path | None = None,
    diagnosis_policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY,
    updater_policy: UpdaterPolicy = RETURN_UPDATER_POLICY,
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
) -> EvolutionPipelineSummary:
    """Analyze, propose, validate, gate, and atomically publish one bundle."""
    if diagnosis_policy.policy_id != updater_policy.policy_id:
        raise EvolutionWorkflowError(
            "Diagnosis and Updater policies must target the same domain"
        )
    output_root = _validate_destination(parent_dir, output_root)
    workspace_root = _validate_workspace_root(parent_dir, output_root, workspace_root)

    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evolution-", dir=output_root.parent))
    updater_workspace = None
    try:
        bundled_evidence = staging / "failure-evidence.json"
        shutil.copyfile(evidence_path, bundled_evidence, follow_symlinks=False)
        card_set = build_failure_card_set(
            bundled_evidence,
            policy=diagnosis_policy,
        )
        cards_path = staging / "failure-cards.json"
        write_failure_card_set(cards_path, card_set)

        spec_path = staging / ".updater-skill-spec.md"
        spec_path.write_text(updater_policy.skill_spec, encoding="utf-8")
        updater_workspace = create_updater_workspace(
            failure_cards_path=cards_path,
            skill_spec_path=spec_path,
            parent_dir=parent_dir,
            root=workspace_root,
        )
        workspace_parent = updater_workspace.workspace.root / "parent-skill"
        parent_files = load_runtime_files(workspace_parent)
        parent_hash = normalized_skill_sha256(workspace_parent)
        patch = updater.propose(
            UpdaterRequest(
                workspace=updater_workspace.workspace.root,
                visible_files=updater_workspace.visible_files,
                cards=card_set.cards,
                parent_files=parent_files,
                parent_skill_sha256=parent_hash,
                policy=updater_policy,
            )
        )
        updater_workspace.cleanup()
        updater_workspace = None
        spec_path.unlink()

        patch_path = staging / "patch.json"
        _write_record(patch_path, patch)
        candidate = create_candidate(
            parent_dir=parent_dir,
            patch=patch,
            cards=card_set.cards,
            evidence_path=bundled_evidence,
            output_dir=staging / "skill",
            expected_parent_sha256=parent_hash,
            diagnosis_policy=diagnosis_policy,
            static_gate_policy=static_gate_policy,
        )
        candidate_path = staging / "candidate.json"
        write_candidate_record(candidate_path, candidate)

        expected_measurement = (
            MeasurementKind.LIVE_MEASURED
            if mode == "live"
            else MeasurementKind.SYNTHETIC_OFFLINE
        )
        if updater.measurement_kind is not expected_measurement:
            raise EvolutionWorkflowError(
                "Updater measurement kind does not match evolution mode"
            )
        summary = EvolutionPipelineSummary(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="evolution_pipeline_summary",
            mode=mode,
            evidence_provenance=card_set.provenance,
            updater_measurement=updater.measurement_kind,
            updater_usage=updater.usage,
            updater_latency_ms=updater.latency_ms,
            failure_card_count=len(card_set.cards),
            patch_operation_count=len(patch.operations),
            parent_skill_sha256=parent_hash,
            candidate_skill_sha256=candidate.content_sha256,
            failure_cards=_artifact_ref(staging, cards_path),
            patch=_artifact_ref(staging, patch_path),
            candidate=_artifact_ref(staging, candidate_path),
        )
        _write_record(staging / "summary.json", summary)
        os.replace(staging, output_root)
        return summary
    except Exception:
        if updater_workspace is not None:
            updater_workspace.cleanup()
        if staging.exists():
            shutil.rmtree(staging)
        raise
