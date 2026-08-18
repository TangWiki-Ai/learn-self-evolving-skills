from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    ContractModel,
    EvidenceRef,
    EvolutionPipelineSummary,
    FailureAttribution,
    FailureCard,
    FailureCardSet,
    FailureCategory,
    FailureEvidenceFixture,
    JudgeSimulatorHealth,
    MeasurementKind,
    PairCategory,
    PairedComparison,
    Patch,
    RunEventType,
    RunnerStatus,
    RunRecord,
    SchemaVersion,
    SkillArtifactManifest,
    VersionedRecord,
    artifact_json_bytes,
    normalized_files_sha256,
)
from ses.evolution.candidate import CandidateError, create_candidate
from ses.evolution.diagnosis import (
    FailureObservation,
    analyze_fixture,
    attribute_failure,
    build_failure_card_set,
)
from ses.evolution.evidence import (
    export_failure_evidence,
    linked_evidence_ref,
    load_failure_evidence,
)
from ses.evolution.patches import PatchValidationError, apply_patch
from ses.evolution.updater import UPDATER_SKILL_SPEC, FakeUpdater
from ses.evolution.workflow import EvolutionWorkflowError, run_evolution_workflow
from ses.evolution.workspace import UpdaterWorkspaceError, create_updater_workspace
from ses.skills.installer import normalized_skill_sha256

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
LIVE = ROOT / "tests/fixtures/evolution/live-failure-evidence.json"
SYNTHETIC = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
CARDS_JSON = (
    ROOT
    / "course/ch08-evidence-linked-candidate/artifacts/synthetic-failure-cards.json"
)
PATCH_JSON = (
    ROOT / "course/ch08-evidence-linked-candidate/artifacts/evidence-linked-patch.json"
)
PARENT_HASH = "a19c423b65f9ef7960d682045832f7a8bf57fbbda759a42e102cb28ddfc8ef26"


def _card_set() -> FailureCardSet:
    return FailureCardSet.model_validate_json(CARDS_JSON.read_text())


def _cards() -> tuple[FailureCard, ...]:
    return _card_set().cards


def _patch() -> Patch:
    return Patch.model_validate_json(PATCH_JSON.read_text())


def _evidence(tmp_path: Path) -> Path:
    path = tmp_path / "failure-evidence.json"
    shutil.copyfile(SYNTHETIC, path)
    return path


def _parent_files() -> dict[str, str]:
    return {
        "SKILL.md": PARENT.joinpath("SKILL.md").read_text(),
        "references/return-workflow.md": PARENT.joinpath(
            "references/return-workflow.md"
        ).read_text(),
    }


def test_failure_analysis_generates_all_six_typed_cards(tmp_path: Path) -> None:
    generated = build_failure_card_set(_evidence(tmp_path))
    assert generated == _card_set()
    assert {card.category for card in generated.cards} == set(FailureCategory)
    assert all(
        card.trace_evidence and card.assertion_evidence for card in generated.cards
    )


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            FailureObservation(False, False, False, True),
            FailureAttribution.RUNTIME_ENVIRONMENT,
        ),
        (FailureObservation(True, False, False, True), FailureAttribution.CASE_GOLD),
        (
            FailureObservation(True, True, False, True),
            FailureAttribution.JUDGE_SIMULATOR,
        ),
        (FailureObservation(True, True, True, True), FailureAttribution.SKILL),
    ],
)
def test_attribution_order_is_fixed(
    observation: FailureObservation, expected: FailureAttribution
) -> None:
    assert attribute_failure(observation).attribution is expected


def test_non_skill_root_short_circuits_before_evidence_or_apply() -> None:
    card = _cards()[0].model_copy(
        update={"attribution": FailureAttribution.RUNTIME_ENVIRONMENT}
    )
    with pytest.raises(PatchValidationError, match="runtime/environment"):
        apply_patch(
            {"SKILL.md": "parent"},
            _patch(),
            cards=(card,),
            evidence_path=Path("does-not-exist.json"),
        )


def test_live_infrastructure_errors_never_generate_a_skill_patch() -> None:
    fixture = load_failure_evidence(LIVE)
    assert (
        sum(case.skill_status.value == "infrastructure_error" for case in fixture.cases)
        == 3
    )
    analysis = analyze_fixture(fixture)
    assert analysis.patch_allowed is False
    assert analysis.cards == ()
    assert "infrastructure_error" in analysis.reason


def test_live_infrastructure_fixture_rejects_even_skill_labeled_operations() -> None:
    operations = tuple(
        operation.model_copy(
            update={
                "trace_evidence": (
                    linked_evidence_ref(LIVE, pointer="/cases/0/trace"),
                ),
                "assertion_evidence": (
                    linked_evidence_ref(LIVE, pointer="/cases/0/assertion"),
                ),
            }
        )
        for operation in _patch().operations
    )
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-live-infrastructure",
        parent_skill_sha256=PARENT_HASH,
        operations=operations,
    )
    with pytest.raises(PatchValidationError, match="infrastructure_error"):
        apply_patch(
            {"SKILL.md": "parent"},
            patch,
            cards=_cards(),
            evidence_path=LIVE,
        )


def test_failure_analysis_rejects_missing_assertion_evidence() -> None:
    fixture = load_failure_evidence(SYNTHETIC)
    cases = list(fixture.cases)
    cases[0] = cases[0].model_copy(update={"assertion": None})

    analysis = analyze_fixture(fixture.model_copy(update={"cases": tuple(cases)}))

    assert not analysis.patch_allowed
    assert "Trace and Assertion" in analysis.reason


@pytest.mark.parametrize(
    "health",
    [JudgeSimulatorHealth.UNHEALTHY, JudgeSimulatorHealth.NOT_REVIEWED],
)
def test_judge_or_simulator_health_blocks_skill_attribution(
    health: JudgeSimulatorHealth,
) -> None:
    fixture = load_failure_evidence(SYNTHETIC)
    cases = list(fixture.cases)
    cases[0] = cases[0].model_copy(update={"judge_simulator_health": health})

    analysis = analyze_fixture(fixture.model_copy(update={"cases": tuple(cases)}))

    assert not analysis.patch_allowed
    assert "Judge/Simulator" in analysis.reason


def test_passing_case_cannot_ground_a_patch(tmp_path: Path) -> None:
    fixture = load_failure_evidence(SYNTHETIC)
    cases = list(fixture.cases)
    cases[0] = cases[0].model_copy(
        update={
            "pair_category": PairCategory.BOTH_PASS,
            "skill_status": RunnerStatus.PASS,
            "failure_categories": (),
            "failure_kinds": {},
        }
    )
    evidence = tmp_path / "failure-evidence.json"
    evidence.write_bytes(
        artifact_json_bytes(fixture.model_copy(update={"cases": tuple(cases)}))
    )
    trace = linked_evidence_ref(evidence, pointer="/cases/0/trace")
    assertion = linked_evidence_ref(evidence, pointer="/cases/0/assertion")
    card = _cards()[0].model_copy(
        update={"trace_evidence": (trace,), "assertion_evidence": (assertion,)}
    )
    operation = (
        _patch()
        .operations[1]
        .model_copy(
            update={
                "trace_evidence": (trace,),
                "assertion_evidence": (assertion,),
                "failure_card_ids": (card.failure_id,),
            }
        )
    )
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-pass-case",
        parent_skill_sha256=PARENT_HASH,
        operations=(operation,),
    )

    with pytest.raises(PatchValidationError, match="agent_fail"):
        apply_patch(_parent_files(), patch, cards=(card,), evidence_path=evidence)


def test_tampered_evidence_is_rejected(tmp_path: Path) -> None:
    tampered = _evidence(tmp_path)
    tampered.write_bytes(tampered.read_bytes() + b"tampered\n")
    with pytest.raises(PatchValidationError, match=r"invalid|hash"):
        apply_patch(
            _parent_files(),
            _patch(),
            cards=_cards(),
            evidence_path=tampered,
        )


def test_failure_card_with_unresolvable_evidence_is_rejected(tmp_path: Path) -> None:
    bogus = EvidenceRef(
        artifact=ArtifactRef(
            root=ArtifactRoot.RUN,
            path="missing.json",
            sha256="f" * 64,
        ),
        json_pointer="/missing",
    )
    cards = tuple(
        card.model_copy(
            update={"trace_evidence": (bogus,), "assertion_evidence": (bogus,)}
        )
        for card in _cards()
    )
    with pytest.raises(PatchValidationError, match=r"workspace|fixture"):
        apply_patch(
            _parent_files(),
            _patch(),
            cards=cards,
            evidence_path=_evidence(tmp_path),
        )


def test_plain_observation_cannot_masquerade_as_trace_or_assertion(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    wrong = linked_evidence_ref(evidence, pointer="/cases/5/observation")
    cards = tuple(
        card.model_copy(
            update={"trace_evidence": (wrong,), "assertion_evidence": (wrong,)}
        )
        if card.category is FailureCategory.SAFETY
        else card
        for card in _cards()
    )
    add = (
        _patch()
        .operations[0]
        .model_copy(update={"trace_evidence": (wrong,), "assertion_evidence": (wrong,)})
    )
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-wrong-evidence-kind",
        parent_skill_sha256=PARENT_HASH,
        operations=(add,),
    )
    with pytest.raises(PatchValidationError, match="wrong case"):
        apply_patch(_parent_files(), patch, cards=cards, evidence_path=evidence)


def test_operation_evidence_must_match_every_named_card(tmp_path: Path) -> None:
    add = (
        _patch()
        .operations[0]
        .model_copy(update={"failure_card_ids": ("failure-overload",)})
    )
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-card-evidence-mismatch",
        parent_skill_sha256=PARENT_HASH,
        operations=(add,),
    )
    with pytest.raises(PatchValidationError, match="does not match"):
        apply_patch(
            _parent_files(),
            patch,
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
        )


def test_missing_operation_evidence_is_rejected(tmp_path: Path) -> None:
    operation = _patch().operations[0].model_copy(update={"trace_evidence": ()})
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-missing-evidence",
        parent_skill_sha256=PARENT_HASH,
        operations=(operation,),
    )
    with pytest.raises(PatchValidationError, match="Trace evidence"):
        apply_patch(
            _parent_files(),
            patch,
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
        )


def test_add_update_delete_and_atomic_failure(tmp_path: Path) -> None:
    patch = _patch()
    parent = _parent_files()
    changed = apply_patch(
        parent, patch, cards=_cards(), evidence_path=_evidence(tmp_path)
    )
    assert "references/safety-notes.md" in changed
    assert changed["SKILL.md"] != parent["SKILL.md"]
    assert "references/return-workflow.md" not in changed
    assert "references/return-workflow.md" in parent

    bad_delete = patch.operations[2].model_copy(
        update={"precondition_sha256": "1" * 64}
    )
    bad_patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-atomic-failure",
        parent_skill_sha256=PARENT_HASH,
        operations=(patch.operations[1], bad_delete),
    )
    before = dict(parent)
    with pytest.raises(PatchValidationError, match="stale precondition"):
        apply_patch(
            parent,
            bad_patch,
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
        )
    assert parent == before


def test_patch_preserves_unchanged_non_markdown_references(tmp_path: Path) -> None:
    parent = {**_parent_files(), "references/schema.json": '{"type":"object"}\n'}

    changed = apply_patch(
        parent,
        _patch(),
        cards=_cards(),
        evidence_path=_evidence(tmp_path),
    )

    assert changed["references/schema.json"] == parent["references/schema.json"]


def test_patch_rejects_a_broad_rewrite(tmp_path: Path) -> None:
    update = (
        _patch()
        .operations[1]
        .model_copy(
            update={
                "content": "\n".join(f"replacement line {index}" for index in range(30))
            }
        )
    )
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-broad-rewrite",
        parent_skill_sha256=PARENT_HASH,
        operations=(update,),
    )
    with pytest.raises(PatchValidationError, match="line teaching budget"):
        apply_patch(
            _parent_files(),
            patch,
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
        )


def test_duplicate_targets_are_rejected() -> None:
    patch = _patch()
    with pytest.raises(ValueError, match="conflict"):
        Patch(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="skill_patch",
            patch_id="patch-conflict",
            parent_skill_sha256=PARENT_HASH,
            operations=(patch.operations[0], patch.operations[0]),
        )


def test_stale_parent_precondition_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CandidateError, match="expected"):
        create_candidate(
            parent_dir=PARENT,
            patch=_patch(),
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
            output_dir=tmp_path / "candidate",
            expected_parent_sha256="1" * 64,
        )


def test_candidate_is_immutable_and_passes_static_gate(tmp_path: Path) -> None:
    before_hash = normalized_skill_sha256(PARENT)
    before_files = {
        path.relative_to(PARENT).as_posix(): path.read_bytes()
        for path in PARENT.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "candidate"
    candidate = create_candidate(
        parent_dir=PARENT,
        patch=_patch(),
        cards=_cards(),
        evidence_path=_evidence(tmp_path),
        output_dir=output,
        expected_parent_sha256=PARENT_HASH,
    )
    assert candidate.parent_skill_sha256 == PARENT_HASH
    assert candidate.patch_sha256 == _patch().patch_sha256
    assert candidate.files["SKILL.md"]
    assert normalized_skill_sha256(PARENT) == before_hash
    assert {
        path.relative_to(PARENT).as_posix(): path.read_bytes()
        for path in PARENT.rglob("*")
        if path.is_file()
    } == before_files
    assert (output / "skill-manifest.json").is_file()


def test_candidate_contract_binds_manifest_inventory_hashes_and_version(
    tmp_path: Path,
) -> None:
    candidate = create_candidate(
        parent_dir=PARENT,
        patch=_patch(),
        cards=_cards(),
        evidence_path=_evidence(tmp_path),
        output_dir=tmp_path / "candidate",
    )
    extra_files = dict(candidate.files)
    extra_files["references/outside.txt"] = "undeclared\n"
    extra_hash = normalized_files_sha256(extra_files)
    with pytest.raises(ValueError, match="inventory"):
        candidate.model_copy(
            update={
                "files": extra_files,
                "content_sha256": extra_hash,
                "manifest": candidate.manifest.model_copy(
                    update={"content_sha256": extra_hash}
                ),
            }
        )

    manifest_files = list(candidate.manifest.files)
    manifest_files[0] = manifest_files[0].model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ValueError, match="file hash"):
        candidate.model_copy(
            update={
                "manifest": candidate.manifest.model_copy(
                    update={"files": tuple(manifest_files)}
                )
            }
        )

    with pytest.raises(ValueError, match="version"):
        candidate.model_copy(update={"version": "mismatched-version"})


def test_candidate_output_cannot_be_nested_in_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    shutil.copytree(PARENT, parent)
    before = {
        path.relative_to(parent).as_posix(): path.read_bytes()
        for path in parent.rglob("*")
        if path.is_file()
    }
    with pytest.raises(CandidateError, match="inside"):
        create_candidate(
            parent_dir=parent,
            patch=_patch(),
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
            output_dir=parent / "candidate",
        )
    assert {
        path.relative_to(parent).as_posix(): path.read_bytes()
        for path in parent.rglob("*")
        if path.is_file()
    } == before


def test_candidate_rejects_noncanonical_output_without_side_effects(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    shutil.copytree(PARENT, parent)

    with pytest.raises(CandidateError, match="canonical"):
        create_candidate(
            parent_dir=parent,
            patch=_patch(),
            cards=_cards(),
            evidence_path=_evidence(tmp_path),
            output_dir=parent / "side-effect" / ".." / ".." / "candidate",
        )

    assert not parent.joinpath("side-effect").exists()
    assert not tmp_path.joinpath("candidate").exists()


def test_updater_workspace_contains_cards_spec_and_parent_only(tmp_path: Path) -> None:
    spec = tmp_path / "updater-spec.md"
    spec.write_text(UPDATER_SKILL_SPEC)
    updater = create_updater_workspace(
        failure_cards_path=CARDS_JSON,
        skill_spec_path=spec,
        parent_dir=PARENT,
        root=tmp_path / "workspaces",
    )
    try:
        assert set(updater.visible_files) == {
            "inputs/failure-cards.json",
            "inputs/skill-spec.md",
            "parent-skill/SKILL.md",
            "parent-skill/references/return-workflow.md",
            "parent-skill/skill-manifest.json",
        }
        assert not (updater.workspace.root / "failure-evidence.json").exists()
        assert not (updater.workspace.root / "src").exists()
        assert not (updater.workspace.root / "credentials").exists()
    finally:
        updater.cleanup()
    assert not updater.workspace.root.exists()


@pytest.mark.parametrize(
    "spec_name",
    ["selection-manifest.json", "final-manifest.json"],
)
def test_updater_workspace_rejects_split_manifests_as_skill_specs(
    tmp_path: Path,
    spec_name: str,
) -> None:
    spec = tmp_path / spec_name
    spec.write_text("private split sentinel", encoding="utf-8")

    with pytest.raises(UpdaterWorkspaceError, match="Skill spec"):
        create_updater_workspace(
            failure_cards_path=CARDS_JSON,
            skill_spec_path=spec,
            parent_dir=PARENT,
            root=tmp_path / "workspaces",
        )

    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize(
    "private_directory",
    ["protected", "hidden-selection", "selection-split", "final-split"],
)
def test_updater_workspace_rejects_skill_specs_below_protected_split_names(
    tmp_path: Path,
    private_directory: str,
) -> None:
    directory = tmp_path / private_directory
    directory.mkdir()
    spec = directory / "updater-spec.md"
    spec.write_text(UPDATER_SKILL_SPEC, encoding="utf-8")

    with pytest.raises(UpdaterWorkspaceError, match="private Skill spec"):
        create_updater_workspace(
            failure_cards_path=CARDS_JSON,
            skill_spec_path=spec,
            parent_dir=PARENT,
            root=tmp_path / "workspaces",
        )

    assert not (tmp_path / "workspaces").exists()


def test_updater_workspace_rejects_a_symlinked_skill_spec_ancestor(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-spec-directory"
    target.mkdir()
    (target / "updater-spec.md").write_text(UPDATER_SKILL_SPEC, encoding="utf-8")
    alias = tmp_path / "spec-alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(UpdaterWorkspaceError, match="symlink"):
        create_updater_workspace(
            failure_cards_path=CARDS_JSON,
            skill_spec_path=alias / "updater-spec.md",
            parent_dir=PARENT,
            root=tmp_path / "workspaces",
        )

    assert not (tmp_path / "workspaces").exists()


@pytest.mark.parametrize("spec_name", ["updater-spec.md", ".updater-skill-spec.md"])
def test_updater_workspace_allows_supported_skill_spec_names(
    tmp_path: Path,
    spec_name: str,
) -> None:
    release_root = tmp_path / "final-release-worktree"
    release_root.mkdir()
    spec = release_root / spec_name
    spec.write_text(UPDATER_SKILL_SPEC, encoding="utf-8")

    updater = create_updater_workspace(
        failure_cards_path=CARDS_JSON,
        skill_spec_path=spec,
        parent_dir=PARENT,
        root=tmp_path / "workspaces",
    )
    try:
        assert (updater.workspace.root / "inputs/skill-spec.md").read_text(
            encoding="utf-8"
        ) == UPDATER_SKILL_SPEC
    finally:
        updater.cleanup()


@pytest.mark.parametrize("overlap", ["parent", "output"])
def test_evolution_rejects_overlapping_workspace_roots(
    tmp_path: Path, overlap: str
) -> None:
    parent = tmp_path / "parent"
    shutil.copytree(PARENT, parent)
    output = tmp_path / "output"
    workspace_root = (
        parent / "updater-workspaces"
        if overlap == "parent"
        else output / "updater-workspaces"
    )
    before = {
        path.relative_to(parent).as_posix(): path.read_bytes()
        for path in parent.rglob("*")
        if path.is_file()
    }

    with pytest.raises(EvolutionWorkflowError, match="workspace root"):
        run_evolution_workflow(
            parent_dir=parent,
            evidence_path=SYNTHETIC,
            output_root=output,
            updater=FakeUpdater(),
            mode="fixed",
            workspace_root=workspace_root,
        )

    assert not workspace_root.exists()
    assert not output.exists()
    assert before == {
        path.relative_to(parent).as_posix(): path.read_bytes()
        for path in parent.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "path_kind", ["parent_workspace", "output_workspace", "output"]
)
def test_evolution_rejects_noncanonical_paths_without_side_effects(
    tmp_path: Path, path_kind: str
) -> None:
    parent = tmp_path / "parent"
    shutil.copytree(PARENT, parent)
    output = tmp_path / "output"
    workspace_root = tmp_path / "workspaces"
    if path_kind == "parent_workspace":
        workspace_root = parent / "side-effect" / ".." / ".." / "workspaces"
    elif path_kind == "output_workspace":
        workspace_root = output / "side-effect" / ".." / ".." / "workspaces"
    else:
        output = parent / "side-effect" / ".." / ".." / "bundle"

    with pytest.raises(EvolutionWorkflowError, match="canonical"):
        run_evolution_workflow(
            parent_dir=parent,
            evidence_path=SYNTHETIC,
            output_root=output,
            updater=FakeUpdater(),
            mode="fixed",
            workspace_root=workspace_root,
        )

    assert not parent.joinpath("side-effect").exists()
    assert not tmp_path.joinpath("output").exists()
    assert not tmp_path.joinpath("bundle").exists()
    assert not tmp_path.joinpath("workspaces").exists()


def test_candidate_patch_cli_publishes_one_atomic_bundle(tmp_path: Path) -> None:
    output = tmp_path / "candidate-bundle"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ses.cli.app",
            "candidate-patch",
            "--parent",
            str(PARENT),
            "--evidence",
            str(SYNTHETIC),
            "--patch",
            str(PATCH_JSON),
            "--failure-cards",
            str(CARDS_JSON),
            "--output",
            str(output),
            "--json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.joinpath("skill/skill-manifest.json").is_file()
    assert output.joinpath("candidate.json").is_file()
    assert json.loads(result.stdout)["static_gate_status"] == "pass"


def test_evolve_cli_runs_evidence_to_candidate_vertical_slice(tmp_path: Path) -> None:
    output = tmp_path / "evolution"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ses.cli.app",
            "evolve",
            "--parent",
            str(PARENT),
            "--evidence",
            str(SYNTHETIC),
            "--output",
            str(output),
            "--mode",
            "fixed",
            "--json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["failure_card_count"] == 6
    assert summary["patch_operation_count"] == 3
    assert output.joinpath("failure-evidence.json").is_file()
    assert output.joinpath("failure-cards.json").is_file()
    assert output.joinpath("patch.json").is_file()
    assert output.joinpath("skill/skill-manifest.json").is_file()
    persisted = EvolutionPipelineSummary.model_validate_json(
        output.joinpath("summary.json").read_bytes()
    )
    for reference in (persisted.failure_cards, persisted.patch, persisted.candidate):
        reference.verify_bytes(output.joinpath(reference.path).read_bytes())


def test_evolution_rejects_a_mislabeled_updater_measurement(tmp_path: Path) -> None:
    updater = FakeUpdater()
    updater.measurement_kind = MeasurementKind.LIVE_MEASURED
    output = tmp_path / "mislabeled"

    with pytest.raises(EvolutionWorkflowError, match="measurement kind"):
        run_evolution_workflow(
            parent_dir=PARENT,
            evidence_path=SYNTHETIC,
            output_root=output,
            updater=updater,
            mode="fixed",
            workspace_root=tmp_path / "workspaces",
        )

    assert not output.exists()


def test_export_derives_provenance_from_paired_measurement(tmp_path: Path) -> None:
    comparison_path = ROOT / "course/ch07-create-v0/artifacts/paired-comparison.json"
    comparison = PairedComparison.model_validate_json(comparison_path.read_text())
    output = tmp_path / "evidence.json"
    fixture = export_failure_evidence(
        comparison_path=comparison_path,
        baseline_events_path=(
            ROOT
            / "course/ch07-create-v0/artifacts/run-ticket08-baseline-fixed/events.jsonl"
        ),
        skill_events_path=(
            ROOT
            / "course/ch07-create-v0/artifacts/run-ticket08-skill-v0-fixed/events.jsonl"
        ),
        output_path=output,
        expected_comparison_sha256=hashlib.sha256(
            comparison_path.read_bytes()
        ).hexdigest(),
        expected_pair_execution_sha256=comparison.pair_execution_sha256,
        expected_skill_sha256=comparison.skill_sha256,
    )
    assert fixture.provenance.value == "synthetic"
    assert output.read_bytes() == output.read_bytes().strip()
    events_path = (
        ROOT
        / "course/ch07-create-v0/artifacts/run-ticket08-skill-v0-fixed/events.jsonl"
    )
    first_attempt = next(
        event
        for event in (
            RunRecord.model_validate_json(line)
            for line in events_path.read_text().splitlines()
        )
        if event.event_type is RunEventType.ATTEMPT
    )
    assert fixture.cases[0].trace is not None
    assert fixture.cases[0].trace.sha256 == first_attempt.artifacts.traces[-1].sha256
    assert fixture.cases[0].failure_kinds == {}
    assert all(
        case.judge_simulator_health is JudgeSimulatorHealth.NOT_REVIEWED
        for case in fixture.cases
    )


def test_ticket09_contract_matrix_round_trips_and_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    output = tmp_path / "evolution"
    summary = run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=SYNTHETIC,
        output_root=output,
        updater=FakeUpdater(),
        mode="fixed",
        workspace_root=tmp_path / "workspaces",
    )
    fixture = FailureEvidenceFixture.model_validate_json(
        output.joinpath("failure-evidence.json").read_bytes()
    )
    card_set = FailureCardSet.model_validate_json(
        output.joinpath("failure-cards.json").read_bytes()
    )
    patch = Patch.model_validate_json(output.joinpath("patch.json").read_bytes())
    candidate = CandidateArtifact.model_validate_json(
        output.joinpath("candidate.json").read_bytes()
    )
    manifest = SkillArtifactManifest.model_validate_json(
        output.joinpath("skill/skill-manifest.json").read_bytes()
    )
    trace = fixture.cases[0].trace
    assert trace is not None
    top_level: tuple[VersionedRecord, ...] = (
        fixture,
        card_set.cards[0],
        card_set,
        patch,
        candidate,
        summary,
        manifest,
    )
    nested: tuple[ContractModel, ...] = (
        fixture.source,
        fixture.cases[0],
        trace,
        *patch.operations,
        manifest.files[0],
    )

    for record in (*top_level, *nested):
        model = type(record)
        wire = record.model_dump(mode="python", round_trip=True)
        assert model.model_validate(wire) == record
        wire["unexpected"] = True
        with pytest.raises(ValueError, match="Extra inputs"):
            model.model_validate(wire)

    for record in top_level:
        wire = record.model_dump(mode="python", round_trip=True)
        wire["schema_version"] = "v2"
        with pytest.raises(ValueError, match="unsupported"):
            type(record).model_validate(wire)


def test_failure_card_set_rejects_future_versions_and_extra_fields() -> None:
    value = json.loads(CARDS_JSON.read_text())
    value["schema_version"] = "v2"
    with pytest.raises(ValueError, match="unsupported"):
        FailureCardSet.model_validate(value)
    value = json.loads(CARDS_JSON.read_text())
    value["unexpected"] = True
    with pytest.raises(ValueError, match="Extra inputs"):
        FailureCardSet.model_validate(value)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("failure_categories", ["unknown"]),
        ("judge_simulator_health", "unknown"),
    ],
)
def test_failure_evidence_rejects_unknown_review_enums(
    field: str, invalid: object
) -> None:
    value = json.loads(SYNTHETIC.read_text())
    value["cases"][0][field] = invalid
    with pytest.raises(ValueError, match=field):
        FailureEvidenceFixture.model_validate(value)


def test_patch_rejects_a_tampered_semantic_hash() -> None:
    value = json.loads(PATCH_JSON.read_text())
    value["patch_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="patch hash"):
        Patch.model_validate(value)


@pytest.mark.parametrize("consumer", ["analysis", "patch"])
def test_evidence_consumers_hash_and_parse_one_byte_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    evidence = _evidence(tmp_path)
    original_read_bytes = Path.read_bytes
    reads = 0

    def tracked_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == evidence:
            reads += 1
            if reads > 1:
                raise AssertionError("evidence was read more than once")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    if consumer == "analysis":
        assert build_failure_card_set(evidence).cards
    else:
        assert apply_patch(
            _parent_files(),
            _patch(),
            cards=_cards(),
            evidence_path=evidence,
        )
    assert reads == 1
