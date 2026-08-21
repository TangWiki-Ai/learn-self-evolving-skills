from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
from pathlib import Path

import pytest

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventKind,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    RecordType,
    SchemaVersion,
    UsagePayload,
)
from ses.engines.claude_code import ClaudeCodeEngine
from ses.foundation.config import LockedModel, ProviderId
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import WorkspaceFactory


def _request(*, timeout: float = 2, resume: str | None = None) -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Handle the return.",
        resume_session_id=resume,
        allowed_tools=("get_order", "return_item"),
        timeout_seconds=timeout,
    )


def _engine(
    tmp_path: Path,
    executable: str,
    *,
    environ: dict[str, str] | None = None,
    output_json_schema: dict[str, object] | None = None,
    provider: ProviderId = ProviderId.SILICONFLOW,
) -> ClaudeCodeEngine:
    workspace = WorkspaceFactory(tmp_path / "workspaces").create(
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-1",
        mcp_servers={"shop": {"command": "python", "args": ["server.py"], "env": {}}},
    )
    return ClaudeCodeEngine(
        model=LockedModel(
            model_id=(
                "claude-sonnet-4-6"
                if provider is ProviderId.CHATANYWHERE
                else "deepseek-ai/DeepSeek-V3.2"
            ),
            base_url=(
                "https://api.chatanywhere.tech/"
                if provider is ProviderId.CHATANYWHERE
                else "https://api.siliconflow.cn/"
            ),
        ),
        credentials=ProviderCredentials(
            api_key="exact-process-secret", provider=provider
        ),
        workspace=workspace,
        executable=executable,
        environ=environ or {"PATH": os.environ.get("PATH", "")},
        system_prompt="Use only the allowed shop tools.",
        output_json_schema=output_json_schema,
    )


def _write_executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


async def _collect(
    engine: ClaudeCodeEngine, request: EngineRequest
) -> list[EngineEvent]:
    return [event async for event in engine.stream(request)]


def test_command_is_an_array_with_bare_stream_json_resume_and_no_key(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "/usr/bin/claude")

    command = engine.build_command(_request(resume="session-previous"))
    environment = engine.build_environment()

    assert command[:5] == [
        "/usr/bin/claude",
        "--bare",
        "-p",
        "--output-format",
        "stream-json",
    ]
    assert command[command.index("--resume") + 1] == "session-previous"
    assert command[command.index("--allowedTools") + 1] == "get_order,return_item"
    disallowed = command[command.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "Read", "Write", "Edit", "Glob", "Grep"} <= set(disallowed)
    assert command[-1] == "Handle the return."
    assert "exact-process-secret" not in "\0".join(command)
    assert environment["ANTHROPIC_API_KEY"] == "exact-process-secret"
    assert environment["CLAUDE_CONFIG_DIR"].endswith("claude-config")
    assert environment["HOME"] == str(engine._workspace.cleanup_root)
    assert environment["HOME"] != str(Path.home())


def test_command_can_enable_full_native_skill_discovery(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "/usr/bin/claude")
    engine = ClaudeCodeEngine(
        model=engine.model,
        credentials=ProviderCredentials(api_key="exact-process-secret"),
        workspace=engine.workspace,
        executable="/usr/bin/claude",
        environ={"PATH": os.environ.get("PATH", "")},
        native_skill_discovery=True,
    )

    command = engine.build_command(_request())

    assert "--bare" not in command
    assert command[:4] == [
        "/usr/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
    ]


def test_command_uses_native_json_schema_and_disables_other_tools(
    tmp_path: Path,
) -> None:
    schema = {
        "type": "object",
        "properties": {"confidence": {"type": "number"}},
        "required": ["confidence"],
        "additionalProperties": False,
    }
    engine = _engine(
        tmp_path,
        "/usr/bin/claude",
        output_json_schema=schema,
    )
    request = _request().model_copy(update={"allowed_tools": ()})

    command = engine.build_command(request)

    assert command[command.index("--tools") + 1] == ""
    encoded = command[command.index("--json-schema") + 1]
    assert json.loads(encoded) == schema
    assert command[-1] == "Handle the return."


def test_structured_output_cannot_enable_case_tools(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        "/usr/bin/claude",
        output_json_schema={"type": "object"},
    )

    with pytest.raises(ValueError, match="cannot enable case tools"):
        engine.build_command(_request())


def test_subprocess_stream_is_normalized_and_secret_is_redacted(tmp_path: Path) -> None:
    executable = tmp_path / "fake-claude"
    _write_executable(
        executable,
        """
import json
events = [
    {"type":"system","subtype":"init","session_id":"session-1"},
    {"type":"assistant","message":{"id":"message-1","content":[
        {"type":"text","text":"done"},
        {"type":"tool_use","id":"tool-1","name":"get_order","input":{"note":"exact-process-secret"}}
    ]}},
    {"type":"user","message":{"content":[
        {"type":"tool_result","tool_use_id":"tool-1","content":{"status":"ok"},"is_error":False}
    ]}},
    {"type":"result","subtype":"success","is_error":False,"session_id":"session-1",
     "usage":{"input_tokens":3,"output_tokens":2},"total_cost_usd":0.0001}
]
for event in events:
    print(json.dumps(event), flush=True)
""",
    )

    events = asyncio.run(_collect(_engine(tmp_path, str(executable)), _request()))

    assert [event.sequence for event in events] == list(range(len(events)))
    assert events[-1].payload.kind is EngineEventKind.COMPLETED
    assert events[-1].payload.exit_status is EngineExitStatus.SUCCESS
    assert "exact-process-secret" not in "\n".join(
        event.model_dump_json() for event in events
    )


def test_chatanywhere_keeps_tokens_but_discards_claude_cost_estimate(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-chatanywhere-claude"
    _write_executable(
        executable,
        """
import json
print(json.dumps({
    "type":"result",
    "subtype":"success",
    "is_error":False,
    "session_id":"session-1",
    "usage":{"input_tokens":17,"output_tokens":5},
    "total_cost_usd":0.42
}), flush=True)
""",
    )
    engine = _engine(
        tmp_path,
        str(executable),
        provider=ProviderId.CHATANYWHERE,
    )

    events = asyncio.run(_collect(engine, _request()))
    usage_payload = next(
        event.payload for event in events if isinstance(event.payload, UsagePayload)
    )
    environment = engine.build_environment()

    assert usage_payload.usage.input_tokens == 17
    assert usage_payload.usage.output_tokens == 5
    assert usage_payload.usage.cost_amount is None
    assert usage_payload.usage.cost_currency is None
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "exact-process-secret"
    assert "ANTHROPIC_API_KEY" not in environment


def test_timeout_kills_the_subprocess_group(tmp_path: Path) -> None:
    executable = tmp_path / "slow-claude"
    _write_executable(
        executable,
        """
import os
import time
from pathlib import Path
Path("process.pid").write_text(str(os.getpid()))
time.sleep(30)
""",
    )
    engine = _engine(tmp_path, str(executable))

    events = asyncio.run(_collect(engine, _request(timeout=0.5)))
    pid = int((engine._workspace.root / "process.pid").read_text())

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == "timeout"
    assert events[-1].payload.exit_status is EngineExitStatus.TIMEOUT
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(pid, signal.SIGKILL)
        raise AssertionError("timed-out Claude process still exists")


def test_explicit_cancel_emits_cancelled_and_cleans_process(tmp_path: Path) -> None:
    executable = tmp_path / "cancel-claude"
    _write_executable(
        executable,
        """
import json
import time
print(json.dumps({"type":"system","subtype":"init","session_id":"session-1"}), flush=True)
time.sleep(30)
""",
    )
    engine = _engine(tmp_path, str(executable))

    async def scenario() -> list[EngineEvent]:
        task = asyncio.create_task(_collect(engine, _request()))
        for _ in range(100):
            if await engine.cancel("request-1"):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("engine process did not start")
        return await task

    events = asyncio.run(scenario())

    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-1].payload.exit_status is EngineExitStatus.CANCELLED
    assert not engine._running


def test_nonzero_exit_is_redacted_and_canonical(tmp_path: Path) -> None:
    executable = tmp_path / "failed-claude"
    _write_executable(
        executable,
        """
import sys
sys.stderr.write("Authorization: Bearer exact-process-secret ordinary-github-secret")
raise SystemExit(9)
""",
    )

    events = asyncio.run(
        _collect(
            _engine(
                tmp_path,
                str(executable),
                environ={
                    "PATH": os.environ.get("PATH", ""),
                    "GITHUB_TOKEN": "ordinary-github-secret",
                },
            ),
            _request(),
        )
    )

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == "process_exit"
    assert "exact-process-secret" not in events[-2].payload.message
    assert "ordinary-github-secret" not in events[-2].payload.message
    assert events[-1].payload.exit_status is EngineExitStatus.ERROR


def test_process_start_failure_is_canonical(tmp_path: Path) -> None:
    events = asyncio.run(
        _collect(_engine(tmp_path, str(tmp_path / "missing-claude")), _request())
    )

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == "process_start"
    assert events[-1].payload.exit_status is EngineExitStatus.ERROR


def test_filesystem_tool_request_is_rejected_as_canonical_error(tmp_path: Path) -> None:
    request = _request().model_copy(update={"allowed_tools": ("Read",)})

    events = asyncio.run(
        _collect(_engine(tmp_path, str(tmp_path / "unused-claude")), request)
    )

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == "unsafe_request"
    assert events[-1].payload.exit_status is EngineExitStatus.ERROR


def test_success_result_followed_by_nonzero_exit_emits_one_failed_completion(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "result-then-fail"
    _write_executable(
        executable,
        """
import json
print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":"session-1"}), flush=True)
raise SystemExit(7)
""",
    )

    events = asyncio.run(_collect(_engine(tmp_path, str(executable)), _request()))
    completed = [
        event.payload for event in events if isinstance(event.payload, CompletedPayload)
    ]

    assert len(completed) == 1
    assert completed[0].exit_status is EngineExitStatus.ERROR
    assert any(
        isinstance(event.payload, ErrorPayload)
        and event.payload.error_code == "process_exit"
        for event in events
    )


def test_success_result_followed_by_hang_emits_one_timeout_completion(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "result-then-hang"
    _write_executable(
        executable,
        """
import json
import time
print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":"session-1"}), flush=True)
time.sleep(30)
""",
    )

    events = asyncio.run(
        _collect(_engine(tmp_path, str(executable)), _request(timeout=0.3))
    )
    completed = [
        event.payload for event in events if isinstance(event.payload, CompletedPayload)
    ]

    assert len(completed) == 1
    assert completed[0].exit_status is EngineExitStatus.TIMEOUT


def test_cleanup_kills_mcp_child_after_claude_parent_exits(tmp_path: Path) -> None:
    executable = tmp_path / "orphaning-claude"
    _write_executable(
        executable,
        """
import json
import subprocess
import sys
from pathlib import Path
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path("mcp-child.pid").write_text(str(child.pid))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":"session-1"}), flush=True)
""",
    )
    engine = _engine(tmp_path, str(executable))

    events = asyncio.run(_collect(engine, _request()))
    child_pid = int((engine._workspace.root / "mcp-child.pid").read_text())

    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-1].payload.exit_status is EngineExitStatus.SUCCESS
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(child_pid, signal.SIGKILL)
        raise AssertionError("MCP child survived Claude process cleanup")
