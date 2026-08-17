"""Canonical evidence-linked evolution records owned by Ticket 09."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from ses.contracts.artifact import RelativeArtifactPath, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.evaluation import EvidenceRef
from ses.contracts.runner import PairCategory, RunnerStatus
from ses.contracts.skill import MeasurementKind


class FailureCategory(StrEnum):
    """The six learner-facing failure categories."""

    TRIGGER = "trigger"
    PATTERN = "pattern"
    OVERLOAD = "overload"
    TERMINOLOGY = "terminology"
    TIMING = "timing"
    SAFETY = "safety"


class FailureAttribution(StrEnum):
    """The ordered diagnosis roots used before a Skill change is allowed."""

    RUNTIME_ENVIRONMENT = "runtime/environment"
    CASE_GOLD = "case/gold"
    JUDGE_SIMULATOR = "Judge/Simulator"
    SKILL = "Skill"


FAILURE_ATTRIBUTION_ORDER: tuple[FailureAttribution, ...] = (
    FailureAttribution.RUNTIME_ENVIRONMENT,
    FailureAttribution.CASE_GOLD,
    FailureAttribution.JUDGE_SIMULATOR,
    FailureAttribution.SKILL,
)


class FailureProvenance(StrEnum):
    """Whether a record came from the supplied run or a teaching fixture."""

    LIVE = "live"
    SYNTHETIC = "synthetic"


class EvidenceSource(ContractModel):
    """Hashes identifying the redacted source packet without exposing its path."""

    source_label: str = Field(min_length=1)
    comparison_sha256: Sha256Digest
    pair_execution_sha256: Sha256Digest
    baseline_events_sha256: Sha256Digest
    skill_events_sha256: Sha256Digest
    skill_sha256: Sha256Digest
    measurement_kind: MeasurementKind


class EvidenceArtifact(ContractModel):
    """A redacted reference to one source artifact."""

    kind: Literal["trace", "assertion", "event_log"]
    source_file: RelativeArtifactPath
    sha256: Sha256Digest

    @model_validator(mode="after")
    def _source_is_not_private(self) -> EvidenceArtifact:
        parts = PurePosixPath(self.source_file).parts
        forbidden = {"selection", "final", "gold", "credentials", ".env"}
        if any(part.casefold() in forbidden for part in parts):
            raise ValueError("evidence source cannot reference private material")
        return self


class FailureEvidenceCase(ContractModel):
    """Minimum per-case summary exported from a live paired run."""

    case_key: str = Field(pattern=r"^case-[0-9]{3}$")
    pair_category: PairCategory
    baseline_status: RunnerStatus
    skill_status: RunnerStatus
    trace: EvidenceArtifact | None = None
    assertion: EvidenceArtifact | None = None
    failure_kinds: Mapping[str, int] = Field(default_factory=dict)
    observation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _evidence_matches_status(self) -> FailureEvidenceCase:
        if self.skill_status in {RunnerStatus.AGENT_FAIL, RunnerStatus.PASS}:
            if self.trace is None:
                raise ValueError("completed Skill outcome requires trace evidence")
        if self.skill_status is RunnerStatus.INFRASTRUCTURE_ERROR:
            if self.assertion is not None:
                raise ValueError("infrastructure errors cannot have Judge evidence")
        for kind, count in self.failure_kinds.items():
            if not kind or isinstance(count, bool) or count < 0:
                raise ValueError("failure kind counts must be nonnegative")
        return self


class FailureEvidenceFixture(VersionedRecord):
    """Auditable, de-identified evidence exported from a paired comparison."""

    record_type: Literal["failure_evidence_fixture"]
    provenance: FailureProvenance
    source: EvidenceSource
    cases: tuple[FailureEvidenceCase, ...]
    redaction_notice: Literal[
        "provider_streams_paths_gold_and_private_model_content_removed"
    ]

    @model_validator(mode="after")
    def _valid_case_inventory(self) -> FailureEvidenceFixture:
        if not self.cases or len({case.case_key for case in self.cases}) != len(
            self.cases
        ):
            raise ValueError("failure evidence cases must be nonempty and unique")
        if self.provenance is FailureProvenance.SYNTHETIC:
            if self.source.measurement_kind is not MeasurementKind.SYNTHETIC_OFFLINE:
                raise ValueError("synthetic evidence must be marked offline")
        elif self.source.measurement_kind is not MeasurementKind.LIVE_MEASURED:
            raise ValueError("live evidence must be marked live_measured")
        return self


class FailureCard(VersionedRecord):
    """One typed, evidence-linked diagnosis record."""

    record_type: Literal["failure_card"]
    failure_id: str = Field(pattern=r"^failure-[a-z0-9-]+$")
    category: FailureCategory
    attribution: FailureAttribution
    provenance: FailureProvenance
    case_key: str = Field(pattern=r"^case-[0-9]{3}$")
    trace_evidence: tuple[EvidenceRef, ...]
    assertion_evidence: tuple[EvidenceRef, ...]
    observation: str = Field(min_length=1)
    confidence: float = Field(gt=0, le=1)
    suggested_scope: str = Field(min_length=1)
    diagnosis_protocol: Literal["failure-attribution-v1"]
    synthetic_reason: str | None = None

    @model_validator(mode="after")
    def _requires_auditable_evidence(self) -> FailureCard:
        if not self.trace_evidence or not self.assertion_evidence:
            raise ValueError("failure cards require Trace and Assertion evidence")
        if self.provenance is FailureProvenance.SYNTHETIC and not self.synthetic_reason:
            raise ValueError("synthetic failure cards require an explicit reason")
        if self.provenance is FailureProvenance.LIVE and self.synthetic_reason:
            raise ValueError("live failure cards cannot carry a synthetic reason")
        return self


class AddPatchOperation(ContractModel):
    """Add one previously absent installable Skill file."""

    operation: Literal["add"]
    target: str = Field(min_length=1)
    precondition_sha256: Sha256Digest
    content: str = Field(min_length=1)
    trace_evidence: tuple[EvidenceRef, ...]
    assertion_evidence: tuple[EvidenceRef, ...]
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    failure_card_ids: tuple[str, ...] = ()


class UpdatePatchOperation(ContractModel):
    """Replace the complete content of one existing installable Skill file."""

    operation: Literal["update"]
    target: str = Field(min_length=1)
    precondition_sha256: Sha256Digest
    content: str = Field(min_length=1)
    trace_evidence: tuple[EvidenceRef, ...]
    assertion_evidence: tuple[EvidenceRef, ...]
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    failure_card_ids: tuple[str, ...] = ()


class DeletePatchOperation(ContractModel):
    """Delete one existing installable Skill file."""

    operation: Literal["delete"]
    target: str = Field(min_length=1)
    precondition_sha256: Sha256Digest
    trace_evidence: tuple[EvidenceRef, ...]
    assertion_evidence: tuple[EvidenceRef, ...]
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    failure_card_ids: tuple[str, ...] = ()


PatchOperation: TypeAlias = Annotated[
    AddPatchOperation | UpdatePatchOperation | DeletePatchOperation,
    Field(discriminator="operation"),
]


def _patch_hash_payload(value: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def patch_payload_sha256(value: Patch) -> Sha256Digest:
    """Return the deterministic hash of a Patch excluding its stored hash."""
    payload = value.model_dump(mode="json", exclude={"patch_sha256"})
    return hashlib.sha256(_patch_hash_payload(payload)).hexdigest()


class Patch(VersionedRecord):
    """An ordered, evidence-linked set of small file operations."""

    record_type: Literal["skill_patch"]
    patch_id: str = Field(pattern=r"^patch-[a-z0-9-]+$")
    parent_skill_sha256: Sha256Digest
    operations: tuple[PatchOperation, ...]
    patch_sha256: Sha256Digest = "0" * 64

    @model_validator(mode="after")
    def _validate_operations_and_hash(self) -> Patch:
        if not self.operations:
            raise ValueError("patch must contain at least one operation")
        targets = [operation.target for operation in self.operations]
        if len(targets) != len(set(targets)):
            raise ValueError("patch operations conflict on the same target")
        expected = patch_payload_sha256(self)
        if self.patch_sha256 == "0" * 64:
            object.__setattr__(self, "patch_sha256", expected)
        elif self.patch_sha256 != expected:
            raise ValueError("patch hash does not match its operations")
        return self


def normalized_files_sha256(files: Mapping[str, str]) -> Sha256Digest:
    """Hash runtime file content with the same stable framing as Skill install."""
    digest = hashlib.sha256()
    for relative in sorted(files):
        payload = files[relative].replace("\r\n", "\n").replace("\r", "\n")
        encoded = payload.encode("utf-8")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b"\0")
        digest.update(encoded)
        digest.update(b"\0")
    return digest.hexdigest()


class CandidateArtifact(VersionedRecord):
    """Complete immutable installable content derived from one accepted parent."""

    record_type: Literal["skill_candidate"]
    candidate_id: str = Field(pattern=r"^candidate-[a-z0-9-]+$")
    parent_skill_sha256: Sha256Digest
    patch_sha256: Sha256Digest
    content_sha256: Sha256Digest
    version: str = Field(min_length=1)
    static_gate_status: Literal["pass"]
    patch: Patch
    files: Mapping[str, str]
    manifest: Mapping[str, JsonValue]
    creation_protocol: Literal["evidence-linked-patch-v1"]

    @model_validator(mode="after")
    def _content_and_lineage_match(self) -> CandidateArtifact:
        if self.patch.parent_skill_sha256 != self.parent_skill_sha256:
            raise ValueError("candidate parent hash does not match Patch")
        if self.patch.patch_sha256 != self.patch_sha256:
            raise ValueError("candidate patch hash does not match Patch")
        if self.content_sha256 != normalized_files_sha256(self.files):
            raise ValueError("candidate content hash does not match installable files")
        if self.manifest.get("content_sha256") != self.content_sha256:
            raise ValueError("candidate manifest content hash does not match files")
        if "SKILL.md" not in self.files:
            raise ValueError("candidate must contain SKILL.md")
        return self
