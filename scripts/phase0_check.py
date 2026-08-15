#!/usr/bin/env python3
"""Run the fast Phase 0 prerequisite and live-provider smoke checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

STATE_COMMIT = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
ABCD_COMMIT = "6b8700ce67c6b37b062dd7a60abc76d7ef832a97"
TAU2_COMMIT = "c3398666e6559e3a063da3fc04b5acf7f941464e"

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2"
MCP_SERVER_NAME = "phase0"
MCP_TOOL_NAME = f"mcp__{MCP_SERVER_NAME}__phase0_ping"
PING_VALUE = "ses-phase0"
PING_RESULT = f"pong:{PING_VALUE}"

STATE_TASK_URL = (
    "https://raw.githubusercontent.com/microsoft/STATE-Bench/"
    f"{STATE_COMMIT}/state_bench/domains/customer_support/tasks/"
    "2-return_defective_electronics.json"
)
STATE_TRAJECTORY_URL = (
    "https://raw.githubusercontent.com/microsoft/STATE-Bench/"
    f"{STATE_COMMIT}/datasets/train_task_trajectories/customer_support/"
    "2-return_defective_electronics.json"
)
ABCD_SAMPLE_URL = (
    "https://raw.githubusercontent.com/asappresearch/abcd/"
    f"{ABCD_COMMIT}/data/abcd_sample.json"
)
ABCD_DATA_URL = (
    "https://raw.githubusercontent.com/asappresearch/abcd/"
    f"{ABCD_COMMIT}/data/abcd_v1.1.json.gz"
)
TAU2_TASKS_URL = (
    "https://raw.githubusercontent.com/sierra-research/tau2-bench/"
    f"{TAU2_COMMIT}/data/tau2/domains/retail/tasks.json"
)
TAU2_RESULT_FILES = {
    "claude-3-7-sonnet-20250219_retail_default_gpt-4.1-2025-04-14_4trials.json": 24_908_843,
    "gpt-4.1-2025-04-14_retail_default_gpt-4.1-2025-04-14_4trials.json": 22_044_059,
    "gpt-4.1-mini-2025-04-14_retail_base_gpt-4.1-2025-04-14_4trials.json": 23_697_012,
    "o4-mini-2025-04-16_retail_default_gpt-4.1-2025-04-14_4trials.json": 22_066_527,
}


class SmokeError(RuntimeError):
    """A user-actionable smoke check failure."""


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


def _tls_context() -> ssl.SSLContext:
    defaults = ssl.get_default_verify_paths()
    candidates = (
        os.environ.get("SSL_CERT_FILE"),
        defaults.cafile,
        defaults.openssl_cafile,
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


TLS_CONTEXT = _tls_context()


def _request(url: str, timeout: float, method: str = "GET") -> Any:
    request = Request(
        url,
        method=method,
        headers={"User-Agent": "learn-self-evolving-skills-phase0/0.1"},
    )
    try:
        return urlopen(request, timeout=timeout, context=TLS_CONTEXT)
    except HTTPError as exc:
        raise SmokeError(f"HTTP {exc.code}: {urlparse(url).netloc}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise SmokeError(
                "Python 无法验证 GitHub TLS 证书。请设置 SSL_CERT_FILE 指向系统 CA bundle。"
            ) from exc
        raise SmokeError(f"无法访问 {urlparse(url).netloc}: {exc.reason}") from exc


def _fetch_json(url: str, timeout: float) -> Any:
    with _request(url, timeout) as response:
        body = response.read()
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SmokeError(f"上游文件不是有效 JSON: {urlparse(url).path}") from exc


def _head_size(url: str, timeout: float) -> int:
    with _request(url, timeout, method="HEAD") as response:
        raw_size = response.headers.get("Content-Length")
    if raw_size is None:
        raise SmokeError(f"上游没有返回文件大小: {urlparse(url).path}")
    try:
        return int(raw_size)
    except ValueError as exc:
        raise SmokeError(f"上游返回了无效文件大小: {raw_size!r}") from exc


def _run_check(name: str, check: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult("PASS", name, check())
    except SmokeError as exc:
        return CheckResult("FAIL", name, str(exc))
    except Exception as exc:  # Keep doctor output actionable instead of a traceback.
        return CheckResult("FAIL", name, f"未预期错误: {type(exc).__name__}: {exc}")


def check_python() -> str:
    if sys.version_info < (3, 11):  # noqa: UP036 - this check is smoke behavior
        raise SmokeError(
            f"需要 Python 3.11+，当前是 {sys.version.split()[0]}。请升级后重试。"
        )
    return f"Python {sys.version.split()[0]}"


def check_claude() -> str:
    executable = shutil.which("claude")
    if executable is None:
        raise SmokeError("找不到 claude。请先安装 Claude Code CLI。")
    try:
        completed = subprocess.run(
            [executable, "--version"],
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
    return f"{executable} ({version[0] if version else 'version unknown'})"


def check_claude_isolation() -> CheckResult:
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    source = "shell"
    settings_path = Path.home() / ".claude" / "settings.json"

    if not base_url and settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings_env = settings.get("env", {}) if isinstance(settings, dict) else {}
            candidate = settings_env.get("ANTHROPIC_BASE_URL")
            if isinstance(candidate, str):
                base_url = candidate
                source = str(settings_path)
        except (OSError, json.JSONDecodeError):
            return CheckResult(
                "WARN",
                "Claude isolation",
                f"无法读取 {settings_path}；live smoke 仍会使用 --bare 和临时配置。",
            )

    if not base_url:
        return CheckResult(
            "PASS",
            "Claude isolation",
            "未发现全局 ANTHROPIC_BASE_URL；live smoke 仍使用 --bare 隔离。",
        )

    host = urlparse(base_url).hostname or "invalid-host"
    if host == urlparse(SILICONFLOW_BASE_URL).hostname:
        return CheckResult(
            "PASS",
            "Claude isolation",
            f"{source} 指向 {host}；live smoke 使用临时 CLAUDE_CONFIG_DIR。",
        )
    return CheckResult(
        "WARN",
        "Claude isolation",
        f"{source} 当前指向 {host}；live smoke 会用 --bare 隔离，不读取该 Provider。",
    )


def check_state_bench(timeout: float) -> str:
    task = _fetch_json(STATE_TASK_URL, timeout)
    trajectory = _fetch_json(STATE_TRAJECTORY_URL, timeout)
    if not isinstance(task, dict) or task.get("task_type") != "return_item":
        raise SmokeError("STATE-Bench 样例结构或 task_type 与预期不符。")
    if not isinstance(trajectory, dict) or not isinstance(
        trajectory.get("conversation"), list
    ):
        raise SmokeError("STATE-Bench 训练轨迹结构与预期不符。")
    return f"commit {STATE_COMMIT[:8]}，return_item 任务和训练轨迹可读取"


def check_abcd(timeout: float) -> str:
    sample = _fetch_json(ABCD_SAMPLE_URL, timeout)
    if not isinstance(sample, list) or not sample:
        raise SmokeError("ABCD 样例结构与预期不符。")
    required = {"scenario", "original", "delexed"}
    if not isinstance(sample[0], dict) or not required.issubset(sample[0]):
        raise SmokeError("ABCD 样例缺少 scenario/original/delexed 字段。")
    size = _head_size(ABCD_DATA_URL, timeout)
    expected_size = 36_985_084
    if size != expected_size:
        raise SmokeError(f"ABCD 完整数据大小漂移: {size} != {expected_size}")
    return f"commit {ABCD_COMMIT[:8]}，样例可解析，完整数据 {size} bytes"


def check_tau2(timeout: float) -> str:
    tasks = _fetch_json(TAU2_TASKS_URL, timeout)
    if not isinstance(tasks, list) or len(tasks) != 114:
        actual = len(tasks) if isinstance(tasks, list) else "not-a-list"
        raise SmokeError(f"tau2 retail task 数量漂移: {actual} != 114")

    base = (
        "https://raw.githubusercontent.com/sierra-research/tau2-bench/"
        f"{TAU2_COMMIT}/data/tau2/results/final/"
    )
    for filename, expected_size in TAU2_RESULT_FILES.items():
        size = _head_size(base + filename, timeout)
        if size != expected_size:
            raise SmokeError(
                f"tau2 轨迹大小漂移: {filename}: {size} != {expected_size}"
            )
    return f"commit {TAU2_COMMIT[:8]}，114 tasks，4 个 4-trial 轨迹文件可访问"


def build_claude_env(
    source: Mapping[str, str], key: str, model: str, config_dir: Path
) -> dict[str, str]:
    child = dict(source)
    for name in (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "SILICONFLOW_API_KEY",
    ):
        child.pop(name, None)

    child.update(
        {
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_BASE_URL": SILICONFLOW_BASE_URL,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CONFIG_DIR": str(config_dir),
        }
    )
    return child


def build_claude_command(executable: str, model: str, mcp_config: Path) -> list[str]:
    prompt = (
        f"Call the MCP tool {MCP_TOOL_NAME} exactly once with "
        f'{{"value":"{PING_VALUE}"}}. Then reply with the exact tool result. '
        "Do not use any other tool."
    )
    return [
        executable,
        "--bare",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        MCP_TOOL_NAME,
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--model",
        model,
        "--max-budget-usd",
        "0.05",
        "--system-prompt",
        "Follow the request and use only the provided MCP tool.",
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


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", redacted)
    redacted = re.sub(
        r"(?i)((?:authorization|x-api-key|anthropic-api-key)\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        redacted,
    )
    return redacted


def check_live_siliconflow(timeout: float) -> CheckResult:
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        return CheckResult(
            "FAIL",
            "SiliconFlow + MCP",
            "缺少 SILICONFLOW_API_KEY。请在本机设置轮换后的新 Key，再运行 --live；不要写入仓库或聊天。",
        )

    executable = shutil.which("claude")
    if executable is None:
        return CheckResult("FAIL", "SiliconFlow + MCP", "找不到 claude 可执行文件。")
    model = os.environ.get("SES_MAIN_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    server_path = Path(__file__).with_name("phase0_mcp_server.py").resolve()

    with tempfile.TemporaryDirectory(prefix="ses-phase0-") as temp_dir:
        temp_path = Path(temp_dir)
        config_dir = temp_path / "claude-config"
        config_dir.mkdir()
        mcp_config = temp_path / "mcp.json"
        mcp_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        MCP_SERVER_NAME: {
                            "command": sys.executable,
                            "args": [str(server_path)],
                            "env": {
                                "ANTHROPIC_API_KEY": "",
                                "ANTHROPIC_AUTH_TOKEN": "",
                                "SILICONFLOW_API_KEY": "",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        child_env = build_claude_env(os.environ, key, model, config_dir)
        command = build_claude_command(executable, model, mcp_config)
        try:
            completed = subprocess.run(
                command,
                cwd=temp_path,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                "FAIL",
                "SiliconFlow + MCP",
                f"实时调用在 {timeout:.0f}s 后超时。请检查网络、模型标识和账户余额。",
            )

    if completed.returncode != 0:
        raw_detail = (completed.stderr or completed.stdout or "无错误详情").strip()
        detail = redact(raw_detail, [key]).replace("\n", " ")[:600]
        return CheckResult(
            "FAIL",
            "SiliconFlow + MCP",
            f"Claude Code 退出码 {completed.returncode}: {detail}",
        )

    try:
        evidence = parse_stream_json(completed.stdout)
    except SmokeError as exc:
        return CheckResult("FAIL", "SiliconFlow + MCP", redact(str(exc), [key]))

    return CheckResult(
        "PASS",
        "SiliconFlow + MCP",
        f"{urlparse(SILICONFLOW_BASE_URL).hostname} / {model} / "
        f"{evidence.event_count} stream-json events / MCP pong verified",
    )


def run_checks(live: bool, timeout: float) -> list[CheckResult]:
    results = [
        _run_check("Python", check_python),
        _run_check("Claude Code", check_claude),
        check_claude_isolation(),
    ]

    data_checks = (
        ("STATE-Bench", lambda: check_state_bench(timeout)),
        ("ABCD", lambda: check_abcd(timeout)),
        ("tau2-bench", lambda: check_tau2(timeout)),
    )
    with ThreadPoolExecutor(max_workers=len(data_checks)) as executor:
        futures = [
            executor.submit(_run_check, name, check) for name, check in data_checks
        ]
        results.extend(future.result() for future in futures)

    if live:
        results.append(check_live_siliconflow(max(timeout, 120)))
    else:
        results.append(
            CheckResult(
                "SKIP",
                "SiliconFlow + MCP",
                "未执行付费实时调用；设置 SILICONFLOW_API_KEY 后加 --live。",
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate local tools, pinned data, and the optional SiliconFlow live path."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run one paid SiliconFlow model request and MCP tool call",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20,
        help="network timeout in seconds (live calls use at least 120 seconds)",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    results = run_checks(args.live, args.timeout)
    for result in results:
        print(f"[{result.status:<4}] {result.name}: {result.detail}")

    failed = any(result.status == "FAIL" for result in results)
    if failed:
        print("\n结论: NO-GO。先处理 FAIL 项再继续。")
        return 1
    if args.live:
        print("\n结论: GO。数据、Claude headless、硅流、MCP 和 stream-json 已跑通。")
    else:
        print("\n结论: 本地与数据 GO；实时链路尚未执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
