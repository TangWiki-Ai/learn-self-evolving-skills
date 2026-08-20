from __future__ import annotations

import json
from pathlib import Path

import pytest

from ses.contracts import (
    SHOPPING_FAILURE_CATEGORY_BY_SUBCODE,
    EvidenceArtifact,
    FailureAttribution,
    FailureCardSet,
    FailureCategory,
    JudgeSimulatorHealth,
    Patch,
    ShoppingFailureSubcode,
    artifact_json_bytes,
)
from ses.evolution.candidate import load_runtime_files
from ses.evolution.candidate_bundle import capture_candidate_bundle
from ses.evolution.diagnosis import (
    SHOPPING_DIAGNOSIS_POLICY,
    DiagnosisError,
    build_failure_card_set,
)
from ses.evolution.evidence import load_failure_evidence
from ses.evolution.patches import PatchValidationError, apply_patch
from ses.evolution.updater import (
    SHOPPING_UPDATER_POLICY,
    FakeUpdater,
    UpdaterError,
    UpdaterRequest,
)
from ses.evolution.workflow import EvolutionWorkflowError, run_evolution_workflow
from ses.skills.installer import normalized_skill_sha256

ROOT = Path(__file__).parents[2]
SYNTHETIC = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
CARDS_JSON = (
    ROOT
    / "course/ch08-evidence-linked-candidate/artifacts/synthetic-failure-cards.json"
)
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
SHA = "a" * 64
SHOPPING_SUBCODES = (
    ShoppingFailureSubcode.MISSED_PRE_PURCHASE,
    ShoppingFailureSubcode.CONSTRAINT_LOST,
    ShoppingFailureSubcode.MISSING_CRITICAL_QUESTION,
    ShoppingFailureSubcode.BENCHMARK_TERM_EXPOSED,
    ShoppingFailureSubcode.CLARIFIED_TOO_LATE,
    ShoppingFailureSubcode.UNAUTHORIZED_PURCHASE,
)


def _shopping_evidence(tmp_path: Path) -> Path:
    fixture = load_failure_evidence(SYNTHETIC)
    cases = []
    for index, (case, subcode) in enumerate(
        zip(fixture.cases, SHOPPING_SUBCODES, strict=True),
        1,
    ):
        prefix = f"develop/case-{index:03d}"
        cases.append(
            case.model_copy(
                update={
                    "shopping_subcode": subcode,
                    "episode_evidence": EvidenceArtifact(
                        kind="episode",
                        source_file=f"{prefix}/episode.json",
                        sha256=SHA,
                    ),
                    "raw_reward_evidence": EvidenceArtifact(
                        kind="raw_reward",
                        source_file=f"{prefix}/raw-reward.json",
                        sha256=SHA,
                    ),
                    "metric_evidence": EvidenceArtifact(
                        kind="metric",
                        source_file=f"{prefix}/metric.json",
                        sha256=SHA,
                    ),
                    "safety_evidence": (
                        EvidenceArtifact(
                            kind="safety",
                            source_file=f"{prefix}/safety.json",
                            sha256=SHA,
                        ),
                    ),
                }
            )
        )
    path = tmp_path / "shopping-failure-evidence.json"
    path.write_bytes(
        artifact_json_bytes(fixture.model_copy(update={"cases": tuple(cases)}))
    )
    return path


def test_shopping_diagnosis_maps_six_subcode_families_and_preserves_domain_refs(
    tmp_path: Path,
) -> None:
    cards = build_failure_card_set(
        _shopping_evidence(tmp_path),
        policy=SHOPPING_DIAGNOSIS_POLICY,
    )

    assert {card.category for card in cards.cards} == set(FailureCategory)
    assert tuple(card.shopping_subcode for card in cards.cards) == SHOPPING_SUBCODES
    assert all(card.episode_evidence for card in cards.cards)
    assert all(card.raw_reward_evidence for card in cards.cards)
    assert all(card.metric_evidence for card in cards.cards)
    assert all(card.safety_evidence for card in cards.cards)
    assert all(
        card.shopping_subcode_protocol == "shopping-failure-subcodes-v1"
        for card in cards.cards
    )


def test_shopping_subcode_contract_covers_every_locked_spec_11_subcode() -> None:
    assert set(SHOPPING_FAILURE_CATEGORY_BY_SUBCODE) == set(ShoppingFailureSubcode)
    assert {
        category: {
            subcode.value
            for subcode, mapped_category in SHOPPING_FAILURE_CATEGORY_BY_SUBCODE.items()
            if mapped_category is category
        }
        for category in FailureCategory
    } == {
        FailureCategory.TRIGGER: {
            "missed_pre_purchase",
            "triggered_post_purchase",
        },
        FailureCategory.PATTERN: {
            "constraint_lost",
            "query_repetition",
            "premature_candidate",
            "option_mismatch",
            "detail_not_verified",
        },
        FailureCategory.OVERLOAD: {
            "missing_critical_question",
            "redundant_question",
            "asked_known_fact",
        },
        FailureCategory.TERMINOLOGY: {
            "benchmark_term_exposed",
            "hidden_profile_exposed",
        },
        FailureCategory.TIMING: {
            "clarified_too_late",
            "premature_purchase",
            "continued_after_terminal",
        },
        FailureCategory.SAFETY: {
            "unauthorized_purchase",
            "purchase_after_rejection",
            "catalog_instruction_followed",
            "gold_leak",
        },
    }


def test_return_evidence_and_cards_keep_their_exact_v1alpha1_wire() -> None:
    fixture = load_failure_evidence(SYNTHETIC)
    cards = FailureCardSet.model_validate_json(CARDS_JSON.read_bytes())

    assert fixture.model_dump(mode="json") == json.loads(SYNTHETIC.read_bytes())
    assert cards.model_dump(mode="json") == json.loads(CARDS_JSON.read_bytes())


def test_shopping_evidence_requires_explicit_diagnosis_policy(tmp_path: Path) -> None:
    with pytest.raises(DiagnosisError, match="shopping evidence requires"):
        build_failure_card_set(_shopping_evidence(tmp_path))


def test_updater_rejects_shopping_cards_under_the_return_policy(
    tmp_path: Path,
) -> None:
    cards = build_failure_card_set(
        _shopping_evidence(tmp_path),
        policy=SHOPPING_DIAGNOSIS_POLICY,
    )
    updater = FakeUpdater()

    with pytest.raises(UpdaterError, match="domain"):
        updater.propose(
            UpdaterRequest(
                workspace=tmp_path,
                visible_files=(),
                cards=cards.cards,
                parent_files=load_runtime_files(PARENT),
                parent_skill_sha256=normalized_skill_sha256(PARENT),
            )
        )


def test_shopping_workflow_injects_policy_and_publishes_only_skill_root_patch(
    tmp_path: Path,
) -> None:
    updater = FakeUpdater()
    output = tmp_path / "shopping-evolution"

    summary = run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=_shopping_evidence(tmp_path),
        output_root=output,
        updater=updater,
        mode="fixed",
        workspace_root=tmp_path / "workspaces",
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
    )

    assert updater.last_request is not None
    assert updater.last_request.policy is SHOPPING_UPDATER_POLICY
    assert all(
        card.attribution is FailureAttribution.SKILL
        and card.shopping_subcode is not None
        and card.metric_evidence
        for card in updater.last_request.cards
    )
    patch = Patch.model_validate_json(output.joinpath("patch.json").read_bytes())
    assert len(patch.operations) == summary.patch_operation_count == 3
    assert {operation.operation for operation in patch.operations} == {
        "add",
        "update",
        "delete",
    }
    assert all(
        operation.failure_card_ids
        and operation.trace_evidence
        and operation.assertion_evidence
        for operation in patch.operations
    )
    assert capture_candidate_bundle(output).candidate.content_sha256 == (
        summary.candidate_skill_sha256
    )


def test_patch_consumer_rejects_a_shopping_subcode_not_bound_to_its_case(
    tmp_path: Path,
) -> None:
    output = tmp_path / "shopping-evolution"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=_shopping_evidence(tmp_path),
        output_root=output,
        updater=FakeUpdater(),
        mode="fixed",
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
    )
    cards = FailureCardSet.model_validate_json(
        output.joinpath("failure-cards.json").read_bytes()
    )
    changed_cards = list(cards.cards)
    changed_cards[0] = changed_cards[0].model_copy(
        update={"shopping_subcode": ShoppingFailureSubcode.TRIGGERED_POST_PURCHASE}
    )
    patch = Patch.model_validate_json(output.joinpath("patch.json").read_bytes())

    with pytest.raises(PatchValidationError, match="subcode"):
        apply_patch(
            load_runtime_files(PARENT),
            patch,
            cards=tuple(changed_cards),
            evidence_path=output / "failure-evidence.json",
        )


def test_shopping_workflow_stops_before_updater_for_non_skill_root(
    tmp_path: Path,
) -> None:
    evidence_path = _shopping_evidence(tmp_path)
    fixture = load_failure_evidence(evidence_path)
    cases = list(fixture.cases)
    cases[0] = cases[0].model_copy(
        update={"judge_simulator_health": JudgeSimulatorHealth.UNHEALTHY}
    )
    evidence_path.write_bytes(
        artifact_json_bytes(fixture.model_copy(update={"cases": tuple(cases)}))
    )
    updater = FakeUpdater()
    output = tmp_path / "blocked-shopping-evolution"

    with pytest.raises(DiagnosisError, match="Judge/Simulator"):
        run_evolution_workflow(
            parent_dir=PARENT,
            evidence_path=evidence_path,
            output_root=output,
            updater=updater,
            mode="fixed",
            workspace_root=tmp_path / "blocked-workspaces",
            diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
            updater_policy=SHOPPING_UPDATER_POLICY,
        )

    assert updater.last_request is None
    assert not output.exists()


def test_workflow_rejects_mixed_domain_policies_before_updater(tmp_path: Path) -> None:
    updater = FakeUpdater()
    output = tmp_path / "mixed-policy-evolution"

    with pytest.raises(EvolutionWorkflowError, match="same domain"):
        run_evolution_workflow(
            parent_dir=PARENT,
            evidence_path=_shopping_evidence(tmp_path),
            output_root=output,
            updater=updater,
            mode="fixed",
            diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        )

    assert updater.last_request is None
    assert not output.exists()
