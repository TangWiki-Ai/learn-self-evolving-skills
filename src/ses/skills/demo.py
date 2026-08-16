"""Select a Skill, run the paired demo, and persist its strict comparison."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from ses.contracts import Trace
from ses.evaluator import SingleCaseRun, run_pinned_case
from ses.reporting import load_l1_result
from ses.shop import CASE_DEFINITION

from .comparison import (
    ComparisonProtocol,
    ComparisonRuns,
    ComparisonSkill,
    ComparisonSource,
    QualitativeResult,
    SkillDemoComparison,
)
from .creator import FakeCreator
from .demo_engine import ENGINE_ID, OfflineSkillDemoEngine
from .resources import load_demo_resources
from .selection import CandidateMode, select_demo_skill


@dataclass(frozen=True, slots=True)
class SkillDemoResult:
    output_root: Path
    baseline_run: SingleCaseRun
    with_skill_run: SingleCaseRun
    comparison_artifact: str
    skill_source: str
    fallback_reason: str | None


def _demo_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def _trace(run: SingleCaseRun) -> Trace:
    return Trace.model_validate_json((run.run_dir / "trace.json").read_bytes())


def _protocol(baseline: Trace, with_skill: Trace) -> ComparisonProtocol:
    baseline_value = {
        "case_id": baseline.case_id,
        "prompt": baseline.request.prompt,
        "allowed_tools": baseline.request.allowed_tools,
        "timeout_seconds": baseline.request.timeout_seconds,
        "engine": ENGINE_ID,
    }
    with_value = {
        "case_id": with_skill.case_id,
        "prompt": with_skill.request.prompt,
        "allowed_tools": with_skill.request.allowed_tools,
        "timeout_seconds": with_skill.request.timeout_seconds,
        "engine": ENGINE_ID,
    }
    same = baseline_value == with_value
    if not same:
        raise ValueError("paired runs do not share the same protocol")
    encoded = json.dumps(baseline_value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return ComparisonProtocol(
        case_id=baseline.case_id,
        prompt=baseline.request.prompt,
        allowed_tools=baseline.request.allowed_tools,
        timeout_seconds=baseline.request.timeout_seconds,
        engine=ENGINE_ID,
        sha256=hashlib.sha256(encoded).hexdigest(),
        same_for_both_runs=True,
    )


def _run_result(run: SingleCaseRun) -> dict[str, JsonValue]:
    value = load_l1_result(run.run_dir.parent, run.run_id, run.case_id)
    return value


def _state_changed(result: Mapping[str, JsonValue]) -> bool:
    state_diff = result.get("state_diff")
    if not isinstance(state_diff, Mapping):
        raise ValueError("run result is missing state_diff")
    changed = state_diff.get("changed")
    if not isinstance(changed, Mapping):
        raise ValueError("run state_diff.changed must be an object")
    return bool(changed)


def _persist(path: Path, comparison: SkillDemoComparison) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(comparison.model_dump_json(indent=2).encode("utf-8") + b"\n")


def run_skill_demo(
    output_root: Path,
    *,
    mode: CandidateMode = CandidateMode.GENERATE,
    candidate_source: Path | None = None,
    creator: FakeCreator | None = None,
) -> SkillDemoResult:
    """Run two fresh cases through one workspace-aware offline Engine."""
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    demo_id = _demo_id()
    selected = select_demo_skill(
        root / ".skill-candidates" / demo_id,
        mode=mode,
        candidate_source=candidate_source,
        creator=creator,
    )
    resources = load_demo_resources()
    baseline = run_pinned_case(
        root,
        run_id=f"run-without-skill-{demo_id}",
        engine_factory=OfflineSkillDemoEngine,
    )
    with_skill = run_pinned_case(
        root,
        run_id=f"run-with-skill-{demo_id}",
        engine_factory=OfflineSkillDemoEngine,
        skill_source=selected.source,
        skill_version=selected.manifest.version,
        skill_sha256=selected.sha256,
    )
    baseline_result = _run_result(baseline)
    with_skill_result = _run_result(with_skill)
    comparison_artifact = f"comparisons/{demo_id}.json"
    comparison = SkillDemoComparison(
        schema_version="v1alpha1",
        record_type="lesson_1_skill_demo_comparison",
        case_id=CASE_DEFINITION.case_id,
        claim="qualitative_demo_only",
        measured=True,
        notice=(
            "This single fixed-case comparison shows a qualitative difference; "
            "it does not prove stable improvement."
        ),
        source=ComparisonSource(
            kind="current_run",
            engine=ENGINE_ID,
            description="Fresh offline runs produced from installed package resources.",
            runtime_config_sha256=resources.runtime_config_sha256,
            model_lock_sha256=resources.model_lock_sha256,
        ),
        protocol=_protocol(_trace(baseline), _trace(with_skill)),
        skill=ComparisonSkill(
            source=selected.source_label,
            reference=selected.source_label in {"reference", "reference_fallback"},
            name=selected.manifest.name,
            version=selected.manifest.version,
            sha256=selected.sha256,
            fallback_reason=selected.fallback_reason,
        ),
        runs=ComparisonRuns(
            without_skill=baseline_result,
            with_skill=with_skill_result,
        ),
        qualitative_result=QualitativeResult(
            outcome=(f"{baseline_result['outcome']} -> {with_skill_result['outcome']}"),
            without_skill_state_changed=_state_changed(baseline_result),
            with_skill_state_changed=_state_changed(with_skill_result),
        ),
        comparison_artifact=comparison_artifact,
    )
    _persist(root / comparison_artifact, comparison)
    return SkillDemoResult(
        output_root=root,
        baseline_run=baseline,
        with_skill_run=with_skill,
        comparison_artifact=comparison_artifact,
        skill_source=selected.source_label,
        fallback_reason=selected.fallback_reason,
    )
