from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ses.contracts import GateOutcome, artifact_json_bytes
from ses.shopping.course_workflow import (
    ShoppingLearnerReceipt,
    ShoppingPairedStageResult,
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_course import build_fixed_develop_evaluation
from ses.shopping.manual_workflow import (
    ShoppingEvolutionStageResult,
    ShoppingGateStageResult,
    register_shopping_candidate,
    run_shopping_evolution_stage,
    run_shopping_gate_stage,
)
from ses.shopping.profile import LoadedShoppingProfile, load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.shopping.reviews import ShoppingReviewError, write_shopping_review
from tests.shopping._fixed_v0_pipeline import CAPSTONE_ROOT, build_fixed_v0_pipeline

REVIEWED_AT = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class EvolvedCourse:
    root: Path
    profile: LoadedShoppingProfile
    paired: ShoppingPairedStageResult
    evolved: ShoppingEvolutionStageResult


@dataclass(frozen=True, slots=True)
class GovernedCourse:
    course: EvolvedCourse
    gate: ShoppingGateStageResult


def _build_evolved_course(tmp_path: Path) -> EvolvedCourse:
    root = tmp_path / "shopping-course"
    profile = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=root,
    )
    static = run_shopping_static_stage(
        profile=profile,
        experiment_root=root,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )
    trigger = run_shopping_trigger_stage(
        profile=profile,
        experiment_root=root,
        skill_source=created.skill_source,
        static_receipt=static.receipt_path,
    )
    fixed = build_fixed_develop_evaluation(
        profile,
        learner_skill_sha256=created.receipt.skill_sha256,
        learner_skill_source=created.skill_source,
    )
    paired = run_shopping_paired_stage(
        profile=profile,
        experiment_root=root,
        skill_source=created.skill_source,
        trigger_receipt=trigger.receipt_path,
        tasks=fixed.tasks,
        baseline_evaluator=fixed.baseline_evaluator,
        skill_evaluator=fixed.skill_evaluator,
    )
    evolved = run_shopping_evolution_stage(
        profile=profile,
        experiment_root=root,
        paired_receipt=paired.receipt_path,
    )
    return EvolvedCourse(root=root, profile=profile, paired=paired, evolved=evolved)


def _build_governed_course(
    tmp_path: Path,
    *,
    scenario: str,
) -> GovernedCourse:
    course = _build_evolved_course(tmp_path)
    registry_root = course.root / "registry"
    lineage_id = (
        f"lineage-shopping-{course.profile.profile.mode}-"
        f"{course.profile.profile_sha256[:16]}"
    )
    open_shopping_registry(registry_root).initialize(
        command_id="command-shopping-initialize",
        accepted_skill=course.root / "skill" / "v0",
        evidence_paths=(course.paired.summary_path,),
        occurred_at=REVIEWED_AT,
        lineage_id=lineage_id,
    )
    register_shopping_candidate(
        registry_root=registry_root,
        candidate_bundle=course.evolved.candidate_bundle,
    )
    gate = run_shopping_gate_stage(
        profile=course.profile,
        experiment_root=course.root,
        registry_root=registry_root,
        candidate_bundle=course.evolved.candidate_bundle,
        scenario=scenario,  # type: ignore[arg-type]
    )
    return GovernedCourse(course=course, gate=gate)


def test_paired_trace_review_is_canonical_safe_and_idempotent(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    trace = pipeline.paired.comparison.cases[0].skill_trace
    assert trace is not None
    trace_path = pipeline.root / trace.path

    first = write_shopping_review(
        pipeline.profile,
        pipeline.root,
        "paired_trace",
        trace_path,
        REVIEWED_AT,
    )
    second = write_shopping_review(
        pipeline.profile,
        pipeline.root,
        "paired_trace",
        trace_path,
        REVIEWED_AT,
    )

    expected_experiment_id = (
        f"experiment-shopping-fixed-{pipeline.profile.profile_sha256[:16]}"
    )
    assert first == second
    assert first.receipt_path == pipeline.root / "reviews" / "paired_trace.json"
    assert first.receipt_path.read_bytes() == artifact_json_bytes(first.receipt)
    assert first.receipt.experiment_id == expected_experiment_id
    assert first.receipt.learner_skill_sha256 == pipeline.paired.receipt.skill_sha256
    assert first.receipt.reviewed_artifact == trace
    assert first.summary == {
        "experiment_id": expected_experiment_id,
        "learner_skill_sha256": pipeline.paired.receipt.skill_sha256,
        "profile_sha256": pipeline.profile.profile_sha256,
        "receipt": "reviews/paired_trace.json",
        "review_kind": "paired_trace",
        "reviewed_artifact": trace.model_dump(mode="json"),
        "reviewed_at": "2026-08-20T09:30:00Z",
        "stage": "learner_review",
    }
    assert "private" not in str(first.summary).casefold()


def test_failure_evidence_review_requires_the_root_pair_projection(
    tmp_path: Path,
) -> None:
    course = _build_evolved_course(tmp_path)

    result = write_shopping_review(
        course.profile,
        course.root,
        "failure_evidence",
        course.evolved.evidence_path,
        REVIEWED_AT,
    )

    assert result.receipt.reviewed_artifact.path == "failure-evidence.json"
    duplicate = course.root / "copied-failure-evidence.json"
    duplicate.write_bytes(course.evolved.evidence_path.read_bytes())
    with pytest.raises(ShoppingReviewError, match="root failure evidence"):
        write_shopping_review(
            course.profile,
            course.root,
            "failure_evidence",
            duplicate,
            REVIEWED_AT,
        )


def test_failure_card_review_requires_the_manual_evolution_card_set(
    tmp_path: Path,
) -> None:
    course = _build_evolved_course(tmp_path)

    result = write_shopping_review(
        course.profile,
        course.root,
        "failure_card",
        course.evolved.failure_cards_path,
        REVIEWED_AT,
    )

    assert (
        result.receipt.reviewed_artifact.path == "manual-evolution/failure-cards.json"
    )
    duplicate = course.root / "manual-evolution" / "copied-failure-cards.json"
    duplicate.write_bytes(course.evolved.failure_cards_path.read_bytes())
    with pytest.raises(ShoppingReviewError, match="manual-evolution Failure Cards"):
        write_shopping_review(
            course.profile,
            course.root,
            "failure_card",
            duplicate,
            REVIEWED_AT,
        )


def test_gate_review_requires_a_rejected_decision_referenced_by_registry(
    tmp_path: Path,
) -> None:
    governed = _build_governed_course(tmp_path / "rejected", scenario="trigger-failure")
    course = governed.course
    assert governed.gate.decision.outcome is GateOutcome.REJECTED

    result = write_shopping_review(
        course.profile,
        course.root,
        "gate_decision",
        governed.gate.decision_path,
        REVIEWED_AT,
    )

    assert result.receipt.reviewed_artifact.path.endswith("/gate-decision.json")
    duplicate = course.root / "copied-rejected-gate-decision.json"
    duplicate.write_bytes(governed.gate.decision_path.read_bytes())
    with pytest.raises(ShoppingReviewError, match="Registry event"):
        write_shopping_review(
            course.profile,
            course.root,
            "gate_decision",
            duplicate,
            REVIEWED_AT,
        )

    accepted = _build_governed_course(tmp_path / "accepted", scenario="accept")
    assert accepted.gate.decision.outcome is GateOutcome.ACCEPTED
    with pytest.raises(ShoppingReviewError, match="rejected"):
        write_shopping_review(
            accepted.course.profile,
            accepted.course.root,
            "gate_decision",
            accepted.gate.decision_path,
            REVIEWED_AT,
        )


def test_registry_history_review_replays_the_exact_profile_lineage(
    tmp_path: Path,
) -> None:
    governed = _build_governed_course(tmp_path / "matching", scenario="trigger-failure")
    course = governed.course
    events = course.root / "registry" / "events.jsonl"

    result = write_shopping_review(
        course.profile,
        course.root,
        "registry_history",
        events,
        REVIEWED_AT,
    )

    assert result.receipt.reviewed_artifact.path == "registry/events.jsonl"
    duplicate = course.root / "copied-events.jsonl"
    duplicate.write_bytes(events.read_bytes())
    with pytest.raises(ShoppingReviewError, match="exact Registry events"):
        write_shopping_review(
            course.profile,
            course.root,
            "registry_history",
            duplicate,
            REVIEWED_AT,
        )

    wrong = _build_evolved_course(tmp_path / "wrong-lineage")
    wrong_events = wrong.root / "registry" / "events.jsonl"
    open_shopping_registry(wrong.root / "registry").initialize(
        command_id="command-shopping-initialize-wrong-lineage",
        accepted_skill=wrong.root / "skill" / "v0",
        evidence_paths=(wrong.paired.summary_path,),
        occurred_at=REVIEWED_AT,
        lineage_id="lineage-shopping-fixed-wrong-profile",
    )
    with pytest.raises(ShoppingReviewError, match="lineage or profile"):
        write_shopping_review(
            wrong.profile,
            wrong.root,
            "registry_history",
            wrong_events,
            REVIEWED_AT,
        )


def test_review_rejects_outside_symlink_and_private_final_paths(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    trace = pipeline.paired.comparison.cases[0].skill_trace
    assert trace is not None
    trace_path = pipeline.root / trace.path
    outside = tmp_path / "outside-trace.json"
    outside.write_bytes(trace_path.read_bytes())
    symlink = pipeline.root / "trace-link.json"
    symlink.symlink_to(trace_path)
    private = pipeline.root / "final" / "private-results.json"
    private.parent.mkdir()
    private.write_bytes(trace_path.read_bytes())

    for unsafe in (outside, symlink, private):
        with pytest.raises(ShoppingReviewError):
            write_shopping_review(
                pipeline.profile,
                pipeline.root,
                "paired_trace",
                unsafe,
                REVIEWED_AT,
            )


def test_review_binds_the_create_receipt_and_rejects_conflicting_resume(
    tmp_path: Path,
) -> None:
    conflict = build_fixed_v0_pipeline(tmp_path / "conflict")
    trace = conflict.paired.comparison.cases[0].skill_trace
    assert trace is not None
    trace_path = conflict.root / trace.path
    write_shopping_review(
        conflict.profile,
        conflict.root,
        "paired_trace",
        trace_path,
        REVIEWED_AT,
    )
    with pytest.raises(ShoppingReviewError, match="changed on resume"):
        write_shopping_review(
            conflict.profile,
            conflict.root,
            "paired_trace",
            trace_path,
            datetime(2026, 8, 20, 9, 31, tzinfo=UTC),
        )

    tampered = build_fixed_v0_pipeline(tmp_path / "tampered")
    tampered_trace = tampered.paired.comparison.cases[0].skill_trace
    assert tampered_trace is not None
    create_path = tampered.root / "receipts" / "create.json"
    create = ShoppingLearnerReceipt.model_validate_json(create_path.read_bytes())
    create_path.write_bytes(
        artifact_json_bytes(create.model_copy(update={"skill_sha256": "f" * 64}))
    )
    with pytest.raises(ShoppingReviewError, match="bind learner Skill"):
        write_shopping_review(
            tampered.profile,
            tampered.root,
            "paired_trace",
            tampered.root / tampered_trace.path,
            REVIEWED_AT,
        )


def test_review_receipt_directory_cannot_be_a_symlink(tmp_path: Path) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    trace = pipeline.paired.comparison.cases[0].skill_trace
    assert trace is not None
    outside = tmp_path / "outside-reviews"
    outside.mkdir()
    (pipeline.root / "reviews").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ShoppingReviewError, match="directory cannot be a symlink"):
        write_shopping_review(
            pipeline.profile,
            pipeline.root,
            "paired_trace",
            pipeline.root / trace.path,
            REVIEWED_AT,
        )
