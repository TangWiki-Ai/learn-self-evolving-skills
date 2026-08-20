"""Canonical learner-owned completion records for the shopping capstone."""

from __future__ import annotations

from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ses.contracts.artifact import ArtifactRef, Sha256Digest
from ses.contracts.base import VersionedRecord
from ses.contracts.primitives import CurrencyCode, UtcDateTime
from ses.contracts.skill import MeasurementKind


class CapstoneMilestonePolicyCheck(VersionedRecord):
    """Trusted wrapper proof that learner policy ran before one target command."""

    record_type: Literal["capstone_milestone_policy_check"]
    milestone: Literal["create", "eval", "evolve", "gate", "automation"]
    command_id: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    implementation_variant: Literal["starter", "solution"]
    implementation_path: str
    implementation_sha256: Sha256Digest
    fixture_path: Literal[
        "course/capstone-shopping-assistant/fixtures/milestone-policy-v1.json"
    ]
    fixture_sha256: Sha256Digest
    policy_result_sha256: Sha256Digest
    status: Literal["passed"]
    target_exit_code: int

    @model_validator(mode="after")
    def _bound_paths(self) -> CapstoneMilestonePolicyCheck:
        expected = (
            "course/capstone-shopping-assistant/"
            f"{self.implementation_variant}/{self.milestone}.py"
        )
        if self.implementation_path != expected:
            raise ValueError("milestone policy check points to another implementation")
        path = PurePosixPath(self.implementation_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("milestone policy implementation path must be relative")
        return self


class CapstoneReviewReceipt(VersionedRecord):
    """Proof that a learner opened one required evidence artifact."""

    record_type: Literal["capstone_review_receipt"]
    experiment_id: str
    profile_sha256: Sha256Digest
    learner_skill_sha256: Sha256Digest
    measurement_kind: MeasurementKind
    network_used: bool
    source_kind: Literal["learner_review"]
    review_kind: Literal[
        "paired_trace",
        "failure_evidence",
        "failure_card",
        "gate_decision",
        "registry_history",
    ]
    reviewed_artifact: ArtifactRef
    reviewed_at: UtcDateTime

    @model_validator(mode="after")
    def _safe_review(self) -> CapstoneReviewReceipt:
        if (
            self.measurement_kind is MeasurementKind.SYNTHETIC_OFFLINE
            and self.network_used
        ):
            raise ValueError("fixed learner review cannot claim network evidence")
        components = {
            part.casefold().replace("_", "-")
            for part in PurePosixPath(self.reviewed_artifact.path).parts
        }
        if "private-results.json" in components or "credentials" in components:
            raise ValueError("learner review cannot expose private final evidence")
        return self


class CapstoneIndex(VersionedRecord):
    """Mechanically verified index of the complete learner-owned workflow."""

    record_type: Literal["capstone_index"]
    experiment_id: str
    lineage_id: str
    profile_sha256: Sha256Digest
    mode: Literal["fixed", "live"]
    learning_completion: Literal["workflow_complete"]
    measurement_kind: MeasurementKind
    network_used: bool
    current_accepted_skill_sha256: Sha256Digest
    total_cost_amount: Decimal
    cost_currency: CurrencyCode
    cost_complete: bool
    create_receipt: ArtifactRef
    static_receipt: ArtifactRef
    trigger_receipt: ArtifactRef
    paired_receipt: ArtifactRef
    review_receipts: tuple[ArtifactRef, ...]
    failure_evidence: ArtifactRef
    failure_cards: ArtifactRef
    patch: ArtifactRef
    manual_gate_decision: ArtifactRef
    manual_registry_events: ArtifactRef
    auto_evolve_state: ArtifactRef
    final_receipt: ArtifactRef
    l3_report: ArtifactRef
    portfolio_manifest: ArtifactRef
    release_manifest: ArtifactRef
    package_runtime_manifest: ArtifactRef
    source_learning_index: ArtifactRef | None = None
    created_at: UtcDateTime

    @field_validator("total_cost_amount", mode="before")
    @classmethod
    def _decimal_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("capstone cost must use a decimal string")
        return value

    @model_validator(mode="after")
    def _complete_safe_index(self) -> CapstoneIndex:
        if not self.total_cost_amount.is_finite() or self.total_cost_amount < 0:
            raise ValueError("capstone cost must be finite and nonnegative")
        expected_measurement = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if self.measurement_kind is not expected_measurement:
            raise ValueError("capstone mode and measurement do not match")
        if self.mode == "fixed":
            if self.network_used:
                raise ValueError("fixed capstone must remain offline")
            if self.source_learning_index is not None:
                raise ValueError("fixed capstone cannot backfill a learning index")
        elif not self.network_used or self.source_learning_index is None:
            raise ValueError(
                "live capstone requires network evidence and a fixed learning index"
            )

        direct_refs = (
            self.create_receipt,
            self.static_receipt,
            self.trigger_receipt,
            self.paired_receipt,
            *self.review_receipts,
            self.failure_evidence,
            self.failure_cards,
            self.patch,
            self.manual_gate_decision,
            self.manual_registry_events,
            self.auto_evolve_state,
            self.final_receipt,
            self.l3_report,
            self.portfolio_manifest,
            self.release_manifest,
            self.package_runtime_manifest,
        )
        identities = {(reference.root, reference.path) for reference in direct_refs}
        if not self.review_receipts or len(identities) != len(direct_refs):
            raise ValueError("capstone evidence references must be distinct")
        for reference in direct_refs:
            parts = {
                part.casefold().replace("_", "-")
                for part in PurePosixPath(reference.path).parts
            }
            if "private-results.json" in parts or "credentials" in parts:
                raise ValueError("CapstoneIndex cannot reference private final data")
        return self


__all__ = [
    "CapstoneIndex",
    "CapstoneMilestonePolicyCheck",
    "CapstoneReviewReceipt",
]
