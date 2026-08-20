"""AttemptEvaluator implementation for lifecycle-safe shopping episodes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Protocol

from pydantic import JsonValue

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    GradeStatus,
    RecordType,
    RunnerStatus,
    SchemaVersion,
    Trace,
    Usage,
    artifact_json_bytes,
)
from ses.contracts.base import VersionedRecord
from ses.contracts.runner import RunArtifacts
from ses.contracts.shopping import (
    EpisodeStep,
    MeasurementLevel,
    OpenShoppingCase,
    ShoppingTaskRef,
    ShopSimulatorEpisodeResult,
    TurnLease,
)
from ses.evaluation import build_trace
from ses.runner.baseline import CaseEvaluation, EvaluationContext
from ses.shopping.adapters import (
    AdapterProtocolError,
    OutcomeUnknownError,
    ShopSimulatorPort,
)
from ses.shopping.gateway import ShoppingGatewayError, ShoppingMCPGateway
from ses.shopping.grading import (
    LockedShoppingGradePolicy,
    ShoppingGradeInput,
    ShoppingGradePolicy,
)
from ses.shopping.safety import (
    ShoppingPrivateExpectation,
    assess_purchase_safety,
)
from ses.skills.installer import (
    SkillManifest,
    install_skill,
    load_skill_manifest,
    normalized_skill_sha256,
)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_TOOLS = (
    "mcp__shop_simulator__search",
    "mcp__shop_simulator__click",
    "mcp__shop_simulator__ask_shopper",
    "mcp__shop_simulator__purchase",
    "mcp__shop_simulator__finish_without_purchase",
)


class ShoppingTurnEngine(Protocol):
    def run_turn(
        self,
        request: EngineRequest,
        gateway: ShoppingMCPGateway,
        lease: TurnLease,
    ) -> tuple[EngineEvent, ...]: ...


def _sum_usage(traces: tuple[Trace, ...]) -> Usage:
    input_tokens = sum(trace.usage.input_tokens for trace in traces if trace.usage)
    output_tokens = sum(trace.usage.output_tokens for trace in traces if trace.usage)
    costs = tuple(trace.usage for trace in traces if trace.usage is not None)
    currencies = {usage.cost_currency for usage in costs if usage.cost_currency}
    if len(currencies) > 1:
        raise ValueError("shopping attempt mixed usage currencies")
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=sum(
            (usage.cost_amount or Decimal(0) for usage in costs), Decimal(0)
        ),
        cost_currency=next(iter(currencies), "CNY"),
    )


def _write_record(run_dir: Path, path: Path, record: VersionedRecord) -> ArtifactRef:
    payload = artifact_json_bytes(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(run_dir).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _write_json(run_dir: Path, path: Path, value: object) -> ArtifactRef:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(run_dir).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


class ShopSimulatorAttemptEvaluator:
    """Deep module that projects a fresh shopping episode into CaseEvaluation."""

    def __init__(
        self,
        *,
        port: ShopSimulatorPort,
        tasks: Mapping[str, ShoppingTaskRef],
        engine_factory: Callable[[EvaluationContext], ShoppingTurnEngine],
        profile_sha256: str,
        measurement_level: MeasurementLevel,
        model_lock_sha256: str = _EMPTY_SHA256,
        skill_sha256: str = _EMPTY_SHA256,
        skill_source: Path | None = None,
        protocol_sha256: str | None = None,
        network_used: bool = False,
        grade_policy: ShoppingGradePolicy | None = None,
        private_expectations: Mapping[str, ShoppingPrivateExpectation] | None = None,
    ) -> None:
        self._port = port
        self._tasks = dict(tasks)
        self._engine_factory = engine_factory
        self._profile_sha256 = profile_sha256
        self._measurement_level = measurement_level
        self._model_lock_sha256 = model_lock_sha256
        self._skill_sha256 = skill_sha256
        self._skill_source = skill_source
        self._skill_manifest: SkillManifest | None = None
        if skill_source is not None:
            manifest = load_skill_manifest(skill_source)
            if normalized_skill_sha256(skill_source) != skill_sha256:
                raise ValueError("shopping Skill source does not match its locked hash")
            self._skill_manifest = manifest
        self._protocol_sha256 = (
            protocol_sha256 or hashlib.sha256(b"ses-shopping-fixed-v1").hexdigest()
        )
        if measurement_level is MeasurementLevel.SYNTHETIC_OFFLINE and network_used:
            raise ValueError("fixed evaluator cannot claim network use")
        self._network_used = network_used
        self._grade_policy = grade_policy or LockedShoppingGradePolicy()
        self._private_expectations = dict(private_expectations or {})
        unknown_expectations = self._private_expectations.keys() - self._tasks.keys()
        if unknown_expectations:
            raise ValueError("shopping expectations reference unknown cases")

    def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
        started = monotonic()
        task = self._tasks.get(context.case_id)
        if task is None:
            raise ValueError("shopping case is absent from the locked profile")
        attempt_root = (
            context.run_dir
            / "artifacts"
            / context.case_id
            / context.iteration_id
            / context.attempt_id
        )
        workspace = attempt_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=False)
        if self._skill_source is not None:
            assert self._skill_manifest is not None
            installation = install_skill(
                self._skill_source,
                workspace / ".claude" / "skills" / self._skill_manifest.name,
                version=self._skill_manifest.version,
            )
            if installation.sha256 != self._skill_sha256:
                raise ValueError("attempt Skill installation changed its locked hash")
        engine = self._engine_factory(context)
        traces: list[Trace] = []
        trace_refs: list[ArtifactRef] = []
        session_id: str | None = None
        open_request = OpenShoppingCase(
            task=task,
            profile_sha256=self._profile_sha256,
            session_owner=context.attempt_id,
            measurement_level=self._measurement_level,
        )
        try:
            with self._port.open_episode(open_request) as episode:
                gateway = ShoppingMCPGateway(
                    episode=episode,
                    artifact_root=attempt_root,
                )
                terminal_step = None
                for turn_index in range(1, context.max_turns + 1):
                    lease = gateway.issue_turn(turn_sequence=turn_index)
                    request = EngineRequest(
                        schema_version=SchemaVersion.V1ALPHA1,
                        record_type=RecordType.ENGINE_REQUEST,
                        request_id=(
                            f"{context.run_id}:{context.case_id}:"
                            f"{context.iteration_id}:{context.attempt_id}:turn-{turn_index}"
                        ),
                        prompt=gateway.current_observation.text,
                        resume_session_id=session_id,
                        allowed_tools=_TOOLS,
                        timeout_seconds=30,
                    )
                    events = tuple(engine.run_turn(request, gateway, lease))
                    trace = build_trace(
                        events,
                        request=request,
                        run_id=context.run_id,
                        case_id=context.case_id,
                        iteration_id=(f"{context.iteration_id}:turn-{turn_index - 1}"),
                        trace_id=(
                            f"trace-{context.run_id}-{context.case_id}-"
                            f"{context.iteration_id}-turn-{turn_index}"
                        ),
                    )
                    traces.append(trace)
                    trace_refs.append(
                        _write_record(
                            context.run_dir,
                            attempt_root / f"trace-turn-{turn_index:04d}.json",
                            trace,
                        )
                    )
                    if trace.exit_status is not EngineExitStatus.SUCCESS:
                        return self._partial(
                            context,
                            traces=tuple(traces),
                            trace_refs=tuple(trace_refs),
                            status=RunnerStatus.INFRASTRUCTURE_ERROR,
                            started=started,
                            error="engine turn failed",
                        )
                    if gateway.receipt is None or gateway.last_step is None:
                        return self._partial(
                            context,
                            traces=tuple(traces),
                            trace_refs=tuple(trace_refs),
                            status=RunnerStatus.AGENT_FAIL,
                            started=started,
                            error="engine turn completed without a shopping action",
                        )
                    if session_id is None:
                        session_id = trace.session_id
                    elif trace.session_id != session_id:
                        return self._partial(
                            context,
                            traces=tuple(traces),
                            trace_refs=tuple(trace_refs),
                            status=RunnerStatus.INFRASTRUCTURE_ERROR,
                            started=started,
                            error="engine changed session within one attempt",
                        )
                    terminal_step = gateway.last_step
                    if terminal_step.terminal:
                        break
                if terminal_step is None or not terminal_step.terminal:
                    return self._partial(
                        context,
                        traces=tuple(traces),
                        trace_refs=tuple(trace_refs),
                        status=RunnerStatus.BUDGET_STOP,
                        started=started,
                        stop_reason="turn_limit",
                    )
                return self._grade_terminal(
                    context,
                    task=task,
                    gateway=gateway,
                    traces=tuple(traces),
                    trace_refs=tuple(trace_refs),
                    terminal_step=terminal_step,
                    attempt_root=attempt_root,
                    started=started,
                )
        except ShoppingGatewayError as exc:
            return self._partial(
                context,
                traces=tuple(traces),
                trace_refs=tuple(trace_refs),
                status=RunnerStatus.AGENT_FAIL,
                started=started,
                error=str(exc),
            )
        except OutcomeUnknownError:
            return self._partial(
                context,
                traces=tuple(traces),
                trace_refs=tuple(trace_refs),
                status=RunnerStatus.INFRASTRUCTURE_ERROR,
                started=started,
                error="outcome_unknown",
                stop_reason="outcome_unknown",
            )
        except AdapterProtocolError as exc:
            return self._partial(
                context,
                traces=tuple(traces),
                trace_refs=tuple(trace_refs),
                status=RunnerStatus.INFRASTRUCTURE_ERROR,
                started=started,
                error=type(exc).__name__,
            )

    def _grade_terminal(
        self,
        context: EvaluationContext,
        *,
        task: ShoppingTaskRef,
        gateway: ShoppingMCPGateway,
        traces: tuple[Trace, ...],
        trace_refs: tuple[ArtifactRef, ...],
        terminal_step: EpisodeStep,
        attempt_root: Path,
        started: float,
    ) -> CaseEvaluation:
        raw = terminal_step.raw_reward
        raw_ref = (
            _write_record(
                context.run_dir,
                attempt_root / "raw-reward.json",
                raw,
            )
            if raw is not None
            else None
        )
        purchase_receipts = gateway.purchase_attempts
        safety = assess_purchase_safety(
            tuple(receipt for receipt, _ in purchase_receipts),
            raw_reward=raw,
            expectation=self._private_expectations.get(context.case_id),
        )
        violation_codes = safety.violation_codes
        receipt_refs = tuple(
            self._run_artifact_ref(context, attempt_root, reference)
            for _, reference in gateway.receipts
        )
        purchase_attempt_refs = tuple(
            self._run_artifact_ref(context, attempt_root, reference)
            for _, reference in purchase_receipts
        )
        safety_ref = _write_json(
            context.run_dir,
            attempt_root / "safety-evidence.json",
            {
                "schema_version": "v1alpha1",
                "record_type": "shopping_safety_evidence",
                **dict(safety.public_evidence),
                "purchase_attempts": [
                    reference.model_dump(mode="json")
                    for reference in purchase_attempt_refs
                ],
            },
        )
        grade_input = ShoppingGradeInput(
            run_id=context.run_id,
            case_id=context.case_id,
            iteration_id=context.iteration_id,
            raw_reward=raw,
            raw_reward_ref=raw_ref,
            purchased_asin=safety.purchased_product_id,
            private_goal_asin=safety.private_goal_product_id,
            safety_violation_count=safety.safety_violation_count,
            safety_evidence=(safety_ref, *purchase_attempt_refs),
            violation_codes=violation_codes,
        )
        metric = self._grade_policy.project(grade_input)
        metric_ref = _write_record(
            context.run_dir,
            attempt_root / "shopping-metric.json",
            metric,
        )
        grade = self._grade_policy.grade(grade_input, metric, metric_ref)
        grade_ref = _write_record(
            context.run_dir,
            attempt_root / "case-grade.json",
            grade,
        )
        usage = _sum_usage(traces)
        terminal_reason = terminal_step.terminal_reason
        assert terminal_reason is not None
        result = ShopSimulatorEpisodeResult(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="shop_simulator_episode_result",
            result_id=(
                f"shopping-result-{context.case_id}-{context.iteration_id}-"
                f"{context.attempt_id}"
            ),
            run_id=context.run_id,
            case_id=context.case_id,
            iteration_id=context.iteration_id,
            episode_nonce=terminal_step.episode_nonce,
            scenario=task.scenario,
            measurement_level=self._measurement_level,
            network_used=self._network_used,
            terminal_reason=terminal_reason,
            traces=trace_refs,
            action_receipts=receipt_refs,
            raw_reward=raw_ref,
            metric=metric_ref,
            grade=grade_ref,
            profile_sha256=self._profile_sha256,
            skill_sha256=self._skill_sha256,
            model_lock_sha256=self._model_lock_sha256,
            protocol_sha256=self._protocol_sha256,
            usage=usage,
            safety_violation_count=safety.safety_violation_count,
        )
        result_ref = _write_record(
            context.run_dir,
            attempt_root / "episode-result.json",
            result,
        )
        evidence: tuple[Mapping[str, JsonValue], ...] = (
            {
                "safety_violation_count": safety.safety_violation_count,
                "violation_codes": list(violation_codes),
                "benchmark_success": metric.benchmark_success,
                "strict_reward": str(metric.r_strict),
                "scenario": task.scenario.value,
            },
        )
        return CaseEvaluation(
            case_id=context.case_id,
            iteration_id=context.iteration_id,
            status=(
                RunnerStatus.PASS
                if grade.status is GradeStatus.PASS
                else RunnerStatus.AGENT_FAIL
            ),
            turn_count=len(traces),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_amount=usage.cost_amount or Decimal(0),
            cost_currency=usage.cost_currency or context.cost_currency,
            latency_ms=round((monotonic() - started) * 1000),
            artifacts=RunArtifacts(
                traces=trace_refs,
                grade=grade_ref,
                domain_result=result_ref,
                shopping_raw_reward=raw_ref,
                shopping_metric=metric_ref,
                shopping_safety_evidence=(safety_ref, *purchase_attempt_refs),
                shopping_action_receipts=receipt_refs,
            ),
            session_resumed=len(traces) > 1,
            evidence=evidence,
            tool_timeline=tuple(
                {
                    "turn": receipt.turn_sequence,
                    "action": receipt.action_kind.value,
                    "receipt": reference.path,
                }
                for (receipt, _), reference in zip(
                    gateway.receipts, receipt_refs, strict=True
                )
            ),
            stop_reason=(
                "finish_without_purchase"
                if terminal_step.terminal_reason == "finish_without_purchase"
                else None
            ),
        )

    @staticmethod
    def _run_artifact_ref(
        context: EvaluationContext,
        attempt_root: Path,
        reference: ArtifactRef,
    ) -> ArtifactRef:
        return ArtifactRef(
            root=ArtifactRoot.RUN,
            path=(attempt_root / reference.path)
            .relative_to(context.run_dir)
            .as_posix(),
            sha256=reference.sha256,
        )

    @staticmethod
    def _partial(
        context: EvaluationContext,
        *,
        traces: tuple[Trace, ...],
        trace_refs: tuple[ArtifactRef, ...],
        status: RunnerStatus,
        started: float,
        error: str | None = None,
        stop_reason: str | None = None,
    ) -> CaseEvaluation:
        usage = _sum_usage(traces)
        return CaseEvaluation(
            case_id=context.case_id,
            iteration_id=context.iteration_id,
            status=status,
            turn_count=len(traces),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_amount=usage.cost_amount or Decimal(0),
            cost_currency=usage.cost_currency or context.cost_currency,
            latency_ms=round((monotonic() - started) * 1000),
            artifacts=RunArtifacts(traces=trace_refs),
            session_resumed=len(traces) > 1,
            error=error,
            stop_reason=stop_reason,
        )
