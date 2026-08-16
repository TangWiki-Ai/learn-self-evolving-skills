"""Fresh baseline-vs-Skill paired evaluation on the qualified develop catalog."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ses.contracts.primitives import SchemaVersion
from ses.contracts.runner import (
    PairCategory as PairCategory,
)
from ses.contracts.runner import (
    PairedCaseResult,
    PairedComparison,
    RunnerStatus,
)
from ses.runner import BaselineRunner, BudgetLimits, DevelopCatalogEvaluator
from ses.runner.baseline import load_run_events
from ses.runner.fake import develop_catalog_sha256, load_develop_catalog
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256
from ses.skills.static_gate import StaticGateStatus, run_static_gate

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def _started(events: list[dict[str, object]]) -> Mapping[str, Any]:
    if not events or events[0].get("event_type") != "run_started":
        raise ValueError("paired run is missing run_started protocol evidence")
    return cast(Mapping[str, Any], events[0])


def _attempt_map(events: list[dict[str, object]]) -> dict[str, Mapping[str, Any]]:
    attempts = {}
    for event in events:
        if (
            event.get("event_type") == "attempt"
            and event.get("iteration_id") == "iteration-0"
        ):
            attempts[str(event["case_id"])] = cast(Mapping[str, Any], event)
    return attempts


def _artifact_path(run_id: str, attempt: Mapping[str, Any], key: str) -> str:
    artifacts = cast(Mapping[str, Any], attempt["artifacts"])
    value = artifacts[key]
    if key == "traces":
        value = cast(list[Mapping[str, Any]], value)[0]
    path = cast(Mapping[str, Any], value)["path"]
    return f"{run_id}/{path}"


def _category(baseline: bool, skill: bool) -> PairCategory:
    if baseline and skill:
        return PairCategory.BOTH_PASS
    if baseline and not skill:
        return PairCategory.PASS_TO_FAIL
    if not baseline and skill:
        return PairCategory.FAIL_TO_PASS
    return PairCategory.BOTH_FAIL


def compare_run_events(
    baseline_events_path: Path, skill_events_path: Path
) -> PairedComparison:
    """Reject incompatible protocols, then derive every paired metric from events."""

    baseline_events = load_run_events(baseline_events_path)
    skill_events = load_run_events(skill_events_path)
    baseline_started = _started(baseline_events)
    skill_started = _started(skill_events)
    baseline_config = cast(Mapping[str, Any], baseline_started["config"])
    skill_config = cast(Mapping[str, Any], skill_started["config"])
    for key in (
        "data_version",
        "model_lock_hash",
        "protocol_version",
        "case_ids",
        "case_plan",
        "iterations",
    ):
        if baseline_config.get(key) != skill_config.get(key):
            raise ValueError(f"paired protocol mismatch: {key}")
    if baseline_config.get("skill_hash") != _EMPTY_HASH:
        raise ValueError("paired baseline must not contain a Skill")
    if skill_config.get("skill_hash") == _EMPTY_HASH:
        raise ValueError("paired Skill run must identify an installed Skill")
    if baseline_events_path.resolve() == skill_events_path.resolve():
        raise ValueError("paired runs must use distinct fresh artifacts")
    protocol_payload = {
        key: baseline_config[key]
        for key in (
            "data_version",
            "model_lock_hash",
            "protocol_version",
            "case_ids",
            "case_plan",
            "iterations",
        )
    }
    protocol_sha256 = hashlib.sha256(
        json.dumps(protocol_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    baseline_attempts = _attempt_map(baseline_events)
    skill_attempts = _attempt_map(skill_events)
    if baseline_attempts.keys() != skill_attempts.keys():
        raise ValueError("paired protocol mismatch: case results")
    rows: list[PairedCaseResult] = []
    for case_id in baseline_config["case_ids"]:
        baseline = baseline_attempts[case_id]
        skill = skill_attempts[case_id]
        baseline_pass = baseline["status"] == RunnerStatus.PASS.value
        skill_pass = skill["status"] == RunnerStatus.PASS.value
        baseline_usage = cast(Mapping[str, Any], baseline["usage"])
        skill_usage = cast(Mapping[str, Any], skill["usage"])
        rows.append(
            PairedCaseResult(
                case_id=case_id,
                category=_category(baseline_pass, skill_pass),
                baseline_status=RunnerStatus(str(baseline["status"])),
                skill_status=RunnerStatus(str(skill["status"])),
                baseline_score=float(baseline_pass),
                skill_score=float(skill_pass),
                score_delta=float(skill_pass) - float(baseline_pass),
                baseline_input_tokens=int(baseline_usage["input_tokens"]),
                skill_input_tokens=int(skill_usage["input_tokens"]),
                baseline_output_tokens=int(baseline_usage["output_tokens"]),
                skill_output_tokens=int(skill_usage["output_tokens"]),
                baseline_cost_amount=Decimal(
                    str(baseline_usage.get("cost_amount") or "0")
                ),
                skill_cost_amount=Decimal(str(skill_usage.get("cost_amount") or "0")),
                baseline_latency_ms=int(baseline["latency_ms"]),
                skill_latency_ms=int(skill["latency_ms"]),
                baseline_trace=_artifact_path(
                    str(baseline_started["run_id"]), baseline, "traces"
                ),
                skill_trace=_artifact_path(
                    str(skill_started["run_id"]), skill, "traces"
                ),
                baseline_state_diff=_artifact_path(
                    str(baseline_started["run_id"]), baseline, "state_diff"
                ),
                skill_state_diff=_artifact_path(
                    str(skill_started["run_id"]), skill, "state_diff"
                ),
                baseline_grade=_artifact_path(
                    str(baseline_started["run_id"]), baseline, "grade"
                ),
                skill_grade=_artifact_path(
                    str(skill_started["run_id"]), skill, "grade"
                ),
            )
        )
    counts = Counter(row.category for row in rows)
    total = len(rows)
    return PairedComparison(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="paired_comparison",
        baseline_run_id=str(baseline_started["run_id"]),
        skill_run_id=str(skill_started["run_id"]),
        skill_sha256=str(skill_config["skill_hash"]),
        protocol_sha256=protocol_sha256,
        compatible=True,
        fresh_baseline=True,
        fresh_skill=True,
        category_counts={category: counts[category] for category in PairCategory},
        baseline_pass_rate=sum(row.baseline_score for row in rows) / total,
        skill_pass_rate=sum(row.skill_score for row in rows) / total,
        baseline_input_tokens=sum(row.baseline_input_tokens for row in rows),
        skill_input_tokens=sum(row.skill_input_tokens for row in rows),
        baseline_output_tokens=sum(row.baseline_output_tokens for row in rows),
        skill_output_tokens=sum(row.skill_output_tokens for row in rows),
        baseline_cost_amount=sum(
            (row.baseline_cost_amount for row in rows), Decimal(0)
        ),
        skill_cost_amount=sum((row.skill_cost_amount for row in rows), Decimal(0)),
        baseline_latency_ms=sum(row.baseline_latency_ms for row in rows),
        skill_latency_ms=sum(row.skill_latency_ms for row in rows),
        cases=tuple(rows),
    )


def run_fresh_paired(
    *, skill_source: Path, output_root: Path, project_root: Path
) -> PairedComparison:
    """Run the static gate first, then create two new isolated develop runs."""

    gate = run_static_gate(skill_source, audit_path=output_root / "static-gate.json")
    if gate.status is not StaticGateStatus.PASS:
        raise ValueError(
            "candidate failed static gate; paid or trigger evaluation is forbidden"
        )
    if output_root.exists() and any(
        path.name.startswith("run-") for path in output_root.iterdir()
    ):
        raise ValueError("fresh paired output root already contains a run")
    catalog = load_develop_catalog()
    case_ids = tuple(catalog)
    model_lock_hash = hashlib.sha256(
        (project_root / "models.lock.json").read_bytes()
    ).hexdigest()
    data_hash = develop_catalog_sha256(catalog)
    skill_hash = normalized_skill_sha256(skill_source)
    manifest = load_skill_manifest(skill_source)
    skill_files = tuple(
        (
            skill_source / PurePosixPath(item.path),
            f"resolve-product-returns/{item.path}",
        )
        for item in manifest.files
    )
    baseline_fail = frozenset(case_ids[:2])
    skill_fail = frozenset((case_ids[1], case_ids[2]))
    budgets = BudgetLimits(max_cases=15, max_turns_per_case=3)
    baseline = BaselineRunner(
        output_root,
        DevelopCatalogEvaluator(
            catalog,
            forced_fail_case_ids=baseline_fail,
            fixed_latency_ms=20,
        ),
    ).run(
        run_id="run-ticket08-baseline-fixed",
        case_ids=case_ids,
        iterations=1,
        budgets=budgets,
        data_version=data_hash,
        model_lock_hash=model_lock_hash,
        skill_hash=_EMPTY_HASH,
        protocol_version="ses-ticket08-paired-v1",
    )
    skill = BaselineRunner(
        output_root,
        DevelopCatalogEvaluator(
            catalog,
            forced_fail_case_ids=skill_fail,
            skill_files=skill_files,
            input_token_overhead=24,
            cost_amount=Decimal("0.0012"),
            fixed_latency_ms=24,
        ),
    ).run(
        run_id="run-ticket08-skill-v0-fixed",
        case_ids=case_ids,
        iterations=1,
        budgets=budgets,
        data_version=data_hash,
        model_lock_hash=model_lock_hash,
        skill_hash=skill_hash,
        protocol_version="ses-ticket08-paired-v1",
    )
    return compare_run_events(baseline.events_path, skill.events_path)
