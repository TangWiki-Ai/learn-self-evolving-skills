"""Private auditor for candidate artifacts and terminal Gate decisions."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from ses.contracts import (
    SELECTION_ITERATION_ID,
    ArtifactRef,
    CandidateArtifact,
    GateAggregateMetrics,
    GateDecision,
    GateErrorEvidence,
    GateOutcome,
    GatePolicy,
    GateReason,
    GateStage,
    GateStepStatus,
    RunnerStatus,
    SelectionPairEvaluation,
    SkillArtifactManifest,
    TriggerEvalResult,
    content_sha256,
)
from ses.evolution.candidate_bundle import (
    CandidateAuditSnapshot,
    CandidateBundleError,
    capture_candidate_audit_snapshot,
    capture_candidate_bundle,
)
from ses.evolution.gate import observed_trigger_cost, validate_trigger_evidence
from ses.evolution.registry_internal import (
    RegistryError,
    RegistryState,
    _protocol_identity,
)
from ses.evolution.registry_store import _RegistryStore
from ses.evolution.selection_evidence import (
    _SelectionSide,
    _validate_selection_event_bytes,
)
from ses.skills.static_gate import StaticGateReport, StaticGateStatus


class _GateDecisionAuditor:
    """Load and deeply verify candidate and decision evidence."""

    def __init__(self, store: _RegistryStore) -> None:
        self._store = store

    def load_candidate(self, bundle: Path) -> CandidateAuditSnapshot:
        """Capture the runtime and every evidence sidecar before registration."""

        try:
            return capture_candidate_bundle(bundle)
        except CandidateBundleError as exc:
            raise RegistryError(f"candidate bundle is invalid: {exc}") from exc

    def candidate_from_ref(
        self,
        reference: ArtifactRef,
        *,
        require_registry_snapshot: bool = False,
    ) -> CandidateArtifact:
        return self._candidate_snapshot_from_ref(
            reference,
            require_registry_snapshot=require_registry_snapshot,
        ).candidate

    def _candidate_snapshot_from_ref(
        self,
        reference: ArtifactRef,
        *,
        require_registry_snapshot: bool,
    ) -> CandidateAuditSnapshot:
        path = self._store.verify_ref(reference)
        try:
            if path.name != "candidate.json":
                raise CandidateBundleError(
                    "candidate reference does not name candidate.json"
                )
            snapshot = capture_candidate_audit_snapshot(
                path.parent,
                exact_inventory=require_registry_snapshot,
            )
            candidate = snapshot.candidate
            if require_registry_snapshot:
                expected = (
                    self._store.root
                    / "objects"
                    / "candidates"
                    / candidate.content_sha256
                    / "candidate.json"
                )
                if path != expected:
                    raise CandidateBundleError(
                        "registered candidate reference is not content-addressed"
                    )
            return snapshot
        except CandidateBundleError as exc:
            raise RegistryError("stored candidate object is invalid") from exc

    def candidate_evidence_from_ref(
        self,
        reference: ArtifactRef,
        *,
        require_registry_snapshot: bool,
    ) -> tuple[ArtifactRef, ...]:
        path = self._store.verify_ref(reference)
        snapshot = self._candidate_snapshot_from_ref(
            reference,
            require_registry_snapshot=require_registry_snapshot,
        )
        return tuple(
            self._store.ref(path.parent / name)
            for name in sorted(snapshot.files)
            if name != "candidate.json"
        )

    def candidate_snapshots_match(
        self,
        registered: ArtifactRef,
        gated: ArtifactRef,
    ) -> bool:
        registered_snapshot = self._candidate_snapshot_from_ref(
            registered,
            require_registry_snapshot=True,
        )
        gated_snapshot = self._candidate_snapshot_from_ref(
            gated,
            require_registry_snapshot=False,
        )
        return registered_snapshot.files == gated_snapshot.files

    def load_decision(
        self,
        path: Path,
    ) -> tuple[GateDecision, ArtifactRef, SelectionPairEvaluation | None]:
        try:
            path.resolve(strict=True).relative_to(self._store.root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise RegistryError(
                "gate decision must be inside the Registry root"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise RegistryError("gate decision must be a regular file")
        try:
            decision = GateDecision.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate decision is invalid") from exc
        pair = self.verify_gate_decision(decision)
        return decision, self._store.ref(path), pair

    def decision_from_ref(self, reference: ArtifactRef | None) -> GateDecision:
        if reference is None:
            raise RegistryError("candidate transition is missing its gate decision")
        path = self._store.verify_ref(reference)
        try:
            return GateDecision.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("stored gate decision is invalid") from exc

    def manifest_from_ref(self, reference: ArtifactRef) -> SkillArtifactManifest:
        path = self._store.verify_ref(reference)
        try:
            return SkillArtifactManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("stored Skill manifest is invalid") from exc

    def _verify_error_evidence(
        self,
        reference: ArtifactRef,
        *,
        stage: GateStage,
    ) -> None:
        path = self._store.verify_ref(reference)
        try:
            evidence = GateErrorEvidence.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate error evidence is invalid") from exc
        if evidence.stage is not stage:
            raise RegistryError("gate error evidence does not match its stage")

    def selection_pair_from_decision(
        self,
        decision: GateDecision,
    ) -> SelectionPairEvaluation | None:
        selection = next(
            step for step in decision.steps if step.stage is GateStage.SELECTION
        )
        if len(selection.evidence) == 1 and selection.status in {
            GateStepStatus.FAIL,
            GateStepStatus.ERROR,
        }:
            self._verify_error_evidence(
                selection.evidence[0],
                stage=GateStage.SELECTION,
            )
            return None
        if selection.status not in {
            GateStepStatus.PASS,
            GateStepStatus.ERROR,
            GateStepStatus.BUDGET_STOP,
        }:
            return None
        if not selection.evidence:
            raise RegistryError("gate selection evidence is missing")
        pair_path = self._store.verify_ref(selection.evidence[0])
        try:
            return SelectionPairEvaluation.model_validate_json(
                pair_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate selection pair evidence is invalid") from exc

    def verify_gate_decision(
        self,
        decision: GateDecision,
    ) -> SelectionPairEvaluation | None:
        references = [
            decision.candidate,
            decision.accepted_manifest,
            decision.gate_policy,
        ]
        references.extend(
            reference for step in decision.steps for reference in step.evidence
        )
        for reference in references:
            self._store.verify_ref(reference)

        candidate = self.candidate_from_ref(decision.candidate)
        if (
            candidate.candidate_id != decision.candidate_id
            or candidate.content_sha256 != decision.candidate_skill_sha256
            or candidate.parent_skill_sha256 != decision.accepted_skill_sha256
        ):
            raise RegistryError("gate decision candidate evidence does not match")

        manifest = self.manifest_from_ref(decision.accepted_manifest)
        if manifest.content_sha256 != decision.accepted_skill_sha256:
            raise RegistryError("gate accepted manifest does not match its version")

        policy_path = self._store.verify_ref(decision.gate_policy)
        try:
            policy = GatePolicy.model_validate_json(
                policy_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate policy evidence is invalid") from exc
        if (
            content_sha256(policy) != decision.gate_policy_sha256
            or policy.selection_lock_sha256 != decision.selection_lock_sha256
            or policy.evaluation_protocol_sha256 != decision.evaluation_protocol_sha256
            or policy.model_lock_sha256 != decision.model_lock_sha256
        ):
            raise RegistryError("gate decision does not match its locked policy")

        candidate_step = next(
            step
            for step in decision.steps
            if step.stage is GateStage.CANDIDATE_VALIDATION
        )
        candidate_evidence = (decision.candidate, decision.accepted_manifest)
        if candidate_step.status is GateStepStatus.PASS:
            if candidate_step.evidence != candidate_evidence:
                raise RegistryError("candidate validation evidence is incomplete")
        elif candidate_step.status is GateStepStatus.FAIL:
            if (
                len(candidate_step.evidence) != 3
                or candidate_step.evidence[:2] != candidate_evidence
            ):
                raise RegistryError("candidate rejection evidence is incomplete")
            self._verify_error_evidence(
                candidate_step.evidence[2],
                stage=GateStage.CANDIDATE_VALIDATION,
            )

        static_step = next(
            step for step in decision.steps if step.stage is GateStage.STATIC
        )
        static_report: StaticGateReport | None = None
        if static_step.status is GateStepStatus.ERROR:
            if len(static_step.evidence) != 1:
                raise RegistryError("gate Static error evidence is incomplete")
            self._verify_error_evidence(
                static_step.evidence[0],
                stage=GateStage.STATIC,
            )
        elif static_step.evidence:
            if len(static_step.evidence) != 1:
                raise RegistryError("gate Static step must bind exactly one report")
            static_path = self._store.verify_ref(static_step.evidence[0])
            try:
                static_report = StaticGateReport.model_validate_json(
                    static_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise RegistryError("gate Static evidence is invalid") from exc
        if static_step.status is GateStepStatus.PASS and (
            static_report is None
            or static_report.status is not StaticGateStatus.PASS
            or static_report.skill_sha256 != decision.candidate_skill_sha256
            or not static_report.checks
            or not all(check.passed for check in static_report.checks)
        ):
            raise RegistryError(
                "passing gate Static evidence does not verify the candidate"
            )
        if static_step.status is not GateStepStatus.PASS and static_report is not None:
            report_failed = (
                static_report.status is StaticGateStatus.FAIL
                or static_report.skill_sha256 != decision.candidate_skill_sha256
                or not static_report.checks
                or not all(check.passed for check in static_report.checks)
            )
            if not report_failed:
                raise RegistryError("failed gate Static step carries a passing report")

        trigger_step = next(
            step for step in decision.steps if step.stage is GateStage.TRIGGER
        )
        trigger: TriggerEvalResult | None = None
        trigger_cost = Decimal(0)
        expected_unpaired_metrics = GateAggregateMetrics(
            cost_currency=policy.cost_currency
        )
        if trigger_step.status is GateStepStatus.PASS:
            if len(trigger_step.evidence) != 1:
                raise RegistryError("gate trigger evidence is missing")
            trigger_path = self._store.verify_ref(trigger_step.evidence[0])
            try:
                trigger = TriggerEvalResult.model_validate_json(
                    trigger_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise RegistryError("gate trigger evidence is invalid") from exc
            try:
                trigger_cost = validate_trigger_evidence(
                    trigger,
                    policy=policy,
                    skill_sha256=decision.candidate_skill_sha256,
                    measurement_kind=decision.measurement_kind,
                    measured_at=decision.decided_at,
                    mode=decision.mode,
                )
            except ValueError as exc:
                raise RegistryError(
                    "gate trigger evidence does not match its decision"
                ) from exc
            if (
                trigger.precision < policy.min_trigger_precision
                or trigger.recall < policy.min_trigger_recall
                or trigger.indeterminate_count > policy.max_trigger_indeterminate
            ):
                raise RegistryError("passing gate Trigger violates its locked policy")
            expected_unpaired_metrics = GateAggregateMetrics(
                trigger_precision=trigger.precision,
                trigger_recall=trigger.recall,
                trigger_indeterminate_count=trigger.indeterminate_count,
                trigger_cost_amount=trigger_cost,
                total_cost_amount=trigger_cost,
                cost_currency=policy.cost_currency,
                total_input_tokens=trigger.usage.input_tokens,
                total_output_tokens=trigger.usage.output_tokens,
            )
        elif trigger_step.status is GateStepStatus.FAIL:
            trigger_path = self._store.verify_ref(trigger_step.evidence[0])
            try:
                failed_trigger = TriggerEvalResult.model_validate_json(
                    trigger_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise RegistryError("failed gate Trigger evidence is invalid") from exc
            failed_cost, cost_complete, unpriced_call_count = observed_trigger_cost(
                failed_trigger,
                policy=policy,
                mode=decision.mode,
            )
            evidence_failed = False
            try:
                failed_cost = validate_trigger_evidence(
                    failed_trigger,
                    policy=policy,
                    skill_sha256=decision.candidate_skill_sha256,
                    measurement_kind=decision.measurement_kind,
                    measured_at=decision.decided_at,
                    mode=decision.mode,
                )
            except ValueError:
                evidence_failed = True
            threshold_failed = (
                failed_trigger.precision < policy.min_trigger_precision
                or failed_trigger.recall < policy.min_trigger_recall
                or failed_trigger.indeterminate_count > policy.max_trigger_indeterminate
            )
            if not evidence_failed and not threshold_failed:
                raise RegistryError("failed gate Trigger satisfies its locked policy")
            expected_unpaired_metrics = GateAggregateMetrics(
                trigger_precision=failed_trigger.precision,
                trigger_recall=failed_trigger.recall,
                trigger_indeterminate_count=failed_trigger.indeterminate_count,
                trigger_cost_amount=failed_cost,
                total_cost_amount=failed_cost,
                cost_currency=policy.cost_currency,
                total_input_tokens=failed_trigger.usage.input_tokens,
                total_output_tokens=failed_trigger.usage.output_tokens,
                cost_complete=cost_complete,
                unpriced_call_count=unpriced_call_count,
            )
        elif trigger_step.status is GateStepStatus.ERROR:
            if len(trigger_step.evidence) != 1:
                raise RegistryError("gate Trigger error evidence is incomplete")
            self._verify_error_evidence(
                trigger_step.evidence[0],
                stage=GateStage.TRIGGER,
            )
            expected_unpaired_metrics = GateAggregateMetrics(
                cost_currency=policy.cost_currency,
                cost_complete=False,
                unpriced_call_count=1,
            )

        selection = next(
            step for step in decision.steps if step.stage is GateStage.SELECTION
        )
        pair = self.selection_pair_from_decision(decision)
        if pair is not None:
            expected_nonce = hashlib.sha256(
                (
                    decision.gate_id
                    + decision.candidate_skill_sha256
                    + decision.decided_at.isoformat()
                ).encode("utf-8")
            ).hexdigest()
            if (
                pair.gate_id != decision.gate_id
                or pair.evaluation_nonce != expected_nonce
                or pair.iteration_id != SELECTION_ITERATION_ID
                or pair.accepted_skill_sha256 != decision.accepted_skill_sha256
                or pair.candidate_skill_sha256 != decision.candidate_skill_sha256
                or pair.selection_lock_sha256 != decision.selection_lock_sha256
                or pair.evaluation_protocol_sha256
                != decision.evaluation_protocol_sha256
                or pair.model_lock_sha256 != decision.model_lock_sha256
                or pair.measurement_kind is not decision.measurement_kind
                or pair.measured_at != decision.decided_at
                or pair.cost_currency != policy.cost_currency
                or len(pair.cases) != policy.selection_case_count
                or tuple(row.slot for row in pair.cases) != policy.selection_slots
                or tuple(row.slot for row in pair.cases if row.critical)
                != policy.critical_slots
            ):
                raise RegistryError("gate selection pair does not match its decision")
            expected_selection_evidence = (
                selection.evidence[0],
                pair.accepted_events,
                pair.candidate_events,
            )
            if selection.evidence != expected_selection_evidence:
                raise RegistryError("gate selection evidence references are incomplete")
            pair_ref = selection.evidence[0]
            for stage in (
                GateStage.CRITICAL_REGRESSION,
                GateStage.OVERALL_QUALITY,
                GateStage.COST,
                GateStage.BUDGET,
            ):
                step = next(item for item in decision.steps if item.stage is stage)
                if (
                    step.status is not GateStepStatus.NOT_EVALUATED
                    and step.evidence != (pair_ref,)
                ):
                    raise RegistryError(
                        "downstream gate evidence does not bind the selection pair"
                    )
            self._verify_pair_event_log(pair, side="accepted")
            self._verify_pair_event_log(pair, side="candidate")
        if pair is not None:
            if trigger is None:
                raise RegistryError("paired gate decision lacks Trigger evidence")
            self._verify_measured_decision(
                decision,
                policy=policy,
                trigger=trigger,
                trigger_cost=trigger_cost,
                pair=pair,
            )
        elif decision.outcome is GateOutcome.ACCEPTED:
            raise RegistryError("accepted gate decision lacks measured evidence")
        else:
            if selection.status in {GateStepStatus.FAIL, GateStepStatus.ERROR}:
                expected_unpaired_metrics = expected_unpaired_metrics.model_copy(
                    update={"cost_complete": False, "unpriced_call_count": 1}
                )
            if decision.metrics != expected_unpaired_metrics:
                raise RegistryError("unpaired gate metrics do not match their evidence")
        return pair

    @staticmethod
    def _verify_measured_decision(
        decision: GateDecision,
        *,
        policy: GatePolicy,
        trigger: TriggerEvalResult,
        trigger_cost: Decimal,
        pair: SelectionPairEvaluation,
    ) -> None:
        metrics = decision.metrics
        count = len(pair.cases)
        accepted_passes = sum(
            row.accepted_status is RunnerStatus.PASS for row in pair.cases
        )
        candidate_passes = sum(
            row.candidate_status is RunnerStatus.PASS for row in pair.cases
        )
        accepted_cost = sum(
            (row.accepted_cost_amount for row in pair.cases),
            Decimal(0),
        )
        candidate_cost = sum(
            (row.candidate_cost_amount for row in pair.cases),
            Decimal(0),
        )
        relative_cost = (
            (candidate_cost - accepted_cost) / accepted_cost
            if accepted_cost > 0 and candidate_cost > accepted_cost
            else Decimal(0)
            if candidate_cost <= accepted_cost
            else None
        )
        critical_regressions = sum(
            row.critical
            and row.accepted_status is RunnerStatus.PASS
            and row.candidate_status is not RunnerStatus.PASS
            for row in pair.cases
        )
        input_tokens = trigger.usage.input_tokens + sum(
            row.accepted_input_tokens + row.candidate_input_tokens for row in pair.cases
        )
        output_tokens = trigger.usage.output_tokens + sum(
            row.accepted_output_tokens + row.candidate_output_tokens
            for row in pair.cases
        )
        expected = (
            trigger.precision,
            trigger.recall,
            trigger.indeterminate_count,
            count,
            accepted_passes,
            candidate_passes,
            accepted_passes / count,
            candidate_passes / count,
            (candidate_passes - accepted_passes) / count,
            critical_regressions,
            trigger_cost,
            accepted_cost,
            candidate_cost,
            trigger_cost + accepted_cost + candidate_cost,
            relative_cost,
            pair.cost_currency,
            input_tokens,
            output_tokens,
            True,
            0,
        )
        actual = (
            metrics.trigger_precision,
            metrics.trigger_recall,
            metrics.trigger_indeterminate_count,
            metrics.selection_case_count,
            metrics.accepted_pass_count,
            metrics.candidate_pass_count,
            metrics.accepted_pass_rate,
            metrics.candidate_pass_rate,
            metrics.quality_delta,
            metrics.critical_regression_count,
            metrics.trigger_cost_amount,
            metrics.accepted_cost_amount,
            metrics.candidate_cost_amount,
            metrics.total_cost_amount,
            metrics.relative_cost_increase,
            metrics.cost_currency,
            metrics.total_input_tokens,
            metrics.total_output_tokens,
            metrics.cost_complete,
            metrics.unpriced_call_count,
        )
        if actual != expected:
            raise RegistryError("gate metrics do not match their measured evidence")
        statuses = {row.accepted_status for row in pair.cases} | {
            row.candidate_status for row in pair.cases
        }
        expected_terminal: (
            tuple[
                GateStage,
                GateStepStatus,
                tuple[GateReason, ...],
            ]
            | None
        ) = None
        if RunnerStatus.BUDGET_STOP in statuses:
            expected_terminal = (
                GateStage.SELECTION,
                GateStepStatus.BUDGET_STOP,
                (GateReason.BUDGET_STOP,),
            )
        elif RunnerStatus.JUDGE_ERROR in statuses:
            expected_terminal = (
                GateStage.SELECTION,
                GateStepStatus.ERROR,
                (GateReason.JUDGE_ERROR,),
            )
        elif statuses - {RunnerStatus.PASS, RunnerStatus.AGENT_FAIL}:
            expected_terminal = (
                GateStage.SELECTION,
                GateStepStatus.ERROR,
                (GateReason.EVALUATION_ERROR,),
            )
        elif critical_regressions > policy.max_critical_regressions:
            expected_terminal = (
                GateStage.CRITICAL_REGRESSION,
                GateStepStatus.FAIL,
                (GateReason.CRITICAL_REGRESSION,),
            )
        elif metrics.quality_delta <= policy.min_quality_delta:
            reason = (
                GateReason.TIE
                if metrics.quality_delta == 0
                else GateReason.OVERALL_REGRESSION
            )
            expected_terminal = (
                GateStage.OVERALL_QUALITY,
                GateStepStatus.FAIL,
                (reason,),
            )
        else:
            cost_reasons: list[GateReason] = []
            if candidate_cost > policy.max_candidate_cost_amount:
                cost_reasons.append(GateReason.COST_LIMIT)
            if (
                relative_cost is None
                or relative_cost > policy.max_relative_cost_increase
            ):
                cost_reasons.append(GateReason.COST_GROWTH)
            if cost_reasons:
                expected_terminal = (
                    GateStage.COST,
                    GateStepStatus.FAIL,
                    tuple(cost_reasons),
                )
            else:
                budget_reasons: list[GateReason] = []
                if (
                    trigger_cost + accepted_cost + candidate_cost
                    > policy.max_gate_cost_amount
                ):
                    budget_reasons.append(GateReason.COST_LIMIT)
                if (
                    input_tokens > policy.max_gate_input_tokens
                    or output_tokens > policy.max_gate_output_tokens
                ):
                    budget_reasons.append(GateReason.TOKEN_BUDGET)
                if budget_reasons:
                    expected_terminal = (
                        GateStage.BUDGET,
                        GateStepStatus.FAIL,
                        tuple(budget_reasons),
                    )

        terminal = next(
            (step for step in decision.steps if step.status is not GateStepStatus.PASS),
            None,
        )
        actual_terminal = (
            None
            if terminal is None
            else (
                terminal.stage,
                terminal.status,
                terminal.reason_codes,
            )
        )
        if actual_terminal != expected_terminal:
            raise RegistryError("gate outcome does not match its locked policy")

    def _verify_pair_event_log(
        self,
        pair: SelectionPairEvaluation,
        *,
        side: _SelectionSide,
    ) -> None:
        reference = (
            pair.accepted_events if side == "accepted" else pair.candidate_events
        )
        path = self._store.verify_ref(reference)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RegistryError("selection event evidence cannot be read") from exc
        try:
            _validate_selection_event_bytes(content, pair, side=side)
        except ValueError as exc:
            raise RegistryError(str(exc)) from exc

    @staticmethod
    def decision_evidence(decision: GateDecision) -> tuple[ArtifactRef, ...]:
        ordered = [
            decision.candidate,
            decision.accepted_manifest,
            decision.gate_policy,
        ]
        ordered.extend(
            reference for step in decision.steps for reference in step.evidence
        )
        unique: dict[tuple[str, str, str], ArtifactRef] = {}
        for reference in ordered:
            key = (reference.root.value, reference.path, reference.sha256)
            unique.setdefault(key, reference)
        return tuple(unique.values())

    def lineage_protocol_identity(
        self,
        state: RegistryState,
    ) -> tuple[str, str, str, str] | None:
        identity: tuple[str, str, str, str] | None = None
        for version in state.versions.values():
            if version.gate_decision is None:
                continue
            current = _protocol_identity(self.decision_from_ref(version.gate_decision))
            if identity is not None and identity != current:
                raise RegistryError("Registry lineage contains mixed gate protocols")
            identity = current
        return identity

    @staticmethod
    def claim_pair_identity(
        pair: SelectionPairEvaluation | None,
        *,
        nonces: set[str],
        run_ids: set[str],
    ) -> None:
        if pair is None:
            return
        pair_run_ids = {pair.accepted_run_id, pair.candidate_run_id}
        if pair.evaluation_nonce in nonces or pair_run_ids & run_ids:
            raise RegistryError(
                "selection nonce and run IDs must be fresh within the lineage"
            )
        nonces.add(pair.evaluation_nonce)
        run_ids.update(pair_run_ids)

    def ensure_pair_identity_is_fresh(
        self,
        pair: SelectionPairEvaluation | None,
        *,
        state: RegistryState,
    ) -> None:
        if pair is None:
            return
        nonces: set[str] = set()
        run_ids: set[str] = set()
        for version in state.versions.values():
            if version.gate_decision is None:
                continue
            decision = self.decision_from_ref(version.gate_decision)
            self.claim_pair_identity(
                self.selection_pair_from_decision(decision),
                nonces=nonces,
                run_ids=run_ids,
            )
        self.claim_pair_identity(pair, nonces=nonces, run_ids=run_ids)


__all__: tuple[str, ...] = ()
