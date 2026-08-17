"""Ordered failure attribution and Skill-patch eligibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ses.contracts import (
    FailureAttribution,
    FailureCard,
    FailureCategory,
    FailureEvidenceFixture,
    FailureProvenance,
)


class DiagnosisError(ValueError):
    """The evidence cannot justify a Skill patch."""


ATTRIBUTION_ORDER: Final[tuple[FailureAttribution, ...]] = (
    FailureAttribution.RUNTIME_ENVIRONMENT,
    FailureAttribution.CASE_GOLD,
    FailureAttribution.JUDGE_SIMULATOR,
    FailureAttribution.SKILL,
)


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
    """Conservatively analyze a fixture without inventing a category."""
    infra = [
        case
        for case in fixture.cases
        if case.skill_status.value == "infrastructure_error"
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
        if case.baseline_status.value != "pass"
        or case.skill_status.value not in {"pass", "agent_fail"}
    ]
    if non_skill_root:
        return FixtureAnalysis(
            cards=(),
            patch_allowed=False,
            reason="baseline or case evidence failed before Skill attribution",
        )
    return FixtureAnalysis(
        cards=(),
        patch_allowed=False,
        reason=(
            "the live fixture contains no explicit Skill-root diagnosis; "
            "no category is inferred"
        ),
    )


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
