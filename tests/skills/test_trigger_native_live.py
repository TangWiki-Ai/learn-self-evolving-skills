from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ses.contracts import DiscoveryStatus
from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.skills.trigger_eval import ClaudeNativeDiscovery

ROOT = Path(__file__).parents[2]


@pytest.mark.live
def test_claude_code_native_skill_discovery_integration(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TRIGGER") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TRIGGER=1 for the paid native trigger test")
    config = load_runtime_config(ROOT / "ses.json")
    if shutil.which(config.claude_executable) is None:
        pytest.skip("Claude Code executable is unavailable")
    credentials = read_siliconflow_credentials(os.environ)
    lock = load_model_lock(ROOT / config.models_lock)
    skill_source = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
    discovery = ClaudeNativeDiscovery(
        skill_source=skill_source,
        model=lock.roles[ModelRole.MAIN],
        credentials=credentials,
        executable=config.claude_executable,
        environ=os.environ,
        workspace_root=tmp_path / "trigger-workspaces",
        timeout_seconds=300,
    )

    positive = discovery.observe("Please help me return a defective laptop.")

    assert positive.status is DiscoveryStatus.TRIGGERED
    assert "Claude Code" in positive.evidence
