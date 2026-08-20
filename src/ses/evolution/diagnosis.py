"""Ordered failure attribution and Skill-patch eligibility checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from ses.contracts import (
    FAILURE_ATTRIBUTION_ORDER,
    SHOPPING_FAILURE_CATEGORY_BY_SUBCODE,
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


@dataclass(frozen=True, slots=True)
class FailureDiagnosisPolicy:
    """Domain classification rules layered over the fixed attribution order."""

    policy_id: Literal["product-returns-v1", "shopping-v1"]
    suggested_scopes: Mapping[FailureCategory, str]
    require_shopping_evidence: bool
    synthetic_reason: str


_SUGGESTED_SCOPES: Final[dict[FailureCategory, str]] = {
    FailureCategory.TRIGGER: "the Skill description or one trigger instruction",
    FailureCategory.PATTERN: "one reusable workflow instruction",
    FailureCategory.OVERLOAD: "one redundant instruction or reference",
    FailureCategory.TERMINOLOGY: "one customer-facing wording instruction",
    FailureCategory.TIMING: "one action-ordering instruction",
    FailureCategory.SAFETY: "one safety guardrail",
}

_SHOPPING_SUGGESTED_SCOPES: Final[dict[FailureCategory, str]] = {
    FailureCategory.TRIGGER: "one pre-purchase applicability instruction",
    FailureCategory.PATTERN: "one constraint, search, option, or detail-check step",
    FailureCategory.OVERLOAD: "one shopper-question decision rule",
    FailureCategory.TERMINOLOGY: "one public-facing terminology guardrail",
    FailureCategory.TIMING: "one clarification, purchase, or terminal-ordering rule",
    FailureCategory.SAFETY: "one authorization or untrusted-catalog guardrail",
}

RETURN_DIAGNOSIS_POLICY = FailureDiagnosisPolicy(
    policy_id="product-returns-v1",
    suggested_scopes=MappingProxyType(_SUGGESTED_SCOPES),
    require_shopping_evidence=False,
    synthetic_reason="Fixed Lesson 8 evidence with an explicit category label.",
)

SHOPPING_DIAGNOSIS_POLICY = FailureDiagnosisPolicy(
    policy_id="shopping-v1",
    suggested_scopes=MappingProxyType(_SHOPPING_SUGGESTED_SCOPES),
    require_shopping_evidence=True,
    synthetic_reason=(
        "Fixed shopping evidence with an explicit reviewed subcode and domain refs."
    ),
)


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


def analyze_fixture(
    fixture: FailureEvidenceFixture,
    *,
    policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY,
) -> FixtureAnalysis:
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
    shopping_cases = [
        case.case_key
        for case in fixture.cases
        if case.shopping_subcode is not None
        or case.episode_evidence is not None
        or case.raw_reward_evidence is not None
        or case.metric_evidence is not None
        or case.safety_evidence
    ]
    if shopping_cases and not policy.require_shopping_evidence:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason=(
                "shopping evidence requires the explicit shopping diagnosis policy "
                "for " + ", ".join(shopping_cases)
            ),
        )
    if policy.require_shopping_evidence:
        incomplete_shopping = [
            case.case_key
            for case in failed
            if case.shopping_subcode is None
            or case.failure_categories
            != (SHOPPING_FAILURE_CATEGORY_BY_SUBCODE[case.shopping_subcode],)
            or case.episode_evidence is None
            or case.raw_reward_evidence is None
            or case.metric_evidence is None
            or not case.safety_evidence
        ]
        if incomplete_shopping:
            return FixtureAnalysis(
                cards=(),
                patch_allowed=False,
                reason=(
                    "shopping diagnosis requires a mapped subcode and episode, raw, "
                    "metric, and safety evidence for " + ", ".join(incomplete_shopping)
                ),
            )
    return FixtureAnalysis(
        cards=(),
        patch_allowed=True,
        reason="fixture is eligible for explicit Skill-root classification",
    )


def analyze_failure_evidence(
    path: Path,
    *,
    policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY,
) -> FixtureAnalysis:
    """Create deterministic Failure Cards from explicitly classified evidence."""
    fixture, _, artifact = load_failure_evidence_verified(path)
    return _analyze_failure_evidence(fixture, artifact, policy=policy)


def _analyze_failure_evidence(
    fixture: FailureEvidenceFixture,
    artifact: ArtifactRef,
    *,
    policy: FailureDiagnosisPolicy,
) -> FixtureAnalysis:
    eligibility = analyze_fixture(fixture, policy=policy)
    if not eligibility.patch_allowed:
        return eligibility

    cards: list[FailureCard] = []
    identity_counts: dict[str, int] = {}
    for index, case in enumerate(fixture.cases):
        if case.skill_status is not RunnerStatus.AGENT_FAIL:
            continue
        category = case.failure_categories[0]
        subcode = case.shopping_subcode
        diagnosis = _attribute_fixture_case(case)
        identity = (
            subcode.value.replace("_", "-") if subcode is not None else category.value
        )
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
        suffix = (
            identity
            if identity_counts[identity] == 1
            else f"{identity}-{case.case_key}"
        )
        episode_evidence = (
            (
                EvidenceRef(
                    artifact=artifact,
                    json_pointer=f"/cases/{index}/episode_evidence",
                ),
            )
            if case.episode_evidence is not None
            else ()
        )
        raw_reward_evidence = (
            (
                EvidenceRef(
                    artifact=artifact,
                    json_pointer=f"/cases/{index}/raw_reward_evidence",
                ),
            )
            if case.raw_reward_evidence is not None
            else ()
        )
        metric_evidence = (
            (
                EvidenceRef(
                    artifact=artifact,
                    json_pointer=f"/cases/{index}/metric_evidence",
                ),
            )
            if case.metric_evidence is not None
            else ()
        )
        safety_evidence = tuple(
            EvidenceRef(
                artifact=artifact,
                json_pointer=f"/cases/{index}/safety_evidence/{evidence_index}",
            )
            for evidence_index, _ in enumerate(case.safety_evidence)
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
                suggested_scope=policy.suggested_scopes[category],
                diagnosis_protocol="failure-attribution-v1",
                synthetic_reason=(
                    policy.synthetic_reason
                    if fixture.provenance is FailureProvenance.SYNTHETIC
                    else None
                ),
                shopping_subcode=subcode,
                shopping_subcode_protocol=(
                    "shopping-failure-subcodes-v1" if subcode is not None else None
                ),
                episode_evidence=episode_evidence,
                raw_reward_evidence=raw_reward_evidence,
                metric_evidence=metric_evidence,
                safety_evidence=safety_evidence,
            )
        )
    return FixtureAnalysis(
        cards=tuple(cards),
        patch_allowed=True,
        reason=f"created {len(cards)} evidence-linked Failure Card(s)",
    )


def build_failure_card_set(
    path: Path,
    *,
    policy: FailureDiagnosisPolicy = RETURN_DIAGNOSIS_POLICY,
) -> FailureCardSet:
    """Build the persisted card-set record or reject ineligible evidence."""
    fixture, _, artifact = load_failure_evidence_verified(path)
    analysis = _analyze_failure_evidence(fixture, artifact, policy=policy)
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
