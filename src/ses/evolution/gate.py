"""Conservative candidate selection gate with aggregate-only public decisions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
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
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256
from ses.skills.static_gate import StaticGateStatus, run_static_gate
from ses.skills.trigger_eval import (
    TRIGGER_PROMPTS,
    SyntheticDiscoveryFixture,
    TriggerPrompt,
    evaluate_triggers,
)

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


class GateEvaluationAdapter(Protocol):
    """Run the two evaluation stages whose implementation varies by mode."""

    measurement_kind: MeasurementKind
    network_used: bool

    def run_trigger(
        self,
        *,
        skill_source: Path,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult: ...

    def run_selection(
        self,
        *,
        gate_id: str,
        evaluation_nonce: str,
        workspace_root: Path,
        output_root: Path,
        accepted_skill_sha256: str,
        candidate_skill_sha256: str,
        policy: GatePolicy,
        measured_at: datetime,
    ) -> SelectionPairEvaluation: ...


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


def _write_record(path: Path, value: VersionedRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(artifact_json_bytes(value))


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


def _load_selection_lock(path: Path) -> str:
    """Read only an explicitly named selection lock; never scan protected data."""

    if ".." in path.parts or any("final" in part.casefold() for part in path.parts):
        raise ValueError("selection lock path cannot name the final split")
    lexical = path if path.is_absolute() else Path.cwd() / path
    if any(component.is_symlink() for component in (lexical, *lexical.parents)):
        raise ValueError("selection lock path cannot contain symlinks")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("selection lock must be a regular file") from exc
    if any("final" in part.casefold() for part in resolved.parts):
        raise ValueError("selection lock path cannot resolve to the final split")
    if not resolved.is_file():
        raise ValueError("selection lock must be a regular file")
    try:
        content = resolved.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("selection lock is invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("selection lock must be an object")
    if payload.get("split") != "selection" or payload.get("locked") is not True:
        raise ValueError("selection lock must identify a locked selection split")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("selection lock requires its pinned inventory")
    return hashlib.sha256(content).hexdigest()


def default_gate_policy(
    project_root: Path,
    selection_lock: Path,
    *,
    trigger_model_id: str = "deterministic-fake",
) -> GatePolicy:
    """Return the measured Lesson 9 policy without reading any case content."""

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
        skill_source: Path,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        del skill_source
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
        workspace_root: Path,
        output_root: Path,
        accepted_skill_sha256: str,
        candidate_skill_sha256: str,
        policy: GatePolicy,
        measured_at: datetime,
    ) -> SelectionPairEvaluation:
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
        accepted_path = output_root / "accepted-events.jsonl"
        candidate_path = output_root / "candidate-events.jsonl"
        _write_fixed_events(
            accepted_path,
            run_id=accepted_run_id,
            side="accepted",
            cases=tuple(cases),
            evaluation_nonce=evaluation_nonce,
            iteration_id=SELECTION_ITERATION_ID,
            skill_sha256=accepted_skill_sha256,
            currency=policy.cost_currency,
        )
        _write_fixed_events(
            candidate_path,
            run_id=candidate_run_id,
            side="candidate",
            cases=tuple(cases),
            evaluation_nonce=evaluation_nonce,
            iteration_id=SELECTION_ITERATION_ID,
            skill_sha256=candidate_skill_sha256,
            currency=policy.cost_currency,
        )
        return SelectionPairEvaluation(
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
            accepted_events=_artifact_ref(workspace_root, accepted_path),
            candidate_events=_artifact_ref(workspace_root, candidate_path),
            cost_currency=policy.cost_currency,
            cases=tuple(cases),
        )


def _write_fixed_events(
    path: Path,
    *,
    run_id: str,
    side: Literal["accepted", "candidate"],
    cases: tuple[SelectionPairCase, ...],
    evaluation_nonce: str,
    iteration_id: str,
    skill_sha256: str,
    currency: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for sequence, row in enumerate(cases):
            status = row.accepted_status if side == "accepted" else row.candidate_status
            score = row.accepted_score if side == "accepted" else row.candidate_score
            input_tokens = (
                row.accepted_input_tokens
                if side == "accepted"
                else row.candidate_input_tokens
            )
            output_tokens = (
                row.accepted_output_tokens
                if side == "accepted"
                else row.candidate_output_tokens
            )
            cost = (
                row.accepted_cost_amount
                if side == "accepted"
                else row.candidate_cost_amount
            )
            stream.write(
                json.dumps(
                    {
                        "cost_amount": str(cost),
                        "cost_currency": currency,
                        "evaluation_nonce": evaluation_nonce,
                        "input_tokens": input_tokens,
                        "iteration_id": iteration_id,
                        "measurement_kind": "synthetic_offline",
                        "output_tokens": output_tokens,
                        "run_id": run_id,
                        "score": score,
                        "sequence": sequence,
                        "skill_sha256": skill_sha256,
                        "slot": row.slot,
                        "status": status.value,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def _verify_reference(
    workspace_root: Path,
    gate_root: Path,
    reference: ArtifactRef,
) -> None:
    path = workspace_root / reference.path
    try:
        path.resolve(strict=True).relative_to(gate_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise GateEvidenceError(
            "selection evidence escapes the fresh gate run"
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise GateEvidenceError("selection evidence must be a regular file")
    reference.verify_bytes(path.read_bytes())


def _verify_selection_event_log(
    workspace_root: Path,
    reference: ArtifactRef,
    *,
    pair: SelectionPairEvaluation,
    side: Literal["accepted", "candidate"],
) -> None:
    path = workspace_root / reference.path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GateEvidenceError("selection event evidence cannot be read") from exc
    if len(lines) != len(pair.cases):
        raise GateEvidenceError("selection event evidence is incomplete")
    run_id = pair.accepted_run_id if side == "accepted" else pair.candidate_run_id
    skill_sha256 = (
        pair.accepted_skill_sha256
        if side == "accepted"
        else pair.candidate_skill_sha256
    )
    for sequence, (line, row) in enumerate(zip(lines, pair.cases, strict=True)):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GateEvidenceError("selection event evidence is invalid JSON") from exc
        status = row.accepted_status if side == "accepted" else row.candidate_status
        score = row.accepted_score if side == "accepted" else row.candidate_score
        input_tokens = (
            row.accepted_input_tokens
            if side == "accepted"
            else row.candidate_input_tokens
        )
        output_tokens = (
            row.accepted_output_tokens
            if side == "accepted"
            else row.candidate_output_tokens
        )
        cost = (
            row.accepted_cost_amount
            if side == "accepted"
            else row.candidate_cost_amount
        )
        expected: dict[str, object] = {
            "cost_amount": str(cost),
            "cost_currency": pair.cost_currency,
            "evaluation_nonce": pair.evaluation_nonce,
            "input_tokens": input_tokens,
            "iteration_id": pair.iteration_id,
            "measurement_kind": pair.measurement_kind.value,
            "output_tokens": output_tokens,
            "run_id": run_id,
            "score": score,
            "sequence": sequence,
            "skill_sha256": skill_sha256,
            "slot": row.slot,
            "status": status.value,
        }
        if payload != expected:
            raise GateEvidenceError(
                "selection event evidence disagrees with its paired summary"
            )


def _candidate_identity_and_parent(
    request: GateRequest,
) -> tuple[CandidateArtifact, str, Path, Path, VersionedRecord]:
    record_path = request.candidate_bundle / "candidate.json"
    skill_path = request.candidate_bundle / "skill"
    try:
        candidate = CandidateArtifact.model_validate_json(
            record_path.read_text(encoding="utf-8")
        )
        parent_hash = normalized_skill_sha256(request.accepted_skill)
        parent_manifest = load_skill_manifest(request.accepted_skill)
    except (OSError, UnicodeError, ValueError) as exc:
        raise GateError(
            "candidate identity or accepted parent failed validation"
        ) from exc
    return candidate, parent_hash, record_path, skill_path, parent_manifest


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


def _decision(
    *,
    request: GateRequest,
    candidate: CandidateArtifact,
    parent_hash: str,
    candidate_ref: ArtifactRef,
    parent_ref: ArtifactRef,
    adapter: GateEvaluationAdapter,
    steps: list[GateStep],
    metrics: GateAggregateMetrics,
    outcome: GateOutcome,
    reasons: tuple[GateReason, ...],
) -> GateDecision:
    return GateDecision(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="gate_decision",
        decision_id=f"decision-{request.gate_id.removeprefix('gate-')}",
        gate_id=request.gate_id,
        lineage_id=request.lineage_id,
        candidate_id=candidate.candidate_id,
        candidate_skill_sha256=candidate.content_sha256,
        accepted_skill_sha256=parent_hash,
        gate_policy_sha256=content_sha256(request.policy),
        selection_lock_sha256=request.policy.selection_lock_sha256,
        evaluation_protocol_sha256=request.policy.evaluation_protocol_sha256,
        model_lock_sha256=request.policy.model_lock_sha256,
        mode=request.mode,
        measurement_kind=adapter.measurement_kind,
        network_used=adapter.network_used,
        decided_at=request.measured_at,
        steps=_finish_steps(steps),
        metrics=metrics,
        outcome=outcome,
        reason_codes=reasons,
        candidate=candidate_ref,
        accepted_manifest=parent_ref,
        gate_policy=_artifact_ref(
            request.workspace_root,
            request.workspace_root / "gates" / request.gate_id / "gate-policy.json",
        ),
    )


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

    gate_root = request.workspace_root / "gates" / request.gate_id
    if gate_root.exists() or gate_root.is_symlink():
        raise ValueError("fresh gate output already exists")
    gate_root.mkdir(parents=True)
    _write_record(gate_root / "gate-policy.json", request.policy)
    steps: list[GateStep] = []
    metrics = GateAggregateMetrics(cost_currency=request.policy.cost_currency)
    try:
        (
            candidate,
            parent_hash,
            source_record,
            candidate_skill,
            parent_manifest,
        ) = _candidate_identity_and_parent(request)
        candidate_copy = gate_root / "candidate.json"
        shutil.copyfile(source_record, candidate_copy, follow_symlinks=False)
        parent_manifest_path = gate_root / "accepted-manifest.json"
        _write_record(parent_manifest_path, parent_manifest)
        candidate_ref = _artifact_ref(request.workspace_root, candidate_copy)
        parent_ref = _artifact_ref(request.workspace_root, parent_manifest_path)
        try:
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.CANDIDATE_INVALID,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.STATIC_FAILED,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.STATIC_FAILED,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
        steps.append(
            GateStep(
                stage=GateStage.STATIC,
                status=GateStepStatus.PASS,
                evidence=(static_ref,),
            )
        )

        try:
            trigger = adapter.run_trigger(
                skill_source=candidate_skill,
                skill_sha256=candidate.content_sha256,
                measured_at=request.measured_at,
            )
        except Exception as exc:
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.TRIGGER_FAILED,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
        trigger_path = gate_root / "trigger-eval.json"
        _write_record(trigger_path, trigger)
        trigger_ref = _artifact_ref(request.workspace_root, trigger_path)
        trigger_cost = Decimal(0)
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.TRIGGER_FAILED,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
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
        private_root = gate_root / "private"
        try:
            pair = adapter.run_selection(
                gate_id=request.gate_id,
                evaluation_nonce=nonce,
                workspace_root=request.workspace_root,
                output_root=private_root,
                accepted_skill_sha256=parent_hash,
                candidate_skill_sha256=candidate.content_sha256,
                policy=request.policy,
                measured_at=request.measured_at,
            )
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
                pair,
                request=request,
                adapter=adapter,
                nonce=nonce,
                gate_root=gate_root,
                parent_hash=parent_hash,
                candidate_hash=candidate.content_sha256,
            )
        except GateEvidenceError as exc:
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.EVIDENCE_INSUFFICIENT,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
        except Exception as exc:
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.EVALUATION_ERROR,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision

        pair_path = gate_root / "private" / "selection-pair.json"
        _write_record(pair_path, pair)
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=reasons,
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision

        if metrics.critical_regression_count > request.policy.max_critical_regressions:
            steps.append(
                GateStep(
                    stage=GateStage.CRITICAL_REGRESSION,
                    status=GateStepStatus.FAIL,
                    reason_codes=(GateReason.CRITICAL_REGRESSION,),
                    evidence=(pair_ref,),
                )
            )
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(GateReason.CRITICAL_REGRESSION,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=(reason,),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
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
            decision = _decision(
                request=request,
                candidate=candidate,
                parent_hash=parent_hash,
                candidate_ref=candidate_ref,
                parent_ref=parent_ref,
                adapter=adapter,
                steps=steps,
                metrics=metrics,
                outcome=GateOutcome.REJECTED,
                reasons=tuple(cost_reasons),
            )
            _write_record(gate_root / "gate-decision.json", decision)
            return decision
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
        decision = _decision(
            request=request,
            candidate=candidate,
            parent_hash=parent_hash,
            candidate_ref=candidate_ref,
            parent_ref=parent_ref,
            adapter=adapter,
            steps=steps,
            metrics=metrics,
            outcome=outcome,
            reasons=reasons,
        )
        _write_record(gate_root / "gate-decision.json", decision)
        return decision
    except Exception:
        if not (gate_root / "gate-decision.json").exists():
            shutil.rmtree(gate_root, ignore_errors=True)
        raise


def _validate_selection_pair(
    pair: SelectionPairEvaluation,
    *,
    request: GateRequest,
    adapter: GateEvaluationAdapter,
    nonce: str,
    gate_root: Path,
    parent_hash: str,
    candidate_hash: str,
) -> None:
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
    _verify_reference(request.workspace_root, gate_root, pair.accepted_events)
    _verify_reference(request.workspace_root, gate_root, pair.candidate_events)
    _verify_selection_event_log(
        request.workspace_root,
        pair.accepted_events,
        pair=pair,
        side="accepted",
    )
    _verify_selection_event_log(
        request.workspace_root,
        pair.candidate_events,
        pair=pair,
        side="candidate",
    )
    if normalized_skill_sha256(request.accepted_skill) != parent_hash:
        raise GateEvidenceError("accepted parent changed during selection")
    if normalized_skill_sha256(request.candidate_bundle / "skill") != candidate_hash:
        raise GateEvidenceError("candidate changed during selection")


__all__ = [
    "FixedGateAdapter",
    "FixedGateScenario",
    "GateError",
    "GateEvaluationAdapter",
    "GateEvidenceError",
    "GateRequest",
    "default_gate_policy",
    "run_candidate_gate",
    "trigger_prompt_set_sha256",
    "validate_trigger_evidence",
]
