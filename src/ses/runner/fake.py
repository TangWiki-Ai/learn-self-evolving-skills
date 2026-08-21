"""Offline develop-catalog evaluator wired through the complete L1 pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CompletedPayload,
    EngineEvent,
    EngineRequest,
    GradeStatus,
    ShopSnapshot,
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
from ses.engines.claude_code import ClaudeCodeEngine
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
from ses.foundation.config import LockedModel, ProviderId
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import WorkspaceFactory
from ses.runner.baseline import CaseEvaluation, EvaluationContext
from ses.shop import CaseEnvironment, ReturnCaseFixture, state_diff
from ses.simulation import FakeSimulator, UserIntent

_FIXED_TRACE_TIME = datetime(2026, 8, 17, tzinfo=UTC)
DevelopCatalogMode = Literal["fixed", "live"]


@dataclass(frozen=True, slots=True)
class ExecutableDevelopCase:
    """Private execution inputs paired with one public case definition."""

    fixture: ReturnCaseFixture
    expected_actions: tuple[tuple[str, Mapping[str, JsonValue]], ...]
    qualification_hash: str
    manifest_data_version: str


@dataclass(frozen=True, slots=True)
class LiveDevelopConfig:
    """Locked Claude runtime used by both sides of a live paired execution."""

    model: LockedModel
    credentials: ProviderCredentials
    executable: str
    environ: Mapping[str, str]
    timeout_seconds: float = 300
    provider: ProviderId = ProviderId.SILICONFLOW
    model_lock_sha256: str | None = None
    cost_currency: str = "USD"

    def __post_init__(self) -> None:
        if self.credentials.provider is not self.provider:
            raise ValueError("live provider differs from its credentials")
        if self.model_lock_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.model_lock_sha256
        ):
            raise ValueError("live model lock hash is invalid")
        if not self.cost_currency.strip():
            raise ValueError("live cost currency must not be blank")


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
    *,
    mode: DevelopCatalogMode = "fixed",
) -> Mapping[str, ExecutableDevelopCase]:
    """Load the catalog while enforcing its fixed/live review boundary."""

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
    review_status = payload.get("review_status")
    intended_use = payload.get("intended_use")
    approved = (
        review_status == "human_approved" and intended_use == "fixed_and_live_journey"
    )
    if mode == "live" and (not approved):
        raise ValueError(
            "live develop evaluation requires the signed human review packet and "
            "an approved catalog manifest"
        )
    if mode == "fixed" and (
        (review_status, intended_use)
        not in {
            (
                "course_authored_pending_human_review",
                "fixed_offline_course_only",
            ),
            ("human_approved", "fixed_and_live_journey"),
        }
    ):
        raise ValueError("fixed develop manifest review boundary is invalid")
    if approved and (
        re.fullmatch(r"[0-9a-f]{40}", str(payload.get("review_commit"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("asset_review_sha256")))
        is None
    ):
        raise ValueError("approved develop manifest has no valid asset review binding")
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
        raise ValueError("fixed develop course catalog must contain at least 15 cases")
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


def _action_fixture(
    case: ExecutableDevelopCase,
    *,
    input_token_overhead: int = 0,
    cost_amount: Decimal = Decimal("0.0010"),
) -> FakeFixture:
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
                payload=UsagePayload(
                    usage=Usage(
                        input_tokens=137 + input_token_overhead,
                        output_tokens=61,
                        cost_amount=cost_amount,
                        cost_currency="CNY",
                    )
                )
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
        self,
        catalog: Mapping[str, ExecutableDevelopCase] | None = None,
        *,
        skill_files: tuple[tuple[Path, str], ...] = (),
        input_token_overhead: int = 0,
        cost_amount: Decimal = Decimal("0.0010"),
        latency_overhead_ms: int = 0,
        fixed_latency_ms: int | None = None,
        live_config: LiveDevelopConfig | None = None,
    ) -> None:
        self._catalog = dict(catalog or load_develop_catalog())
        self._skill_files = skill_files
        self._input_token_overhead = input_token_overhead
        self._cost_amount = cost_amount
        self._latency_overhead_ms = latency_overhead_ms
        self._fixed_latency_ms = fixed_latency_ms
        self._live_config = live_config

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
        fixture_source = context.run_dir / attempt_root / "case-fixture.json"
        fixture_source.parent.mkdir(parents=True, exist_ok=True)
        fixture_source.write_text(
            json.dumps(
                case.fixture.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        workspace_factory = WorkspaceFactory(context.run_dir / "workspaces")
        workspace = workspace_factory.create(
            run_id=context.run_id,
            case_id=context.case_id,
            iteration_id=f"{context.iteration_id}:{context.attempt_id}",
            files=((fixture_source, "case-fixture.json"),),
            skill_files=self._skill_files,
        )
        environment = (
            None if self._live_config is not None else CaseEnvironment(case.fixture)
        )
        trace_refs: list[ArtifactRef] = []

        def persist_trace(trace: Trace) -> None:
            if self._fixed_latency_ms is not None:
                turn_offset = timedelta(seconds=len(trace_refs))
                trace = trace.model_copy(
                    update={
                        "events": tuple(
                            event.model_copy(
                                update={
                                    "occurred_at": _FIXED_TRACE_TIME
                                    + turn_offset
                                    + timedelta(microseconds=event.sequence)
                                }
                            )
                            for event in trace.events
                        )
                    }
                )
            trace_refs.append(
                _write_record(
                    context.run_dir,
                    f"{attempt_root}/trace-turn-{len(trace_refs)}.json",
                    trace,
                )
            )

        try:
            before = environment.snapshot() if environment is not None else None
            before_ref = (
                _write_record(context.run_dir, f"{attempt_root}/before.json", before)
                if before is not None
                else None
            )
            simulator = FakeSimulator(
                UserIntent(
                    want=case.fixture.user_prompt,
                    allowed_facts={"item_id": case.fixture.item.item_id},
                )
            )
            if self._live_config is None:
                assert environment is not None
                engine: Engine = _ShopBoundEngine(
                    _SequencedFakeEngine(
                        _action_fixture(
                            case,
                            input_token_overhead=self._input_token_overhead,
                            cost_amount=self._cost_amount,
                        )
                    ),
                    environment,
                )
                allowed_tools = case.fixture.required_tools
                timeout_seconds: float = 30
            else:
                shop_artifacts = workspace.root / "shop-artifacts"
                workspace = workspace_factory.configure_mcp(
                    workspace,
                    {
                        "shop": {
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "ses.shop.mcp_server",
                                "--fixture",
                                str(workspace.root / "case-fixture.json"),
                                "--artifact-root",
                                str(shop_artifacts),
                            ],
                            "env": {},
                        }
                    },
                )
                engine = ClaudeCodeEngine(
                    model=self._live_config.model,
                    credentials=self._live_config.credentials,
                    workspace=workspace,
                    executable=self._live_config.executable,
                    environ=self._live_config.environ,
                    system_prompt=(
                        "Use native Skill discovery when an installed Skill applies. "
                        "Resolve the user's return request using only the allowed Skill "
                        "and shop MCP tools. Inspect facts and policy, preview any "
                        "mutation, then confirm only when the user's request authorizes it."
                    ),
                    native_skill_discovery=True,
                )
                allowed_tools = (
                    "Skill",
                    *(f"mcp__shop__{name}" for name in case.fixture.required_tools),
                )
                timeout_seconds = self._live_config.timeout_seconds
            multi = await MultiTurnEvaluator(
                engine,
                allowed_tools=allowed_tools,
                timeout_seconds=timeout_seconds,
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
            cost_complete = multi.usage.cost_amount is not None
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
                    cost_complete=cost_complete,
                    latency_ms=(
                        self._fixed_latency_ms
                        if self._fixed_latency_ms is not None
                        else multi.latency_ms + self._latency_overhead_ms
                    ),
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
                if environment is not None:
                    assert before is not None
                    after = environment.snapshot()
                else:
                    before = ShopSnapshot.model_validate_json(
                        (workspace.root / "shop-artifacts/shop/before.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    after = ShopSnapshot.model_validate_json(
                        (workspace.root / "shop-artifacts/shop/after.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    before_ref = _write_record(
                        context.run_dir, f"{attempt_root}/before.json", before
                    )
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
                tool_prefix = "mcp__shop__" if self._live_config is not None else ""
                expected_names = tuple(
                    f"{tool_prefix}{name}" for name, _ in case.expected_actions
                )
                final_arguments = case.expected_actions[-1][1]
                rule_assertions = judge_rules_across_traces(
                    multi.traces,
                    (
                        tool_order(expected_names, exact=True),
                        tool_count(f"{tool_prefix}process_return", 2),
                        tool_arguments(f"{tool_prefix}process_return", final_arguments),
                    ),
                    evidence_artifacts=trace_refs,
                    ignored_tool_names=(
                        frozenset({"Skill"})
                        if self._live_config is not None
                        else frozenset()
                    ),
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
            if environment is not None:
                environment.close()
