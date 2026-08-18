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
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateError,
    GateEvaluationAdapter,
    GateEvidenceError,
    GateRequest,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.governance import CandidateGovernanceCommand, govern_candidate
from ses.evolution.patches import (
    EMPTY_CONTENT_SHA256,
    PatchValidationError,
    apply_patch,
    file_content_sha256,
    validate_patch,
)
from ses.evolution.registry import (
    RegistryError,
    RegistryState,
    RegistryVersion,
    SkillRegistry,
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
    "CandidateGovernanceCommand",
    "ClaudeCodeUpdater",
    "Diagnosis",
    "DiagnosisError",
    "EvidenceError",
    "EvolutionWorkflowError",
    "FailureObservation",
    "FakeUpdater",
    "FixedGateAdapter",
    "FixedGateScenario",
    "FixtureAnalysis",
    "GateError",
    "GateEvaluationAdapter",
    "GateEvidenceError",
    "GateRequest",
    "PatchValidationError",
    "RegistryError",
    "RegistryState",
    "RegistryVersion",
    "SkillRegistry",
    "UpdaterError",
    "UpdaterRequest",
    "analyze_failure_evidence",
    "analyze_fixture",
    "apply_patch",
    "attribute_failure",
    "build_failure_card_set",
    "build_patch",
    "create_candidate",
    "default_gate_policy",
    "evidence_ref_for_fixture",
    "export_failure_evidence",
    "file_content_sha256",
    "govern_candidate",
    "linked_evidence_ref",
    "load_failure_evidence",
    "load_patch",
    "publish_candidate_bundle",
    "run_candidate_gate",
    "run_evolution_workflow",
    "validate_patch",
    "write_candidate_record",
    "write_failure_card_set",
]
