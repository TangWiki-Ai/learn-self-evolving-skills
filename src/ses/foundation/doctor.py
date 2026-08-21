"""Local prerequisite checks used by Journey station 0."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ses.foundation.config import (
    ProviderId,
    RuntimeConfig,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import credential_values, redact

STATE_COMMIT = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
ABCD_COMMIT = "6b8700ce67c6b37b062dd7a60abc76d7ef832a97"
TAU2_COMMIT = "c3398666e6559e3a063da3fc04b5acf7f941464e"


class SmokeError(RuntimeError):
    """A user-actionable prerequisite failure."""


@dataclass(frozen=True)
class CheckResult:
    status: str
    name: str
    detail: str


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
        return CheckResult(
            "PASS",
            "Claude isolation",
            "不读取个人 settings、Skill 或 memory; 运行使用 --bare 和临时配置。",
        )
    host = urlparse(base_url).hostname or "invalid-host"
    return CheckResult(
        "WARN",
        "Claude isolation",
        f"忽略 shell Provider {host}; 运行使用 --bare 和临时 CLAUDE_CONFIG_DIR。",
    )


def _run_check(name: str, check: Any, *, secrets: Sequence[str] = ()) -> CheckResult:
    try:
        return CheckResult("PASS", name, str(check()))
    except SmokeError as exc:
        return CheckResult("FAIL", name, redact(str(exc), secrets))
    except Exception as exc:
        detail = redact(str(exc), secrets)
        return CheckResult("FAIL", name, f"未预期错误: {type(exc).__name__}: {detail}")


def _load_runtime(
    project_root: Path,
    config_path: Path,
    provider: ProviderId,
) -> tuple[RuntimeConfig | None, CheckResult]:
    try:
        config = load_runtime_config(config_path)
        lock = load_model_lock(project_root / config.models_lock_for(provider))
        if lock.provider is not provider:
            raise ValueError("selected provider does not match the loaded model lock")
    except ValueError as exc:
        return None, CheckResult("FAIL", "Configuration", str(exc))
    return config, CheckResult(
        "PASS",
        "Configuration",
        f"v1alpha1 / {provider.value} / {lock.model.model_id} / "
        f"{lock.engine} {lock.engine_version}",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_local_data(project_root: Path, config: RuntimeConfig) -> str:
    manifest_path = project_root / config.data_manifest
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


def run_doctor(
    *,
    project_root: Path,
    config_path: Path,
    environ: Mapping[str, str] | None = None,
    provider: ProviderId = ProviderId.SILICONFLOW,
) -> list[CheckResult]:
    """Check local tools, the selected configuration, and pinned data."""
    source_environment = os.environ if environ is None else environ
    secrets = credential_values(source_environment)
    config, config_result = _load_runtime(project_root, config_path, provider)
    executable = config.claude_executable if config is not None else "claude"
    data_result = (
        _run_check(
            "Data", lambda: check_local_data(project_root, config), secrets=secrets
        )
        if config is not None
        else CheckResult("SKIP", "Data", "Configuration 无效。Data 未检查。")
    )
    return [
        _run_check("Python", check_python, secrets=secrets),
        _run_check("Claude Code", lambda: check_claude(executable), secrets=secrets),
        check_claude_isolation(source_environment),
        config_result,
        data_result,
    ]
