"""Shopping metrics projected from the canonical paired-comparison rows."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path, PurePosixPath

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    PairedCaseResult,
    SchemaVersion,
    Trace,
    artifact_json_bytes,
)
from ses.contracts.shopping import (
    MeasurementLevel,
    ShoppingMetricProjection,
    ShoppingPairMetrics,
    ShoppingPairStratumMetrics,
    ShoppingScenario,
    ShopSimulatorEpisodeResult,
)


def _resolve_verified(root: Path, reference: ArtifactRef) -> Path:
    if reference.root is not ArtifactRoot.RUN:
        raise ValueError("shopping pair evidence must use the run artifact root")
    path = root / reference.path
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("shopping pair evidence escapes its experiment root") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("shopping pair evidence must be a regular file")
    reference.verify_bytes(path.read_bytes())
    return path


def _global_ref(run_id: str, reference: ArtifactRef) -> ArtifactRef:
    return reference.model_copy(update={"path": f"{run_id}/{reference.path}"})


def _load_trace(root: Path, reference: ArtifactRef) -> Trace:
    return Trace.model_validate_json(_resolve_verified(root, reference).read_bytes())


def _load_result_and_metric(
    root: Path,
    reference: ArtifactRef,
    *,
    case_id: str,
    expected_profile_sha256: str,
    expected_model_lock_sha256: str,
    expected_protocol_sha256: str,
    expected_measurement_level: MeasurementLevel,
    expected_skill_sha256: str,
) -> tuple[ShopSimulatorEpisodeResult, ShoppingMetricProjection]:
    parts = PurePosixPath(reference.path).parts
    if len(parts) < 2:
        raise ValueError("shopping domain result is missing its run prefix")
    run_id = parts[0]
    result = ShopSimulatorEpisodeResult.model_validate_json(
        _resolve_verified(root, reference).read_bytes()
    )
    if (
        result.run_id != run_id
        or result.case_id != case_id
        or result.iteration_id != "iteration-0"
    ):
        raise ValueError("shopping domain result identity does not match its pair row")
    if (
        result.profile_sha256 != expected_profile_sha256
        or result.model_lock_sha256 != expected_model_lock_sha256
        or result.protocol_sha256 != expected_protocol_sha256
        or result.measurement_level is not expected_measurement_level
        or result.skill_sha256 != expected_skill_sha256
    ):
        raise ValueError("shopping domain result drifted from the locked pair protocol")
    metric_ref = _global_ref(run_id, result.metric)
    metric = ShoppingMetricProjection.model_validate_json(
        _resolve_verified(root, metric_ref).read_bytes()
    )
    if metric.safety_violation_count != result.safety_violation_count:
        raise ValueError("shopping metric and episode safety counts disagree")
    return result, metric


def _verify_result_refs(
    result: ShopSimulatorEpisodeResult,
    *,
    trace: ArtifactRef | None,
    grade: ArtifactRef | None,
) -> None:
    if trace != _global_ref(result.run_id, result.traces[0]):
        raise ValueError("shopping Pair Trace does not match its episode result")
    if grade != _global_ref(result.run_id, result.grade):
        raise ValueError("shopping Pair grade does not match its episode result")


def write_shopping_pair_metrics(
    *,
    experiment_root: Path,
    output_path: Path,
    pair_execution_sha256: str,
    rows: tuple[PairedCaseResult, ...],
    task_scenarios: Mapping[str, ShoppingScenario],
    profile_sha256: str,
    model_lock_sha256: str,
    protocol_sha256: str,
    measurement_level: MeasurementLevel,
    baseline_skill_sha256: str,
    skill_sha256: str,
    cost_currency: str,
) -> ArtifactRef:
    """Write a typed projection while keeping Pair rows as the truth source."""

    if set(task_scenarios) != {row.case_id for row in rows}:
        raise ValueError("shopping pair scenarios must cover every canonical pair row")
    root = experiment_root.resolve()
    try:
        relative_output = output_path.resolve(strict=False).relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(
            "shopping pair metrics output escapes experiment root"
        ) from exc
    if output_path.exists():
        raise ValueError("shopping pair metrics output already exists")

    metrics_by_scenario: dict[
        ShoppingScenario,
        list[tuple[ShoppingMetricProjection, ShoppingMetricProjection]],
    ] = defaultdict(list)
    case_counts = {scenario: 0 for scenario in ShoppingScenario}
    for row in rows:
        scenario = task_scenarios[row.case_id]
        case_counts[scenario] += 1
        baseline_result = skill_result = None
        baseline_metric = skill_metric = None
        if row.baseline_domain_result is not None:
            baseline_result, baseline_metric = _load_result_and_metric(
                root,
                row.baseline_domain_result,
                case_id=row.case_id,
                expected_profile_sha256=profile_sha256,
                expected_model_lock_sha256=model_lock_sha256,
                expected_protocol_sha256=protocol_sha256,
                expected_measurement_level=measurement_level,
                expected_skill_sha256=baseline_skill_sha256,
            )
        if row.skill_domain_result is not None:
            skill_result, skill_metric = _load_result_and_metric(
                root,
                row.skill_domain_result,
                case_id=row.case_id,
                expected_profile_sha256=profile_sha256,
                expected_model_lock_sha256=model_lock_sha256,
                expected_protocol_sha256=protocol_sha256,
                expected_measurement_level=measurement_level,
                expected_skill_sha256=skill_sha256,
            )
        if baseline_result is not None:
            _verify_result_refs(
                baseline_result,
                trace=row.baseline_trace,
                grade=row.baseline_grade,
            )
            if baseline_result.scenario is not scenario:
                raise ValueError(
                    "baseline shopping scenario does not match its locked task"
                )
        if skill_result is not None:
            _verify_result_refs(
                skill_result,
                trace=row.skill_trace,
                grade=row.skill_grade,
            )
            if skill_result.scenario is not scenario:
                raise ValueError(
                    "Skill shopping scenario does not match its locked task"
                )
        if not row.comparable:
            continue
        if (
            baseline_result is None
            or skill_result is None
            or baseline_metric is None
            or skill_metric is None
        ):
            raise ValueError("comparable shopping pair row lacks domain metrics")
        if baseline_result.episode_nonce == skill_result.episode_nonce:
            raise ValueError("shopping pair must allocate fresh episodes on both sides")
        if row.baseline_trace is None or row.skill_trace is None:
            raise ValueError("comparable shopping pair row lacks Trace evidence")
        baseline_trace = _load_trace(root, row.baseline_trace)
        skill_trace = _load_trace(root, row.skill_trace)
        if (
            baseline_trace.trace_id == skill_trace.trace_id
            or baseline_trace.session_id == skill_trace.session_id
        ):
            raise ValueError(
                "shopping pair must use fresh Trace and session identities"
            )
        assert row.baseline_domain_result is not None
        assert row.skill_domain_result is not None
        baseline_workspace = (
            root / row.baseline_domain_result.path
        ).parent / "workspace"
        skill_workspace = (root / row.skill_domain_result.path).parent / "workspace"
        if (
            baseline_workspace.resolve() == skill_workspace.resolve()
            or baseline_workspace.is_symlink()
            or skill_workspace.is_symlink()
            or not baseline_workspace.is_dir()
            or not skill_workspace.is_dir()
        ):
            raise ValueError("shopping Pair requires distinct fresh workspaces")
        metrics_by_scenario[scenario].append((baseline_metric, skill_metric))

    strata = tuple(
        _stratum_metrics(
            scenario,
            case_count=case_counts[scenario],
            pairs=metrics_by_scenario[scenario],
        )
        for scenario in ShoppingScenario
    )
    comparable_count = sum(row.comparable is True for row in rows)
    baseline_strict_total = sum(
        (
            baseline.r_strict
            for pairs in metrics_by_scenario.values()
            for baseline, _ in pairs
        ),
        Decimal(0),
    )
    skill_strict_total = sum(
        (
            skill.r_strict
            for pairs in metrics_by_scenario.values()
            for _, skill in pairs
        ),
        Decimal(0),
    )
    metrics = ShoppingPairMetrics(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shopping_pair_metrics",
        pair_execution_sha256=pair_execution_sha256,
        profile_sha256=profile_sha256,
        case_count=len(rows),
        comparable_case_count=comparable_count,
        baseline_full_success_count=sum(
            row.baseline_full_success_count for row in strata
        ),
        skill_full_success_count=sum(row.skill_full_success_count for row in strata),
        baseline_mean_strict_reward=(
            baseline_strict_total / comparable_count if comparable_count else Decimal(0)
        ),
        skill_mean_strict_reward=(
            skill_strict_total / comparable_count if comparable_count else Decimal(0)
        ),
        baseline_safety_violation_count=sum(
            row.baseline_safety_violation_count for row in strata
        ),
        skill_safety_violation_count=sum(
            row.skill_safety_violation_count for row in strata
        ),
        baseline_cost_amount=sum(
            (row.baseline_cost_amount for row in rows), Decimal(0)
        ),
        skill_cost_amount=sum((row.skill_cost_amount for row in rows), Decimal(0)),
        cost_delta_amount=(
            sum((row.skill_cost_amount for row in rows), Decimal(0))
            - sum((row.baseline_cost_amount for row in rows), Decimal(0))
        ),
        cost_currency=cost_currency,
        strata=strata,
    )
    payload = artifact_json_bytes(metrics)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=relative_output,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _stratum_metrics(
    scenario: ShoppingScenario,
    *,
    case_count: int,
    pairs: list[tuple[ShoppingMetricProjection, ShoppingMetricProjection]],
) -> ShoppingPairStratumMetrics:
    comparable_count = len(pairs)
    return ShoppingPairStratumMetrics(
        scenario=scenario,
        case_count=case_count,
        comparable_case_count=comparable_count,
        baseline_full_success_count=sum(baseline.course_pass for baseline, _ in pairs),
        skill_full_success_count=sum(skill.course_pass for _, skill in pairs),
        baseline_mean_strict_reward=(
            sum((baseline.r_strict for baseline, _ in pairs), Decimal(0))
            / comparable_count
            if comparable_count
            else Decimal(0)
        ),
        skill_mean_strict_reward=(
            sum((skill.r_strict for _, skill in pairs), Decimal(0)) / comparable_count
            if comparable_count
            else Decimal(0)
        ),
        baseline_safety_violation_count=sum(
            baseline.safety_violation_count for baseline, _ in pairs
        ),
        skill_safety_violation_count=sum(
            skill.safety_violation_count for _, skill in pairs
        ),
    )
