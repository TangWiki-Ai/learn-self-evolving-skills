"""Deep orchestration module for the complete Skill v0 vertical slice."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    MeasurementKind,
    SchemaVersion,
    SkillV0PipelineSummary,
)
from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.reporting.l2 import write_l2_html
from ses.runner import LiveDevelopConfig
from ses.skills.paired import run_fresh_paired
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.static_gate import StaticGateStatus, run_static_gate
from ses.skills.trigger_eval import (
    ClaudeNativeDiscovery,
    DiscoveryBackend,
    SyntheticDiscoveryFixture,
    evaluate_triggers,
)
from ses.skills.v0 import FakeV0Creator, LiveV0Creator, V0Creator, create_skill_v0


@dataclass(frozen=True, slots=True)
class SkillV0WorkflowConfig:
    project_root: Path
    output_root: Path
    seed_manifest: Path
    mode: Literal["fixed", "live"] = "fixed"
    creator_timeout: float = 180
    trigger_timeout: float = 60
    paired_timeout: float = 300


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def run_skill_v0_workflow(
    config: SkillV0WorkflowConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> SkillV0PipelineSummary:
    """Create, gate, measure, compare, render, and persist one fresh pipeline."""
    if config.mode not in {"fixed", "live"}:
        raise ValueError("workflow mode must be fixed or live")
    if config.output_root.exists() and any(config.output_root.iterdir()):
        raise ValueError("output root must be absent or empty for a fresh pipeline")
    source_environment = os.environ if environ is None else environ
    runtime = load_runtime_config(config.project_root / "ses.json")
    lock = load_model_lock(config.project_root / runtime.models_lock)
    credentials = (
        read_siliconflow_credentials(source_environment)
        if config.mode == "live"
        else None
    )
    pack = load_creator_seed_pack(config.seed_manifest)
    creator: V0Creator
    if config.mode == "fixed":
        creator = FakeV0Creator()
    else:
        assert credentials is not None
        creator = LiveV0Creator(
            model=lock.roles[ModelRole.CREATOR],
            credentials=credentials,
            executable=runtime.claude_executable,
            environ=source_environment,
            timeout_seconds=config.creator_timeout,
        )
    skill = create_skill_v0(
        seed_pack=pack,
        output_dir=config.output_root / "skill" / "v0",
        creator=creator,
        workspace_root=config.output_root / "creator-workspaces",
    )
    gate = run_static_gate(
        skill.source, audit_path=config.output_root / "static-gate.json"
    )
    if gate.status is not StaticGateStatus.PASS:
        raise ValueError("v0 candidate failed static gate")
    discovery: DiscoveryBackend
    measurement = (
        MeasurementKind.SYNTHETIC_OFFLINE
        if config.mode == "fixed"
        else MeasurementKind.LIVE_MEASURED
    )
    measured_at = (
        datetime(2026, 8, 17, tzinfo=UTC)
        if config.mode == "fixed"
        else datetime.now(UTC)
    )
    if config.mode == "fixed":
        discovery = SyntheticDiscoveryFixture()
    else:
        assert credentials is not None
        discovery = ClaudeNativeDiscovery(
            skill_source=skill.source,
            model=lock.roles[ModelRole.MAIN],
            credentials=credentials,
            executable=runtime.claude_executable,
            environ=source_environment,
            workspace_root=config.output_root / "trigger-workspaces",
            timeout_seconds=config.paired_timeout,
        )
    trigger = evaluate_triggers(
        skill_sha256=skill.sha256,
        engine_version=f"{lock.engine}:{lock.engine_version}",
        model_id=lock.roles[ModelRole.MAIN].model_id,
        measurement_kind=measurement,
        measured_at=measured_at,
        discovery=discovery,
    )
    trigger_path = config.output_root / "trigger-eval.json"
    _write_json(trigger_path, trigger.model_dump(mode="json"))
    live_config = None
    if config.mode == "live":
        assert credentials is not None
        live_config = LiveDevelopConfig(
            model=lock.roles[ModelRole.MAIN],
            credentials=credentials,
            executable=runtime.claude_executable,
            environ=source_environment,
            timeout_seconds=config.trigger_timeout,
        )
    paired = run_fresh_paired(
        skill_source=skill.source,
        output_root=config.output_root,
        project_root=config.project_root,
        live_config=live_config,
        measured_at=measured_at,
        engine_version=f"{lock.engine}:{lock.engine_version}",
    )
    paired_path = config.output_root / "paired-comparison.json"
    _write_json(paired_path, paired.model_dump(mode="json"))
    l2_path = write_l2_html(
        paired,
        trigger,
        config.output_root / "l2.html",
        artifact_root=config.output_root,
    )
    summary = SkillV0PipelineSummary(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_v0_pipeline_summary",
        mode=config.mode,
        seed_count=len(pack.records),
        skill_sha256=skill.sha256,
        creator_measurement=measurement,
        trigger_measurement=trigger.measurement_kind,
        paired_measurement=paired.measurement_kind,
        static_gate="pass",
        trigger_precision=trigger.precision,
        trigger_recall=trigger.recall,
        paired_case_count=len(paired.cases),
        baseline_pass_rate=paired.baseline_pass_rate,
        skill_pass_rate=paired.skill_pass_rate,
        trigger_result=_ref(config.output_root, trigger_path),
        paired_comparison=_ref(config.output_root, paired_path),
        l2_html=_ref(config.output_root, l2_path),
    )
    _write_json(config.output_root / "summary.json", summary.model_dump(mode="json"))
    return summary
