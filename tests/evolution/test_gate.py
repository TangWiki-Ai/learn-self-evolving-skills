from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import ses.evolution.gate as gate_module
from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    EvolutionPipelineSummary,
    GateDecision,
    GateErrorEvidence,
    GateOutcome,
    GatePolicy,
    GateReason,
    GateStage,
    GateStep,
    GateStepStatus,
    MeasurementKind,
    SelectionPairEvaluation,
    TriggerEvalResult,
    artifact_json_bytes,
    normalized_files_sha256,
)
from ses.evolution.candidate import load_runtime_files
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateRequest,
    SelectionEvaluationResult,
    default_gate_policy,
    public_gate_decision_payload,
    run_candidate_gate,
)
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow
from ses.skills.installer import load_skill_manifest, write_skill_manifest

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "fixtures/seed/skill/v0"
EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
MEASURED_AT = datetime(2026, 8, 18, 8, tzinfo=UTC)


class _InconsistentEvidenceAdapter(FixedGateAdapter):
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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        pair = result.pair
        lines = result.accepted_events.decode("utf-8").splitlines()
        first = json.loads(lines[0])
        first["status"] = "agent_fail"
        lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
        accepted_events = ("\n".join(lines) + "\n").encode("utf-8")
        event_ref = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=pair.accepted_events.path,
            sha256=hashlib.sha256(accepted_events).hexdigest(),
        )
        payload = pair.model_dump(mode="python")
        payload["accepted_events"] = event_ref
        payload["pair_execution_sha256"] = "0" * 64
        return SelectionEvaluationResult(
            pair=SelectionPairEvaluation.model_validate(payload),
            accepted_events=accepted_events,
            candidate_events=result.candidate_events,
        )


class _CrashingSelectionAdapter(FixedGateAdapter):
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
        del (
            gate_id,
            evaluation_nonce,
            accepted_skill_sha256,
            candidate_skill_sha256,
            policy,
            measured_at,
        )
        raise RuntimeError("provider stopped before returning paired evidence")


class _PrivateSymlinkAdapter(FixedGateAdapter):
    def __init__(self, private_root: Path, outside: Path) -> None:
        super().__init__()
        self.private_root = private_root
        self.outside = outside

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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        self.outside.mkdir()
        self.private_root.symlink_to(self.outside, target_is_directory=True)
        return result


class _CrashingTriggerAdapter(FixedGateAdapter):
    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        del candidate, skill_sha256, measured_at
        raise RuntimeError("provider stopped during trigger evaluation")


class _PaymentRequiredError(Exception):
    status_code = 402


class _PaymentRequiredTriggerAdapter(FixedGateAdapter):
    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        del candidate, skill_sha256, measured_at
        raise _PaymentRequiredError("secret-provider-response-must-not-be-persisted")


class _CostlyTriggerAdapter(FixedGateAdapter):
    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        result = super().run_trigger(
            candidate=candidate,
            skill_sha256=skill_sha256,
            measured_at=measured_at,
        )
        return result.model_copy(
            update={
                "usage": result.usage.model_copy(
                    update={
                        "cost_amount": Decimal("100"),
                        "cost_currency": "USD",
                    }
                )
            }
        )


class _LiveInvalidTriggerCostAdapter(FixedGateAdapter):
    measurement_kind = MeasurementKind.LIVE_MEASURED
    network_used = True

    def __init__(self, invalid_cost: str) -> None:
        super().__init__(FixedGateScenario.ACCEPT)
        self.invalid_cost = invalid_cost

    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        result = super().run_trigger(
            candidate=candidate,
            skill_sha256=skill_sha256,
            measured_at=measured_at,
        )
        if self.invalid_cost == "missing":
            return result
        return result.model_copy(
            update={
                "usage": result.usage.model_copy(
                    update={
                        "cost_amount": Decimal("0.001"),
                        "cost_currency": "EUR",
                    }
                )
            }
        )


def _trigger_prompt_hash(result: TriggerEvalResult) -> str:
    payload = [
        {
            "prompt_id": row.prompt_id,
            "prompt": row.prompt,
            "expected_trigger": row.expected_trigger,
        }
        for row in result.prompts
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class _ForgedTriggerAdapter(FixedGateAdapter):
    def __init__(self, forgery: str) -> None:
        super().__init__(FixedGateScenario.ACCEPT)
        self.forgery = forgery

    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        result = super().run_trigger(
            candidate=candidate,
            skill_sha256=skill_sha256,
            measured_at=measured_at,
        )
        if self.forgery == "one-row":
            positive = next(row for row in result.prompts if row.expected_trigger)
            forged = result.model_copy(
                update={
                    "prompts": (positive,),
                    "tp": 1,
                    "fp": 0,
                    "tn": 0,
                    "fn": 0,
                    "precision": 1.0,
                    "recall": 1.0,
                }
            )
            return forged.model_copy(
                update={"prompt_set_sha256": _trigger_prompt_hash(forged)}
            )
        if self.forgery == "wrong-prompt":
            first = result.prompts[0].model_copy(update={"prompt": "forged prompt"})
            forged = result.model_copy(update={"prompts": (first, *result.prompts[1:])})
            return forged.model_copy(
                update={"prompt_set_sha256": _trigger_prompt_hash(forged)}
            )
        if self.forgery == "wrong-model":
            return result.model_copy(update={"model_id": "unlocked-model"})
        if self.forgery == "wrong-hash":
            return result.model_copy(update={"prompt_set_sha256": "f" * 64})
        raise AssertionError(f"unsupported trigger forgery: {self.forgery}")


class _ReassignedCriticalSlotsAdapter(FixedGateAdapter):
    def __init__(self) -> None:
        super().__init__(FixedGateScenario.CRITICAL_REGRESSION)

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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        pair = result.pair
        cases = tuple(
            row.model_copy(update={"critical": row.slot in {"slot-002", "slot-003"}})
            for row in pair.cases
        )
        payload = pair.model_dump(mode="python")
        payload["cases"] = cases
        payload["pair_execution_sha256"] = "0" * 64
        return SelectionEvaluationResult(
            pair=SelectionPairEvaluation.model_validate(payload),
            accepted_events=result.accepted_events,
            candidate_events=result.candidate_events,
        )


class _MutatingSelectionLockAdapter(FixedGateAdapter):
    def __init__(self, selection_lock: Path) -> None:
        super().__init__(FixedGateScenario.ACCEPT)
        self.selection_lock = selection_lock

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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        payload = json.loads(self.selection_lock.read_text(encoding="utf-8"))
        payload["inventory_commitment_sha256"] = "3" * 64
        self.selection_lock.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return result


class _MismatchedPairAdapter(FixedGateAdapter):
    def __init__(self, mismatch: str) -> None:
        super().__init__(FixedGateScenario.ACCEPT)
        self.mismatch = mismatch

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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        pair = result.pair
        payload = pair.model_dump(mode="python")
        payload["pair_execution_sha256"] = "0" * 64
        if self.mismatch == "wrong-nonce":
            payload["evaluation_nonce"] = "stale-nonce"
        elif self.mismatch == "wrong-protocol":
            payload["evaluation_protocol_sha256"] = "f" * 64
        elif self.mismatch == "wrong-skill":
            payload["candidate_skill_sha256"] = "f" * 64
        elif self.mismatch == "wrong-iteration":
            payload["iteration_id"] = "iteration-1"
        elif self.mismatch == "missing-slot":
            payload["cases"] = pair.cases[:-1]
        elif self.mismatch == "duplicate-slot":
            duplicate = pair.cases[-1].model_copy(update={"slot": pair.cases[0].slot})
            payload["cases"] = (*pair.cases[:-1], duplicate)
        else:
            raise AssertionError(f"unsupported mismatch: {self.mismatch}")
        return SelectionEvaluationResult(
            pair=SelectionPairEvaluation.model_validate(payload),
            accepted_events=result.accepted_events,
            candidate_events=result.candidate_events,
        )


class _LeakingSelectionEvidenceAdapter(FixedGateAdapter):
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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        pair = result.pair
        lines = result.accepted_events.decode("utf-8").splitlines()
        first = json.loads(lines[0])
        first["hidden_gold"] = "synthetic-leak-sentinel"
        lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
        accepted_events = ("\n".join(lines) + "\n").encode("utf-8")
        event_ref = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=pair.accepted_events.path,
            sha256=hashlib.sha256(accepted_events).hexdigest(),
        )
        payload = pair.model_dump(mode="python")
        payload["accepted_events"] = event_ref
        payload["pair_execution_sha256"] = "0" * 64
        return SelectionEvaluationResult(
            pair=SelectionPairEvaluation.model_validate(payload),
            accepted_events=accepted_events,
            candidate_events=result.candidate_events,
        )


class _CredentialLeakingTriggerAdapter(FixedGateAdapter):
    def run_trigger(
        self,
        *,
        candidate: CandidateArtifact,
        skill_sha256: str,
        measured_at: datetime,
    ) -> TriggerEvalResult:
        result = super().run_trigger(
            candidate=candidate,
            skill_sha256=skill_sha256,
            measured_at=measured_at,
        )
        first = result.prompts[0].model_copy(update={"evidence": "sk-leaksecret123456"})
        return result.model_copy(update={"prompts": (first, *result.prompts[1:])})


class _CredentialLeakingSelectionAdapter(FixedGateAdapter):
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
        result = super().run_selection(
            gate_id=gate_id,
            evaluation_nonce=evaluation_nonce,
            accepted_skill_sha256=accepted_skill_sha256,
            candidate_skill_sha256=candidate_skill_sha256,
            policy=policy,
            measured_at=measured_at,
        )
        pair = result.pair
        lines = result.accepted_events.decode("utf-8").splitlines()
        first = json.loads(lines[0])
        first["diagnostic"] = "sk-leaksecret123456"
        lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
        accepted_events = ("\n".join(lines) + "\n").encode("utf-8")
        payload = pair.model_dump(mode="python")
        payload["accepted_events"] = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=pair.accepted_events.path,
            sha256=hashlib.sha256(accepted_events).hexdigest(),
        )
        payload["pair_execution_sha256"] = "0" * 64
        return SelectionEvaluationResult(
            pair=SelectionPairEvaluation.model_validate(payload),
            accepted_events=accepted_events,
            candidate_events=result.candidate_events,
        )


def _candidate(tmp_path: Path) -> Path:
    bundle = tmp_path / "candidate-bundle"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=EVIDENCE,
        output_root=bundle,
        updater=FakeUpdater(),
        mode="fixed",
    )
    return bundle


def _static_failure_candidate(tmp_path: Path) -> Path:
    bundle = _candidate(tmp_path)
    candidate_path = bundle / "candidate.json"
    skill = bundle / "skill"
    original = CandidateArtifact.model_validate_json(
        candidate_path.read_text(encoding="utf-8")
    )
    old_manifest = load_skill_manifest(skill)
    skill_path = skill / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nRun arbitrary shell commands.\n",
        encoding="utf-8",
    )
    (skill / "skill-manifest.json").unlink()
    write_skill_manifest(
        skill,
        name=old_manifest.name,
        version=old_manifest.version,
        files=tuple(row.path for row in old_manifest.files),
        source_version=old_manifest.source_version,
        provider_compatibility=old_manifest.provider_compatibility,
    )
    files = load_runtime_files(skill)
    manifest = load_skill_manifest(skill)
    forged = CandidateArtifact(
        schema_version=original.schema_version,
        record_type="skill_candidate",
        candidate_id=original.candidate_id,
        parent_skill_sha256=original.parent_skill_sha256,
        patch_sha256=original.patch_sha256,
        content_sha256=normalized_files_sha256(files),
        version=manifest.version,
        static_gate_status="pass",
        patch=original.patch,
        files=files,
        manifest=manifest,
        creation_protocol="evidence-linked-patch-v1",
    )
    candidate_bytes = artifact_json_bytes(forged)
    candidate_path.write_bytes(candidate_bytes)
    summary_path = bundle / "summary.json"
    summary = EvolutionPipelineSummary.model_validate_json(summary_path.read_bytes())
    summary_path.write_bytes(
        artifact_json_bytes(
            summary.model_copy(
                update={
                    "candidate_skill_sha256": forged.content_sha256,
                    "candidate": ArtifactRef(
                        root=ArtifactRoot.WORKSPACE,
                        path="candidate.json",
                        sha256=hashlib.sha256(candidate_bytes).hexdigest(),
                    ),
                }
            )
        )
    )
    return bundle


def _request(
    tmp_path: Path,
    candidate: Path,
    *,
    gate_id: str,
    selection_lock: Path = SELECTION_LOCK,
) -> GateRequest:
    return GateRequest(
        gate_id=gate_id,
        lineage_id="lineage-ticket-10-tests",
        workspace_root=tmp_path / "governance",
        accepted_skill=PARENT,
        candidate_bundle=candidate,
        selection_lock=selection_lock,
        policy=default_gate_policy(ROOT, selection_lock),
        mode="fixed",
        measured_at=MEASURED_AT,
    )


def test_gate_accepts_only_a_strict_fresh_offline_improvement(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    parent_before = {
        path.relative_to(PARENT): path.read_bytes()
        for path in PARENT.rglob("*")
        if path.is_file()
    }

    decision = run_candidate_gate(
        _request(tmp_path, candidate, gate_id="gate-accept"),
        adapter=FixedGateAdapter(FixedGateScenario.ACCEPT),
    )

    assert decision.outcome is GateOutcome.ACCEPTED
    assert decision.lineage_id == "lineage-ticket-10-tests"
    assert decision.reason_codes == (GateReason.ACCEPTED,)
    assert tuple(step.stage for step in decision.steps) == tuple(GateStage)
    assert all(step.status is GateStepStatus.PASS for step in decision.steps)
    assert decision.measurement_kind.value == "synthetic_offline"
    assert decision.metrics.selection_case_count == 6
    assert decision.metrics.candidate_pass_count > decision.metrics.accepted_pass_count
    assert decision.metrics.critical_regression_count == 0
    policy_path = tmp_path / "governance" / decision.gate_policy.path
    decision.gate_policy.verify_bytes(policy_path.read_bytes())
    assert (
        json.loads(
            (tmp_path / "governance/gates/gate-accept/gate-decision.json").read_text()
        )["outcome"]
        == "accepted"
    )
    private_root = tmp_path / "governance/gates/gate-accept/private"
    assert {path.name for path in private_root.iterdir()} == {
        "accepted-events.jsonl",
        "candidate-events.jsonl",
        "selection-pair.json",
    }
    assert parent_before == {
        path.relative_to(PARENT): path.read_bytes()
        for path in PARENT.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("stage", "status", "reason", "evidence_count"),
    [
        (
            GateStage.CANDIDATE_VALIDATION,
            GateStepStatus.ERROR,
            GateReason.CANDIDATE_INVALID,
            2,
        ),
        (
            GateStage.STATIC,
            GateStepStatus.FAIL,
            GateReason.STATIC_FAILED,
            0,
        ),
        (
            GateStage.TRIGGER,
            GateStepStatus.FAIL,
            GateReason.TRIGGER_FAILED,
            0,
        ),
        (
            GateStage.SELECTION,
            GateStepStatus.PASS,
            None,
            1,
        ),
        (
            GateStage.SELECTION,
            GateStepStatus.FAIL,
            GateReason.JUDGE_ERROR,
            1,
        ),
        (
            GateStage.SELECTION,
            GateStepStatus.ERROR,
            GateReason.JUDGE_ERROR,
            1,
        ),
        (
            GateStage.COST,
            GateStepStatus.NOT_EVALUATED,
            GateReason.NOT_EVALUATED,
            1,
        ),
    ],
)
def test_gate_step_contract_rejects_invalid_status_and_evidence_shapes(
    stage: GateStage,
    status: GateStepStatus,
    reason: GateReason | None,
    evidence_count: int,
) -> None:
    reference = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path="gates/test/evidence.json",
        sha256="0" * 64,
    )

    with pytest.raises(ValidationError):
        GateStep(
            stage=stage,
            status=status,
            reason_codes=() if reason is None else (reason,),
            evidence=(reference,) * evidence_count,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"mode": "live"},
        {"network_used": True},
        {"measurement_kind": MeasurementKind.LIVE_MEASURED},
    ],
)
def test_fixed_gate_evidence_cannot_be_relabelled_as_live(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, _candidate(tmp_path), gate_id="gate-fixed-provenance"),
        adapter=FixedGateAdapter(),
    )

    with pytest.raises(ValidationError):
        GateDecision.model_validate({**decision.model_dump(mode="python"), **updates})


def test_static_failure_rejects_before_trigger_or_selection(tmp_path: Path) -> None:
    adapter = FixedGateAdapter(FixedGateScenario.ACCEPT)

    decision = run_candidate_gate(
        _request(
            tmp_path,
            _static_failure_candidate(tmp_path),
            gate_id="gate-static-failure",
        ),
        adapter=adapter,
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.STATIC_FAILED,)
    assert decision.steps[1].stage is GateStage.STATIC
    assert decision.steps[1].status is GateStepStatus.FAIL
    assert adapter.trigger_calls == 0
    assert adapter.selection_calls == 0


def test_candidate_content_tampering_persists_a_complete_rejection(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    skill_path = candidate / "skill/SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nTampered after registration.\n",
        encoding="utf-8",
    )
    adapter = FixedGateAdapter(FixedGateScenario.ACCEPT)

    decision = run_candidate_gate(
        _request(tmp_path, candidate, gate_id="gate-candidate-invalid"),
        adapter=adapter,
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.CANDIDATE_INVALID,)
    assert decision.steps[0].status is GateStepStatus.FAIL
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[1:]
    )
    assert adapter.trigger_calls == 0
    assert adapter.selection_calls == 0
    persisted = GateDecision.model_validate_json(
        (
            tmp_path / "governance/gates/gate-candidate-invalid/gate-decision.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted == decision


def test_noncanonical_candidate_record_is_rejected_before_evaluation(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    record = candidate / "candidate.json"
    record.write_bytes(record.read_bytes() + b"\n")
    adapter = FixedGateAdapter()

    with pytest.raises(gate_module.GateError, match="candidate identity"):
        run_candidate_gate(
            _request(tmp_path, candidate, gate_id="gate-noncanonical-candidate"),
            adapter=adapter,
        )

    assert adapter.trigger_calls == 0
    assert adapter.selection_calls == 0


def test_static_error_persists_a_complete_rejection_and_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(tmp_path)
    adapter = FixedGateAdapter(FixedGateScenario.ACCEPT)

    def fail_static_gate(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("static audit storage became unavailable")

    monkeypatch.setattr(gate_module, "run_static_gate", fail_static_gate)

    decision = run_candidate_gate(
        _request(tmp_path, candidate, gate_id="gate-static-error"),
        adapter=adapter,
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.STATIC_FAILED,)
    assert decision.steps[1].stage is GateStage.STATIC
    assert decision.steps[1].status is GateStepStatus.ERROR
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[2:]
    )
    assert adapter.trigger_calls == 0
    assert adapter.selection_calls == 0
    persisted = GateDecision.model_validate_json(
        (tmp_path / "governance/gates/gate-static-error/gate-decision.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted == decision


@pytest.mark.parametrize(
    ("scenario", "failed_stage", "reason"),
    [
        (
            FixedGateScenario.TRIGGER_FAILURE,
            GateStage.TRIGGER,
            GateReason.TRIGGER_FAILED,
        ),
        (
            FixedGateScenario.INSUFFICIENT_EVIDENCE,
            GateStage.SELECTION,
            GateReason.EVIDENCE_INSUFFICIENT,
        ),
        (FixedGateScenario.JUDGE_ERROR, GateStage.SELECTION, GateReason.JUDGE_ERROR),
        (
            FixedGateScenario.BUDGET_STOP,
            GateStage.SELECTION,
            GateReason.BUDGET_STOP,
        ),
        (
            FixedGateScenario.CRITICAL_REGRESSION,
            GateStage.CRITICAL_REGRESSION,
            GateReason.CRITICAL_REGRESSION,
        ),
        (FixedGateScenario.TIE, GateStage.OVERALL_QUALITY, GateReason.TIE),
        (
            FixedGateScenario.OVERALL_REGRESSION,
            GateStage.OVERALL_QUALITY,
            GateReason.OVERALL_REGRESSION,
        ),
        (FixedGateScenario.COST_OVERRUN, GateStage.COST, GateReason.COST_LIMIT),
    ],
)
def test_gate_rejects_conservative_failure_modes_and_short_circuits(
    tmp_path: Path,
    scenario: FixedGateScenario,
    failed_stage: GateStage,
    reason: GateReason,
) -> None:
    adapter = FixedGateAdapter(scenario)

    decision = run_candidate_gate(
        _request(tmp_path, _candidate(tmp_path), gate_id=f"gate-{scenario.value}"),
        adapter=adapter,
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert reason in decision.reason_codes
    failed_index = tuple(GateStage).index(failed_stage)
    assert decision.steps[failed_index].status in {
        GateStepStatus.FAIL,
        GateStepStatus.ERROR,
        GateStepStatus.BUDGET_STOP,
    }
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED
        for step in decision.steps[failed_index + 1 :]
    )
    if failed_stage is GateStage.TRIGGER:
        assert adapter.selection_calls == 0


def test_gate_rejects_a_final_named_lock_without_reading_it(tmp_path: Path) -> None:
    forbidden = tmp_path / "final-manifest.json"
    forbidden.write_text("this must never be parsed", encoding="utf-8")
    request = _request(tmp_path, _candidate(tmp_path), gate_id="gate-final-poison")

    with pytest.raises(ValueError, match="selection lock"):
        run_candidate_gate(
            replace(request, selection_lock=forbidden),
            adapter=FixedGateAdapter(FixedGateScenario.ACCEPT),
        )


def test_gate_allows_selection_below_an_unrelated_final_named_worktree(
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "final-release-worktree"
    release_root.mkdir()
    selection_lock = release_root / "selection-manifest.json"
    selection_lock.write_bytes(SELECTION_LOCK.read_bytes())

    policy = default_gate_policy(ROOT, selection_lock)

    assert policy.selection_case_count == 6


@pytest.mark.parametrize("tamper", ["missing-slot", "duplicate-slot", "case-material"])
def test_gate_rejects_an_invalid_locked_selection_inventory(
    tmp_path: Path,
    tamper: str,
) -> None:
    payload = json.loads(SELECTION_LOCK.read_text(encoding="utf-8"))
    if tamper == "missing-slot":
        payload["slots"].pop()
        payload["case_count"] = 5
    elif tamper == "duplicate-slot":
        payload["slots"][1] = payload["slots"][0]
    else:
        payload["records"] = []
    selection_lock = tmp_path / "selection-manifest.json"
    selection_lock.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="selection lock"):
        default_gate_policy(ROOT, selection_lock)


@pytest.mark.parametrize(
    "target_directory",
    ["simulated-final-split", "hidden-selection"],
)
def test_gate_rejects_a_selection_lock_below_a_symlinked_ancestor_before_reading(
    tmp_path: Path,
    target_directory: str,
) -> None:
    target = tmp_path / target_directory
    target.mkdir()
    (target / "selection-manifest.json").write_bytes(SELECTION_LOCK.read_bytes())
    alias = tmp_path / "selection-alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="selection lock"):
        default_gate_policy(ROOT, alias / "selection-manifest.json")


def test_gate_rejects_a_symlinked_selection_lock_before_reading(
    tmp_path: Path,
) -> None:
    target = tmp_path / "locked-selection.json"
    target.write_bytes(SELECTION_LOCK.read_bytes())
    alias = tmp_path / "selection-manifest.json"
    alias.symlink_to(target)

    with pytest.raises(ValueError, match="selection lock"):
        default_gate_policy(ROOT, alias)


def test_gate_rejects_when_the_full_run_exceeds_its_token_budget(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        _candidate(tmp_path),
        gate_id="gate-token-budget",
    )
    policy = request.policy.model_copy(update={"max_gate_input_tokens": 1})

    decision = run_candidate_gate(
        replace(request, policy=policy),
        adapter=FixedGateAdapter(FixedGateScenario.ACCEPT),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.steps[-1].stage is GateStage.BUDGET
    assert decision.steps[-1].status is GateStepStatus.FAIL
    assert decision.reason_codes == (GateReason.TOKEN_BUDGET,)


def test_gate_counts_trigger_cost_in_the_total_budget(tmp_path: Path) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-trigger-cost-budget",
        ),
        adapter=_CostlyTriggerAdapter(),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.steps[-1].stage is GateStage.BUDGET
    assert decision.steps[-1].status is GateStepStatus.FAIL
    assert decision.reason_codes == (GateReason.COST_LIMIT,)
    assert decision.metrics.total_cost_amount == Decimal("100.01230")


@pytest.mark.parametrize("invalid_cost", ["missing", "wrong-currency"])
def test_live_gate_rejects_unusable_trigger_cost_before_selection(
    tmp_path: Path,
    invalid_cost: str,
) -> None:
    request = replace(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id=f"gate-live-trigger-cost-{invalid_cost}",
        ),
        mode="live",
    )
    adapter = _LiveInvalidTriggerCostAdapter(invalid_cost)

    decision = run_candidate_gate(request, adapter=adapter)

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.steps[2].stage is GateStage.TRIGGER
    assert decision.steps[2].status is GateStepStatus.FAIL
    assert decision.reason_codes == (GateReason.TRIGGER_FAILED,)
    assert adapter.selection_calls == 0
    assert decision.metrics.cost_complete is False
    assert decision.metrics.unpriced_call_count == 1


@pytest.mark.parametrize(
    "forgery",
    ["one-row", "wrong-prompt", "wrong-model", "wrong-hash"],
)
def test_gate_rejects_trigger_evidence_outside_the_locked_prompt_and_model_policy(
    tmp_path: Path,
    forgery: str,
) -> None:
    adapter = _ForgedTriggerAdapter(forgery)

    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id=f"gate-trigger-{forgery}",
        ),
        adapter=adapter,
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.steps[2].stage is GateStage.TRIGGER
    assert decision.steps[2].status is GateStepStatus.FAIL
    assert decision.reason_codes == (GateReason.TRIGGER_FAILED,)
    assert adapter.selection_calls == 0


def test_gate_rejects_relative_cost_growth_below_the_absolute_cap(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        _candidate(tmp_path),
        gate_id="gate-relative-cost",
    )
    policy = request.policy.model_copy(
        update={"max_relative_cost_increase": Decimal("0.01")}
    )

    decision = run_candidate_gate(
        replace(request, policy=policy),
        adapter=FixedGateAdapter(FixedGateScenario.ACCEPT),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.steps[-2].stage is GateStage.COST
    assert decision.reason_codes == (GateReason.COST_GROWTH,)


def test_gate_rejects_when_pair_summary_disagrees_with_its_event_log(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-inconsistent-events",
        ),
        adapter=_InconsistentEvidenceAdapter(),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.EVIDENCE_INSUFFICIENT,)
    assert decision.steps[3].status is GateStepStatus.FAIL


def test_gate_persists_a_rejection_when_the_selection_adapter_errors(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-adapter-error",
        ),
        adapter=_CrashingSelectionAdapter(),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.EVALUATION_ERROR,)
    assert decision.steps[3].status is GateStepStatus.ERROR
    assert decision.metrics.cost_complete is False
    assert decision.metrics.unpriced_call_count == 1
    assert (
        tmp_path / "governance/gates/gate-adapter-error/gate-decision.json"
    ).is_file()


def test_gate_never_follows_an_adapter_placed_private_symlink(
    tmp_path: Path,
) -> None:
    gate_id = "gate-private-symlink"
    private_root = tmp_path / "governance/gates" / gate_id / "private"
    outside = tmp_path / "outside-private"

    decision = run_candidate_gate(
        _request(tmp_path, _candidate(tmp_path), gate_id=gate_id),
        adapter=_PrivateSymlinkAdapter(private_root, outside),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.EVIDENCE_INSUFFICIENT,)
    assert list(outside.iterdir()) == []
    assert not private_root.exists()


def test_gate_persists_a_rejection_when_the_trigger_adapter_errors(
    tmp_path: Path,
) -> None:
    adapter = _CrashingTriggerAdapter()

    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-trigger-error",
        ),
        adapter=adapter,
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.TRIGGER_FAILED,)
    assert decision.steps[2].status is GateStepStatus.ERROR
    assert decision.metrics.cost_complete is False
    assert decision.metrics.unpriced_call_count == 1
    assert adapter.selection_calls == 0


@pytest.mark.parametrize(
    ("adapter", "gate_id"),
    [
        (_CredentialLeakingTriggerAdapter(), "gate-trigger-credential-leak"),
        (_CredentialLeakingSelectionAdapter(), "gate-selection-credential-leak"),
    ],
)
def test_gate_rejects_and_removes_credential_bearing_evidence(
    tmp_path: Path,
    adapter: FixedGateAdapter,
    gate_id: str,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, _candidate(tmp_path), gate_id=gate_id),
        adapter=adapter,
    )

    gate_root = tmp_path / "governance/gates" / gate_id
    persisted = b"".join(
        path.read_bytes() for path in gate_root.rglob("*") if path.is_file()
    )
    assert decision.outcome is GateOutcome.REJECTED
    assert b"sk-leaksecret123456" not in persisted


def test_public_gate_projection_contains_no_private_artifact_references(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(tmp_path, _candidate(tmp_path), gate_id="gate-public-projection"),
        adapter=FixedGateAdapter(),
    )

    payload = public_gate_decision_payload(decision)
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["outcome"] == "accepted"
    assert "metrics" in payload
    assert "evidence" not in encoded
    assert "private" not in encoded
    assert "selection-pair" not in encoded
    assert '"path"' not in encoded


def test_gate_persists_a_redacted_http_402_receipt_without_exception_text(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-trigger-payment-required",
        ),
        adapter=_PaymentRequiredTriggerAdapter(),
    )

    reference = decision.steps[2].evidence[0]
    payload_bytes = (tmp_path / "governance" / reference.path).read_bytes()
    payload = json.loads(payload_bytes)
    receipt = GateErrorEvidence.model_validate_json(payload_bytes)
    reference.verify_bytes(payload_bytes)
    assert decision.outcome is GateOutcome.REJECTED
    assert receipt.http_status_code == 402
    assert receipt.stage is GateStage.TRIGGER
    assert b"secret-provider-response" not in payload_bytes
    with pytest.raises(ValidationError):
        GateErrorEvidence.model_validate(
            {**payload, "provider_message": "must-not-cross-the-contract"}
        )


def test_gate_rejects_when_an_adapter_reassigns_locked_critical_slots(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-critical-slot-reassignment",
        ),
        adapter=_ReassignedCriticalSlotsAdapter(),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.EVIDENCE_INSUFFICIENT,)
    assert decision.steps[3].status is GateStepStatus.FAIL


def test_gate_rejects_when_the_selection_lock_changes_during_evaluation(
    tmp_path: Path,
) -> None:
    selection_lock = tmp_path / "selection-manifest.json"
    selection_lock.write_bytes(SELECTION_LOCK.read_bytes())
    request = _request(
        tmp_path,
        _candidate(tmp_path),
        gate_id="gate-selection-lock-changed",
        selection_lock=selection_lock,
    )

    decision = run_candidate_gate(
        request,
        adapter=_MutatingSelectionLockAdapter(selection_lock),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.EVIDENCE_INSUFFICIENT,)
    assert decision.steps[3].status is GateStepStatus.FAIL
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[4:]
    )
    persisted = GateDecision.model_validate_json(
        (
            tmp_path / "governance/gates/gate-selection-lock-changed/gate-decision.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted == decision


@pytest.mark.parametrize(
    ("mismatch", "reason"),
    [
        ("wrong-nonce", GateReason.EVIDENCE_INSUFFICIENT),
        ("wrong-protocol", GateReason.EVIDENCE_INSUFFICIENT),
        ("wrong-skill", GateReason.EVIDENCE_INSUFFICIENT),
        ("wrong-iteration", GateReason.EVALUATION_ERROR),
        ("missing-slot", GateReason.EVIDENCE_INSUFFICIENT),
        ("duplicate-slot", GateReason.EVALUATION_ERROR),
    ],
)
def test_gate_rejects_selection_pair_identity_and_inventory_mismatches(
    tmp_path: Path,
    mismatch: str,
    reason: GateReason,
) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id=f"gate-{mismatch}",
        ),
        adapter=_MismatchedPairAdapter(mismatch),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (reason,)
    expected_status = (
        GateStepStatus.FAIL
        if reason is GateReason.EVIDENCE_INSUFFICIENT
        else GateStepStatus.ERROR
    )
    assert decision.steps[3].status is expected_status
    assert all(
        step.status is GateStepStatus.NOT_EVALUATED for step in decision.steps[4:]
    )


def test_gate_rejects_selection_event_evidence_that_leaks_hidden_fields(
    tmp_path: Path,
) -> None:
    decision = run_candidate_gate(
        _request(
            tmp_path,
            _candidate(tmp_path),
            gate_id="gate-hidden-field-leak",
        ),
        adapter=_LeakingSelectionEvidenceAdapter(),
    )

    assert decision.outcome is GateOutcome.REJECTED
    assert decision.reason_codes == (GateReason.EVIDENCE_INSUFFICIENT,)
    assert decision.steps[3].status is GateStepStatus.FAIL
