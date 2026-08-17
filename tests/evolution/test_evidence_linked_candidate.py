from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ses.contracts import (
    FailureAttribution,
    FailureCard,
    FailureCategory,
    Patch,
    SchemaVersion,
)
from ses.evolution.candidate import CandidateError, create_candidate
from ses.evolution.diagnosis import (
    FailureObservation,
    analyze_fixture,
    attribute_failure,
)
from ses.evolution.evidence import linked_evidence_ref, load_failure_evidence
from ses.evolution.patches import (
    PatchValidationError,
    apply_patch,
)
from ses.evolution.workspace import create_updater_workspace
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


def _cards() -> tuple[FailureCard, ...]:
    return tuple(
        FailureCard.model_validate(value)
        for value in json.loads(CARDS_JSON.read_text())["cards"]
    )


def _patch() -> Patch:
    return Patch.model_validate_json(PATCH_JSON.read_text())


def test_six_failure_card_categories_are_typed() -> None:
    assert {card.category for card in _cards()} == set(FailureCategory)
    assert all(card.provenance.value == "synthetic" for card in _cards())
    assert all(card.trace_evidence and card.assertion_evidence for card in _cards())


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


def test_tampered_evidence_is_rejected(tmp_path: Path) -> None:
    tampered = tmp_path / SYNTHETIC.name
    tampered.write_bytes(SYNTHETIC.read_bytes() + b"tampered\n")
    with pytest.raises(PatchValidationError, match="hash"):
        apply_patch(
            {"SKILL.md": "parent"},
            _patch(),
            cards=_cards(),
            evidence_path=tampered,
        )


def test_missing_evidence_is_rejected(tmp_path: Path) -> None:
    operation = _patch().operations[0].model_copy(update={"trace_evidence": ()})
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-missing-evidence",
        parent_skill_sha256=PARENT_HASH,
        operations=(operation,),
    )
    with pytest.raises(PatchValidationError, match="Trace"):
        apply_patch(
            {"SKILL.md": "parent"},
            patch,
            cards=_cards(),
            evidence_path=SYNTHETIC,
        )


def test_add_update_delete_and_atomic_failure() -> None:
    patch = _patch()
    parent = {
        "SKILL.md": PARENT.joinpath("SKILL.md").read_text(),
        "references/return-workflow.md": PARENT.joinpath(
            "references/return-workflow.md"
        ).read_text(),
    }
    changed = apply_patch(parent, patch, cards=_cards(), evidence_path=SYNTHETIC)
    assert "references/safety-notes.md" in changed
    assert changed["SKILL.md"] != parent["SKILL.md"]
    assert "references/return-workflow.md" not in changed
    assert parent["SKILL.md"] != changed["SKILL.md"]
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
        apply_patch(parent, bad_patch, cards=_cards(), evidence_path=SYNTHETIC)
    assert parent == before


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


def test_stale_parent_precondition_is_rejected() -> None:
    with pytest.raises(CandidateError, match="expected"):
        create_candidate(
            parent_dir=PARENT,
            patch=_patch(),
            cards=_cards(),
            evidence_path=SYNTHETIC,
            output_dir=Path(".ses/test-stale-candidate"),
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
        evidence_path=SYNTHETIC,
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


def test_updater_workspace_isolated_to_fixture_and_parent(tmp_path: Path) -> None:
    updater = create_updater_workspace(
        evidence_path=SYNTHETIC,
        parent_dir=PARENT,
        root=tmp_path / "workspaces",
    )
    try:
        assert "inputs/synthetic-failure-evidence.json" in updater.visible_files
        assert "parent-skill/SKILL.md" in updater.visible_files
        assert all(
            not any(token in path.lower() for token in ("selection", "final", "gold"))
            for path in updater.visible_files
        )
        assert all(not Path(path).is_absolute() for path in updater.visible_files)
        assert not (updater.workspace.root / "src").exists()
        assert not (updater.workspace.root / "credentials").exists()
    finally:
        updater.cleanup()
    assert not updater.workspace.root.exists()


def test_cli_offline_vertical_slice(tmp_path: Path) -> None:
    output = tmp_path / "candidate"
    record = tmp_path / "candidate.json"
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
            "--record-output",
            str(record),
            "--parent-sha256",
            PARENT_HASH,
            "--workspace-root",
            str(tmp_path / "updater-workspaces"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.joinpath("skill-manifest.json").is_file()
    assert json.loads(record.read_text())["static_gate_status"] == "pass"
