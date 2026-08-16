from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.trigger_eval import ClaudeNativeDiscovery, DiscoveryStatus
from ses.skills.v0 import FakeV0Creator, create_skill_v0

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
    skill = create_skill_v0(
        seed_pack=load_creator_seed_pack(
            ROOT / "data" / "skill-v0" / "creator" / "seed-manifest.json"
        ),
        output_dir=tmp_path / "v0",
        creator=FakeV0Creator(),
        workspace_root=tmp_path / "creator-workspaces",
    )
    discovery = ClaudeNativeDiscovery(
        skill_source=skill.source,
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
