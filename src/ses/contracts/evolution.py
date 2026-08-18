"""Canonical evidence-linked evolution records owned by Ticket 09."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, field_validator, model_validator

from ses.contracts.artifact import ArtifactRef, RelativeArtifactPath, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import Usage
from ses.contracts.evaluation import EvidenceRef
from ses.contracts.primitives import (
    CurrencyCode,
    NonEmptyStr,
    StrictNonNegativeInt,
    UtcDateTime,
)
from ses.contracts.runner import PairCategory, RunnerStatus
from ses.contracts.skill import MeasurementKind, SkillArtifactManifest


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


class JudgeSimulatorHealth(StrEnum):
    """Reviewed health of the Judge and Simulator protocol for one case."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    NOT_REVIEWED = "not_reviewed"


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
    failure_categories: tuple[FailureCategory, ...] = ()
    judge_simulator_health: JudgeSimulatorHealth
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
        if len(self.failure_categories) != len(set(self.failure_categories)):
            raise ValueError("failure categories must be unique")
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


class FailureCardSet(VersionedRecord):
    """The reviewed Skill-root diagnoses produced from one evidence fixture."""

    record_type: Literal["failure_card_set"]
    provenance: FailureProvenance
    evidence_fixture: ArtifactRef
    cards: tuple[FailureCard, ...]
    analysis_protocol: Literal["failure-card-analysis-v1"]

    @model_validator(mode="after")
    def _cards_match_source(self) -> FailureCardSet:
        if not self.cards:
            raise ValueError("Failure Card set must not be empty")
        identifiers = [card.failure_id for card in self.cards]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Failure Card IDs must be unique")
        for card in self.cards:
            if card.provenance is not self.provenance:
                raise ValueError("Failure Card provenance does not match its set")
            for reference in (*card.trace_evidence, *card.assertion_evidence):
                if reference.artifact != self.evidence_fixture:
                    raise ValueError("Failure Card evidence must use the set fixture")
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
    manifest: SkillArtifactManifest
    creation_protocol: Literal["evidence-linked-patch-v1"]

    @model_validator(mode="after")
    def _content_and_lineage_match(self) -> CandidateArtifact:
        if self.patch.parent_skill_sha256 != self.parent_skill_sha256:
            raise ValueError("candidate parent hash does not match Patch")
        if self.patch.patch_sha256 != self.patch_sha256:
            raise ValueError("candidate patch hash does not match Patch")
        if self.content_sha256 != normalized_files_sha256(self.files):
            raise ValueError("candidate content hash does not match installable files")
        if self.manifest.content_sha256 != self.content_sha256:
            raise ValueError("candidate manifest content hash does not match files")
        if self.version != self.manifest.version:
            raise ValueError("candidate version does not match its manifest")
        manifest_files = {item.path: item.sha256 for item in self.manifest.files}
        if set(manifest_files) != set(self.files):
            raise ValueError("candidate file inventory does not match its manifest")
        for path, content in self.files.items():
            actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if manifest_files[path] != actual:
                raise ValueError(
                    f"candidate file hash does not match its manifest: {path}"
                )
        if "SKILL.md" not in self.files:
            raise ValueError("candidate must contain SKILL.md")
        return self


class EvolutionPipelineSummary(VersionedRecord):
    """Canonical handoff from evidence analysis through candidate publication."""

    record_type: Literal["evolution_pipeline_summary"]
    mode: Literal["fixed", "live"]
    evidence_provenance: FailureProvenance
    updater_measurement: MeasurementKind
    updater_usage: Usage
    updater_latency_ms: StrictNonNegativeInt
    failure_card_count: StrictNonNegativeInt
    patch_operation_count: StrictNonNegativeInt
    parent_skill_sha256: Sha256Digest
    candidate_skill_sha256: Sha256Digest
    failure_cards: ArtifactRef
    patch: ArtifactRef
    candidate: ArtifactRef

    @model_validator(mode="after")
    def _mode_matches_measurement(self) -> EvolutionPipelineSummary:
        expected = (
            MeasurementKind.LIVE_MEASURED
            if self.mode == "live"
            else MeasurementKind.SYNTHETIC_OFFLINE
        )
        if self.updater_measurement is not expected:
            raise ValueError("Updater mode and measurement kind do not match")
        if self.failure_card_count < 1 or self.patch_operation_count < 1:
            raise ValueError("evolution summary requires cards and patch operations")
        return self


class GateStage(StrEnum):
    """The conservative candidate gate order."""

    CANDIDATE_VALIDATION = "candidate_validation"
    STATIC = "static"
    TRIGGER = "trigger"
    SELECTION = "fresh_selection_pair"
    CRITICAL_REGRESSION = "critical_regression"
    OVERALL_QUALITY = "overall_quality"
    COST = "cost"
    BUDGET = "budget"


class GateStepStatus(StrEnum):
    """One gate stage outcome."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    BUDGET_STOP = "budget_stop"
    NOT_EVALUATED = "not_evaluated"


class GateOutcome(StrEnum):
    """Final candidate disposition produced by the gate."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class GateReason(StrEnum):
    """Stable, aggregate-only reasons safe to expose to an Updater."""

    ACCEPTED = "accepted"
    CANDIDATE_INVALID = "candidate_invalid"
    STATIC_FAILED = "static_gate_failed"
    TRIGGER_FAILED = "trigger_gate_failed"
    EVIDENCE_INSUFFICIENT = "selection_evidence_insufficient"
    JUDGE_ERROR = "judge_error"
    EVALUATION_ERROR = "selection_evaluation_error"
    BUDGET_STOP = "budget_stop"
    CRITICAL_REGRESSION = "critical_case_regression"
    TIE = "selection_tie"
    OVERALL_REGRESSION = "overall_quality_regression"
    COST_LIMIT = "absolute_cost_limit"
    COST_GROWTH = "relative_cost_growth_limit"
    TOKEN_BUDGET = "token_budget_limit"
    NOT_EVALUATED = "not_evaluated_after_prior_failure"


SELECTION_ITERATION_ID: Literal["iteration-0"] = "iteration-0"


class GatePolicy(VersionedRecord):
    """Versioned thresholds and locked protocol identities for one lineage."""

    record_type: Literal["skill_gate_policy"]
    policy_id: str = Field(pattern=r"^gate-policy-[a-z0-9-]+$")
    selection_case_count: StrictNonNegativeInt
    critical_case_count: StrictNonNegativeInt
    selection_slots: tuple[Annotated[str, Field(pattern=r"^slot-[0-9]{3}$")], ...]
    critical_slots: tuple[Annotated[str, Field(pattern=r"^slot-[0-9]{3}$")], ...]
    trigger_prompt_set_sha256: Sha256Digest
    trigger_model_id: NonEmptyStr
    min_trigger_precision: float = Field(ge=0, le=1)
    min_trigger_recall: float = Field(ge=0, le=1)
    max_trigger_indeterminate: StrictNonNegativeInt
    min_quality_delta: float = Field(ge=0, lt=1)
    max_critical_regressions: StrictNonNegativeInt
    max_candidate_cost_amount: Decimal
    max_relative_cost_increase: Decimal
    max_gate_cost_amount: Decimal
    max_gate_input_tokens: StrictNonNegativeInt
    max_gate_output_tokens: StrictNonNegativeInt
    cost_currency: CurrencyCode
    selection_lock_sha256: Sha256Digest
    evaluation_protocol_sha256: Sha256Digest
    model_lock_sha256: Sha256Digest

    @field_validator(
        "max_candidate_cost_amount",
        "max_relative_cost_increase",
        "max_gate_cost_amount",
        mode="before",
    )
    @classmethod
    def _decimal_wire_value(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("gate policy decimals must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_policy(self) -> GatePolicy:
        if self.selection_case_count != 6:
            raise ValueError("selection gate requires exactly six locked cases")
        if not 0 < self.critical_case_count <= self.selection_case_count:
            raise ValueError("critical case count must fit the selection plan")
        if len(self.selection_slots) != self.selection_case_count or len(
            set(self.selection_slots)
        ) != len(self.selection_slots):
            raise ValueError("selection slots must be complete and unique")
        if (
            len(self.critical_slots) != self.critical_case_count
            or len(set(self.critical_slots)) != len(self.critical_slots)
            or not set(self.critical_slots) <= set(self.selection_slots)
        ):
            raise ValueError("critical slots must be a locked selection subset")
        for value in (
            self.max_candidate_cost_amount,
            self.max_relative_cost_increase,
            self.max_gate_cost_amount,
        ):
            if not value.is_finite() or value < 0:
                raise ValueError("gate policy costs must be finite and nonnegative")
        return self


class SelectionPairCase(ContractModel):
    """Private outcome for one opaque locked selection slot."""

    slot: str = Field(pattern=r"^slot-[0-9]{3}$")
    critical: bool
    accepted_status: RunnerStatus
    candidate_status: RunnerStatus
    accepted_score: float = Field(ge=0, le=1)
    candidate_score: float = Field(ge=0, le=1)
    accepted_input_tokens: StrictNonNegativeInt
    candidate_input_tokens: StrictNonNegativeInt
    accepted_output_tokens: StrictNonNegativeInt
    candidate_output_tokens: StrictNonNegativeInt
    accepted_cost_amount: Decimal
    candidate_cost_amount: Decimal

    @field_validator("accepted_cost_amount", "candidate_cost_amount", mode="before")
    @classmethod
    def _decimal_case_cost(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("selection costs must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_case(self) -> SelectionPairCase:
        for value in (self.accepted_cost_amount, self.candidate_cost_amount):
            if not value.is_finite() or value < 0:
                raise ValueError("selection costs must be finite and nonnegative")
        for status, score in (
            (self.accepted_status, self.accepted_score),
            (self.candidate_status, self.candidate_score),
        ):
            if status is RunnerStatus.PASS and score != 1:
                raise ValueError("passing selection outcomes require score 1")
            if status is RunnerStatus.AGENT_FAIL and score != 0:
                raise ValueError("failed selection outcomes require score 0")
        return self


def selection_pair_payload_sha256(value: SelectionPairEvaluation) -> Sha256Digest:
    """Hash a private pair record without its stored self hash."""

    payload = value.model_dump(mode="json", exclude={"pair_execution_sha256"})
    return hashlib.sha256(_patch_hash_payload(payload)).hexdigest()


class SelectionPairEvaluation(VersionedRecord):
    """Private accepted-vs-candidate evidence from one fresh selection run."""

    record_type: Literal["selection_pair_evaluation"]
    evaluation_id: str = Field(pattern=r"^selection-[a-z0-9-]+$")
    gate_id: str = Field(pattern=r"^gate-[a-z0-9-]+$")
    evaluation_nonce: NonEmptyStr
    iteration_id: Literal["iteration-0"]
    accepted_skill_sha256: Sha256Digest
    candidate_skill_sha256: Sha256Digest
    selection_lock_sha256: Sha256Digest
    evaluation_protocol_sha256: Sha256Digest
    model_lock_sha256: Sha256Digest
    measurement_kind: MeasurementKind
    measured_at: UtcDateTime
    accepted_run_id: NonEmptyStr
    candidate_run_id: NonEmptyStr
    accepted_events: ArtifactRef
    candidate_events: ArtifactRef
    cost_currency: CurrencyCode
    cases: tuple[SelectionPairCase, ...]
    pair_execution_sha256: Sha256Digest = "0" * 64

    @model_validator(mode="after")
    def _valid_pair(self) -> SelectionPairEvaluation:
        if self.accepted_skill_sha256 == self.candidate_skill_sha256:
            raise ValueError("selection pair requires two distinct Skill versions")
        if self.accepted_run_id == self.candidate_run_id:
            raise ValueError("selection pair requires distinct fresh run IDs")
        if self.accepted_events == self.candidate_events:
            raise ValueError("selection pair requires distinct event evidence")
        slots = [row.slot for row in self.cases]
        if not slots or len(slots) != len(set(slots)):
            raise ValueError("selection slots must be nonempty and unique")
        expected = selection_pair_payload_sha256(self)
        if self.pair_execution_sha256 == "0" * 64:
            object.__setattr__(self, "pair_execution_sha256", expected)
        elif self.pair_execution_sha256 != expected:
            raise ValueError("selection pair hash does not match its evidence")
        return self


class GateErrorEvidence(VersionedRecord):
    """Credential-safe receipt for a failed gate operation."""

    record_type: Literal["gate_error_evidence"]
    stage: GateStage
    error_type: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    http_status_code: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None


class GateStep(ContractModel):
    """One ordered gate result with content-addressed evidence."""

    stage: GateStage
    status: GateStepStatus
    reason_codes: tuple[GateReason, ...] = ()
    evidence: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def _valid_step(self) -> GateStep:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("gate step reasons must be unique")
        if self.status is GateStepStatus.PASS and self.reason_codes:
            raise ValueError("passing gate steps cannot carry rejection reasons")
        if self.status is not GateStepStatus.PASS and not self.reason_codes:
            raise ValueError("non-passing gate steps require a reason")
        if self.status is GateStepStatus.NOT_EVALUATED and self.reason_codes != (
            GateReason.NOT_EVALUATED,
        ):
            raise ValueError("skipped gate steps require the not-evaluated reason")
        if self.status is GateStepStatus.NOT_EVALUATED:
            if self.evidence:
                raise ValueError("skipped gate steps cannot carry evidence")
            return self

        allowed_statuses = {
            GateStage.CANDIDATE_VALIDATION: {
                GateStepStatus.PASS,
                GateStepStatus.FAIL,
            },
            GateStage.STATIC: {
                GateStepStatus.PASS,
                GateStepStatus.FAIL,
                GateStepStatus.ERROR,
            },
            GateStage.TRIGGER: {
                GateStepStatus.PASS,
                GateStepStatus.FAIL,
                GateStepStatus.ERROR,
            },
            GateStage.SELECTION: {
                GateStepStatus.PASS,
                GateStepStatus.FAIL,
                GateStepStatus.ERROR,
                GateStepStatus.BUDGET_STOP,
            },
            GateStage.CRITICAL_REGRESSION: {
                GateStepStatus.PASS,
                GateStepStatus.FAIL,
            },
            GateStage.OVERALL_QUALITY: {
                GateStepStatus.PASS,
                GateStepStatus.FAIL,
            },
            GateStage.COST: {GateStepStatus.PASS, GateStepStatus.FAIL},
            GateStage.BUDGET: {GateStepStatus.PASS, GateStepStatus.FAIL},
        }
        if self.status not in allowed_statuses[self.stage]:
            raise ValueError("gate step status does not belong to its stage")

        if self.stage is GateStage.SELECTION:
            expected_reasons = {
                GateStepStatus.FAIL: {(GateReason.EVIDENCE_INSUFFICIENT,)},
                GateStepStatus.ERROR: {
                    (GateReason.JUDGE_ERROR,),
                    (GateReason.EVALUATION_ERROR,),
                },
                GateStepStatus.BUDGET_STOP: {(GateReason.BUDGET_STOP,)},
            }
            allowed_reasons = expected_reasons.get(self.status)
            if allowed_reasons is not None and self.reason_codes not in allowed_reasons:
                raise ValueError("selection gate reasons do not match the step status")

        if self.stage is GateStage.CANDIDATE_VALIDATION:
            expected_counts = {2} if self.status is GateStepStatus.PASS else {3}
        elif self.stage is GateStage.SELECTION:
            if self.status is GateStepStatus.FAIL:
                expected_counts = {1}
            elif self.status is GateStepStatus.ERROR:
                expected_counts = (
                    {3} if self.reason_codes == (GateReason.JUDGE_ERROR,) else {1, 3}
                )
            else:
                expected_counts = {3}
        else:
            expected_counts = {1}
        if len(self.evidence) not in expected_counts:
            raise ValueError(
                "gate step evidence is incomplete for its stage and status"
            )

        allowed = {
            GateStage.CANDIDATE_VALIDATION: {GateReason.CANDIDATE_INVALID},
            GateStage.STATIC: {GateReason.STATIC_FAILED},
            GateStage.TRIGGER: {GateReason.TRIGGER_FAILED},
            GateStage.SELECTION: {
                GateReason.EVIDENCE_INSUFFICIENT,
                GateReason.JUDGE_ERROR,
                GateReason.EVALUATION_ERROR,
                GateReason.BUDGET_STOP,
            },
            GateStage.CRITICAL_REGRESSION: {GateReason.CRITICAL_REGRESSION},
            GateStage.OVERALL_QUALITY: {
                GateReason.TIE,
                GateReason.OVERALL_REGRESSION,
            },
            GateStage.COST: {GateReason.COST_LIMIT, GateReason.COST_GROWTH},
            GateStage.BUDGET: {GateReason.COST_LIMIT, GateReason.TOKEN_BUDGET},
        }
        if (
            self.status is not GateStepStatus.PASS
            and not set(self.reason_codes) <= allowed[self.stage]
        ):
            raise ValueError("gate step reason does not belong to its stage")
        return self


class GateAggregateMetrics(ContractModel):
    """Aggregate selection evidence safe to expose outside the Gate."""

    trigger_precision: float = Field(default=0, ge=0, le=1)
    trigger_recall: float = Field(default=0, ge=0, le=1)
    trigger_indeterminate_count: StrictNonNegativeInt = 0
    selection_case_count: StrictNonNegativeInt = 0
    accepted_pass_count: StrictNonNegativeInt = 0
    candidate_pass_count: StrictNonNegativeInt = 0
    accepted_pass_rate: float = Field(default=0, ge=0, le=1)
    candidate_pass_rate: float = Field(default=0, ge=0, le=1)
    quality_delta: float = Field(default=0, ge=-1, le=1)
    critical_regression_count: StrictNonNegativeInt = 0
    trigger_cost_amount: Decimal = Decimal(0)
    accepted_cost_amount: Decimal = Decimal(0)
    candidate_cost_amount: Decimal = Decimal(0)
    total_cost_amount: Decimal = Decimal(0)
    relative_cost_increase: Decimal | None = None
    cost_currency: CurrencyCode = "USD"
    total_input_tokens: StrictNonNegativeInt = 0
    total_output_tokens: StrictNonNegativeInt = 0

    @field_validator(
        "trigger_cost_amount",
        "accepted_cost_amount",
        "candidate_cost_amount",
        "total_cost_amount",
        "relative_cost_increase",
        mode="before",
    )
    @classmethod
    def _decimal_metric(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, Decimal)):
            raise ValueError("gate metric decimals must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_metrics(self) -> GateAggregateMetrics:
        if self.accepted_pass_count > self.selection_case_count:
            raise ValueError("accepted passes exceed the selection case count")
        if self.candidate_pass_count > self.selection_case_count:
            raise ValueError("candidate passes exceed the selection case count")
        for value in (
            self.trigger_cost_amount,
            self.accepted_cost_amount,
            self.candidate_cost_amount,
            self.total_cost_amount,
        ):
            if not value.is_finite() or value < 0:
                raise ValueError("gate costs must be finite and nonnegative")
        if self.relative_cost_increase is not None and (
            not self.relative_cost_increase.is_finite()
            or self.relative_cost_increase < 0
        ):
            raise ValueError("relative cost increase must be finite and nonnegative")
        return self


class GateDecision(VersionedRecord):
    """Complete aggregate decision over one immutable candidate."""

    record_type: Literal["gate_decision"]
    decision_id: str = Field(pattern=r"^decision-[a-z0-9-]+$")
    gate_id: str = Field(pattern=r"^gate-[a-z0-9-]+$")
    lineage_id: str = Field(pattern=r"^lineage-[a-z0-9-]+$")
    candidate_id: str = Field(pattern=r"^candidate-[a-z0-9-]+$")
    candidate_skill_sha256: Sha256Digest
    accepted_skill_sha256: Sha256Digest
    gate_policy_sha256: Sha256Digest
    selection_lock_sha256: Sha256Digest
    evaluation_protocol_sha256: Sha256Digest
    model_lock_sha256: Sha256Digest
    mode: Literal["fixed", "live"]
    measurement_kind: MeasurementKind
    network_used: bool
    decided_at: UtcDateTime
    steps: tuple[GateStep, ...]
    metrics: GateAggregateMetrics
    outcome: GateOutcome
    reason_codes: tuple[GateReason, ...]
    candidate: ArtifactRef
    accepted_manifest: ArtifactRef
    gate_policy: ArtifactRef

    @model_validator(mode="after")
    def _valid_decision(self) -> GateDecision:
        if self.gate_policy.sha256 != self.gate_policy_sha256:
            raise ValueError("gate policy reference does not match its locked hash")
        if tuple(step.stage for step in self.steps) != tuple(GateStage):
            raise ValueError("gate steps must use the complete fixed order")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("gate decision reasons must be unique")
        terminal_seen = False
        for step in self.steps:
            if terminal_seen and step.status is not GateStepStatus.NOT_EVALUATED:
                raise ValueError("gate steps after a failure must not be evaluated")
            if step.status is not GateStepStatus.PASS:
                terminal_seen = True
        all_pass = all(step.status is GateStepStatus.PASS for step in self.steps)
        if self.outcome is GateOutcome.ACCEPTED:
            if not all_pass or self.reason_codes != (GateReason.ACCEPTED,):
                raise ValueError("accepted decisions require every gate to pass")
        else:
            failed = next(
                (step for step in self.steps if step.status is not GateStepStatus.PASS),
                None,
            )
            if (
                all_pass
                or failed is None
                or failed.status is GateStepStatus.NOT_EVALUATED
                or self.reason_codes != failed.reason_codes
            ):
                raise ValueError(
                    "rejected decisions require the first failed gate reasons"
                )
        expected_measurement = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if self.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if self.measurement_kind is not expected_measurement:
            raise ValueError("gate mode and measurement kind do not match")
        if self.mode == "fixed" and self.network_used:
            raise ValueError("fixed gates cannot claim network use")
        if self.outcome is GateOutcome.ACCEPTED and self.mode == "live":
            if not self.network_used:
                raise ValueError("accepted live gates require actual network use")
        return self


class RegistryEventType(StrEnum):
    """Append-only version-governance transitions."""

    INITIALIZED = "registry_initialized"
    CANDIDATE_REGISTERED = "candidate_registered"
    CANDIDATE_ACCEPTED = "candidate_accepted"
    CANDIDATE_REJECTED = "candidate_rejected"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


class VersionStatus(StrEnum):
    """Replayed lifecycle state for a content-addressed Skill version."""

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


def registry_event_payload_sha256(value: RegistryEvent) -> Sha256Digest:
    """Hash an event payload including its previous link but excluding self hash."""

    payload = value.model_dump(mode="json", exclude={"event_sha256"})
    return hashlib.sha256(_patch_hash_payload(payload)).hexdigest()


class RegistryEvent(VersionedRecord):
    """One tamper-evident append-only Registry transition."""

    record_type: Literal["registry_event"]
    registry_id: str = Field(pattern=r"^registry-[a-z0-9-]+$")
    lineage_id: str = Field(pattern=r"^lineage-[a-z0-9-]+$")
    event_id: str = Field(pattern=r"^event-[a-z0-9-]+$")
    command_id: str = Field(pattern=r"^command-[a-z0-9-]+$")
    command_sha256: Sha256Digest
    sequence: StrictNonNegativeInt
    occurred_at: UtcDateTime
    event_type: RegistryEventType
    version_id: NonEmptyStr
    version_sha256: Sha256Digest
    parent_skill_sha256: Sha256Digest | None = None
    previous_accepted_skill_sha256: Sha256Digest | None = None
    current_accepted_skill_sha256: Sha256Digest
    status: VersionStatus
    version_manifest: ArtifactRef
    candidate: ArtifactRef | None = None
    gate_decision: ArtifactRef | None = None
    evidence: tuple[ArtifactRef, ...] = ()
    reason: NonEmptyStr
    previous_event_sha256: Sha256Digest
    event_sha256: Sha256Digest = "0" * 64

    @model_validator(mode="after")
    def _valid_event_shape_and_hash(self) -> RegistryEvent:
        if self.event_type is RegistryEventType.INITIALIZED:
            if (
                self.sequence != 0
                or self.previous_event_sha256 != "0" * 64
                or self.previous_accepted_skill_sha256 is not None
                or self.parent_skill_sha256 is not None
                or self.current_accepted_skill_sha256 != self.version_sha256
                or self.status is not VersionStatus.ACCEPTED
                or self.candidate is not None
                or self.gate_decision is not None
                or not self.evidence
            ):
                raise ValueError("registry initialization event is inconsistent")
        elif self.previous_accepted_skill_sha256 is None:
            raise ValueError("registry transitions require the previous accepted Skill")
        elif self.event_type is RegistryEventType.CANDIDATE_REGISTERED:
            if (
                self.parent_skill_sha256 != self.previous_accepted_skill_sha256
                or self.current_accepted_skill_sha256
                != self.previous_accepted_skill_sha256
                or self.status is not VersionStatus.CANDIDATE
                or self.candidate is None
                or self.gate_decision is not None
            ):
                raise ValueError("candidate registration event is inconsistent")
        elif self.event_type in {
            RegistryEventType.CANDIDATE_ACCEPTED,
            RegistryEventType.CANDIDATE_REJECTED,
        }:
            expected_status = (
                VersionStatus.ACCEPTED
                if self.event_type is RegistryEventType.CANDIDATE_ACCEPTED
                else VersionStatus.REJECTED
            )
            if (
                self.current_accepted_skill_sha256
                != self.previous_accepted_skill_sha256
                or self.status is not expected_status
                or self.candidate is None
                or self.gate_decision is None
            ):
                raise ValueError("candidate decision event is inconsistent")
        elif self.event_type is RegistryEventType.PROMOTED:
            if (
                self.current_accepted_skill_sha256 != self.version_sha256
                or self.status is not VersionStatus.ACCEPTED
                or self.gate_decision is None
            ):
                raise ValueError("promotion event is inconsistent")
        elif self.event_type is RegistryEventType.ROLLED_BACK and (
            self.current_accepted_skill_sha256 != self.version_sha256
            or self.status is not VersionStatus.ACCEPTED
            or not self.evidence
        ):
            raise ValueError("rollback event is inconsistent")
        expected = registry_event_payload_sha256(self)
        if self.event_sha256 == "0" * 64:
            object.__setattr__(self, "event_sha256", expected)
        elif self.event_sha256 != expected:
            raise ValueError("registry event hash does not match its payload")
        return self
