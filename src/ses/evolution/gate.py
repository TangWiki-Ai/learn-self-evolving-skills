"""Conservative candidate selection gate with aggregate-only public decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from ses.contracts import (
    SELECTION_ITERATION_ID,
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    GateAggregateMetrics,
    GateDecision,
    GateErrorEvidence,
    GateOutcome,
    GatePolicy,
    GateReason,
    GateStage,
    GateStep,
    GateStepStatus,
    MeasurementKind,
    RunnerStatus,
    SchemaVersion,
    SelectionPairCase,
    SelectionPairEvaluation,
    TriggerEvalResult,
    TriggerPromptResult,
    VersionedRecord,
    artifact_json_bytes,
    content_sha256,
)
from ses.evolution.candidate import load_runtime_files
from ses.evolution.candidate_bundle import (
    CandidateAuditSnapshot,
    CandidateBundleError,
    capture_candidate_bundle,
)
from ses.evolution.selection_evidence import (
    _selection_event_bytes,
    _validate_selection_event_bytes,
)
from ses.foundation.credentials import credential_values, redact
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256
from ses.skills.static_gate import StaticGateStatus, run_static_gate
from ses.skills.trigger_eval import (
    TRIGGER_PROMPTS,
    SyntheticDiscoveryFixture,
    TriggerPrompt,
    evaluate_triggers,
)
from ses.testset.holdout import HoldoutManifest

_SAFE_GATE_ID = re.compile(r"^gate-[a-z0-9][a-z0-9-]{0,63}$")
_SAFE_LINEAGE_ID = re.compile(r"^lineage-[a-z0-9][a-z0-9-]{0,95}$")
_FIXED_PROTOCOL = b"ses-ticket10-selection-pair-v1"


class GateError(ValueError):
    """The gate request or its evidence cannot be trusted."""


class GateEvidenceError(GateError):
    """A paid or fixed evaluator returned incomplete or incompatible evidence."""


class FixedGateScenario(StrEnum):
    """Explicit synthetic fixtures for the Lesson 9 decision table."""

    ACCEPT = "accept"
    TRIGGER_FAILURE = "trigger-failure"
    INSUFFICIENT_EVIDENCE = "insufficient-evidence"
    JUDGE_ERROR = "judge-error"
    BUDGET_STOP = "budget-stop"
    CRITICAL_REGRESSION = "critical-regression"
    TIE = "tie"
    OVERALL_REGRESSION = "overall-regression"
    COST_OVERRUN = "cost-overrun"


@dataclass(frozen=True, slots=True)
class GateRequest:
    """Complete caller input for one immutable gate run."""

    gate_id: str
    lineage_id: str
    workspace_root: Path
    accepted_skill: Path
    candidate_bundle: Path
    selection_lock: Path
    policy: GatePolicy
    mode: Literal["fixed", "live"]
    measured_at: datetime

    def __post_init__(self) -> None:
        if not _SAFE_GATE_ID.fullmatch(self.gate_id):
            raise ValueError("gate_id must be a safe gate-prefixed identifier")
        if not _SAFE_LINEAGE_ID.fullmatch(self.lineage_id):
            raise ValueError("lineage_id must be a safe lineage-prefixed identifier")
        if self.measured_at.tzinfo is None or self.measured_at.utcoffset() is None:
            raise ValueError("gate measured_at must include a timezone")
        object.__setattr__(self, "measured_at", self.measured_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class SelectionEvaluationResult:
    """Private paired evidence returned in memory for Gate-owned persistence."""

    pair: SelectionPairEvaluation
    accepted_events: bytes
    candidate_events: bytes


class GateEvaluationAdapter(Protocol):
    """Return evaluation results without receiving Gate filesystem capabilities."""

    measurement_kind: MeasurementKind
    network_used: bool

    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult: ...

    def run_selection(
        self,
        *,
        gate_id: str,
        evaluation_nonce: str,
        accepted_skill_sha256: str,
        candidate_skill_sha256: str,
        policy: GatePolicy,
        measured_at: datetime,
    ) -> SelectionEvaluationResult: ...


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trigger_prompt_set_sha256(
    rows: Sequence[TriggerPrompt | TriggerPromptResult],
) -> str:
    """Hash ordered Trigger prompt definitions without outcome evidence."""

    payload = [
        {
            "prompt_id": row.prompt_id,
            "prompt": row.prompt,
            "expected_trigger": row.expected_trigger,
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_trigger_evidence(
    trigger: TriggerEvalResult,
    *,
    policy: GatePolicy,
    skill_sha256: str,
    measurement_kind: MeasurementKind,
    measured_at: datetime,
    mode: Literal["fixed", "live"],
) -> Decimal:
    """Validate locked Trigger identity and return its governed monetary cost."""

    prompt_ids = tuple(row.prompt_id for row in trigger.prompts)
    prompt_texts = tuple(row.prompt for row in trigger.prompts)
    prompt_hash = trigger_prompt_set_sha256(trigger.prompts)
    if (
        trigger.skill_sha256 != skill_sha256
        or trigger.measurement_kind is not measurement_kind
        or trigger.measured_at != measured_at
        or trigger.model_id != policy.trigger_model_id
        or len(trigger.prompts) != 20
        or sum(row.expected_trigger for row in trigger.prompts) != 10
        or len(set(prompt_ids)) != len(prompt_ids)
        or len(set(prompt_texts)) != len(prompt_texts)
        or trigger.prompt_set_sha256 != prompt_hash
        or prompt_hash != policy.trigger_prompt_set_sha256
    ):
        raise GateEvidenceError("trigger evidence does not match its locked policy")
    cost = trigger.usage.cost_amount
    if cost is None:
        if mode == "live":
            raise GateEvidenceError("live trigger evidence must report monetary cost")
        return Decimal(0)
    if trigger.usage.cost_currency != policy.cost_currency:
        raise GateEvidenceError("trigger cost currency does not match gate policy")
    return cost


def observed_trigger_cost(
    trigger: TriggerEvalResult,
    *,
    policy: GatePolicy,
    mode: Literal["fixed", "live"],
) -> tuple[Decimal, bool, int]:
    """Return only cost that can be expressed in the policy currency."""

    amount = trigger.usage.cost_amount
    if amount is None:
        if mode == "fixed":
            return Decimal(0), True, 0
        return Decimal(0), False, 1
    if trigger.usage.cost_currency != policy.cost_currency:
        return Decimal(0), False, 1
    return amount, True, 0


def _artifact_ref(workspace_root: Path, path: Path) -> ArtifactRef:
    try:
        relative = path.resolve().relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise GateEvidenceError("gate evidence escapes its workspace") from exc
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=relative.as_posix(),
        sha256=_sha256(path),
    )


def _memory_artifact_ref(path: str, content: bytes) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _assert_credential_safe(value: VersionedRecord) -> None:
    payload = artifact_json_bytes(value).decode("utf-8")
    if redact(payload, credential_values(os.environ)) != payload:
        raise GateEvidenceError("gate evidence contains credential material")


def _write_record(
    path: Path,
    value: VersionedRecord,
    *,
    private: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(artifact_json_bytes(value))


def _write_bytes(path: Path, content: bytes, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _write_private_evidence(
    gate_root: Path,
    result: SelectionEvaluationResult,
) -> None:
    """Persist the complete private allowlist through one trusted directory fd."""

    private_root = gate_root / "private"
    created = False
    try:
        os.mkdir(private_root, mode=0o700)
        created = True
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(private_root, directory_flags)
        try:
            entries = {
                "accepted-events.jsonl": result.accepted_events,
                "candidate-events.jsonl": result.candidate_events,
                "selection-pair.json": artifact_json_bytes(result.pair),
            }
            for name, content in entries.items():
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if private_root.is_symlink():
            private_root.unlink(missing_ok=True)
        elif created:
            shutil.rmtree(private_root, ignore_errors=True)
        raise GateEvidenceError(
            "private selection evidence could not be stored"
        ) from exc


def _create_gate_root(workspace_root: Path, gate_id: str) -> Path:
    if workspace_root.is_symlink():
        raise ValueError("gate workspace root must be a canonical directory")
    resolved_root = workspace_root.resolve(strict=False)
    if workspace_root.absolute() != resolved_root:
        raise ValueError("gate workspace root must not contain symlinks")
    workspace_root.mkdir(parents=True, exist_ok=True)
    if not workspace_root.is_dir():
        raise ValueError("gate workspace root must be a canonical directory")
    resolved_root = workspace_root.resolve(strict=True)
    gates_root = workspace_root / "gates"
    if gates_root.is_symlink():
        raise ValueError("gate output cannot use a symlinked ancestor")
    gates_root.mkdir(exist_ok=True)
    if gates_root.resolve(strict=True).parent != resolved_root:
        raise ValueError("gate output escapes its workspace")
    gate_root = gates_root / gate_id
    if gate_root.exists() or gate_root.is_symlink():
        raise ValueError("fresh gate output already exists")
    gate_root.mkdir()
    if gate_root.resolve(strict=True).parent != gates_root.resolve(strict=True):
        raise ValueError("gate output escapes its workspace")
    return gate_root


def _snapshot_skill(source: Path, target: Path, *, expected_hash: str) -> Path:
    """Copy one exact manifest inventory before any evaluator can read it."""

    try:
        if source.is_symlink() or source.absolute() != source.resolve(strict=True):
            raise ValueError("Skill source cannot contain symlink ancestors")
        manifest = load_skill_manifest(source)
        declared = {item.path for item in manifest.files} | {"skill-manifest.json"}
        actual: set[str] = set()
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("Skill source cannot contain symlinks")
            if path.is_file():
                actual.add(path.relative_to(source).as_posix())
        if actual != declared:
            raise ValueError("Skill source contains undeclared files")
        target.mkdir()
        for item in manifest.files:
            source_file = source / item.path
            destination = target / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination, follow_symlinks=False)
        shutil.copyfile(
            source / "skill-manifest.json",
            target / "skill-manifest.json",
            follow_symlinks=False,
        )
        if normalized_skill_sha256(target) != expected_hash:
            raise ValueError("Skill snapshot hash differs from its immutable identity")
        for path in target.rglob("*"):
            os.chmod(path, 0o555 if path.is_dir() else 0o444)
        os.chmod(target, 0o555)
    except (OSError, UnicodeError, ValueError) as exc:
        raise GateError("Skill snapshot failed validation") from exc
    return target


def _error_evidence(
    workspace_root: Path,
    gate_root: Path,
    *,
    stage: GateStage,
    error: Exception,
) -> ArtifactRef:
    """Persist a credential-safe error receipt without serializing exception text."""

    try:
        status = getattr(error, "status_code", None)
    except Exception:
        status = None
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
    ):
        status = None
    error_type = type(error).__name__
    if not error_type.isidentifier() or len(error_type) > 128:
        error_type = "Exception"
    path = gate_root / f"{stage.value}-error.json"
    _write_record(
        path,
        GateErrorEvidence(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="gate_error_evidence",
            stage=stage,
            error_type=error_type,
            http_status_code=status,
        ),
    )
    return _artifact_ref(workspace_root, path)


def _names_final_split(path: Path) -> bool:
    """Return whether a path component names a protected final-split asset."""

    final_names = {
        "final",
        "final-split",
        "final_manifest",
        "final-manifest.json",
    }
    return any(part.casefold() in final_names for part in path.parts)


def _load_selection_lock(path: Path) -> str:
    """Read only an explicitly named selection lock; never scan protected data."""

    if ".." in path.parts or _names_final_split(path):
        raise ValueError("selection lock path cannot name the final split")
    lexical = path if path.is_absolute() else Path.cwd() / path
    if any(component.is_symlink() for component in (lexical, *lexical.parents)):
        raise ValueError("selection lock path cannot contain symlinks")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("selection lock must be a regular file") from exc
    if _names_final_split(resolved):
        raise ValueError("selection lock path cannot resolve to the final split")
    if not resolved.is_file():
        raise ValueError("selection lock must be a regular file")
    try:
        content = resolved.read_bytes()
        lock = HoldoutManifest.model_validate_json(content)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("selection lock is invalid") from exc
    if lock.split != "selection":
        raise ValueError("selection lock must identify a locked selection split")
    return hashlib.sha256(content).hexdigest()


def default_gate_policy(
    project_root: Path,
    selection_lock: Path,
    *,
    trigger_model_id: str = "deterministic-fake",
) -> GatePolicy:
    """Return the Lesson 9 fixed-reference policy without reading case content."""

    protocol_sha256 = hashlib.sha256(_FIXED_PROTOCOL).hexdigest()
    prompt_set_hash = trigger_prompt_set_sha256(TRIGGER_PROMPTS)
    return GatePolicy(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_gate_policy",
        policy_id="gate-policy-lesson-09-v1",
        selection_case_count=6,
        critical_case_count=2,
        selection_slots=tuple(f"slot-{index:03d}" for index in range(1, 7)),
        critical_slots=("slot-001", "slot-002"),
        trigger_prompt_set_sha256=prompt_set_hash,
        trigger_model_id=trigger_model_id,
        min_trigger_precision=1.0,
        min_trigger_recall=1.0,
        max_trigger_indeterminate=0,
        min_quality_delta=0.0,
        max_critical_regressions=0,
        max_candidate_cost_amount=Decimal("0.010"),
        max_relative_cost_increase=Decimal("0.20"),
        max_gate_cost_amount=Decimal("0.020"),
        max_gate_input_tokens=5_000,
        max_gate_output_tokens=3_000,
        cost_currency="USD",
        selection_lock_sha256=_load_selection_lock(selection_lock),
        evaluation_protocol_sha256=protocol_sha256,
        model_lock_sha256=_sha256(project_root / "models.lock.json"),
    )


class FixedGateAdapter:
    """Create fresh synthetic evidence; it never calls a Provider."""

    measurement_kind = MeasurementKind.SYNTHETIC_OFFLINE
    network_used = False

    def __init__(self, scenario: FixedGateScenario = FixedGateScenario.ACCEPT) -> None:
        self.scenario = scenario
        self.trigger_calls = 0
        self.selection_calls = 0

    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        del candidate
        self.trigger_calls += 1
        expected = {row.prompt: row.expected_trigger for row in TRIGGER_PROMPTS}
        if self.scenario is FixedGateScenario.TRIGGER_FAILURE:
            positive = next(row for row in TRIGGER_PROMPTS if row.expected_trigger)
            expected[positive.prompt] = False
        return evaluate_triggers(
            skill_sha256=skill_sha256,
            engine_version="ses-fixed-gate:1",
            model_id="deterministic-fake",
            measurement_kind=self.measurement_kind,
            measured_at=measured_at,
            discovery=SyntheticDiscoveryFixture(expected),
        )

    def run_selection(
        self,
        *,
        gate_id: str,
        evaluation_nonce: str,
        accepted_skill_sha256: str,
        candidate_skill_sha256: str,
        policy: GatePolicy,
        measured_at: datetime,
    ) -> SelectionEvaluationResult:
        self.selection_calls += 1
        if self.scenario is FixedGateScenario.INSUFFICIENT_EVIDENCE:
            raise GateEvidenceError("fixed selection fixture omitted required evidence")

        accepted_passes = (True, True, True, False, False, False)
        candidate_passes = (True, True, True, True, False, False)
        if self.scenario is FixedGateScenario.TIE:
            candidate_passes = accepted_passes
        elif self.scenario is FixedGateScenario.OVERALL_REGRESSION:
            candidate_passes = (True, True, False, False, False, False)
        elif self.scenario is FixedGateScenario.CRITICAL_REGRESSION:
            candidate_passes = (False, True, True, True, True, False)

        accepted_cost = Decimal("0.001")
        candidate_cost = (
            Decimal("0.003")
            if self.scenario is FixedGateScenario.COST_OVERRUN
            else Decimal("0.00105")
        )
        cases: list[SelectionPairCase] = []
        for index, (accepted_pass, candidate_pass) in enumerate(
            zip(accepted_passes, candidate_passes, strict=True),
            1,
        ):
            candidate_status = (
                RunnerStatus.JUDGE_ERROR
                if self.scenario is FixedGateScenario.JUDGE_ERROR and index == 4
                else RunnerStatus.BUDGET_STOP
                if self.scenario is FixedGateScenario.BUDGET_STOP and index == 4
                else RunnerStatus.PASS
                if candidate_pass
                else RunnerStatus.AGENT_FAIL
            )
            cases.append(
                SelectionPairCase(
                    slot=f"slot-{index:03d}",
                    critical=f"slot-{index:03d}" in policy.critical_slots,
                    accepted_status=(
                        RunnerStatus.PASS if accepted_pass else RunnerStatus.AGENT_FAIL
                    ),
                    candidate_status=candidate_status,
                    accepted_score=1.0 if accepted_pass else 0.0,
                    candidate_score=(
                        1.0 if candidate_status is RunnerStatus.PASS else 0.0
                    ),
                    accepted_input_tokens=100,
                    candidate_input_tokens=100,
                    accepted_output_tokens=50,
                    candidate_output_tokens=50,
                    accepted_cost_amount=accepted_cost,
                    candidate_cost_amount=candidate_cost,
                )
            )

        accepted_run_id = f"run-{gate_id}-accepted-fixed"
        candidate_run_id = f"run-{gate_id}-candidate-fixed"
        accepted_path = f"gates/{gate_id}/private/accepted-events.jsonl"
        candidate_path = f"gates/{gate_id}/private/candidate-events.jsonl"
        pair = SelectionPairEvaluation(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="selection_pair_evaluation",
            evaluation_id=f"selection-{gate_id.removeprefix('gate-')}",
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            iteration_id=SELECTION_ITERATION_ID,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            selection_lock_sha256=policy.selection_lock_sha256,
            evaluation_protocol_sha256=policy.evaluation_protocol_sha256,
            model_lock_sha256=policy.model_lock_sha256,
            measurement_kind=self.measurement_kind,
            measured_at=measured_at,
            accepted_run_id=accepted_run_id,
            candidate_run_id=candidate_run_id,
            accepted_events=_memory_artifact_ref(accepted_path, b""),
            candidate_events=_memory_artifact_ref(candidate_path, b""),
            cost_currency=policy.cost_currency,
            cases=tuple(cases),
        )
        accepted_events = _selection_event_bytes(pair, side="accepted")
        candidate_events = _selection_event_bytes(pair, side="candidate")
        pair = pair.model_copy(
            update={
                "accepted_events": _memory_artifact_ref(
                    accepted_path,
                    accepted_events,
                ),
                "candidate_events": _memory_artifact_ref(
                    candidate_path,
                    candidate_events,
                ),
                "pair_execution_sha256": "0" * 64,
            }
        )
        return SelectionEvaluationResult(
            pair=pair,
            accepted_events=accepted_events,
            candidate_events=candidate_events,
        )


def _verify_memory_reference(
    reference: ArtifactRef,
    content: bytes,
    *,
    expected_path: str,
) -> None:
    if reference.root is not ArtifactRoot.WORKSPACE or reference.path != expected_path:
        raise GateEvidenceError("selection evidence path is not Gate-owned")
    try:
        reference.verify_bytes(content)
    except ValueError as exc:
        raise GateEvidenceError("selection evidence checksum is invalid") from exc


def _verify_selection_event_log(
    content: bytes,
    *,
    pair: SelectionPairEvaluation,
    side: Literal["accepted", "candidate"],
) -> None:
    try:
        raw = content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GateEvidenceError("selection event evidence cannot be read") from exc
    if redact(raw, credential_values(os.environ)) != raw:
        raise GateEvidenceError("selection event evidence contains credential material")
    try:
        _validate_selection_event_bytes(content, pair, side=side)
    except ValueError as exc:
        raise GateEvidenceError(str(exc)) from exc


def _candidate_identity_and_parent(
    request: GateRequest,
) -> tuple[CandidateArtifact, str, CandidateAuditSnapshot, Path, VersionedRecord]:
    skill_path = request.candidate_bundle / "skill"
    try:
        snapshot = capture_candidate_bundle(
            request.candidate_bundle,
            verify_runtime=False,
        )
        candidate = snapshot.candidate
        parent_hash = normalized_skill_sha256(request.accepted_skill)
        parent_manifest = load_skill_manifest(request.accepted_skill)
    except (CandidateBundleError, OSError, UnicodeError, ValueError) as exc:
        raise GateError(
            "candidate identity or accepted parent failed validation"
        ) from exc
    return candidate, parent_hash, snapshot, skill_path, parent_manifest


def _validate_candidate_content(
    candidate: CandidateArtifact,
    *,
    parent_hash: str,
    skill_path: Path,
) -> None:
    try:
        candidate_hash = normalized_skill_sha256(skill_path)
        actual_files = load_runtime_files(skill_path)
        actual_manifest = load_skill_manifest(skill_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise GateError("candidate content failed validation") from exc
    if candidate.parent_skill_sha256 != parent_hash:
        raise GateError("candidate parent is not the current accepted Skill")
    if candidate.content_sha256 != candidate_hash:
        raise GateError("candidate content differs from its immutable record")
    if dict(candidate.files) != actual_files or candidate.manifest != actual_manifest:
        raise GateError("candidate files differ from its immutable record")


def _trigger_metrics(
    trigger: TriggerEvalResult,
    *,
    trigger_cost: Decimal,
    cost_currency: str,
    cost_complete: bool = True,
    unpriced_call_count: int = 0,
) -> GateAggregateMetrics:
    return GateAggregateMetrics(
        trigger_precision=trigger.precision,
        trigger_recall=trigger.recall,
        trigger_indeterminate_count=trigger.indeterminate_count,
        trigger_cost_amount=trigger_cost,
        total_cost_amount=trigger_cost,
        cost_currency=cost_currency,
        total_input_tokens=trigger.usage.input_tokens,
        total_output_tokens=trigger.usage.output_tokens,
        cost_complete=cost_complete,
        unpriced_call_count=unpriced_call_count,
    )


def _selection_metrics(
    trigger: TriggerEvalResult,
    pair: SelectionPairEvaluation,
    *,
    trigger_cost: Decimal,
) -> GateAggregateMetrics:
    count = len(pair.cases)
    accepted_passes = sum(
        row.accepted_status is RunnerStatus.PASS for row in pair.cases
    )
    candidate_passes = sum(
        row.candidate_status is RunnerStatus.PASS for row in pair.cases
    )
    accepted_cost = sum((row.accepted_cost_amount for row in pair.cases), Decimal(0))
    candidate_cost = sum((row.candidate_cost_amount for row in pair.cases), Decimal(0))
    relative = (
        (candidate_cost - accepted_cost) / accepted_cost
        if accepted_cost > 0 and candidate_cost > accepted_cost
        else Decimal(0)
        if candidate_cost <= accepted_cost
        else None
    )
    critical = sum(
        row.critical
        and row.accepted_status is RunnerStatus.PASS
        and row.candidate_status is not RunnerStatus.PASS
        for row in pair.cases
    )
    return GateAggregateMetrics(
        trigger_precision=trigger.precision,
        trigger_recall=trigger.recall,
        trigger_indeterminate_count=trigger.indeterminate_count,
        selection_case_count=count,
        accepted_pass_count=accepted_passes,
        candidate_pass_count=candidate_passes,
        accepted_pass_rate=accepted_passes / count,
        candidate_pass_rate=candidate_passes / count,
        quality_delta=(candidate_passes - accepted_passes) / count,
        critical_regression_count=critical,
        trigger_cost_amount=trigger_cost,
        accepted_cost_amount=accepted_cost,
        candidate_cost_amount=candidate_cost,
        total_cost_amount=trigger_cost + accepted_cost + candidate_cost,
        relative_cost_increase=relative,
        cost_currency=pair.cost_currency,
        total_input_tokens=sum(
            row.accepted_input_tokens + row.candidate_input_tokens for row in pair.cases
        )
        + trigger.usage.input_tokens,
        total_output_tokens=sum(
            row.accepted_output_tokens + row.candidate_output_tokens
            for row in pair.cases
        )
        + trigger.usage.output_tokens,
    )


def _not_evaluated(stage: GateStage) -> GateStep:
    return GateStep(
        stage=stage,
        status=GateStepStatus.NOT_EVALUATED,
        reason_codes=(GateReason.NOT_EVALUATED,),
    )


def _finish_steps(steps: list[GateStep]) -> tuple[GateStep, ...]:
    for stage in tuple(GateStage)[len(steps) :]:
        steps.append(_not_evaluated(stage))
    return tuple(steps)


@dataclass(frozen=True, slots=True)
class _GateDecisionWriter:
    """Own the one terminal decision write for every Gate exit path."""

    request: GateRequest
    candidate: CandidateArtifact
    parent_hash: str
    candidate_ref: ArtifactRef
    parent_ref: ArtifactRef
    adapter: GateEvaluationAdapter
    gate_root: Path

    def finish(
        self,
        *,
        steps: list[GateStep],
        metrics: GateAggregateMetrics,
        outcome: GateOutcome,
        reasons: tuple[GateReason, ...],
    ) -> GateDecision:
        decision = GateDecision(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="gate_decision",
            decision_id=(f"decision-{self.request.gate_id.removeprefix('gate-')}"),
            gate_id=self.request.gate_id,
            lineage_id=self.request.lineage_id,
            candidate_id=self.candidate.candidate_id,
            candidate_skill_sha256=self.candidate.content_sha256,
            accepted_skill_sha256=self.parent_hash,
            gate_policy_sha256=content_sha256(self.request.policy),
            selection_lock_sha256=self.request.policy.selection_lock_sha256,
            evaluation_protocol_sha256=(self.request.policy.evaluation_protocol_sha256),
            model_lock_sha256=self.request.policy.model_lock_sha256,
            mode=self.request.mode,
            measurement_kind=self.adapter.measurement_kind,
            network_used=self.adapter.network_used,
            decided_at=self.request.measured_at,
            steps=_finish_steps(steps),
            metrics=metrics,
            outcome=outcome,
            reason_codes=reasons,
            candidate=self.candidate_ref,
            accepted_manifest=self.parent_ref,
            gate_policy=_artifact_ref(
                self.request.workspace_root,
                self.gate_root / "gate-policy.json",
            ),
        )
        _write_record(self.gate_root / "gate-decision.json", decision)
        return decision


def public_gate_decision_payload(decision: GateDecision) -> dict[str, object]:
    """Project a private GateDecision into its candidate-safe aggregate form."""

    return {
        "schema_version": decision.schema_version.value,
        "record_type": "gate_decision_projection",
        "decision_id": decision.decision_id,
        "gate_id": decision.gate_id,
        "lineage_id": decision.lineage_id,
        "candidate_id": decision.candidate_id,
        "candidate_skill_sha256": decision.candidate_skill_sha256,
        "accepted_skill_sha256": decision.accepted_skill_sha256,
        "gate_policy_sha256": decision.gate_policy_sha256,
        "mode": decision.mode,
        "measurement_kind": decision.measurement_kind.value,
        "network_used": decision.network_used,
        "decided_at": decision.decided_at.isoformat().replace("+00:00", "Z"),
        "steps": [
            {
                "stage": step.stage.value,
                "status": step.status.value,
                "reason_codes": [reason.value for reason in step.reason_codes],
            }
            for step in decision.steps
        ],
        "metrics": decision.metrics.model_dump(mode="json"),
        "outcome": decision.outcome.value,
        "reason_codes": [reason.value for reason in decision.reason_codes],
    }


def run_candidate_gate(
    request: GateRequest,
    *,
    adapter: GateEvaluationAdapter,
) -> GateDecision:
    """Execute every eligible gate in order and persist one complete decision."""

    lock_hash = _load_selection_lock(request.selection_lock)
    if lock_hash != request.policy.selection_lock_sha256:
        raise ValueError("selection lock changed after the policy was versioned")
    expected_measurement = (
        MeasurementKind.SYNTHETIC_OFFLINE
        if request.mode == "fixed"
        else MeasurementKind.LIVE_MEASURED
    )
    if adapter.measurement_kind is not expected_measurement:
        raise ValueError("gate mode does not match its evaluation adapter")

    gate_root = _create_gate_root(request.workspace_root, request.gate_id)
    _write_record(gate_root / "gate-policy.json", request.policy)
    steps: list[GateStep] = []
    metrics = GateAggregateMetrics(cost_currency=request.policy.cost_currency)
    try:
        (
            candidate,
            parent_hash,
            candidate_snapshot,
            source_candidate_skill,
            parent_manifest,
        ) = _candidate_identity_and_parent(request)
        for name, content in candidate_snapshot.files.items():
            _write_bytes(gate_root / name, content)
        candidate_copy = gate_root / "candidate.json"
        parent_manifest_path = gate_root / "accepted-manifest.json"
        _write_record(parent_manifest_path, parent_manifest)
        candidate_ref = _artifact_ref(request.workspace_root, candidate_copy)
        parent_ref = _artifact_ref(request.workspace_root, parent_manifest_path)
        terminal = _GateDecisionWriter(
            request=request,
            candidate=candidate,
            parent_hash=parent_hash,
            candidate_ref=candidate_ref,
            parent_ref=parent_ref,
            adapter=adapter,
            gate_root=gate_root,
        )
        try:
            candidate_skill = _snapshot_skill(
                source_candidate_skill,
                gate_root / "candidate-skill",
                expected_hash=candidate.content_sha256,
            )
            _snapshot_skill(
                request.accepted_skill,
                gate_root / "accepted-skill",
                expected_hash=parent_hash,
            )
            _validate_candidate_content(
                candidate,
                parent_hash=parent_hash,
                skill_path=candidate_skill,
            )
        except GateError as exc:
            error_ref = _error_evidence(
                request.workspace_root,
                gate_root,
                stage=GateStage.CANDIDATE_VALIDATION,
                error=exc,
            )
            steps.append(
                GateStep(
                    stage=GateStage.CANDIDATE_VALIDATION,
                    status=GateStepStatus.FAIL,
                    reason_codes=(GateReason.CANDIDATE_INVALID,),
                    evidence=(candidate_ref, parent_ref, error_ref),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.CANDIDATE_INVALID,),
            )
        steps.append(
            GateStep(
                stage=GateStage.CANDIDATE_VALIDATION,
                status=GateStepStatus.PASS,
                evidence=(candidate_ref, parent_ref),
            )
        )

        static_path = gate_root / "static-gate.json"
        try:
            static = run_static_gate(candidate_skill, audit_path=static_path)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            error_ref = _error_evidence(
                request.workspace_root,
                gate_root,
                stage=GateStage.STATIC,
                error=exc,
            )
            steps.append(
                GateStep(
                    stage=GateStage.STATIC,
                    status=GateStepStatus.ERROR,
                    reason_codes=(GateReason.STATIC_FAILED,),
                    evidence=(error_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.STATIC_FAILED,),
            )
        static_ref = _artifact_ref(request.workspace_root, static_path)
        if (
            static.status is not StaticGateStatus.PASS
            or static.skill_sha256 != candidate.content_sha256
        ):
            steps.append(
                GateStep(
                    stage=GateStage.STATIC,
                    status=GateStepStatus.FAIL,
                    reason_codes=(GateReason.STATIC_FAILED,),
                    evidence=(static_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.STATIC_FAILED,),
            )
        steps.append(
            GateStep(
                stage=GateStage.STATIC,
                status=GateStepStatus.PASS,
                evidence=(static_ref,),
            )
        )

        try:
            trigger = adapter.run_trigger(
                candidate=candidate,
                skill_sha256=candidate.content_sha256,
                measured_at=request.measured_at,
            )
        except Exception as exc:
            metrics = GateAggregateMetrics(
                cost_currency=request.policy.cost_currency,
                cost_complete=False,
                unpriced_call_count=1,
            )
            error_ref = _error_evidence(
                request.workspace_root,
                gate_root,
                stage=GateStage.TRIGGER,
                error=exc,
            )
            steps.append(
                GateStep(
                    stage=GateStage.TRIGGER,
                    status=GateStepStatus.ERROR,
                    reason_codes=(GateReason.TRIGGER_FAILED,),
                    evidence=(error_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.TRIGGER_FAILED,),
            )
        trigger_cost, cost_complete, unpriced_call_count = observed_trigger_cost(
            trigger,
            policy=request.policy,
            mode=request.mode,
        )
        try:
            _assert_credential_safe(trigger)
        except GateEvidenceError as exc:
            metrics = GateAggregateMetrics(
                cost_currency=request.policy.cost_currency,
                cost_complete=False,
                unpriced_call_count=1,
            )
            error_ref = _error_evidence(
                request.workspace_root,
                gate_root,
                stage=GateStage.TRIGGER,
                error=exc,
            )
            steps.append(
                GateStep(
                    stage=GateStage.TRIGGER,
                    status=GateStepStatus.ERROR,
                    reason_codes=(GateReason.TRIGGER_FAILED,),
                    evidence=(error_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.TRIGGER_FAILED,),
            )
        trigger_path = gate_root / "trigger-eval.json"
        _write_record(trigger_path, trigger)
        trigger_ref = _artifact_ref(request.workspace_root, trigger_path)
        try:
            trigger_cost = validate_trigger_evidence(
                trigger,
                policy=request.policy,
                skill_sha256=candidate.content_sha256,
                measurement_kind=adapter.measurement_kind,
                measured_at=request.measured_at,
                mode=request.mode,
            )
            trigger_evidence_valid = True
        except GateEvidenceError:
            trigger_evidence_valid = False
        metrics = _trigger_metrics(
            trigger,
            trigger_cost=trigger_cost,
            cost_currency=request.policy.cost_currency,
            cost_complete=cost_complete,
            unpriced_call_count=unpriced_call_count,
        )
        trigger_pass = (
            trigger_evidence_valid
            and trigger.precision >= request.policy.min_trigger_precision
            and trigger.recall >= request.policy.min_trigger_recall
            and trigger.indeterminate_count <= request.policy.max_trigger_indeterminate
        )
        if not trigger_pass:
            steps.append(
                GateStep(
                    stage=GateStage.TRIGGER,
                    status=GateStepStatus.FAIL,
                    reason_codes=(GateReason.TRIGGER_FAILED,),
                    evidence=(trigger_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.TRIGGER_FAILED,),
            )
        steps.append(
            GateStep(
                stage=GateStage.TRIGGER,
                status=GateStepStatus.PASS,
                evidence=(trigger_ref,),
            )
        )

        nonce = hashlib.sha256(
            (
                request.gate_id
                + candidate.content_sha256
                + request.measured_at.isoformat()
            ).encode("utf-8")
        ).hexdigest()
        try:
            selection = adapter.run_selection(
                gate_id=request.gate_id,
                evaluation_nonce=nonce,
                accepted_skill_sha256=parent_hash,
                candidate_skill_sha256=candidate.content_sha256,
                policy=request.policy,
                measured_at=request.measured_at,
            )
            pair = selection.pair
            try:
                current_lock_hash = _load_selection_lock(request.selection_lock)
            except (OSError, ValueError) as exc:
                raise GateEvidenceError(
                    "selection lock became invalid during paired evaluation"
                ) from exc
            if current_lock_hash != request.policy.selection_lock_sha256:
                raise GateEvidenceError(
                    "selection lock changed during paired evaluation"
                )
            _validate_selection_pair(
                selection,
                request=request,
                adapter=adapter,
                nonce=nonce,
                gate_root=gate_root,
                parent_hash=parent_hash,
                candidate_hash=candidate.content_sha256,
            )
            _write_private_evidence(gate_root, selection)
        except GateEvidenceError as exc:
            metrics = metrics.model_copy(
                update={"cost_complete": False, "unpriced_call_count": 1}
            )
            error_ref = _error_evidence(
                request.workspace_root,
                gate_root,
                stage=GateStage.SELECTION,
                error=exc,
            )
            steps.append(
                GateStep(
                    stage=GateStage.SELECTION,
                    status=GateStepStatus.FAIL,
                    reason_codes=(GateReason.EVIDENCE_INSUFFICIENT,),
                    evidence=(error_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.EVIDENCE_INSUFFICIENT,),
            )
        except Exception as exc:
            metrics = metrics.model_copy(
                update={"cost_complete": False, "unpriced_call_count": 1}
            )
            error_ref = _error_evidence(
                request.workspace_root,
                gate_root,
                stage=GateStage.SELECTION,
                error=exc,
            )
            steps.append(
                GateStep(
                    stage=GateStage.SELECTION,
                    status=GateStepStatus.ERROR,
                    reason_codes=(GateReason.EVALUATION_ERROR,),
                    evidence=(error_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.EVALUATION_ERROR,),
            )

        pair_path = gate_root / "private" / "selection-pair.json"
        pair_ref = _artifact_ref(request.workspace_root, pair_path)
        evidence = (pair_ref, pair.accepted_events, pair.candidate_events)
        metrics = _selection_metrics(trigger, pair, trigger_cost=trigger_cost)
        statuses = {row.accepted_status for row in pair.cases} | {
            row.candidate_status for row in pair.cases
        }
        reasons: tuple[GateReason, ...]
        if RunnerStatus.BUDGET_STOP in statuses:
            steps.append(
                GateStep(
                    stage=GateStage.SELECTION,
                    status=GateStepStatus.BUDGET_STOP,
                    reason_codes=(GateReason.BUDGET_STOP,),
                    evidence=evidence,
                )
            )
            reasons = (GateReason.BUDGET_STOP,)
        elif RunnerStatus.JUDGE_ERROR in statuses:
            steps.append(
                GateStep(
                    stage=GateStage.SELECTION,
                    status=GateStepStatus.ERROR,
                    reason_codes=(GateReason.JUDGE_ERROR,),
                    evidence=evidence,
                )
            )
            reasons = (GateReason.JUDGE_ERROR,)
        elif statuses - {RunnerStatus.PASS, RunnerStatus.AGENT_FAIL}:
            steps.append(
                GateStep(
                    stage=GateStage.SELECTION,
                    status=GateStepStatus.ERROR,
                    reason_codes=(GateReason.EVALUATION_ERROR,),
                    evidence=evidence,
                )
            )
            reasons = (GateReason.EVALUATION_ERROR,)
        else:
            steps.append(
                GateStep(
                    stage=GateStage.SELECTION,
                    status=GateStepStatus.PASS,
                    evidence=evidence,
                )
            )
            reasons = ()
        if reasons:
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=reasons,
            )

        if metrics.critical_regression_count > request.policy.max_critical_regressions:
            steps.append(
                GateStep(
                    stage=GateStage.CRITICAL_REGRESSION,
                    status=GateStepStatus.FAIL,
                    reason_codes=(GateReason.CRITICAL_REGRESSION,),
                    evidence=(pair_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.CRITICAL_REGRESSION,),
            )
        steps.append(
            GateStep(
                stage=GateStage.CRITICAL_REGRESSION,
                status=GateStepStatus.PASS,
                evidence=(pair_ref,),
            )
        )

        if metrics.quality_delta <= request.policy.min_quality_delta:
            reason = (
                GateReason.TIE
                if metrics.quality_delta == 0
                else GateReason.OVERALL_REGRESSION
            )
            steps.append(
                GateStep(
                    stage=GateStage.OVERALL_QUALITY,
                    status=GateStepStatus.FAIL,
                    reason_codes=(reason,),
                    evidence=(pair_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(reason,),
            )
        steps.append(
            GateStep(
                stage=GateStage.OVERALL_QUALITY,
                status=GateStepStatus.PASS,
                evidence=(pair_ref,),
            )
        )

        cost_reasons: list[GateReason] = []
        if metrics.candidate_cost_amount > request.policy.max_candidate_cost_amount:
            cost_reasons.append(GateReason.COST_LIMIT)
        if (
            metrics.relative_cost_increase is None
            or metrics.relative_cost_increase
            > request.policy.max_relative_cost_increase
        ):
            cost_reasons.append(GateReason.COST_GROWTH)
        if cost_reasons:
            steps.append(
                GateStep(
                    stage=GateStage.COST,
                    status=GateStepStatus.FAIL,
                    reason_codes=tuple(cost_reasons),
                    evidence=(pair_ref,),
                )
            )
            return terminal.finish(
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=tuple(cost_reasons),
            )
        steps.append(
            GateStep(
                stage=GateStage.COST,
                status=GateStepStatus.PASS,
                evidence=(pair_ref,),
            )
        )

        budget_reasons: list[GateReason] = []
        if metrics.total_cost_amount > request.policy.max_gate_cost_amount:
            budget_reasons.append(GateReason.COST_LIMIT)
        if (
            metrics.total_input_tokens > request.policy.max_gate_input_tokens
            or metrics.total_output_tokens > request.policy.max_gate_output_tokens
        ):
            budget_reasons.append(GateReason.TOKEN_BUDGET)
        if budget_reasons:
            steps.append(
                GateStep(
                    stage=GateStage.BUDGET,
                    status=GateStepStatus.FAIL,
                    reason_codes=tuple(budget_reasons),
                    evidence=(pair_ref,),
                )
            )
            outcome = GateOutcome.REJECTED
            reasons = tuple(budget_reasons)
        else:
            steps.append(
                GateStep(
                    stage=GateStage.BUDGET,
                    status=GateStepStatus.PASS,
                    evidence=(pair_ref,),
                )
            )
            outcome = GateOutcome.ACCEPTED
            reasons = (GateReason.ACCEPTED,)
        return terminal.finish(
            steps=steps,
            metrics=metrics,
            outcome=outcome,
            reasons=reasons,
        )
    except Exception:
        if not (gate_root / "gate-decision.json").exists():
            shutil.rmtree(gate_root, ignore_errors=True)
        raise


def _validate_selection_pair(
    result: SelectionEvaluationResult,
    *,
    request: GateRequest,
    adapter: GateEvaluationAdapter,
    nonce: str,
    gate_root: Path,
    parent_hash: str,
    candidate_hash: str,
) -> None:
    pair = result.pair
    _assert_credential_safe(pair)
    expected = (
        pair.gate_id == request.gate_id
        and pair.evaluation_nonce == nonce
        and pair.iteration_id == SELECTION_ITERATION_ID
        and pair.accepted_skill_sha256 == parent_hash
        and pair.candidate_skill_sha256 == candidate_hash
        and pair.selection_lock_sha256 == request.policy.selection_lock_sha256
        and pair.evaluation_protocol_sha256 == request.policy.evaluation_protocol_sha256
        and pair.model_lock_sha256 == request.policy.model_lock_sha256
        and pair.measurement_kind is adapter.measurement_kind
        and pair.measured_at == request.measured_at
        and pair.cost_currency == request.policy.cost_currency
        and len(pair.cases) == request.policy.selection_case_count
        and tuple(row.slot for row in pair.cases) == request.policy.selection_slots
        and tuple(row.slot for row in pair.cases if row.critical)
        == request.policy.critical_slots
    )
    if not expected:
        raise GateEvidenceError("selection pair does not match its locked request")
    expected_prefix = f"gates/{request.gate_id}/private"
    _verify_memory_reference(
        pair.accepted_events,
        result.accepted_events,
        expected_path=f"{expected_prefix}/accepted-events.jsonl",
    )
    _verify_memory_reference(
        pair.candidate_events,
        result.candidate_events,
        expected_path=f"{expected_prefix}/candidate-events.jsonl",
    )
    _verify_selection_event_log(
        result.accepted_events,
        pair=pair,
        side="accepted",
    )
    _verify_selection_event_log(
        result.candidate_events,
        pair=pair,
        side="candidate",
    )
    if normalized_skill_sha256(gate_root / "accepted-skill") != parent_hash:
        raise GateEvidenceError("accepted snapshot changed during selection")
    if normalized_skill_sha256(gate_root / "candidate-skill") != candidate_hash:
        raise GateEvidenceError("candidate snapshot changed during selection")


__all__ = [
    "FixedGateAdapter",
    "FixedGateScenario",
    "GateError",
    "GateEvaluationAdapter",
    "GateEvidenceError",
    "GateRequest",
    "SelectionEvaluationResult",
    "default_gate_policy",
    "observed_trigger_cost",
    "public_gate_decision_payload",
    "run_candidate_gate",
    "trigger_prompt_set_sha256",
    "validate_trigger_evidence",
]
