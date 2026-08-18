from __future__ import annotations

from pathlib import Path

import pytest

from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.runner import LiveDevelopConfig
from ses.skills.paired import run_fresh_paired

ROOT = Path(__file__).parents[2]


@pytest.mark.live
def test_claude_code_live_paired_rejects_pending_course_catalog(
    tmp_path: Path,
) -> None:
    runtime = load_runtime_config(ROOT / "ses.json")
    lock = load_model_lock(ROOT / runtime.models_lock)
    skill = ROOT / "course/ch07-create-v0/artifacts/skill/v0"

    with pytest.raises(ValueError, match="independent signed human review"):
        run_fresh_paired(
            skill_source=skill,
            output_root=tmp_path / "live-paired",
            project_root=ROOT,
            live_config=LiveDevelopConfig(
                model=lock.roles[ModelRole.MAIN],
                credentials=read_siliconflow_credentials(
                    {"SILICONFLOW_API_KEY": "must-not-be-used"}
                ),
                executable=runtime.claude_executable,
                environ={},
                timeout_seconds=0.1,
            ),
        )

    assert not any((tmp_path / "live-paired").glob("run-*/events.jsonl"))
