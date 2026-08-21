from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from ses.contracts import DiscoveryStatus
from ses.foundation.config import (
    LockedModel,
    ModelRole,
    ProviderId,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import ProviderCredentials, read_siliconflow_credentials
from ses.skills.trigger_eval import ClaudeNativeDiscovery

ROOT = Path(__file__).parents[2]


def test_chatanywhere_discovery_keeps_unavailable_cost_explicit(tmp_path: Path) -> None:
    executable = tmp_path / "fake-chatanywhere-claude"
    executable.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "session-unpriced",
    "usage": {"input_tokens": 21, "output_tokens": 8}
}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    discovery = ClaudeNativeDiscovery(
        skill_source=ROOT / "fixtures/seed/skill/v0",
        model=LockedModel(
            model_id="claude-sonnet-4-6",
            base_url="https://api.chatanywhere.tech/",
        ),
        credentials=ProviderCredentials(
            api_key="chatanywhere-test-secret",
            provider=ProviderId.CHATANYWHERE,
        ),
        executable=str(executable),
        environ={"PATH": os.environ.get("PATH", "")},
        workspace_root=tmp_path / "trigger-workspaces",
    )

    observation = discovery.observe("A prompt with no matching Skill.")

    assert observation.status is DiscoveryStatus.NOT_TRIGGERED
    assert discovery.input_tokens == 21
    assert discovery.output_tokens == 8
    assert discovery.cost_amount is None
    assert discovery.cost_currency is None


@pytest.mark.live
def test_claude_code_native_skill_discovery_integration(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_TRIGGER") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_TRIGGER=1 for the paid native trigger test")
    config = load_runtime_config(ROOT / "ses.json")
    if shutil.which(config.claude_executable) is None:
        pytest.skip("Claude Code executable is unavailable")
    credentials = read_siliconflow_credentials(os.environ)
    lock = load_model_lock(ROOT / config.models_lock)
    skill_source = ROOT / "fixtures/seed/skill/v0"
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
