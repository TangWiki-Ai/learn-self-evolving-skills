"""Ordered failure attribution and Skill-patch eligibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ses.contracts import (
    FAILURE_ATTRIBUTION_ORDER,
    ArtifactRef,
    EvidenceRef,
    FailureAttribution,
    FailureCard,
    FailureCardSet,
    FailureCategory,
    FailureEvidenceCase,
    FailureEvidenceFixture,
    FailureProvenance,
    JudgeSimulatorHealth,
    RunnerStatus,
    SchemaVersion,
    artifact_json_bytes,
)
from ses.evolution.evidence import (
    load_failure_evidence_verified,
)


class DiagnosisError(ValueError):
    """The evidence cannot justify a Skill patch."""


ATTRIBUTION_ORDER: Final[tuple[FailureAttribution, ...]] = FAILURE_ATTRIBUTION_ORDER


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """Small diagnostic input used by deterministic tests and adapters."""

    runtime_healthy: bool
    case_gold_healthy: bool
    judge_simulator_healthy: bool
    skill_failed: bool


@dataclass(frozen=True, slots=True)
class Diagnosis:
    attribution: FailureAttribution
    patch_allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class FixtureAnalysis:
    cards: tuple[FailureCard, ...]
    patch_allowed: bool
    reason: str


_SUGGESTED_SCOPES: Final[dict[FailureCategory, str]] = {
    FailureCategory.TRIGGER: "the Skill description or one trigger instruction",
    FailureCategory.PATTERN: "one reusable workflow instruction",
    FailureCategory.OVERLOAD: "one redundant instruction or reference",
    FailureCategory.TERMINOLOGY: "one customer-facing wording instruction",
    FailureCategory.TIMING: "one action-ordering instruction",
    FailureCategory.SAFETY: "one safety guardrail",
}


def _attribute_fixture_case(case: FailureEvidenceCase) -> Diagnosis:
    diagnosis = attribute_failure(
        FailureObservation(
            runtime_healthy=(
                case.skill_status is not RunnerStatus.INFRASTRUCTURE_ERROR
            ),
            case_gold_healthy=case.baseline_status is RunnerStatus.PASS,
            judge_simulator_healthy=(
                case.judge_simulator_health is JudgeSimulatorHealth.HEALTHY
                and case.assertion is not None
            ),
            skill_failed=case.skill_status is RunnerStatus.AGENT_FAIL,
        )
    )
    if diagnosis.attribution is FailureAttribution.JUDGE_SIMULATOR:
        if case.assertion is None:
            return Diagnosis(
                diagnosis.attribution,
                False,
                "Skill failure requires both Trace and Assertion evidence",
            )
        if case.judge_simulator_health is JudgeSimulatorHealth.NOT_REVIEWED:
            return Diagnosis(
                diagnosis.attribution,
                False,
                "Judge/Simulator health has not been reviewed",
            )
        if case.judge_simulator_health is JudgeSimulatorHealth.UNHEALTHY:
            return Diagnosis(
                diagnosis.attribution,
                False,
                "Judge/Simulator review found an unhealthy evaluation protocol",
            )
    return diagnosis


def attribute_failure(observation: FailureObservation) -> Diagnosis:
    """Apply runtime -> case/gold -> Judge/Simulator -> Skill, exactly in order."""
    if not observation.runtime_healthy:
        return Diagnosis(
            FailureAttribution.RUNTIME_ENVIRONMENT,
            False,
            "runtime/environment evidence is not healthy",
        )
    if not observation.case_gold_healthy:
        return Diagnosis(
            FailureAttribution.CASE_GOLD,
            False,
            "case/gold evidence is not trustworthy",
        )
    if not observation.judge_simulator_healthy:
        return Diagnosis(
            FailureAttribution.JUDGE_SIMULATOR,
            False,
            "Judge/Simulator evidence is not trustworthy",
        )
    if not observation.skill_failed:
        raise DiagnosisError("no observed Skill failure exists")
    return Diagnosis(
        FailureAttribution.SKILL,
        True,
        "the remaining observed failure is attributable to Skill behavior",
    )


def analyze_fixture(fixture: FailureEvidenceFixture) -> FixtureAnalysis:
    """Check whether a fixture is eligible for Skill-root classification."""
    infra = [
        case
        for case in fixture.cases
        if case.skill_status is RunnerStatus.INFRASTRUCTURE_ERROR
    ]
    if infra:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason=(
                f"{len(infra)} Skill-side infrastructure_error case(s) block "
                "Skill patch generation"
            ),
        )

    non_skill_root = [
        case
        for case in fixture.cases
        if case.baseline_status is not RunnerStatus.PASS
        or case.skill_status not in {RunnerStatus.PASS, RunnerStatus.AGENT_FAIL}
    ]
    if non_skill_root:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason="baseline or case evidence failed before Skill attribution",
        )
    failed = [
        case for case in fixture.cases if case.skill_status is RunnerStatus.AGENT_FAIL
    ]
    if not failed:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason="the fixture contains no observed Skill failure",
        )
    for case in failed:
        diagnosis = _attribute_fixture_case(case)
        if not diagnosis.patch_allowed:
            return FixtureAnalysis(
                cards=(),
                patch_allowed=False,
                reason=diagnosis.reason,
            )
    missing_evidence = [case.case_key for case in failed if case.trace is None]
    if missing_evidence:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason=(
                "Skill failure requires Trace evidence for "
                + ", ".join(missing_evidence)
            ),
        )
    ambiguous = [case.case_key for case in failed if len(case.failure_categories) != 1]
    if ambiguous:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason=(
                "failure evidence requires semantic classification for "
                + ", ".join(ambiguous)
            ),
        )
    return FixtureAnalysis(
        cards=(),
        patch_allowed=True,
        reason="fixture is eligible for explicit Skill-root classification",
    )


def analyze_failure_evidence(path: Path) -> FixtureAnalysis:
    """Create deterministic Failure Cards from explicitly classified evidence."""
    fixture, _, artifact = load_failure_evidence_verified(path)
    return _analyze_failure_evidence(fixture, artifact)


def _analyze_failure_evidence(
    fixture: FailureEvidenceFixture,
    artifact: ArtifactRef,
) -> FixtureAnalysis:
    eligibility = analyze_fixture(fixture)
    if not eligibility.patch_allowed:
        return eligibility

    cards: list[FailureCard] = []
    category_counts: dict[FailureCategory, int] = {}
    for index, case in enumerate(fixture.cases):
        if case.skill_status is not RunnerStatus.AGENT_FAIL:
            continue
        category = case.failure_categories[0]
        diagnosis = _attribute_fixture_case(case)
        category_counts[category] = category_counts.get(category, 0) + 1
        suffix = (
            category.value
            if category_counts[category] == 1
            else f"{category.value}-{case.case_key}"
        )
        cards.append(
            FailureCard(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="failure_card",
                failure_id=f"failure-{suffix}",
                category=category,
                attribution=diagnosis.attribution,
                provenance=fixture.provenance,
                case_key=case.case_key,
                trace_evidence=(
                    EvidenceRef(
                        artifact=artifact,
                        json_pointer=f"/cases/{index}/trace",
                    ),
                ),
                assertion_evidence=(
                    EvidenceRef(
                        artifact=artifact,
                        json_pointer=f"/cases/{index}/assertion",
                    ),
                ),
                observation=case.observation,
                confidence=1.0
                if fixture.provenance is FailureProvenance.SYNTHETIC
                else 0.8,
                suggested_scope=_SUGGESTED_SCOPES[category],
                diagnosis_protocol="failure-attribution-v1",
                synthetic_reason=(
                    "Fixed Lesson 8 evidence with an explicit category label."
                    if fixture.provenance is FailureProvenance.SYNTHETIC
                    else None
                ),
            )
        )
    return FixtureAnalysis(
        cards=tuple(cards),
        patch_allowed=True,
        reason=f"created {len(cards)} evidence-linked Failure Card(s)",
    )


def build_failure_card_set(path: Path) -> FailureCardSet:
    """Build the persisted card-set record or reject ineligible evidence."""
    fixture, _, artifact = load_failure_evidence_verified(path)
    analysis = _analyze_failure_evidence(fixture, artifact)
    if not analysis.patch_allowed:
        raise DiagnosisError(analysis.reason)
    return FailureCardSet(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="failure_card_set",
        provenance=fixture.provenance,
        evidence_fixture=artifact,
        cards=analysis.cards,
        analysis_protocol="failure-card-analysis-v1",
    )


def write_failure_card_set(path: Path, cards: FailureCardSet) -> None:
    """Write one canonical Failure Card set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(artifact_json_bytes(cards))


def require_skill_root_cards(cards: tuple[FailureCard, ...]) -> None:
    """Reject every patch whose cards are missing or rooted outside Skill."""
    if not cards:
        raise DiagnosisError("patch requires at least one failure card")
    for card in cards:
        if card.attribution is not FailureAttribution.SKILL:
            raise DiagnosisError(
                f"failure card {card.failure_id} is rooted at {card.attribution.value}"
            )
        if card.provenance is FailureProvenance.LIVE and card.category not in set(
            FailureCategory
        ):
            raise DiagnosisError("live failure card has an unsupported category")
