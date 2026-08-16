"""Run the first-course qualitative with/without Skill comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ses.contracts import Trace
from ses.evaluator import SingleCaseRun, SingleCaseRunError, run_pinned_case
from ses.foundation.config import load_model_lock, load_runtime_config
from ses.reporting import load_l1_result
from ses.shop import CASE_DEFINITION
from ses.skills.creator import CreatorError, FakeCreator
from ses.skills.demo_fixtures import with_skill_fixture, without_skill_fixture
from ses.skills.installer import SkillInstallError, normalized_skill_sha256
from ses.skills.reference import REFERENCE_SKILL_VERSION, reference_skill_source


class SkillDemoError(RuntimeError):
    """The paired Lesson 1 demo could not produce a comparable artifact."""


@dataclass(frozen=True, slots=True)
class SkillDemoResult:
    """Stable handles for the two fresh runs and their comparison artifact."""

    output_root: Path
    baseline_run: SingleCaseRun
    with_skill_run: SingleCaseRun
    comparison_artifact: str
    skill_source: str
    fallback_reason: str | None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _demo_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def _run_id(label: str, demo_id: str) -> str:
    return f"run-{label}-{demo_id}"


def _load_run_result(run: SingleCaseRun) -> dict[str, object]:
    loaded = load_l1_result(run.run_dir.parent, run.run_id, run.case_id)
    return cast(dict[str, object], loaded)


def _load_trace(run: SingleCaseRun) -> Trace:
    return Trace.model_validate_json((run.run_dir / "trace.json").read_bytes())


def _protocol(trace: Trace) -> dict[str, object]:
    request = trace.request
    value: dict[str, object] = {
        "case_id": trace.case_id,
        "iteration_id": trace.iteration_id,
        "prompt": request.prompt,
        "allowed_tools": list(request.allowed_tools),
        "timeout_seconds": request.timeout_seconds,
        "engine": "FakeEngine",
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["sha256"] = hashlib.sha256(encoded).hexdigest()
    return value


def _model_config(repo_root: Path) -> dict[str, object]:
    config = load_runtime_config(repo_root / "ses.json")
    lock = load_model_lock(repo_root / config.models_lock)
    return cast(
        dict[str, object],
        lock.model_dump(mode="json", round_trip=True),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SkillDemoError("comparison result contains a non-object value")
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
        )
        stream.write(b"\n")


def _candidate_or_reference(
    *,
    output_root: Path,
    demo_id: str,
    creator: FakeCreator,
    repo_root: Path,
) -> tuple[Path, str, str, str, bool, str | None]:
    candidate_dir = output_root / ".skill-candidates" / demo_id / "generated"
    try:
        candidate = creator.create(candidate_dir, seed_traces=())
        actual_hash = normalized_skill_sha256(candidate.source)
        if actual_hash != candidate.sha256:
            raise SkillInstallError("generated Skill hash does not match its content")
        return (
            candidate.source,
            candidate.version,
            candidate.sha256,
            "generated",
            False,
            None,
        )
    except (CreatorError, OSError, SkillInstallError, ValueError) as exc:
        source = reference_skill_source(repo_root)
        try:
            digest = normalized_skill_sha256(source)
        except (OSError, SkillInstallError, ValueError) as reference_error:
            raise SkillDemoError("reference Skill is unavailable") from reference_error
        return (
            source,
            REFERENCE_SKILL_VERSION,
            digest,
            "reference_fallback",
            True,
            str(exc) or type(exc).__name__,
        )


def _qualitative_comparison(
    *,
    baseline: SingleCaseRun,
    with_skill: SingleCaseRun,
    skill_source: str,
    skill_version: str,
    skill_sha256: str,
    reference: bool,
    fallback_reason: str | None,
    repo_root: Path,
) -> dict[str, object]:
    baseline_result = _load_run_result(baseline)
    with_skill_result = _load_run_result(with_skill)
    baseline_protocol = _protocol(_load_trace(baseline))
    with_skill_protocol = _protocol(_load_trace(with_skill))
    protocol_same = baseline_protocol["sha256"] == with_skill_protocol["sha256"]
    without_model_config = _model_config(repo_root)
    with_model_config = _model_config(repo_root)
    if not protocol_same:
        raise SkillDemoError("paired runs do not share the same protocol")
    return {
        "schema_version": "v1alpha1",
        "artifact_type": "lesson_1_skill_demo_comparison",
        "case_id": CASE_DEFINITION.case_id,
        "claim": "qualitative_demo_only",
        "notice": (
            "This single fixed-case comparison shows a qualitative difference; "
            "it does not prove stable improvement."
        ),
        "protocol": {
            "same_for_both_runs": protocol_same,
            "without_skill": baseline_protocol,
            "with_skill": with_skill_protocol,
        },
        "model_config": {
            "same_for_both_runs": without_model_config == with_model_config,
            "without_skill": without_model_config,
            "with_skill": with_model_config,
        },
        "skill": {
            "source": skill_source,
            "reference": reference,
            "version": skill_version,
            "sha256": skill_sha256,
            "fallback_reason": fallback_reason,
        },
        "runs": {
            "without_skill": baseline_result,
            "with_skill": with_skill_result,
        },
        "qualitative_result": {
            "outcome": f"{baseline_result['outcome']} -> {with_skill_result['outcome']}",
            "state_changed": {
                "without_skill": bool(
                    _mapping(baseline_result["state_diff"])["changed"]
                ),
                "with_skill": bool(
                    _mapping(with_skill_result["state_diff"])["changed"]
                ),
            },
        },
    }


def run_skill_demo(
    output_root: Path,
    *,
    creator: FakeCreator | None = None,
) -> SkillDemoResult:
    """Create a Skill and run paired fresh offline conversations."""
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    demo_id = _demo_id()
    source, version, digest, source_label, reference, fallback_reason = (
        _candidate_or_reference(
            output_root=root,
            demo_id=demo_id,
            creator=creator or FakeCreator(),
            repo_root=_repo_root(),
        )
    )
    baseline = run_pinned_case(
        root,
        run_id=_run_id("without-skill", demo_id),
        fixture=without_skill_fixture(),
    )
    with_skill = run_pinned_case(
        root,
        run_id=_run_id("with-skill", demo_id),
        fixture=with_skill_fixture(),
        skill_source=source,
        skill_version=version,
        skill_sha256=digest,
    )
    comparison = _qualitative_comparison(
        baseline=baseline,
        with_skill=with_skill,
        skill_source=source_label,
        skill_version=version,
        skill_sha256=digest,
        reference=reference,
        fallback_reason=fallback_reason,
        repo_root=_repo_root(),
    )
    comparison_artifact = f"comparisons/{demo_id}.json"
    comparison["comparison_artifact"] = comparison_artifact
    _write_json(root / comparison_artifact, comparison)
    return SkillDemoResult(
        output_root=root,
        baseline_run=baseline,
        with_skill_run=with_skill,
        comparison_artifact=comparison_artifact,
        skill_source=source_label,
        fallback_reason=fallback_reason,
    )


def _render_run(label: str, value: Mapping[str, object]) -> list[str]:
    lines = [
        f"{label} ({value.get('run_id')}): outcome={value.get('outcome')}",
        "  Messages:",
    ]
    messages = value.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                lines.append(f"    {message.get('role')}: {message.get('content')}")
    lines.append("  Tool calls:")
    tool_calls = value.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, Mapping):
                lines.append(
                    "    "
                    f"{call.get('tool_name')} input={json.dumps(call.get('input'), sort_keys=True)} "
                    f"error={call.get('is_error')}"
                )
    state_diff = value.get("state_diff")
    if isinstance(state_diff, Mapping):
        lines.append(
            f"  State result: changed={bool(state_diff.get('changed'))} "
            f"summary={state_diff.get('summary')}"
        )
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses skill-demo",
        description="Compare the fixed return case without and with a demo Skill.",
    )
    parser.add_argument("--output-root", type=Path, default=Path(".ses/skill-demo"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = run_skill_demo(args.output_root)
        comparison = json.loads(
            (result.output_root / result.comparison_artifact).read_text(
                encoding="utf-8"
            )
        )
    except (SingleCaseRunError, SkillDemoError, OSError, ValueError) as exc:
        print(f"skill_demo_error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))
        return 0
    print(comparison["notice"])
    runs = _mapping(comparison["runs"])
    print("\n".join(_render_run("Without Skill", _mapping(runs["without_skill"]))))
    print("\n".join(_render_run("With Skill", _mapping(runs["with_skill"]))))
    print(f"\nComparison artifact: {result.output_root / result.comparison_artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
