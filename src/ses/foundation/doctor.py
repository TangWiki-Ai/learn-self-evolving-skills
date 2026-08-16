"""Shared implementation for ``ses doctor`` and the Phase 0 script."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventKind,
    EngineExitStatus,
    EngineRequest,
    RecordType,
    SchemaVersion,
    ToolCallPayload,
    ToolResultPayload,
)
from ses.engines.claude_code import ClaudeCodeEngine
from ses.foundation.config import (
    LockedModel,
    ModelLock,
    ModelRole,
    RuntimeConfig,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import (
    ProviderCredentials,
    build_claude_environment,
    credential_values,
    read_siliconflow_credentials,
)
from ses.foundation.credentials import redact as redact
from ses.foundation.workspace import WorkspaceFactory

STATE_COMMIT = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
ABCD_COMMIT = "6b8700ce67c6b37b062dd7a60abc76d7ef832a97"
TAU2_COMMIT = "c3398666e6559e3a063da3fc04b5acf7f941464e"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2"
MCP_SERVER_NAME = "phase0"
MCP_TOOL_NAME = f"mcp__{MCP_SERVER_NAME}__phase0_ping"
PING_VALUE = "ses-phase0"
PING_RESULT = f"pong:{PING_VALUE}"


class SmokeError(RuntimeError):
    """A user-actionable doctor check failure."""


@dataclass(frozen=True)
class CheckResult:
    status: str
    name: str
    detail: str


@dataclass(frozen=True)
class StreamEvidence:
    event_count: int
    has_model_response: bool
    has_tool_call: bool
    has_tool_result: bool
    has_success_result: bool


def check_python() -> str:
    if sys.version_info < (3, 11):  # noqa: UP036 - runtime diagnostic
        raise SmokeError(f"需要 Python 3.11+, 当前是 {sys.version.split()[0]}。")
    return f"Python {sys.version.split()[0]}"


def check_claude(executable: str = "claude") -> str:
    resolved = shutil.which(executable)
    if resolved is None:
        raise SmokeError("找不到 claude。请先安装 Claude Code CLI。")
    try:
        completed = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeError("claude --version 超时。请检查 CLI 安装。") from exc
    if completed.returncode != 0:
        raise SmokeError("claude --version 执行失败。请运行 claude doctor。")
    version = (completed.stdout or completed.stderr).strip().splitlines()
    return f"{resolved} ({version[0] if version else 'version unknown'})"


def check_claude_isolation(
    environ: Mapping[str, str] | None = None,
) -> CheckResult:
    """Report shell provider state without reading personal Claude files."""
    source = os.environ if environ is None else environ
    base_url = source.get("ANTHROPIC_BASE_URL")
    if not base_url:
        detail = "不读取个人 settings、Skill 或 memory; 运行使用 --bare 和临时配置。"
        return CheckResult("PASS", "Claude isolation", detail)
    host = urlparse(base_url).hostname or "invalid-host"
    detail = f"忽略 shell Provider {host}; 运行使用 --bare 和临时 CLAUDE_CONFIG_DIR。"
    return CheckResult("WARN", "Claude isolation", detail)


def _run_check(name: str, check: Any, *, secrets: Sequence[str] = ()) -> CheckResult:
    try:
        return CheckResult("PASS", name, str(check()))
    except SmokeError as exc:
        return CheckResult("FAIL", name, redact(str(exc), secrets))
    except Exception as exc:  # Keep user diagnostics actionable.
        detail = redact(str(exc), secrets)
        return CheckResult("FAIL", name, f"未预期错误: {type(exc).__name__}: {detail}")


def _load_runtime(
    project_root: Path, config_path: Path | None
) -> tuple[RuntimeConfig | None, ModelLock | None, CheckResult]:
    if config_path is None:
        return (
            None,
            None,
            CheckResult(
                "SKIP",
                "Configuration",
                "未提供 --config; offline 检查不会选择或运行模型。",
            ),
        )
    try:
        config = load_runtime_config(config_path)
        lock = load_model_lock(project_root / config.models_lock)
    except ValueError as exc:
        return None, None, CheckResult("FAIL", "Configuration", str(exc))
    return (
        config,
        lock,
        CheckResult(
            "PASS",
            "Configuration",
            f"v1alpha1 / {len(lock.roles)} roles / {lock.engine} {lock.engine_version}",
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_local_data(project_root: Path, config: RuntimeConfig | None = None) -> str:
    manifest_path = project_root / (
        config.data_manifest if config is not None else "data/upstream/manifest.json"
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"无法读取数据 manifest: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "v1alpha1":
        raise SmokeError("数据 manifest schema_version 无效。")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise SmokeError("数据 manifest 缺少 sources。")
    expected = {
        "state_bench": STATE_COMMIT,
        "abcd": ABCD_COMMIT,
        "tau2": TAU2_COMMIT,
    }
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise SmokeError("数据 source 必须是 object。")
        name = source.get("name")
        if name not in expected:
            continue
        if source.get("commit") != expected[name]:
            raise SmokeError(f"{name} commit 与固定版本不一致。")
        fixtures = source.get("fixture_files")
        if not isinstance(fixtures, list):
            raise SmokeError(f"{name} 缺少 fixture_files。")
        for fixture in fixtures:
            if not isinstance(fixture, dict):
                raise SmokeError(f"{name} fixture 记录无效。")
            relative = fixture.get("path")
            checksum = fixture.get("sha256")
            if not isinstance(relative, str) or not isinstance(checksum, str):
                raise SmokeError(f"{name} fixture path/checksum 无效。")
            fixture_path = manifest_path.parent / relative
            if not fixture_path.is_file() or _sha256(fixture_path) != checksum:
                raise SmokeError(f"{name} fixture checksum 失败: {relative}")
        seen.add(name)
    missing = set(expected) - seen
    if missing:
        raise SmokeError("数据 manifest 缺少: " + ", ".join(sorted(missing)))
    return "3 个固定 benchmark source 和本地 fixture checksum 通过"


async def _live_model_and_mcp(
    *,
    project_root: Path,
    timeout: float,
    executable: str,
    model: LockedModel,
    credentials: ProviderCredentials,
    environ: Mapping[str, str],
) -> tuple[CheckResult, CheckResult]:
    server = Path(__file__).with_name("phase0_mcp.py")
    workspace = WorkspaceFactory().create(
        run_id="doctor",
        case_id="phase0",
        iteration_id="live",
        mcp_servers={
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": [str(server)],
                "env": {},
            }
        },
    )
    try:
        engine = ClaudeCodeEngine(
            model=model,
            credentials=credentials,
            workspace=workspace,
            executable=executable,
            environ=environ,
            system_prompt="Follow the request and use only the provided MCP tool.",
        )
        request = EngineRequest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ENGINE_REQUEST,
            request_id="doctor-live-request",
            prompt=(
                f"Call {MCP_TOOL_NAME} exactly once with "
                f'{{"value":"{PING_VALUE}"}}. Then reply with the exact tool result.'
            ),
            allowed_tools=(MCP_TOOL_NAME,),
            timeout_seconds=timeout,
        )
        events = [event async for event in engine.stream(request)]
    finally:
        shutil.rmtree(workspace.cleanup_root or workspace.root)
    has_model = any(
        event.payload.kind in {EngineEventKind.TEXT_DELTA, EngineEventKind.TOOL_CALL}
        for event in events
    )
    completed = events[-1].payload if events else None
    success = (
        isinstance(completed, CompletedPayload)
        and completed.exit_status is EngineExitStatus.SUCCESS
    )
    host = urlparse(model.base_url).hostname or "invalid-host"
    model_result = CheckResult(
        "PASS" if has_model and success else "FAIL",
        "Model",
        f"{host} / {model.model_id} / {len(events)} canonical events",
    )
    mcp_result = validate_mcp_exchange(events)
    if not success and mcp_result.status == "PASS":
        mcp_result = CheckResult("FAIL", "MCP", "engine did not complete successfully")
    return model_result, mcp_result


def validate_mcp_exchange(events: Sequence[EngineEvent]) -> CheckResult:
    """Verify one exact ping call and its successful correlated pong result."""
    calls = [
        event.payload for event in events if isinstance(event.payload, ToolCallPayload)
    ]
    results = [
        event.payload
        for event in events
        if isinstance(event.payload, ToolResultPayload)
    ]
    if len(calls) != 1 or len(results) != 1:
        return CheckResult("FAIL", "MCP", "expected exactly one tool call and result")
    call = calls[0]
    result = results[0]
    if call.tool_name != MCP_TOOL_NAME:
        return CheckResult("FAIL", "MCP", "unexpected tool name")
    if dict(call.arguments) != {"value": PING_VALUE}:
        return CheckResult("FAIL", "MCP", "unexpected ping arguments")
    if result.tool_call_id != call.tool_call_id:
        return CheckResult("FAIL", "MCP", "tool result does not match the call")
    if result.is_error:
        return CheckResult("FAIL", "MCP", "ping tool returned is_error=true")
    content = result.content
    exact_pong = content == PING_RESULT
    if isinstance(content, tuple) and len(content) == 1:
        block = content[0]
        exact_pong = (
            isinstance(block, Mapping)
            and block.get("type") == "text"
            and block.get("text") == PING_RESULT
        )
    if not exact_pong:
        return CheckResult("FAIL", "MCP", "ping result content did not match")
    return CheckResult("PASS", "MCP", "exact correlated phase0 pong verified")


def run_doctor(
    *,
    project_root: Path,
    config_path: Path | None,
    live: bool,
    timeout: float,
    environ: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """Run checks in local tools, config, data, model, MCP order."""
    source_environment = os.environ if environ is None else environ
    secrets = credential_values(source_environment)
    config, lock, config_result = _load_runtime(project_root, config_path)
    executable = config.claude_executable if config is not None else "claude"
    claude_result = _run_check(
        "Claude Code", lambda: check_claude(executable), secrets=secrets
    )
    results = [
        _run_check("Python", check_python, secrets=secrets),
        claude_result,
        check_claude_isolation(source_environment),
        config_result,
        _run_check(
            "Data", lambda: check_local_data(project_root, config), secrets=secrets
        ),
    ]
    if not live:
        results.extend(
            (
                CheckResult("SKIP", "Model", "未执行付费实时调用; 加 --live 后运行。"),
                CheckResult("SKIP", "MCP", "未执行模型驱动 MCP 调用。"),
            )
        )
        return results
    if config is None or lock is None:
        results.extend(
            (
                CheckResult(
                    "FAIL", "Model", "live 检查需要严格 config 和 model lock。"
                ),
                CheckResult("SKIP", "MCP", "缺少 config/model lock。"),
            )
        )
        return results
    if claude_result.status == "FAIL":
        results.extend(
            (
                CheckResult("FAIL", "Model", "Claude Code 本地检查失败。"),
                CheckResult("SKIP", "MCP", "Claude Code 不可用。"),
            )
        )
        return results
    if (
        re.search(
            rf"(?<!\d){re.escape(lock.engine_version)}(?!\d)", claude_result.detail
        )
        is None
    ):
        results.extend(
            (
                CheckResult(
                    "FAIL",
                    "Model",
                    f"Claude Code version mismatch: lock={lock.engine_version}; "
                    f"observed={claude_result.detail}",
                ),
                CheckResult("SKIP", "MCP", "Claude Code 版本与 lock 不一致。"),
            )
        )
        return results
    try:
        credentials = read_siliconflow_credentials(source_environment)
    except RuntimeError as exc:
        results.extend(
            (
                CheckResult("FAIL", "Model", str(exc)),
                CheckResult("SKIP", "MCP", "模型凭据不可用。"),
            )
        )
        return results
    model = lock.roles[ModelRole.MAIN]
    try:
        model_result, mcp_result = asyncio.run(
            _live_model_and_mcp(
                project_root=project_root,
                timeout=max(timeout, 120),
                executable=executable,
                model=model,
                credentials=credentials,
                environ=source_environment,
            )
        )
    except Exception as exc:  # Keep live failures actionable and secret-free.
        detail = redact(str(exc), (*secrets, credentials.api_key))
        model_result = CheckResult("FAIL", "Model", f"live check failed: {detail}")
        mcp_result = CheckResult("SKIP", "MCP", "模型检查未完成。")
    results.extend((model_result, mcp_result))
    return results


def run_checks(live: bool, timeout: float) -> list[CheckResult]:
    """Phase 0 compatibility wrapper around the package doctor."""
    return run_doctor(
        project_root=Path(__file__).resolve().parents[3],
        config_path=None,
        live=live,
        timeout=timeout,
    )


def build_claude_env(
    source: Mapping[str, str], key: str, model: str, config_dir: Path
) -> dict[str, str]:
    """Compatibility wrapper retained for Phase 0 callers."""
    return build_claude_environment(
        source,
        ProviderCredentials(api_key=key),
        base_url=SILICONFLOW_BASE_URL,
        model_id=model,
        config_dir=config_dir,
    )


def build_claude_command(executable: str, model: str, mcp_config: Path) -> list[str]:
    """Build the legacy live-smoke argv without placing credentials in it."""
    prompt = (
        f"Call the MCP tool {MCP_TOOL_NAME} exactly once with "
        f'{{"value":"{PING_VALUE}"}}. Then reply with the exact tool result.'
    )
    return [
        executable,
        "--bare",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        MCP_TOOL_NAME,
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--model",
        model,
        prompt,
    ]


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def parse_stream_json(stdout: str) -> StreamEvidence:
    """Retain the Phase 0 evidence parser for old script consumers."""
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeError(f"stream-json 第 {line_number} 行不是有效 JSON。") from exc
        if not isinstance(event, dict):
            raise SmokeError(f"stream-json 第 {line_number} 行不是 JSON object。")
        events.append(event)
    if not events:
        raise SmokeError("Claude Code 没有输出 stream-json 事件。")
    nested = [item for event in events for item in _walk_dicts(event)]
    evidence = StreamEvidence(
        event_count=len(events),
        has_model_response=any(event.get("type") == "assistant" for event in events),
        has_tool_call=any(
            item.get("type") == "tool_use" and item.get("name") == MCP_TOOL_NAME
            for item in nested
        ),
        has_tool_result=any(
            item.get("type") == "tool_result"
            and PING_RESULT in json.dumps(item, ensure_ascii=False)
            for item in nested
        ),
        has_success_result=any(
            event.get("type") == "result" and not event.get("is_error", False)
            for event in events
        ),
    )
    missing = [
        label
        for present, label in (
            (evidence.has_model_response, "model response"),
            (evidence.has_tool_call, "MCP tool call"),
            (evidence.has_tool_result, "MCP tool result"),
            (evidence.has_success_result, "successful result"),
        )
        if not present
    ]
    if missing:
        raise SmokeError("stream-json 缺少: " + ", ".join(missing))
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses doctor",
        description="Validate local tools, pinned data, and the optional live path.",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timeout", type=float, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    project_root = args.project_root.resolve()
    config_path = args.config
    if config_path is not None and not config_path.is_absolute():
        config_path = project_root / config_path
    results = run_doctor(
        project_root=project_root,
        config_path=config_path,
        live=args.live,
        timeout=args.timeout,
    )
    for result in results:
        safe_detail = redact(result.detail)
        print(f"[{result.status:<4}] {result.name}: {safe_detail}")
    failed = any(result.status == "FAIL" for result in results)
    if failed:
        print("\n结论: NO-GO。先处理 FAIL 项再继续。")
        return 1
    if args.live:
        print("\n结论: GO。数据、Claude headless、模型、MCP 和事件规范化已跑通。")
    else:
        print("\n结论: 本地检查完成; 实时链路尚未执行。")
    return 0
