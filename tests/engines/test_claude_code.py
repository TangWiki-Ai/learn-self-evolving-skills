from __future__ import annotations

import asyncio
import os
import signal
import stat
from pathlib import Path

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventKind,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    RecordType,
    SchemaVersion,
)
from ses.engines.claude_code import ClaudeCodeEngine
from ses.foundation.config import LockedModel
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


def _engine(tmp_path: Path, executable: str) -> ClaudeCodeEngine:
    workspace = WorkspaceFactory(tmp_path / "workspaces").create(
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-1",
        mcp_servers={"shop": {"command": "python", "args": ["server.py"], "env": {}}},
    )
    return ClaudeCodeEngine(
        model=LockedModel(
            model_id="deepseek-ai/DeepSeek-V3.2",
            base_url="https://api.siliconflow.cn/",
        ),
        credentials=ProviderCredentials(api_key="exact-process-secret"),
        workspace=workspace,
        executable=executable,
        environ={"PATH": os.environ.get("PATH", "")},
        system_prompt="Use only the allowed shop tools.",
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
    assert command[-1] == "Handle the return."
    assert "exact-process-secret" not in "\0".join(command)
    assert environment["ANTHROPIC_API_KEY"] == "exact-process-secret"
    assert environment["CLAUDE_CONFIG_DIR"].endswith(".claude-isolated")


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
sys.stderr.write("Authorization: Bearer exact-process-secret")
raise SystemExit(9)
""",
    )

    events = asyncio.run(_collect(_engine(tmp_path, str(executable)), _request()))

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == "process_exit"
    assert "exact-process-secret" not in events[-2].payload.message
    assert events[-1].payload.exit_status is EngineExitStatus.ERROR


def test_process_start_failure_is_canonical(tmp_path: Path) -> None:
    events = asyncio.run(
        _collect(_engine(tmp_path, str(tmp_path / "missing-claude")), _request())
    )

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == "process_start"
    assert events[-1].payload.exit_status is EngineExitStatus.ERROR
