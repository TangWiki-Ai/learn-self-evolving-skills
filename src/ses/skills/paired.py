"""Fresh baseline-vs-Skill paired evaluation on the fixed develop catalog."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ses.contracts import ArtifactRef, MeasurementKind
from ses.contracts.artifact import ArtifactRoot
from ses.contracts.primitives import SchemaVersion
from ses.contracts.runner import (
    PairCategory as PairCategory,
)
from ses.contracts.runner import (
    PairedCaseResult,
    PairedComparison,
    RunnerStatus,
    pair_execution_sha256,
)
from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    DevelopCatalogEvaluator,
    LiveDevelopConfig,
)
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


def _artifact_ref(
    attempt: Mapping[str, Any], key: str, *, run_id: str
) -> ArtifactRef | None:
    artifacts = cast(Mapping[str, Any], attempt["artifacts"])
    value = artifacts.get(key)
    if key == "traces":
        traces = cast(list[Mapping[str, Any]], value)
        if not traces:
            return None
        value = traces[0]
    if value is None:
        return None
    source = ArtifactRef.model_validate(value)
    return source.model_copy(update={"path": f"{run_id}/{source.path}"})


def _category(baseline: bool, skill: bool) -> PairCategory:
    if baseline and skill:
        return PairCategory.BOTH_PASS
    if baseline and not skill:
        return PairCategory.PASS_TO_FAIL
    if not baseline and skill:
        return PairCategory.FAIL_TO_PASS
    return PairCategory.BOTH_FAIL


def _file_ref(path: Path, *, relative_to: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.resolve().relative_to(relative_to.resolve()).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _verify_ref(root: Path, reference: ArtifactRef) -> None:
    path = root / reference.path
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("paired evidence escapes its controlled root") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("paired evidence must be a regular file")
    reference.verify_bytes(path.read_bytes())


def compare_run_events(
    baseline_events_path: Path,
    skill_events_path: Path,
    *,
    output_root: Path,
    measurement_kind: MeasurementKind,
    measured_at: datetime,
    engine_version: str,
    model_id: str,
    shopping_metrics_builder: (
        Callable[[str, tuple[PairedCaseResult, ...]], ArtifactRef] | None
    ) = None,
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
    root = output_root.resolve()
    for path in (baseline_events_path, skill_events_path):
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "paired event log escapes the controlled run root"
            ) from exc
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
    baseline_run_id = str(baseline_started["run_id"])
    skill_run_id = str(skill_started["run_id"])
    shopping_pair = shopping_metrics_builder is not None
    completed_statuses = {RunnerStatus.PASS, RunnerStatus.AGENT_FAIL}
    for case_id in baseline_config["case_ids"]:
        baseline = baseline_attempts[case_id]
        skill = skill_attempts[case_id]
        baseline_status = RunnerStatus(str(baseline["status"]))
        skill_status = RunnerStatus(str(skill["status"]))
        baseline_pass = baseline_status is RunnerStatus.PASS
        skill_pass = skill_status is RunnerStatus.PASS
        baseline_usage = cast(Mapping[str, Any], baseline["usage"])
        skill_usage = cast(Mapping[str, Any], skill["usage"])
        rows.append(
            PairedCaseResult(
                case_id=case_id,
                category=_category(baseline_pass, skill_pass),
                baseline_status=baseline_status,
                skill_status=skill_status,
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
                baseline_trace=_artifact_ref(
                    baseline, "traces", run_id=baseline_run_id
                ),
                skill_trace=_artifact_ref(skill, "traces", run_id=skill_run_id),
                baseline_state_diff=_artifact_ref(
                    baseline, "state_diff", run_id=baseline_run_id
                ),
                skill_state_diff=_artifact_ref(
                    skill, "state_diff", run_id=skill_run_id
                ),
                baseline_grade=_artifact_ref(baseline, "grade", run_id=baseline_run_id),
                skill_grade=_artifact_ref(skill, "grade", run_id=skill_run_id),
                comparable=(
                    baseline_status in completed_statuses
                    and skill_status in completed_statuses
                    if shopping_pair
                    else None
                ),
                baseline_domain_result=(
                    _artifact_ref(baseline, "domain_result", run_id=baseline_run_id)
                    if shopping_pair
                    else None
                ),
                skill_domain_result=(
                    _artifact_ref(skill, "domain_result", run_id=skill_run_id)
                    if shopping_pair
                    else None
                ),
            )
        )
    counts = Counter(row.category for row in rows)
    currencies = {
        str(cast(Mapping[str, Any], attempt["usage"])["cost_currency"])
        for attempt in (*baseline_attempts.values(), *skill_attempts.values())
    }
    if len(currencies) != 1:
        raise ValueError("paired protocol mismatch: cost currency")
    baseline_ref = _file_ref(baseline_events_path, relative_to=output_root)
    skill_ref = _file_ref(skill_events_path, relative_to=output_root)
    for reference in (
        baseline_ref,
        skill_ref,
        *(
            reference
            for row in rows
            for reference in (
                row.baseline_trace,
                row.skill_trace,
                row.baseline_state_diff,
                row.skill_state_diff,
                row.baseline_domain_result,
                row.skill_domain_result,
                row.baseline_grade,
                row.skill_grade,
            )
            if reference is not None
        ),
    ):
        _verify_ref(root, reference)
    execution_sha256 = pair_execution_sha256(
        baseline_events=baseline_ref,
        skill_events=skill_ref,
        protocol_sha256=protocol_sha256,
        measured_at=measured_at,
        measurement_kind=measurement_kind,
    )
    shopping_metrics = (
        shopping_metrics_builder(execution_sha256, tuple(rows))
        if shopping_metrics_builder is not None
        else None
    )
    if shopping_metrics is not None:
        _verify_ref(root, shopping_metrics)
    comparable_rows = (
        tuple(row for row in rows if row.comparable) if shopping_pair else tuple(rows)
    )
    comparable_total = len(comparable_rows)
    return PairedComparison(
        schema_version=(
            SchemaVersion.V1ALPHA2 if shopping_pair else SchemaVersion.V1ALPHA1
        ),
        record_type="paired_comparison",
        baseline_run_id=baseline_run_id,
        skill_run_id=skill_run_id,
        skill_sha256=str(skill_config["skill_hash"]),
        protocol_sha256=protocol_sha256,
        pair_execution_sha256=execution_sha256,
        measurement_kind=measurement_kind,
        measured_at=measured_at,
        data_version=str(baseline_config["data_version"]),
        model_lock_sha256=str(baseline_config["model_lock_hash"]),
        engine_version=engine_version,
        model_id=model_id,
        baseline_events=baseline_ref,
        skill_events=skill_ref,
        category_counts={category: counts[category] for category in PairCategory},
        baseline_pass_rate=(
            sum(row.baseline_score for row in comparable_rows) / comparable_total
            if comparable_total
            else 0.0
        ),
        skill_pass_rate=(
            sum(row.skill_score for row in comparable_rows) / comparable_total
            if comparable_total
            else 0.0
        ),
        baseline_input_tokens=sum(row.baseline_input_tokens for row in rows),
        skill_input_tokens=sum(row.skill_input_tokens for row in rows),
        baseline_output_tokens=sum(row.baseline_output_tokens for row in rows),
        skill_output_tokens=sum(row.skill_output_tokens for row in rows),
        baseline_cost_amount=sum(
            (row.baseline_cost_amount for row in rows), Decimal(0)
        ),
        skill_cost_amount=sum((row.skill_cost_amount for row in rows), Decimal(0)),
        cost_currency=currencies.pop(),
        baseline_latency_ms=sum(row.baseline_latency_ms for row in rows),
        skill_latency_ms=sum(row.skill_latency_ms for row in rows),
        cases=tuple(rows),
        shopping_metrics=shopping_metrics,
    )


def run_fresh_paired(
    *,
    skill_source: Path,
    output_root: Path,
    project_root: Path,
    live_config: LiveDevelopConfig | None = None,
    measured_at: datetime | None = None,
    engine_version: str | None = None,
) -> PairedComparison:
    """Run the static gate first, then create two new isolated develop runs."""

    output_root = output_root.resolve()
    gate = run_static_gate(skill_source, audit_path=output_root / "static-gate.json")
    if gate.status is not StaticGateStatus.PASS:
        raise ValueError(
            "candidate failed static gate; paid or trigger evaluation is forbidden"
        )
    if output_root.exists() and any(
        path.name.startswith("run-") for path in output_root.iterdir()
    ):
        raise ValueError("fresh paired output root already contains a run")
    is_live = live_config is not None
    catalog = load_develop_catalog(mode="live" if is_live else "fixed")
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
    budgets = BudgetLimits(
        max_cases=15,
        max_turns_per_case=3,
        cost_currency="USD" if is_live else "CNY",
    )
    run_suffix = "live" if is_live else "fixed"
    baseline = BaselineRunner(
        output_root,
        DevelopCatalogEvaluator(
            catalog,
            fixed_latency_ms=None if is_live else 20,
            live_config=live_config,
            cost_amount=Decimal(0),
        ),
    ).run(
        run_id=f"run-ticket08-baseline-{run_suffix}",
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
            skill_files=skill_files,
            fixed_latency_ms=None if is_live else 20,
            live_config=live_config,
            cost_amount=Decimal(0),
        ),
    ).run(
        run_id=f"run-ticket08-skill-v0-{run_suffix}",
        case_ids=case_ids,
        iterations=1,
        budgets=budgets,
        data_version=data_hash,
        model_lock_hash=model_lock_hash,
        skill_hash=skill_hash,
        protocol_version="ses-ticket08-paired-v1",
    )
    return compare_run_events(
        baseline.events_path,
        skill.events_path,
        output_root=output_root,
        measurement_kind=(
            MeasurementKind.LIVE_MEASURED
            if is_live
            else MeasurementKind.SYNTHETIC_OFFLINE
        ),
        measured_at=(
            measured_at
            if measured_at is not None
            else datetime.now(UTC)
            if is_live
            else datetime(2026, 8, 17, tzinfo=UTC)
        ),
        engine_version=engine_version
        or ("claude-code:unknown" if is_live else "ses-fake-develop:1"),
        model_id=(live_config.model.model_id if live_config else "deterministic-fake"),
    )
