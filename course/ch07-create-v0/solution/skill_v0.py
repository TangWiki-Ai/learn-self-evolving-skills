"""Lesson 7 solution delegates to the production Skill v0 protocol."""

from datetime import UTC, datetime
from pathlib import Path

from ses.contracts import MeasurementKind
from ses.skills.paired import run_fresh_paired
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.static_gate import run_static_gate
from ses.skills.trigger_eval import evaluate_triggers


def load_seeds(manifest: Path) -> object:
    return load_creator_seed_pack(manifest)


def static_gate(skill: Path) -> object:
    return run_static_gate(skill)


def trigger_eval(skill_hash: str, discovery: object) -> object:
    return evaluate_triggers(
        skill_sha256=skill_hash,
        engine_version="claude-code:2.1.220",
        model_id="synthetic-fixture",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=datetime(2026, 8, 17, tzinfo=UTC),
        discovery=discovery,  # type: ignore[arg-type]
    )


def paired_compare(skill: Path, output: Path, project_root: Path) -> object:
    return run_fresh_paired(
        skill_source=skill,
        output_root=output,
        project_root=project_root,
    )
