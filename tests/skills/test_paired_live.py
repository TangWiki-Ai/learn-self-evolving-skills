from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ses.contracts import MeasurementKind, Trace
from ses.evaluation import trace_tool_calls
from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.runner import LiveDevelopConfig
from ses.runner.baseline import load_run_events
from ses.skills.paired import run_fresh_paired

ROOT = Path(__file__).parents[2]


@pytest.mark.live
def test_claude_code_live_paired_uses_native_agent_on_both_fresh_sides(
    tmp_path: Path,
) -> None:
    if os.environ.get("RUN_LIVE_CLAUDE_PAIRED") != "1":
        pytest.skip("set RUN_LIVE_CLAUDE_PAIRED=1 for the paid 15-case paired test")
    runtime = load_runtime_config(ROOT / "ses.json")
    if shutil.which(runtime.claude_executable) is None:
        pytest.skip("Claude Code executable is unavailable")
    lock = load_model_lock(ROOT / runtime.models_lock)
    skill = ROOT / "course/ch07-create-v0/artifacts/skill/v0"

    result = run_fresh_paired(
        skill_source=skill,
        output_root=tmp_path / "live-paired",
        project_root=ROOT,
        live_config=LiveDevelopConfig(
            model=lock.roles[ModelRole.MAIN],
            credentials=read_siliconflow_credentials(os.environ),
            executable=runtime.claude_executable,
            environ=os.environ,
            timeout_seconds=300,
        ),
        measured_at=datetime.now(UTC),
        engine_version=f"{lock.engine}:{lock.engine_version}",
    )

    assert result.measurement_kind is MeasurementKind.LIVE_MEASURED
    assert len(result.cases) == 15
    assert result.baseline_run_id != result.skill_run_id
    assert result.baseline_events.sha256 != result.skill_events.sha256
    assert any(
        row.baseline_trace is not None and row.baseline_trace.sha256
        for row in result.cases
    )
    assert any(
        row.skill_trace is not None and row.skill_trace.sha256 for row in result.cases
    )

    def observed_tools(run_id: str) -> tuple[str, ...]:
        run_dir = tmp_path / "live-paired" / run_id
        events = load_run_events(run_dir / "events.jsonl")
        traces = (
            Trace.model_validate_json((run_dir / ref["path"]).read_text())
            for event in events
            if event.get("event_type") == "attempt"
            for ref in event["artifacts"]["traces"]  # type: ignore[index]
        )
        return tuple(
            call.tool_name for trace in traces for call in trace_tool_calls(trace)
        )

    baseline_tools = observed_tools(result.baseline_run_id)
    skill_tools = observed_tools(result.skill_run_id)
    assert "Skill" not in baseline_tools
    assert "Skill" in skill_tools
    assert "mcp__shop__process_return" in baseline_tools
    assert "mcp__shop__process_return" in skill_tools
