"""Run the pinned return case through FakeEngine, Shop MCP, and Judges."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import as_file, files
from pathlib import Path
from types import TracebackType
from typing import cast

from pydantic import JsonValue

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CompletedPayload,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    GradeStatus,
    RecordType,
    SchemaVersion,
    ShopSnapshot,
    StateDiff,
    ToolCallPayload,
    ToolResult,
    ToolResultPayload,
    ToolResultStatus,
    VersionedRecord,
    artifact_json_bytes,
)
from ses.engines.base import Engine
from ses.engines.fake import FakeEngine, FakeFixture, load_fake_fixture
from ses.evaluation import (
    aggregate_case_grade,
    build_trace,
    expect,
    judge_rules,
    judge_state,
    tool_arguments,
    tool_count,
    tool_order,
)
from ses.foundation.workspace import WorkspaceFactory
from ses.reporting import build_l1_result, l1_json_bytes
from ses.shop import CASE_DEFINITION, PINNED_CASE_FIXTURE, CaseEnvironment, state_diff
from ses.skills.installer import SkillInstallation, install_skill, load_skill_manifest

_RUN_ID_PATTERN = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ITERATION_ID = "iteration-0"
_EXPECTED_TOOL_ORDER = (
    "get_order",
    "get_policies",
    "process_return",
    "process_return",
)
_EXPECTED_ACTIONS: tuple[tuple[str, Mapping[str, JsonValue]], ...] = (
    ("get_order", {"order_id": "ORD-6006"}),
    ("get_policies", {"topic": "return"}),
    ("process_return", {"item_id": "ITEM-9050", "reason": "defective"}),
    (
        "process_return",
        {
            "item_id": "ITEM-9050",
            "reason": "defective",
            "confirm": True,
            "amount_minor": 129_900,
        },
    ),
)


class RunOutcome(StrEnum):
    """Top-level outcomes kept distinct in CLI and report data."""

    PASS = "pass"
    EXPECT_FAIL = "expect_fail"
    AGENT_FAIL = "agent_fail"
    JUDGE_ERROR = "judge_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BUDGET_STOP = "budget_stop"


class SingleCaseRunError(RuntimeError):
    """A single-case run stopped before it could emit a complete L1 result."""

    def __init__(self, outcome: RunOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class SingleCaseRun:
    """Internal paths plus stable public identity for a completed invocation."""

    run_id: str
    case_id: str
    outcome: RunOutcome
    run_dir: Path
    result_path: Path


def classify_run_outcome(
    *,
    preflight_passed: bool,
    exit_status: EngineExitStatus | None,
    grade_status: GradeStatus | None,
    infrastructure_error: bool = False,
) -> RunOutcome:
    """Apply one explicit precedence table to run-level failure categories."""
    if not preflight_passed:
        return RunOutcome.EXPECT_FAIL
    if infrastructure_error:
        return RunOutcome.INFRASTRUCTURE_ERROR
    if exit_status is EngineExitStatus.BUDGET_STOP:
        return RunOutcome.BUDGET_STOP
    if exit_status is not EngineExitStatus.SUCCESS:
        return RunOutcome.INFRASTRUCTURE_ERROR
    if grade_status is GradeStatus.FAIL:
        return RunOutcome.AGENT_FAIL
    if grade_status in {GradeStatus.ERROR, GradeStatus.NOT_EVALUATED}:
        return RunOutcome.JUDGE_ERROR
    if grade_status is GradeStatus.PASS:
        return RunOutcome.PASS
    return RunOutcome.JUDGE_ERROR


class _ShopMCPClient:
    """Narrow JSON-RPC client used only by this offline integration path."""

    def __init__(self, *, workspace: Path, artifact_root: Path) -> None:
        child_environment = {
            name: os.environ[name]
            for name in ("LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR")
            if name in os.environ
        }
        child_environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        self._process: subprocess.Popen[str] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ses.shop.mcp_server",
                "--artifact-root",
                str(artifact_root),
            ],
            cwd=workspace,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if (
            self._process.stdin is None
            or self._process.stdout is None
            or self._process.stderr is None
        ):
            self._process.kill()
            raise RuntimeError("Shop MCP stdio pipes are unavailable")
        self._stdin = self._process.stdin
        self._stdout = self._process.stdout
        self._stderr = self._process.stderr
        self._request_id = 0

    def __enter__(self) -> _ShopMCPClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        self.close(check=exc_type is None)

    def _request(
        self, method: str, params: Mapping[str, object] | None = None
    ) -> Mapping[str, object]:
        if self._process.poll() is not None:
            raise RuntimeError("Shop MCP exited before handling the request")
        self._request_id += 1
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        self._stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self._stdin.flush()
        line = self._stdout.readline()
        if not line:
            raise RuntimeError("Shop MCP closed stdout without a response")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Shop MCP returned malformed JSON") from exc
        if not isinstance(response, Mapping):
            raise RuntimeError("Shop MCP response must be an object")
        if response.get("id") != self._request_id:
            raise RuntimeError("Shop MCP response ID does not match the request")
        if "error" in response:
            raise RuntimeError("Shop MCP returned a JSON-RPC error")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Shop MCP response is missing its result object")
        return cast(Mapping[str, object], result)

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ses-evaluator", "version": "0.1.0"},
            },
        )

    def available_tools(self) -> tuple[str, ...]:
        result = self._request("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("Shop MCP tools/list did not return a list")
        names: list[str] = []
        for tool in tools:
            if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
                raise RuntimeError("Shop MCP returned an invalid tool schema")
            names.append(cast(str, tool["name"]))
        return tuple(names)

    def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        result = self._request(
            "tools/call", {"name": tool_name, "arguments": dict(arguments)}
        )
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise RuntimeError("Shop MCP tool result lacks structuredContent")
        return ToolResult.model_validate(structured)

    def close(self, *, check: bool) -> None:
        if self._process.poll() is None:
            try:
                self._stdin.close()
            except OSError:
                pass
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
                if check:
                    raise RuntimeError(
                        "Shop MCP did not stop within the timeout"
                    ) from None
        if check and self._process.returncode != 0:
            detail = self._stderr.read().strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"Shop MCP exited with code {self._process.returncode}{suffix}"
            )


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{uuid.uuid4().hex[:10]}"


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must be a safe run-prefixed identifier")
    return run_id


def _load_default_fixture() -> FakeFixture:
    resource = files("ses.evaluator").joinpath("fixtures/pinned_return_success.json")
    with as_file(resource) as path:
        return load_fake_fixture(path)


async def _run_engine_with_mcp(
    *,
    engine: Engine,
    request: EngineRequest,
    mcp: _ShopMCPClient,
) -> tuple[EngineEvent, ...]:
    events: list[EngineEvent] = []
    pending: dict[str, ToolResultPayload] = {}
    async for event in engine.stream(request):
        payload = event.payload
        if isinstance(payload, ToolCallPayload):
            if payload.tool_call_id in pending:
                raise RuntimeError("FakeEngine repeated a pending tool_call_id")
            tool_result = mcp.call_tool(payload.tool_name, payload.arguments)
            pending[payload.tool_call_id] = ToolResultPayload(
                tool_call_id=payload.tool_call_id,
                content=cast(
                    JsonValue,
                    tool_result.model_dump(mode="json", round_trip=True),
                ),
                is_error=tool_result.status is ToolResultStatus.ERROR,
            )
        elif isinstance(payload, ToolResultPayload):
            actual = pending.pop(payload.tool_call_id, None)
            if actual is None:
                raise RuntimeError("FakeEngine emitted a tool result without a call")
            event = event.model_copy(update={"payload": actual})
        elif isinstance(payload, CompletedPayload) and pending:
            raise RuntimeError("FakeEngine completed with unresolved tool calls")
        events.append(event)
    return tuple(events)


def _expected_diff() -> StateDiff:
    environment = CaseEnvironment()
    try:
        before = environment.snapshot()
        for tool_name, arguments in _EXPECTED_ACTIONS:
            result = environment.execute(tool_name, arguments)
            if result.status is not ToolResultStatus.SUCCESS:
                raise RuntimeError("the pinned expected action sequence is invalid")
        after = environment.snapshot()
        return state_diff(before, after)
    finally:
        environment.close()


def _artifact_ref(run_dir: Path, path: Path) -> ArtifactRef:
    relative = path.resolve().relative_to(run_dir.resolve()).as_posix()
    payload = path.read_bytes()
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=relative,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _write_record(run_dir: Path, relative: str, record: VersionedRecord) -> ArtifactRef:
    payload = artifact_json_bytes(record)
    destination = run_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return _artifact_ref(run_dir, destination)


def _run_pinned_case(
    *,
    output_root: Path,
    run_id: str,
    fixture: FakeFixture | None,
    engine_factory: Callable[[Path], Engine] | None = None,
    skill_source: Path | None = None,
    skill_version: str | None = None,
    skill_sha256: str | None = None,
) -> SingleCaseRun:
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = WorkspaceFactory(run_dir / "workspaces").create(
        run_id=run_id,
        case_id=CASE_DEFINITION.case_id,
        iteration_id=_ITERATION_ID,
    )
    installation: SkillInstallation | None = None
    if skill_source is not None:
        manifest = load_skill_manifest(skill_source)
        installation = install_skill(
            skill_source,
            workspace.root / ".claude" / "skills" / manifest.name,
            version=skill_version,
        )
        if skill_sha256 is not None and skill_sha256 != installation.sha256:
            raise ValueError("candidate Skill hash changed before installation")
    elif skill_version is not None or skill_sha256 is not None:
        raise ValueError("Skill metadata requires a Skill source")
    request = EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id=f"{run_id}:request",
        prompt=CASE_DEFINITION.user_prompt,
        allowed_tools=CASE_DEFINITION.required_tools,
        timeout_seconds=30,
    )
    engine = (
        engine_factory(workspace.root)
        if engine_factory is not None
        else FakeEngine(fixture or _load_default_fixture())
    )

    with _ShopMCPClient(workspace=workspace.root, artifact_root=run_dir) as mcp:
        mcp.initialize()
        available_tools = mcp.available_tools()
        preflight = expect(
            CASE_DEFINITION,
            PINNED_CASE_FIXTURE.model_dump(mode="json", round_trip=True),
            fixture_id=CASE_DEFINITION.fixture_id,
            available_tools=available_tools,
            environment_ready=True,
            environment_closed=False,
            budget={"max_total_tokens": 5_000},
        )
        if not preflight.passed:
            detail = "; ".join(failure.message for failure in preflight.failures)
            raise SingleCaseRunError(RunOutcome.EXPECT_FAIL, detail)
        events = asyncio.run(
            _run_engine_with_mcp(
                engine=engine,
                request=request,
                mcp=mcp,
            )
        )

    before_path = run_dir / "shop" / "before.json"
    after_path = run_dir / "shop" / "after.json"
    before = ShopSnapshot.model_validate_json(before_path.read_bytes())
    after = ShopSnapshot.model_validate_json(after_path.read_bytes())
    actual_diff = state_diff(before, after)
    trace = build_trace(
        events,
        request=request,
        run_id=run_id,
        case_id=CASE_DEFINITION.case_id,
        iteration_id=_ITERATION_ID,
        skill_version=None if installation is None else installation.version,
        skill_sha256=None if installation is None else installation.sha256,
    )

    before_ref = _artifact_ref(run_dir, before_path)
    after_ref = _artifact_ref(run_dir, after_path)
    trace_ref = _write_record(run_dir, "trace.json", trace)
    diff_ref = _write_record(run_dir, "state-diff.json", actual_diff)
    state_assertions = judge_state(
        _expected_diff(),
        actual_diff,
        evidence_artifact=diff_ref,
    )
    rule_assertions = judge_rules(
        trace,
        (
            tool_order(_EXPECTED_TOOL_ORDER, exact=True),
            tool_count("process_return", 2),
            tool_arguments(
                "process_return",
                {
                    "item_id": "ITEM-9050",
                    "reason": "defective",
                    "confirm": True,
                    "amount_minor": 129_900,
                },
            ),
        ),
        evidence_artifact=trace_ref,
    )
    grade = aggregate_case_grade(
        (*state_assertions, *rule_assertions),
        run_id=run_id,
        case_id=CASE_DEFINITION.case_id,
        iteration_id=_ITERATION_ID,
    )
    grade_ref = _write_record(run_dir, "grade.json", grade)
    outcome = classify_run_outcome(
        preflight_passed=True,
        exit_status=trace.exit_status,
        grade_status=grade.status,
    )
    result = build_l1_result(
        trace=trace,
        state_diff=actual_diff,
        grade=grade,
        outcome=outcome.value,
        artifacts={
            "before_snapshot": before_ref,
            "after_snapshot": after_ref,
            "trace": trace_ref,
            "state_diff": diff_ref,
            "grade": grade_ref,
        },
    )
    result_path = run_dir / "result.json"
    with result_path.open("xb") as stream:
        stream.write(l1_json_bytes(result))
        stream.flush()
        os.fsync(stream.fileno())
    return SingleCaseRun(
        run_id=run_id,
        case_id=CASE_DEFINITION.case_id,
        outcome=outcome,
        run_dir=run_dir,
        result_path=result_path,
    )


def run_pinned_case(
    output_root: Path,
    *,
    run_id: str | None = None,
    fixture: FakeFixture | None = None,
    engine_factory: Callable[[Path], Engine] | None = None,
    skill_source: Path | None = None,
    skill_version: str | None = None,
    skill_sha256: str | None = None,
) -> SingleCaseRun:
    """Execute one fresh, offline, pinned case and persist immutable evidence.

    A Skill source is optional. When supplied, the evaluator installs only its
    allowlisted runtime files into this run's new workspace and records the
    installed semantic identity in the Trace.
    """
    if fixture is not None and engine_factory is not None:
        raise ValueError("fixture and engine_factory are mutually exclusive")
    selected_run_id = _validate_run_id(run_id or _new_run_id())
    try:
        return _run_pinned_case(
            output_root=output_root,
            run_id=selected_run_id,
            fixture=fixture,
            engine_factory=engine_factory,
            skill_source=skill_source,
            skill_version=skill_version,
            skill_sha256=skill_sha256,
        )
    except SingleCaseRunError:
        raise
    except Exception as exc:
        raise SingleCaseRunError(
            RunOutcome.INFRASTRUCTURE_ERROR,
            str(exc) or type(exc).__name__,
        ) from exc
