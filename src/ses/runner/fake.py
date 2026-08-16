"""Offline develop-catalog evaluator wired through the complete L1 pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CompletedPayload,
    EngineEvent,
    EngineRequest,
    GradeStatus,
    StateDiff,
    TextDeltaPayload,
    ToolCallPayload,
    ToolResultPayload,
    ToolResultStatus,
    Trace,
    Usage,
    UsagePayload,
    VersionedRecord,
    artifact_json_bytes,
)
from ses.contracts.runner import RunArtifacts, RunnerStatus
from ses.engines.base import Engine
from ses.engines.fake import FakeEngine, FakeFixture, FakeStep
from ses.evaluation import (
    aggregate_case_grade,
    judge_rules_across_traces,
    judge_state,
    tool_arguments,
    tool_count,
    tool_order,
    trace_messages,
    trace_tool_calls,
)
from ses.evaluator.multi_turn import MultiTurnEvaluator, MultiTurnOutcome
from ses.foundation.workspace import WorkspaceFactory
from ses.runner.baseline import CaseEvaluation, EvaluationContext
from ses.shop import CaseEnvironment, ReturnCaseFixture, state_diff
from ses.simulation import FakeSimulator, UserIntent


@dataclass(frozen=True, slots=True)
class ExecutableDevelopCase:
    """Private execution inputs paired with one public case definition."""

    fixture: ReturnCaseFixture
    expected_actions: tuple[tuple[str, Mapping[str, JsonValue]], ...]
    qualification_hash: str
    manifest_data_version: str


def _verify_reference(root: Path, value: object) -> Path:
    if not isinstance(value, Mapping):
        raise ValueError("develop catalog artifact reference is invalid")
    relative = value.get("path")
    expected = value.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError("develop catalog artifact reference is incomplete")
    reference = ArtifactRef(
        root=ArtifactRoot.RUN,
        path=relative,
        sha256=expected,
    )
    resolved_root = root.resolve()
    path = (root / reference.path).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError("develop catalog artifact escapes its manifest root")
    reference.verify_bytes(path.read_bytes())
    return path


def _verify_curation_manifest(root: Path, path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("curation manifest must be an object")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("curation manifest sources must be nonempty")
    selected = 0
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("curation manifest source is invalid")
        artifacts = source.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise ValueError("curation manifest artifacts are invalid")
        for reference in artifacts.values():
            if reference is not None:
                _verify_reference(root, reference)
        if source.get("selected") is True:
            selected += 1
            if artifacts.get("rubric_draft") is None:
                raise ValueError("selected curation source requires a rubric draft")
    if payload.get("source_candidate_count") != len(sources):
        raise ValueError("curation source count does not match its manifest")
    if payload.get("selected_source_count") != selected:
        raise ValueError("curation selected count does not match its manifest")


def load_develop_catalog(
    manifest_path: Path | None = None,
) -> Mapping[str, ExecutableDevelopCase]:
    """Load qualified executable cases from the versioned develop manifest."""

    if manifest_path is None:
        manifest_path = (
            Path(__file__).resolve().parents[3]
            / "data"
            / "testset"
            / "ticket07"
            / "generated"
            / "develop-manifest.json"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("develop manifest must be an object")
    cases = payload.get("cases")
    data_version = payload.get("data_version")
    if not isinstance(cases, list) or not isinstance(data_version, str):
        raise ValueError("develop manifest is incomplete")
    root = manifest_path.parent
    _verify_reference(root, payload.get("qualification_manifest"))
    curation_path = _verify_reference(root, payload.get("curation_manifest"))
    _verify_curation_manifest(root, curation_path)
    version_body = dict(payload)
    version_body.pop("data_version", None)
    computed_data_version = hashlib.sha256(
        json.dumps(
            version_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if computed_data_version != data_version:
        raise ValueError("develop manifest data_version does not match its content")
    catalog: dict[str, ExecutableDevelopCase] = {}
    for row in cases:
        if not isinstance(row, Mapping):
            raise ValueError("develop manifest case is invalid")
        case_id = row.get("case_id")
        actions = row.get("expected_actions")
        qualification_hash = row.get("qualification_hash")
        if (
            not isinstance(case_id, str)
            or not isinstance(actions, list)
            or not isinstance(qualification_hash, str)
        ):
            raise ValueError("develop manifest case is incomplete")
        fixture_path = _verify_reference(root, row.get("fixture"))
        fixture = ReturnCaseFixture.model_validate_json(
            fixture_path.read_text(encoding="utf-8")
        )
        if fixture.case_id != case_id:
            raise ValueError("develop fixture identity does not match manifest")
        parsed_actions: list[tuple[str, Mapping[str, JsonValue]]] = []
        for action in actions:
            if not isinstance(action, Mapping):
                raise ValueError("develop expected action is invalid")
            tool_name = action.get("tool_name")
            arguments = action.get("arguments")
            if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
                raise ValueError("develop expected action is incomplete")
            parsed_actions.append(
                (tool_name, cast(Mapping[str, JsonValue], dict(arguments)))
            )
        if case_id in catalog:
            raise ValueError("develop manifest case IDs must be unique")
        catalog[case_id] = ExecutableDevelopCase(
            fixture=fixture,
            expected_actions=tuple(parsed_actions),
            qualification_hash=qualification_hash,
            manifest_data_version=data_version,
        )
    if len(catalog) < 15:
        raise ValueError("qualified develop catalog must contain at least 15 cases")
    return catalog


def develop_catalog_sha256(
    catalog: Mapping[str, ExecutableDevelopCase],
) -> str:
    """Hash the exact fixtures and expected actions supplied to evaluation."""

    payload = [
        {
            "case_id": case_id,
            "fixture": case.fixture.model_dump(mode="json"),
            "expected_actions": [
                {"tool_name": tool_name, "arguments": dict(arguments)}
                for tool_name, arguments in case.expected_actions
            ],
            "qualification_hash": case.qualification_hash,
            "manifest_data_version": case.manifest_data_version,
        }
        for case_id, case in sorted(catalog.items())
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class _SequencedFakeEngine:
    """Ask for one fact on the fresh turn, then execute the case on resume."""

    def __init__(self, action_fixture: FakeFixture) -> None:
        opening = FakeFixture(
            session_id=action_fixture.session_id,
            events=(
                FakeStep(
                    payload=TextDeltaPayload(
                        message_id="opening-message",
                        text="Please share the item id so I can complete the return.",
                    )
                ),
                FakeStep(
                    payload=UsagePayload(usage=Usage(input_tokens=31, output_tokens=13))
                ),
            ),
        )
        self._opening = FakeEngine(opening)
        self._action = FakeEngine(action_fixture)

    async def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]:
        engine = self._opening if request.resume_session_id is None else self._action
        async for event in engine.stream(request):
            yield event

    async def cancel(self, request_id: str) -> bool:
        opening = await self._opening.cancel(request_id)
        action = await self._action.cancel(request_id)
        return opening or action


class _ShopBoundEngine:
    """Execute canonical tool calls against one fresh case environment."""

    def __init__(self, engine: Engine, environment: CaseEnvironment) -> None:
        self._engine = engine
        self._environment = environment

    async def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]:
        pending: dict[str, ToolResultPayload] = {}
        async for event in self._engine.stream(request):
            payload = event.payload
            if isinstance(payload, ToolCallPayload):
                if payload.tool_call_id in pending:
                    raise RuntimeError("engine repeated a pending tool call")
                result = self._environment.execute(payload.tool_name, payload.arguments)
                pending[payload.tool_call_id] = ToolResultPayload(
                    tool_call_id=payload.tool_call_id,
                    content=cast(
                        JsonValue,
                        result.model_dump(mode="json", round_trip=True),
                    ),
                    is_error=result.status is ToolResultStatus.ERROR,
                )
            elif isinstance(payload, ToolResultPayload):
                actual = pending.pop(payload.tool_call_id, None)
                if actual is None:
                    raise RuntimeError("engine emitted a tool result without a call")
                event = event.model_copy(update={"payload": actual})
            elif isinstance(payload, CompletedPayload) and pending:
                raise RuntimeError("engine completed with unresolved tool calls")
            yield event

    async def cancel(self, request_id: str) -> bool:
        return await self._engine.cancel(request_id)


def _action_fixture(case: ExecutableDevelopCase) -> FakeFixture:
    steps: list[FakeStep] = [
        FakeStep(
            payload=TextDeltaPayload(
                message_id="message-1",
                text="I will inspect the order, review policy, and process the return.",
            )
        )
    ]
    for index, (tool_name, arguments) in enumerate(case.expected_actions):
        tool_call_id = f"tool-{index:02d}-{tool_name}"
        steps.extend(
            (
                FakeStep(
                    payload=ToolCallPayload(
                        message_id="message-1",
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                ),
                FakeStep(
                    payload=ToolResultPayload(
                        tool_call_id=tool_call_id,
                        content={"fixture_placeholder": True},
                        is_error=False,
                    )
                ),
            )
        )
    steps.extend(
        (
            FakeStep(
                payload=TextDeltaPayload(
                    message_id="message-2", text="The return operation is complete."
                )
            ),
            FakeStep(
                payload=UsagePayload(usage=Usage(input_tokens=137, output_tokens=61))
            ),
        )
    )
    return FakeFixture(
        session_id=f"session-{case.fixture.case_id}", events=tuple(steps)
    )


def _artifact_ref(run_dir: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.resolve().relative_to(run_dir.resolve()).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _write_record(run_dir: Path, relative: str, record: VersionedRecord) -> ArtifactRef:
    destination = run_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = artifact_json_bytes(record)
    with destination.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return _artifact_ref(run_dir, destination)


def _expected_diff(case: ExecutableDevelopCase) -> StateDiff:
    environment = CaseEnvironment(case.fixture)
    try:
        before = environment.snapshot()
        for tool_name, arguments in case.expected_actions:
            result = environment.execute(tool_name, arguments)
            if result.status is not ToolResultStatus.SUCCESS:
                raise RuntimeError("develop catalog contains invalid expected actions")
        return state_diff(before, environment.snapshot())
    finally:
        environment.close()


def _timeline(traces: tuple[Trace, ...]) -> tuple[Mapping[str, JsonValue], ...]:
    timeline: list[Mapping[str, JsonValue]] = []
    for turn_index, trace in enumerate(traces):
        timeline.extend(
            {
                "turn": turn_index,
                "sequence": call.sequence,
                "tool_name": call.tool_name,
                "arguments": dict(call.arguments),
                "is_error": None if call.result is None else call.result.is_error,
            }
            for call in trace_tool_calls(trace)
        )
    return tuple(timeline)


def _transcript(traces: tuple[Trace, ...]) -> tuple[Mapping[str, JsonValue], ...]:
    transcript: list[Mapping[str, JsonValue]] = []
    for trace in traces:
        transcript.append({"role": "user", "content": trace.request.prompt})
        transcript.extend(
            {"role": "assistant", "content": message.text}
            for message in trace_messages(trace)
        )
    return tuple(transcript)


class DevelopCatalogEvaluator:
    """Evaluate catalog cases with isolated Shop state, Simulator, Trace, and Judges."""

    def __init__(
        self, catalog: Mapping[str, ExecutableDevelopCase] | None = None
    ) -> None:
        self._catalog = dict(catalog or load_develop_catalog())

    def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
        case = self._catalog.get(context.case_id)
        if case is None:
            raise ValueError(
                f"case is not in the executable develop catalog: {context.case_id}"
            )
        return asyncio.run(self._evaluate(context, case))

    async def _evaluate(
        self, context: EvaluationContext, case: ExecutableDevelopCase
    ) -> CaseEvaluation:
        attempt_root = (
            f"artifacts/{context.case_id}/{context.iteration_id}/{context.attempt_id}"
        )
        WorkspaceFactory(context.run_dir / "workspaces").create(
            run_id=context.run_id,
            case_id=context.case_id,
            iteration_id=f"{context.iteration_id}:{context.attempt_id}",
        )
        environment = CaseEnvironment(case.fixture)
        trace_refs: list[ArtifactRef] = []

        def persist_trace(trace: Trace) -> None:
            trace_refs.append(
                _write_record(
                    context.run_dir,
                    f"{attempt_root}/trace-turn-{len(trace_refs)}.json",
                    trace,
                )
            )

        try:
            before = environment.snapshot()
            before_ref = _write_record(
                context.run_dir, f"{attempt_root}/before.json", before
            )
            simulator = FakeSimulator(
                UserIntent(
                    want=case.fixture.user_prompt,
                    allowed_facts={"item_id": case.fixture.item.item_id},
                )
            )
            engine = _ShopBoundEngine(
                _SequencedFakeEngine(_action_fixture(case)), environment
            )
            multi = await MultiTurnEvaluator(
                engine,
                allowed_tools=case.fixture.required_tools,
                on_trace=persist_trace,
            ).evaluate(
                run_id=context.run_id,
                case_id=context.case_id,
                iteration_id=context.iteration_id,
                simulator=simulator,
                max_turns=context.max_turns,
                max_input_tokens=context.max_input_tokens,
                max_output_tokens=context.max_output_tokens,
                max_cost_amount=context.max_cost_amount,
                cost_currency=context.cost_currency,
            )
            usage_cost = multi.usage.cost_amount or Decimal(0)
            usage_currency = multi.usage.cost_currency or "CNY"
            resumed = any(
                trace.request.resume_session_id is not None for trace in multi.traces
            )
            timeline = _timeline(multi.traces)
            transcript = _transcript(multi.traces)

            def evaluation(
                status: RunnerStatus,
                artifacts: RunArtifacts,
                *,
                evidence: tuple[Mapping[str, JsonValue], ...] = (),
                diff: Mapping[str, JsonValue] | None = None,
                error: str | None = None,
                stop_reason: str | None = None,
            ) -> CaseEvaluation:
                return CaseEvaluation(
                    case_id=context.case_id,
                    iteration_id=context.iteration_id,
                    status=status,
                    turn_count=len(multi.traces),
                    input_tokens=multi.usage.input_tokens,
                    output_tokens=multi.usage.output_tokens,
                    cost_amount=usage_cost,
                    cost_currency=usage_currency,
                    latency_ms=multi.latency_ms,
                    artifacts=artifacts,
                    session_resumed=resumed,
                    evidence=evidence,
                    tool_timeline=timeline,
                    state_diff={} if diff is None else diff,
                    transcript=transcript,
                    error=error,
                    stop_reason=stop_reason,
                )

            partial_artifacts = RunArtifacts(
                traces=tuple(trace_refs), before_snapshot=before_ref
            )
            if multi.outcome is MultiTurnOutcome.BUDGET_STOP:
                return evaluation(
                    RunnerStatus.BUDGET_STOP,
                    partial_artifacts,
                    stop_reason=multi.stop_reason,
                )
            if multi.outcome is MultiTurnOutcome.SIMULATOR_ERROR:
                return evaluation(
                    RunnerStatus.SIMULATOR_ERROR,
                    partial_artifacts,
                    error=multi.stop_reason,
                )
            if multi.outcome is not MultiTurnOutcome.COMPLETED:
                return evaluation(
                    RunnerStatus.INFRASTRUCTURE_ERROR,
                    partial_artifacts,
                    error=multi.stop_reason or multi.outcome.value,
                )

            try:
                after = environment.snapshot()
                after_ref = _write_record(
                    context.run_dir, f"{attempt_root}/after.json", after
                )
                actual_diff = state_diff(before, after)
                diff_ref = _write_record(
                    context.run_dir, f"{attempt_root}/state-diff.json", actual_diff
                )
            except Exception as exc:
                return evaluation(
                    RunnerStatus.INFRASTRUCTURE_ERROR,
                    partial_artifacts,
                    error=f"snapshot persistence raised {type(exc).__name__}",
                )
            try:
                state_assertions = judge_state(
                    _expected_diff(case), actual_diff, evidence_artifact=diff_ref
                )
                expected_names = tuple(name for name, _ in case.expected_actions)
                final_arguments = case.expected_actions[-1][1]
                rule_assertions = judge_rules_across_traces(
                    multi.traces,
                    (
                        tool_order(expected_names, exact=True),
                        tool_count("process_return", 2),
                        tool_arguments("process_return", final_arguments),
                    ),
                    evidence_artifacts=trace_refs,
                )
                grade = aggregate_case_grade(
                    (*state_assertions, *rule_assertions),
                    run_id=context.run_id,
                    case_id=context.case_id,
                    iteration_id=context.iteration_id,
                )
                grade_ref = _write_record(
                    context.run_dir, f"{attempt_root}/grade.json", grade
                )
            except Exception as exc:
                return evaluation(
                    RunnerStatus.JUDGE_ERROR,
                    RunArtifacts(
                        traces=tuple(trace_refs),
                        before_snapshot=before_ref,
                        after_snapshot=after_ref,
                        state_diff=diff_ref,
                    ),
                    diff=cast(
                        Mapping[str, JsonValue], actual_diff.model_dump(mode="json")
                    ),
                    error=f"judge raised {type(exc).__name__}",
                )
            status = (
                RunnerStatus.PASS
                if grade.status is GradeStatus.PASS
                else RunnerStatus.AGENT_FAIL
                if grade.status is GradeStatus.FAIL
                else RunnerStatus.JUDGE_ERROR
            )
            return evaluation(
                status,
                RunArtifacts(
                    traces=tuple(trace_refs),
                    before_snapshot=before_ref,
                    after_snapshot=after_ref,
                    state_diff=diff_ref,
                    grade=grade_ref,
                ),
                evidence=tuple(
                    cast(
                        Mapping[str, JsonValue],
                        assertion.model_dump(mode="json"),
                    )
                    for assertion in grade.assertions
                ),
                diff=cast(Mapping[str, JsonValue], actual_diff.model_dump(mode="json")),
            )
        finally:
            environment.close()
