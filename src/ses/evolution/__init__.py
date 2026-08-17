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
    analyze_failure_evidence,
    analyze_fixture,
    attribute_failure,
    build_failure_card_set,
    write_failure_card_set,
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
from ses.evolution.updater import (
    ClaudeCodeUpdater,
    FakeUpdater,
    UpdaterError,
    UpdaterRequest,
    build_patch,
)
from ses.evolution.workflow import (
    EvolutionWorkflowError,
    publish_candidate_bundle,
    run_evolution_workflow,
)

__all__ = [
    "ATTRIBUTION_ORDER",
    "EMPTY_CONTENT_SHA256",
    "CandidateError",
    "ClaudeCodeUpdater",
    "Diagnosis",
    "DiagnosisError",
    "EvidenceError",
    "EvolutionWorkflowError",
    "FailureObservation",
    "FakeUpdater",
    "FixtureAnalysis",
    "PatchValidationError",
    "UpdaterError",
    "UpdaterRequest",
    "analyze_failure_evidence",
    "analyze_fixture",
    "apply_patch",
    "attribute_failure",
    "build_failure_card_set",
    "build_patch",
    "create_candidate",
    "evidence_ref_for_fixture",
    "export_failure_evidence",
    "file_content_sha256",
    "linked_evidence_ref",
    "load_failure_evidence",
    "load_patch",
    "publish_candidate_bundle",
    "run_evolution_workflow",
    "validate_patch",
    "write_candidate_record",
    "write_failure_card_set",
]
