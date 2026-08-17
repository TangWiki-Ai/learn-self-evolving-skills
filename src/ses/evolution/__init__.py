"""Evidence-linked candidate generation for Ticket 09."""

from ses.evolution.candidate import (
    CandidateError,
    create_candidate,
    load_patch,
    write_candidate_record,
)
from ses.evolution.diagnosis import (
    ATTRIBUTION_ORDER,
    Diagnosis,
    DiagnosisError,
    FailureObservation,
    FixtureAnalysis,
    analyze_fixture,
    attribute_failure,
)
from ses.evolution.evidence import (
    EvidenceError,
    evidence_ref_for_fixture,
    export_failure_evidence,
    linked_evidence_ref,
    load_failure_evidence,
)
from ses.evolution.patches import (
    EMPTY_CONTENT_SHA256,
    PatchValidationError,
    apply_patch,
    file_content_sha256,
    validate_patch,
)

__all__ = [
    "ATTRIBUTION_ORDER",
    "EMPTY_CONTENT_SHA256",
    "CandidateError",
    "Diagnosis",
    "DiagnosisError",
    "EvidenceError",
    "FailureObservation",
    "FixtureAnalysis",
    "PatchValidationError",
    "analyze_fixture",
    "apply_patch",
    "attribute_failure",
    "create_candidate",
    "evidence_ref_for_fixture",
    "export_failure_evidence",
    "file_content_sha256",
    "linked_evidence_ref",
    "load_failure_evidence",
    "load_patch",
    "validate_patch",
    "write_candidate_record",
]
