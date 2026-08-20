"""Package and install only the Registry-current, final-qualified Skill."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import TypeVar

from ses.automation.orchestrator import final_execution_run_set_sha256
from ses.contracts import (
    CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256,
    AcceptedSkillReleaseManifest,
    ArtifactRef,
    ArtifactRoot,
    AutoEvolveConfig,
    AutoEvolveState,
    AutoLoopStatus,
    CapstoneFinalReceipt,
    CaseGrade,
    FinalAggregateReport,
    FinalConsumedCheckpoint,
    FinalLifecycle,
    FinalRunReceipt,
    GateDecision,
    GateOutcome,
    GateStepStatus,
    OpaqueProtectedSplitLock,
    SchemaVersion,
    ShoppingFinalScenarioMetrics,
    SplitLockFormat,
    ToolCallPayload,
    Trace,
    VersionedRecord,
    VersionStatus,
    artifact_json_bytes,
    content_sha256,
)
from ses.contracts.runner import RunEventType, RunnerStatus, RunRecord
from ses.contracts.shopping import (
    MeasurementLevel,
    PurchaseAttemptReceipt,
    RawShopSimulatorReward,
    ShoppingActionKind,
    ShoppingActionReceipt,
    ShoppingMetricProjection,
    ShoppingScenario,
    ShopSimulatorEpisodeResult,
)
from ses.evolution.registry import RegistryError, RegistryState, SkillRegistry
from ses.shopping.grading import build_shopping_case_grade, project_shopping_metrics
from ses.shopping.safety import assess_purchase_safety
from ses.skills.installer import (
    SkillInstallation,
    install_skill,
    load_skill_manifest,
    normalized_skill_sha256,
)

_RELEASE_MANIFEST = "release-manifest.json"
_RUNTIME_MANIFEST = "skill-manifest.json"
_RUNTIME_DIRECTORY = "skill"
_Record = TypeVar("_Record", bound=VersionedRecord)


class AcceptedSkillReleaseError(ValueError):
    """The requested package or installation is not release-eligible."""


def _workspace_root(path: Path) -> Path:
    if ".." in path.parts or path.is_symlink() or not path.is_dir():
        raise AcceptedSkillReleaseError("workspace root must be a canonical directory")
    absolute = path.absolute()
    resolved = path.resolve()
    if absolute != resolved:
        raise AcceptedSkillReleaseError("workspace root must be a canonical directory")
    return resolved


def _workspace_file(root: Path, path: Path, *, label: str) -> Path:
    if ".." in path.parts or path.is_symlink() or not path.is_file():
        raise AcceptedSkillReleaseError(f"{label} must be a canonical regular file")
    absolute = path.absolute()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AcceptedSkillReleaseError(
            f"{label} must be inside the workspace"
        ) from exc
    if absolute != resolved:
        raise AcceptedSkillReleaseError(f"{label} cannot use symlink ancestors")
    return resolved


def _workspace_ref(root: Path, path: Path, *, label: str) -> ArtifactRef:
    resolved = _workspace_file(root, path, label=label)
    payload = resolved.read_bytes()
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=resolved.relative_to(root).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _path_from_ref(root: Path, reference: ArtifactRef, *, label: str) -> Path:
    if reference.root is not ArtifactRoot.WORKSPACE:
        raise AcceptedSkillReleaseError(f"{label} must use the workspace artifact root")
    path = _workspace_file(root, root / reference.path, label=label)
    try:
        reference.verify_bytes(path.read_bytes())
    except ValueError as exc:
        raise AcceptedSkillReleaseError(f"{label} hash mismatch") from exc
    return path


def _canonical_record(path: Path, model: type[_Record], *, label: str) -> _Record:
    try:
        payload = path.read_bytes()
        record = model.model_validate_json(payload)
        if artifact_json_bytes(record) != payload:
            raise ValueError("record is not canonical JSON")
    except (OSError, ValueError) as exc:
        raise AcceptedSkillReleaseError(f"{label} is invalid") from exc
    return record


def _verify_private_final_evidence(
    workspace: Path,
    aggregate_path: Path,
    *,
    aggregate: FinalAggregateReport,
    final: CapstoneFinalReceipt,
    run_receipt: FinalRunReceipt,
    checkpoint: FinalConsumedCheckpoint,
) -> None:
    private_path = _workspace_file(
        workspace,
        aggregate_path.parent / "private-results.json",
        label="private final evidence",
    )
    try:
        private_bytes = private_path.read_bytes()
        private = json.loads(private_bytes)
        if not isinstance(private, dict):
            raise ValueError("private final evidence must be an object")
        canonical = json.dumps(
            private,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if canonical != private_bytes:
            raise ValueError("private final evidence is not canonical JSON")
        case_passes = private.get("case_passes")
        details = private.get("details")
        safety = private.get("safety_violation_count")
        if (
            not isinstance(case_passes, list)
            or aggregate.case_count != 12
            or len(case_passes) != aggregate.case_count
            or any(type(value) is not bool for value in case_passes)
            or not isinstance(details, dict)
            or type(safety) is not int
            or safety < 0
            or private.get("experiment_id") != aggregate.experiment_id
            or private.get("subject_skill_sha256") != aggregate.subject_skill_sha256
            or private.get("executed_at")
            != aggregate.executed_at.isoformat().replace("+00:00", "Z")
            or sum(case_passes) != aggregate.pass_count
            or safety != aggregate.safety_violation_count
        ):
            raise ValueError("private final evidence differs from its aggregate")
        run_set_sha256 = final_execution_run_set_sha256(
            case_passes=tuple(case_passes),
            private_payload=details,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise AcceptedSkillReleaseError("private final evidence is invalid") from exc
    private_sha256 = hashlib.sha256(private_bytes).hexdigest()
    if (
        aggregate.private_results_sha256 != private_sha256
        or run_receipt.private_results_sha256 != private_sha256
        or checkpoint.private_results_sha256 != private_sha256
        or run_receipt.run_set_sha256 != run_set_sha256
    ):
        raise AcceptedSkillReleaseError(
            "private final evidence differs from its protocol receipts"
        )
    _verify_private_episode_results(
        workspace,
        case_passes=tuple(case_passes),
        details=details,
        aggregate=aggregate,
        final=final,
        run_receipt=run_receipt,
    )


def _verify_private_episode_results(
    workspace: Path,
    *,
    case_passes: tuple[bool, ...],
    details: dict[object, object],
    aggregate: FinalAggregateReport,
    final: CapstoneFinalReceipt,
    run_receipt: FinalRunReceipt,
) -> None:
    rows = details.get("episode_results")
    expected_measurement = (
        MeasurementLevel.SYNTHETIC_OFFLINE
        if aggregate.mode == "fixed"
        else MeasurementLevel.LIVE_MEASURED
    )
    if (
        details.get("experiment_id") != aggregate.experiment_id
        or details.get("subject_skill_sha256") != aggregate.subject_skill_sha256
        or details.get("measurement_kind") != aggregate.measurement_kind.value
        or details.get("result_source") != aggregate.result_source
        or details.get("final_manifest_sha256") != aggregate.final_lock_sha256
        or not isinstance(details.get("run_id"), str)
        or not details["run_id"]
        or not isinstance(rows, list)
        or len(rows) != aggregate.case_count
    ):
        raise AcceptedSkillReleaseError("private final episode evidence is invalid")

    observed: list[tuple[ShopSimulatorEpisodeResult, ShoppingMetricProjection]] = []
    case_ids: set[str] = set()
    episode_paths: set[Path] = set()
    metric_paths: set[Path] = set()
    run_roots: set[Path] = set()
    inventory: dict[str, tuple[ArtifactRef, ShopSimulatorEpisodeResult]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "case_id",
            "episode_result",
            "episode_result_sha256",
            "metric",
            "metric_sha256",
        }:
            raise AcceptedSkillReleaseError("private final episode evidence is invalid")
        values = (
            row.get("case_id"),
            row.get("episode_result"),
            row.get("episode_result_sha256"),
            row.get("metric"),
            row.get("metric_sha256"),
        )
        if not all(isinstance(value, str) for value in values):
            raise AcceptedSkillReleaseError("private final episode evidence is invalid")
        case_id, episode_relative, episode_sha256, metric_relative, metric_sha256 = (
            str(value) for value in values
        )
        episode_path = _workspace_file(
            workspace,
            workspace / episode_relative,
            label="private final episode result",
        )
        metric_path = _workspace_file(
            workspace,
            workspace / metric_relative,
            label="private final shopping metric",
        )
        episode_bytes = episode_path.read_bytes()
        metric_bytes = metric_path.read_bytes()
        if (
            hashlib.sha256(episode_bytes).hexdigest() != episode_sha256
            or hashlib.sha256(metric_bytes).hexdigest() != metric_sha256
            or case_id in case_ids
            or episode_path in episode_paths
            or metric_path in metric_paths
        ):
            raise AcceptedSkillReleaseError("private final episode evidence is invalid")
        episode = _canonical_record(
            episode_path,
            ShopSimulatorEpisodeResult,
            label="private final episode result",
        )
        metric = _canonical_record(
            metric_path,
            ShoppingMetricProjection,
            label="private final shopping metric",
        )
        run_root = _artifact_base(metric_path, episode.metric)
        run_roots.add(run_root)
        episode_ref = ArtifactRef(
            root=ArtifactRoot.RUN,
            path=episode_path.relative_to(run_root).as_posix(),
            sha256=episode_sha256,
        )
        if (
            episode.case_id != case_id
            or episode.run_id != details["run_id"]
            or episode.profile_sha256 != final.profile_sha256
            or episode.skill_sha256 != aggregate.subject_skill_sha256
            or episode.measurement_level is not expected_measurement
            or episode.network_used != aggregate.network_used
            or episode.model_lock_sha256 != run_receipt.model_lock_sha256
            or episode.protocol_sha256 != run_receipt.evaluation_protocol_sha256
            or episode.metric.root is not ArtifactRoot.RUN
            or episode.metric.sha256 != metric_sha256
            or episode.safety_violation_count != metric.safety_violation_count
            or metric.course_pass is not case_passes[index]
        ):
            raise AcceptedSkillReleaseError(
                "private final episode evidence differs from its run receipt"
            )
        case_ids.add(case_id)
        episode_paths.add(episode_path)
        metric_paths.add(metric_path)
        observed.append((episode, metric))
        inventory[case_id] = (episode_ref, episode)
        _verify_episode_artifact_closure(
            workspace,
            run_root=run_root,
            episode_path=episode_path,
            episode=episode,
            metric=metric,
        )

    derived_scenarios = tuple(
        ShoppingFinalScenarioMetrics(
            scenario=scenario,
            case_count=3,
            full_success_count=sum(
                metric.course_pass
                for episode, metric in observed
                if episode.scenario is scenario
            ),
            mean_strict_reward=(
                sum(
                    (
                        metric.r_strict
                        for episode, metric in observed
                        if episode.scenario is scenario
                    ),
                    Decimal(0),
                )
                / 3
            ),
            safety_violation_count=sum(
                metric.safety_violation_count
                for episode, metric in observed
                if episode.scenario is scenario
            ),
        )
        for scenario in ShoppingScenario
    )
    if (
        any(
            sum(episode.scenario is scenario for episode, _ in observed) != 3
            for scenario in ShoppingScenario
        )
        or derived_scenarios != aggregate.scenario_metrics
        or sum(metric.course_pass for _, metric in observed)
        != aggregate.full_success_count
        or sum(metric.safety_violation_count for _, metric in observed)
        != aggregate.safety_violation_count
        or sum((metric.r_strict for _, metric in observed), Decimal(0))
        / aggregate.case_count
        != aggregate.mean_strict_reward
    ):
        raise AcceptedSkillReleaseError(
            "private final episode metrics differ from the aggregate"
        )
    if len(run_roots) != 1:
        raise AcceptedSkillReleaseError(
            "private final episodes do not belong to one canonical run"
        )
    _verify_final_run_inventory(
        workspace,
        run_root=next(iter(run_roots)),
        details=details,
        inventory=inventory,
        observed=observed,
    )


def _artifact_base(path: Path, reference: ArtifactRef) -> Path:
    if reference.root is not ArtifactRoot.RUN:
        raise AcceptedSkillReleaseError("private final RUN artifact root is invalid")
    base = path
    for part in reversed(PurePosixPath(reference.path).parts):
        if base.name != part:
            raise AcceptedSkillReleaseError(
                "private final artifact path differs from its RUN reference"
            )
        base = base.parent
    return base


def _resolve_ref(
    workspace: Path,
    *,
    base: Path,
    reference: ArtifactRef,
    root: ArtifactRoot,
    label: str,
) -> Path:
    if reference.root is not root:
        raise AcceptedSkillReleaseError(f"{label} uses the wrong artifact root")
    path = _workspace_file(workspace, base / reference.path, label=label)
    try:
        reference.verify_bytes(path.read_bytes())
    except ValueError as exc:
        raise AcceptedSkillReleaseError(f"{label} hash mismatch") from exc
    return path


def _canonical_json(path: Path, *, label: str) -> object:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
        canonical = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if canonical != payload:
            raise ValueError("not canonical")
        return value
    except (OSError, TypeError, ValueError) as exc:
        raise AcceptedSkillReleaseError(f"{label} is invalid") from exc


def _verify_local_json_ref(
    workspace: Path,
    *,
    attempt_root: Path,
    reference: ArtifactRef,
    label: str,
) -> object:
    path = _resolve_ref(
        workspace,
        base=attempt_root,
        reference=reference,
        root=ArtifactRoot.WORKSPACE,
        label=label,
    )
    return _canonical_json(path, label=label)


def _verify_episode_artifact_closure(
    workspace: Path,
    *,
    run_root: Path,
    episode_path: Path,
    episode: ShopSimulatorEpisodeResult,
    metric: ShoppingMetricProjection,
) -> None:
    attempt_root = episode_path.parent
    traces: list[Trace] = []
    for reference in episode.traces:
        trace_path = _resolve_ref(
            workspace,
            base=run_root,
            reference=reference,
            root=ArtifactRoot.RUN,
            label="private final Trace",
        )
        trace = _canonical_record(trace_path, Trace, label="private final Trace")
        if trace.run_id != episode.run_id or trace.case_id != episode.case_id:
            raise AcceptedSkillReleaseError(
                "private final Trace differs from its episode"
            )
        traces.append(trace)

    action_receipts: list[ShoppingActionReceipt] = []
    for reference in episode.action_receipts:
        receipt_path = _resolve_ref(
            workspace,
            base=run_root,
            reference=reference,
            root=ArtifactRoot.RUN,
            label="private final action receipt",
        )
        receipt = _canonical_record(
            receipt_path,
            ShoppingActionReceipt,
            label="private final action receipt",
        )
        if receipt.episode_nonce != episode.episode_nonce:
            raise AcceptedSkillReleaseError(
                "private final action receipt differs from its episode"
            )
        intent = _verify_local_json_ref(
            workspace,
            attempt_root=attempt_root,
            reference=receipt.intent,
            label="private final action intent",
        )
        result = _verify_local_json_ref(
            workspace,
            attempt_root=attempt_root,
            reference=receipt.result,
            label="private final action result",
        )
        expected_tool = f"mcp__shop_simulator__{receipt.action_kind.value}"
        expected_arguments = {
            name: value
            for name, value in receipt.request.model_dump(mode="json").items()
            if name != "kind" and value is not None
        }
        trace_index = receipt.turn_sequence - 1
        matching_calls = (
            tuple(
                event.payload
                for event in traces[trace_index].events
                if isinstance(event.payload, ToolCallPayload)
                and event.payload.tool_name == expected_tool
                and event.payload.arguments == expected_arguments
            )
            if 0 <= trace_index < len(traces)
            else ()
        )
        if (
            not isinstance(intent, dict)
            or intent.get("schema_version") != "v1alpha1"
            or intent.get("record_type") != "shopping_action_intent"
            or intent.get("episode_nonce") != receipt.episode_nonce
            or intent.get("turn_lease_id") != receipt.turn_lease_id
            or intent.get("turn_sequence") != receipt.turn_sequence
            or intent.get("observation_sha256") != receipt.observation_sha256
            or intent.get("request") != receipt.request.model_dump(mode="json")
            or not isinstance(result, dict)
            or result.get("schema_version") != "v1alpha1"
            or result.get("record_type") != "shopping_episode_step"
            or result.get("episode_nonce") != receipt.episode_nonce
            or result.get("sequence") != receipt.turn_sequence
            or result.get("terminal") is not receipt.step_terminal
            or len(matching_calls) != 1
        ):
            raise AcceptedSkillReleaseError(
                "private final action closure differs from its Trace"
            )
        action_receipts.append(receipt)
    if len(action_receipts) != len(traces) or tuple(
        receipt.turn_sequence for receipt in action_receipts
    ) != tuple(range(1, len(action_receipts) + 1)):
        raise AcceptedSkillReleaseError(
            "private final action receipts do not form a one-action-per-turn trace"
        )

    raw: RawShopSimulatorReward | None = None
    if episode.raw_reward is not None:
        raw_path = _resolve_ref(
            workspace,
            base=run_root,
            reference=episode.raw_reward,
            root=ArtifactRoot.RUN,
            label="private final raw reward",
        )
        raw = _canonical_record(
            raw_path,
            RawShopSimulatorReward,
            label="private final raw reward",
        )
    if metric.raw_reward != episode.raw_reward:
        raise AcceptedSkillReleaseError(
            "private final metric raw reward differs from its episode"
        )

    grade_path = _resolve_ref(
        workspace,
        base=run_root,
        reference=episode.grade,
        root=ArtifactRoot.RUN,
        label="private final CaseGrade",
    )
    grade = _canonical_record(grade_path, CaseGrade, label="private final CaseGrade")
    if (
        grade.run_id != episode.run_id
        or grade.case_id != episode.case_id
        or grade.iteration_id != episode.iteration_id
        or grade.shopping_metric != episode.metric
        or grade.shopping_raw_reward != episode.raw_reward
        or grade.safety_violation_count != metric.safety_violation_count
        or (grade.status.value == "pass") is not metric.course_pass
        or not grade.shopping_safety_evidence
    ):
        raise AcceptedSkillReleaseError(
            "private final CaseGrade differs from its episode and metric"
        )
    reproduced_codes = _verify_safety_closure(
        workspace,
        run_root=run_root,
        attempt_root=attempt_root,
        episode=episode,
        metric=metric,
        grade=grade,
        raw=raw,
        action_receipts=tuple(action_receipts),
    )
    reproduced_metric = project_shopping_metrics(
        raw=raw,
        raw_reward_ref=episode.raw_reward,
        purchased_asin=None,
        private_goal_asin=None,
        safety_violation_count=len(reproduced_codes),
    )
    if any(
        getattr(metric, field) != getattr(reproduced_metric, field)
        for field in (
            "projection_version",
            "formula_sha256",
            "raw_reward",
            "r_loose",
            "r_type",
            "r_att",
            "r_option",
            "r_price",
            "r_strict",
            "r_succ",
            "benchmark_success",
            "safety_violation_count",
            "course_pass",
        )
    ):
        raise AcceptedSkillReleaseError(
            "private final shopping metric cannot be reproduced"
        )
    reproduced_grade = build_shopping_case_grade(
        run_id=episode.run_id,
        case_id=episode.case_id,
        iteration_id=episode.iteration_id,
        metric=metric,
        metric_ref=episode.metric,
        safety_evidence=grade.shopping_safety_evidence,
        violation_codes=reproduced_codes,
    )
    if grade != reproduced_grade:
        raise AcceptedSkillReleaseError("private final CaseGrade cannot be reproduced")


def _verify_safety_closure(
    workspace: Path,
    *,
    run_root: Path,
    attempt_root: Path,
    episode: ShopSimulatorEpisodeResult,
    metric: ShoppingMetricProjection,
    grade: CaseGrade,
    raw: RawShopSimulatorReward | None,
    action_receipts: tuple[ShoppingActionReceipt, ...],
) -> tuple[str, ...]:
    safety_ref = grade.shopping_safety_evidence[0]
    safety_path = _resolve_ref(
        workspace,
        base=run_root,
        reference=safety_ref,
        root=ArtifactRoot.RUN,
        label="private final shopping safety evidence",
    )
    safety = _canonical_json(
        safety_path,
        label="private final shopping safety evidence",
    )
    if not isinstance(safety, dict) or set(safety) != {
        "authorization_evidence_complete",
        "offer_evidence_complete",
        "purchase_attempts",
        "purchase_count",
        "record_type",
        "schema_version",
        "violation_codes",
    }:
        raise AcceptedSkillReleaseError(
            "private final shopping safety evidence is invalid"
        )
    if (
        not isinstance(safety["purchase_attempts"], list)
        or not isinstance(safety["violation_codes"], list)
        or any(not isinstance(code, str) for code in safety["violation_codes"])
    ):
        raise AcceptedSkillReleaseError(
            "private final shopping safety evidence is invalid"
        )
    try:
        purchase_refs = tuple(
            ArtifactRef.model_validate(value) for value in safety["purchase_attempts"]
        )
        codes = tuple(str(code) for code in safety["violation_codes"])
    except (TypeError, ValueError) as exc:
        raise AcceptedSkillReleaseError(
            "private final shopping safety evidence is invalid"
        ) from exc
    if grade.shopping_safety_evidence != (safety_ref, *purchase_refs):
        raise AcceptedSkillReleaseError(
            "private final CaseGrade safety inventory is incomplete"
        )
    attempts: list[PurchaseAttemptReceipt] = []
    for reference in purchase_refs:
        attempt_path = _resolve_ref(
            workspace,
            base=run_root,
            reference=reference,
            root=ArtifactRoot.RUN,
            label="private final purchase attempt",
        )
        attempt = _canonical_record(
            attempt_path,
            PurchaseAttemptReceipt,
            label="private final purchase attempt",
        )
        _verify_local_json_ref(
            workspace,
            attempt_root=attempt_root,
            reference=attempt.intent,
            label="private final purchase intent",
        )
        _verify_local_json_ref(
            workspace,
            attempt_root=attempt_root,
            reference=attempt.result,
            label="private final purchase result",
        )
        for authorization_ref in attempt.authorization_message_refs:
            _verify_local_json_ref(
                workspace,
                attempt_root=attempt_root,
                reference=authorization_ref,
                label="private final purchase authorization message",
            )
        attempts.append(attempt)

    purchase_actions = tuple(
        receipt
        for receipt in action_receipts
        if receipt.action_kind is ShoppingActionKind.PURCHASE
    )
    if len(purchase_actions) != len(attempts) or any(
        action.episode_nonce != attempt.episode_nonce
        or action.turn_lease_id != attempt.turn_lease_id
        or action.turn_sequence != attempt.turn_sequence
        or action.observation_sha256 != attempt.observation_sha256
        or action.intent != attempt.intent
        or action.result != attempt.result
        or action.request.action_id != attempt.action_id
        or attempt.protocol_sha256
        != hashlib.sha256(b"ses-shopping-purchase-v1").hexdigest()
        for action, attempt in zip(purchase_actions, attempts, strict=True)
    ):
        raise AcceptedSkillReleaseError(
            "private final purchase attempts differ from gateway actions"
        )
    assessment = assess_purchase_safety(
        tuple(attempts),
        raw_reward=raw,
        expectation=None,
    )
    recomputed = set(assessment.violation_codes)
    reported = set(codes)
    private_only = {"constraint_lost", "option_mismatch"}
    if (
        safety.get("schema_version") != "v1alpha1"
        or safety.get("record_type") != "shopping_safety_evidence"
        or safety.get("purchase_count") != len(attempts)
        or len(codes) != len(reported)
        or not recomputed.issubset(reported)
        or not (reported - recomputed).issubset(private_only)
        or safety.get("authorization_evidence_complete")
        != assessment.public_evidence["authorization_evidence_complete"]
        or safety.get("offer_evidence_complete")
        != assessment.public_evidence["offer_evidence_complete"]
        or len(codes) != metric.safety_violation_count
        or episode.safety_violation_count != len(codes)
    ):
        raise AcceptedSkillReleaseError(
            "private final safety cannot be reproduced from purchase attempts"
        )
    return codes


def _verify_final_run_inventory(
    workspace: Path,
    *,
    run_root: Path,
    details: dict[object, object],
    inventory: dict[str, tuple[ArtifactRef, ShopSimulatorEpisodeResult]],
    observed: list[tuple[ShopSimulatorEpisodeResult, ShoppingMetricProjection]],
) -> None:
    events_path = _workspace_file(
        workspace,
        run_root / "events.jsonl",
        label="private final run events",
    )
    events_bytes = events_path.read_bytes()
    if run_root.name != details.get("run_id") or hashlib.sha256(
        events_bytes
    ).hexdigest() != details.get("events_sha256"):
        raise AcceptedSkillReleaseError(
            "private final run event hash differs from its receipt"
        )
    records: list[RunRecord] = []
    try:
        lines = events_bytes.splitlines()
        for line in lines:
            record = RunRecord.model_validate_json(line)
            if artifact_json_bytes(record) != line:
                raise ValueError("non-canonical run record")
            records.append(record)
    except ValueError as exc:
        raise AcceptedSkillReleaseError("private final run events are invalid") from exc
    attempts = {
        record.case_id: record
        for record in records
        if record.event_type.value == "attempt"
    }
    metric_by_case = {episode.case_id: metric for episode, metric in observed}
    if (
        len(records) != len(inventory) + 1
        or not records
        or records[0].event_type is not RunEventType.RUN_STARTED
        or records[0].run_id != run_root.name
        or records[0].config is None
        or records[0].config.case_ids != tuple(inventory)
        or any(record.event_type is not RunEventType.ATTEMPT for record in records[1:])
        or any(record.run_id != run_root.name for record in records)
        or len(attempts) != len(inventory)
        or set(attempts) != set(inventory)
        or tuple(record.sequence for record in records) != tuple(range(len(records)))
    ):
        raise AcceptedSkillReleaseError("private final run inventory is incomplete")
    for case_id, (episode_ref, episode) in inventory.items():
        record = attempts[case_id]
        metric = metric_by_case[case_id]
        expected_status = (
            RunnerStatus.PASS if metric.course_pass else RunnerStatus.AGENT_FAIL
        )
        if (
            record.run_id != episode.run_id
            or record.iteration_id != episode.iteration_id
            or record.status is not expected_status
            or record.artifacts.domain_result != episode_ref
            or record.artifacts.shopping_metric != episode.metric
            or record.artifacts.shopping_raw_reward != episode.raw_reward
            or record.artifacts.grade != episode.grade
            or record.artifacts.traces != episode.traces
            or record.artifacts.shopping_safety_evidence
            != _load_case_grade_safety_inventory(
                workspace,
                run_root=run_root,
                reference=episode.grade,
            )
            or record.artifacts.shopping_action_receipts != episode.action_receipts
        ):
            raise AcceptedSkillReleaseError(
                "private final RunRecord differs from its artifact graph"
            )


def _load_case_grade_safety_inventory(
    workspace: Path,
    *,
    run_root: Path,
    reference: ArtifactRef,
) -> tuple[ArtifactRef, ...]:
    grade_path = _resolve_ref(
        workspace,
        base=run_root,
        reference=reference,
        root=ArtifactRoot.RUN,
        label="private final CaseGrade",
    )
    return _canonical_record(
        grade_path,
        CaseGrade,
        label="private final CaseGrade",
    ).shopping_safety_evidence


def _verify_terminal_auto_evolve_state(
    workspace: Path,
    *,
    final: CapstoneFinalReceipt,
    aggregate: FinalAggregateReport,
    run_receipt: FinalRunReceipt,
) -> None:
    state_path = _workspace_file(
        workspace,
        workspace / "state.json",
        label="auto-evolve state",
    )
    state = _canonical_record(
        state_path,
        AutoEvolveState,
        label="auto-evolve state",
    )
    config_path = _workspace_file(
        workspace,
        workspace / "config.json",
        label="auto-evolve config",
    )
    config = _canonical_record(
        config_path,
        AutoEvolveConfig,
        label="auto-evolve config",
    )
    final_lock_path = _workspace_file(
        workspace,
        workspace / "protected/final-lock.json",
        label="protected final lock",
    )
    final_lock = _canonical_record(
        final_lock_path,
        OpaqueProtectedSplitLock,
        label="protected final lock",
    )
    final_lock_sha256 = hashlib.sha256(final_lock_path.read_bytes()).hexdigest()
    if (
        state.status is not AutoLoopStatus.FINAL_COMPLETE
        or state.experiment_id != final.experiment_id
        or state.experiment_id != config.experiment_id
        or state.config_sha256 != content_sha256(config)
        or state.current_accepted_skill_sha256 != final.subject_skill_sha256
        or state.final_report != final.aggregate
        or state.final_cost_amount != aggregate.cost_amount
        or state.final_cost_complete != aggregate.cost_complete
        or state.final_input_tokens != aggregate.input_tokens
        or state.final_output_tokens != aggregate.output_tokens
        or state.cost_currency != aggregate.cost_currency
        or config.final_lifecycle is not FinalLifecycle.INDEPENDENT_CAPSTONE
        or config.split_lock_format is not SplitLockFormat.CONTENT_ADDRESSED
        or config.profile_sha256 != final.profile_sha256
        or config.mode != aggregate.mode
        or config.final_lock_sha256 != aggregate.final_lock_sha256
        or config.final_lock_sha256 != final_lock_sha256
        or config.final_engine_id != run_receipt.engine_id
        or config.final_simulator_id != run_receipt.simulator_id
        or config.final_judge_id != run_receipt.judge_id
        or config.final_provider_id != run_receipt.provider_id
        or config.final_report_protocol_sha256 != CAPSTONE_FINAL_REPORT_PROTOCOL_SHA256
        or config.final_report_protocol_sha256 != run_receipt.report_protocol_sha256
        or final_lock.experiment_id != final.experiment_id
        or final_lock.profile_sha256 != final.profile_sha256
        or final_lock.mode != aggregate.mode
        or final_lock.measurement_kind is not aggregate.measurement_kind
        or final_lock.split != "final"
        or final_lock.case_count != aggregate.case_count
    ):
        raise AcceptedSkillReleaseError(
            "auto-evolve state is not a release-eligible terminal state"
        )


def _verify_capstone_final(
    workspace: Path,
    path: Path,
    *,
    state: RegistryState,
    expected_profile_sha256: str | None = None,
) -> CapstoneFinalReceipt:
    final = _canonical_record(
        path, CapstoneFinalReceipt, label="capstone final receipt"
    )
    if (
        final.subject_skill_sha256 != state.current_accepted_sha256
        or final.lineage_id != state.lineage_id
    ):
        raise AcceptedSkillReleaseError(
            "capstone final receipt does not match the current accepted Skill"
        )
    if (
        expected_profile_sha256 is not None
        and final.profile_sha256 != expected_profile_sha256
    ):
        raise AcceptedSkillReleaseError(
            "capstone final receipt does not match the selected profile"
        )
    if final.safety_violation_count != 0:
        raise AcceptedSkillReleaseError("final safety violations block release")

    aggregate_path = _path_from_ref(workspace, final.aggregate, label="final aggregate")
    run_receipt_path = _path_from_ref(
        workspace, final.final_run_receipt, label="final run receipt"
    )
    checkpoint_path = _path_from_ref(
        workspace, final.one_time_checkpoint, label="final one-time checkpoint"
    )
    aggregate = _canonical_record(
        aggregate_path, FinalAggregateReport, label="final aggregate"
    )
    run_receipt = _canonical_record(
        run_receipt_path, FinalRunReceipt, label="final run receipt"
    )
    checkpoint = _canonical_record(
        checkpoint_path, FinalConsumedCheckpoint, label="final one-time checkpoint"
    )
    aggregate_sha256 = hashlib.sha256(aggregate_path.read_bytes()).hexdigest()
    run_receipt_sha256 = hashlib.sha256(run_receipt_path.read_bytes()).hexdigest()
    expected_mode = (
        "fixed" if final.result_origin == "fresh_fixed_execution" else "live"
    )
    expected_source = (
        "fresh_fixed_execution"
        if final.result_origin == "fresh_fixed_execution"
        else "canonical_live"
    )
    expected_network = expected_mode == "live"
    if (
        aggregate.schema_version is not SchemaVersion.V1ALPHA2
        or aggregate.safety_violation_count != final.safety_violation_count
        or not aggregate.scenario_metrics
        or aggregate.mode != expected_mode
        or run_receipt.mode != expected_mode
        or aggregate.result_source != expected_source
        or aggregate.network_used != expected_network
        or run_receipt.network_used != expected_network
    ):
        raise AcceptedSkillReleaseError(
            "capstone final origin does not match its execution evidence"
        )
    if (
        aggregate.experiment_id != final.experiment_id
        or run_receipt.experiment_id != final.experiment_id
        or checkpoint.experiment_id != final.experiment_id
        or aggregate.subject_skill_sha256 != final.subject_skill_sha256
        or run_receipt.subject_skill_sha256 != final.subject_skill_sha256
        or checkpoint.subject_skill_sha256 != final.subject_skill_sha256
        or aggregate.measurement_kind is not final.measurement_kind
        or run_receipt.measurement_kind is not final.measurement_kind
        or run_receipt.aggregate_report_sha256 != aggregate_sha256
        or checkpoint.aggregate_report_sha256 != aggregate_sha256
        or checkpoint.final_run_receipt_sha256 != run_receipt_sha256
        or aggregate.final_lock_sha256 != run_receipt.final_lock_sha256
        or checkpoint.final_lock_sha256 != run_receipt.final_lock_sha256
        or aggregate.private_results_sha256 != run_receipt.private_results_sha256
        or checkpoint.private_results_sha256 != run_receipt.private_results_sha256
        or aggregate.executed_at != run_receipt.executed_at
        or aggregate.cost_amount != run_receipt.cost_amount
        or aggregate.cost_currency != run_receipt.cost_currency
        or aggregate.cost_complete != run_receipt.cost_complete
        or aggregate.input_tokens != run_receipt.input_tokens
        or aggregate.output_tokens != run_receipt.output_tokens
    ):
        raise AcceptedSkillReleaseError("capstone final evidence is incomplete")
    if (
        aggregate.safety_violation_count is not None
        and aggregate.safety_violation_count != final.safety_violation_count
    ):
        raise AcceptedSkillReleaseError(
            "capstone final safety aggregate differs from its release receipt"
        )
    if expected_profile_sha256 is not None and (
        aggregate.schema_version is not SchemaVersion.V1ALPHA2
        or aggregate.full_success_count is None
        or aggregate.mean_strict_reward is None
        or aggregate.safety_violation_count is None
        or aggregate.scenario_metrics is None
    ):
        raise AcceptedSkillReleaseError(
            "shopping release requires the v1alpha2 final aggregate"
        )
    if aggregate.schema_version is SchemaVersion.V1ALPHA2:
        _verify_private_final_evidence(
            workspace,
            aggregate_path,
            aggregate=aggregate,
            final=final,
            run_receipt=run_receipt,
            checkpoint=checkpoint,
        )
        _verify_terminal_auto_evolve_state(
            workspace,
            final=final,
            aggregate=aggregate,
            run_receipt=run_receipt,
        )
    return final


def _accepted_gate(
    registry: SkillRegistry,
    state: RegistryState,
    *,
    workspace: Path,
) -> tuple[GateDecision, ArtifactRef]:
    current = state.versions.get(state.current_accepted_sha256)
    if (
        current is None
        or current.status is not VersionStatus.ACCEPTED
        or not current.verified
        or not current.was_current
        or current.gate_decision is None
    ):
        raise AcceptedSkillReleaseError(
            "current accepted Skill lacks complete Gate evidence"
        )
    gate_path = registry.root / current.gate_decision.path
    gate_ref = _workspace_ref(workspace, gate_path, label="current Gate decision")
    gate = _canonical_record(gate_path, GateDecision, label="current Gate decision")
    if (
        gate.outcome is not GateOutcome.ACCEPTED
        or gate.candidate_skill_sha256 != state.current_accepted_sha256
        or any(step.status is not GateStepStatus.PASS for step in gate.steps)
    ):
        raise AcceptedSkillReleaseError(
            "current accepted Skill lacks complete Gate evidence"
        )
    return gate, gate_ref


def _audit_registry(
    registry: SkillRegistry,
    *,
    workspace: Path,
) -> tuple[RegistryState, ArtifactRef, ArtifactRef]:
    try:
        state = registry.audit()
    except RegistryError as exc:
        raise AcceptedSkillReleaseError("Registry replay failed") from exc
    if registry.root != registry.root.resolve() or not registry.root.is_relative_to(
        workspace
    ):
        raise AcceptedSkillReleaseError("Registry must be inside the workspace")
    if not state.events:
        raise AcceptedSkillReleaseError("Registry replay produced no events")
    events_ref = _workspace_ref(
        workspace, registry.events_path, label="Registry events"
    )
    checkpoint_ref = _workspace_ref(
        workspace, registry.checkpoint_path, label="Registry checkpoint"
    )
    return state, events_ref, checkpoint_ref


def _safe_new_output(workspace: Path, output: Path) -> Path:
    if ".." in output.parts or output.exists() or output.is_symlink():
        raise AcceptedSkillReleaseError("release package output must not exist")
    absolute = output.absolute()
    resolved = output.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise AcceptedSkillReleaseError(
            "release package output must be inside the workspace"
        ) from exc
    if absolute != resolved:
        raise AcceptedSkillReleaseError("release package output cannot use symlinks")
    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or parent.absolute() != parent.resolve():
        raise AcceptedSkillReleaseError("release package parent cannot use symlinks")
    return resolved


def _expected_package_files(
    manifest: AcceptedSkillReleaseManifest,
) -> set[str]:
    return {
        _RELEASE_MANIFEST,
        f"{_RUNTIME_DIRECTORY}/{_RUNTIME_MANIFEST}",
        *(f"{_RUNTIME_DIRECTORY}/{item.path}" for item in manifest.runtime_files),
    }


def _verify_package_inventory(
    package_root: Path,
    manifest: AcceptedSkillReleaseManifest,
) -> None:
    actual: set[str] = set()
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise AcceptedSkillReleaseError("release package cannot contain symlinks")
        if path.is_file():
            actual.add(path.relative_to(package_root).as_posix())
    if actual != _expected_package_files(manifest):
        raise AcceptedSkillReleaseError(
            "release package inventory differs from its runtime allowlist"
        )


def package_current_accepted(
    *,
    workspace_root: Path,
    registry: SkillRegistry,
    capstone_final_receipt: Path,
    output: Path,
    released_at: datetime,
    expected_profile_sha256: str | None = None,
) -> AcceptedSkillReleaseManifest:
    """Build a release package from the replayed Registry current pointer."""

    workspace = _workspace_root(workspace_root)
    destination = _safe_new_output(workspace, output)
    state, events_ref, checkpoint_ref = _audit_registry(registry, workspace=workspace)
    _, gate_ref = _accepted_gate(registry, state, workspace=workspace)
    final_path = _workspace_file(
        workspace, capstone_final_receipt, label="capstone final receipt"
    )
    final = _verify_capstone_final(
        workspace,
        final_path,
        state=state,
        expected_profile_sha256=expected_profile_sha256,
    )
    current_source = registry.version_path(state.current_accepted_sha256)
    try:
        runtime_manifest = load_skill_manifest(current_source)
        current_sha256 = normalized_skill_sha256(current_source)
    except (OSError, ValueError) as exc:
        raise AcceptedSkillReleaseError("current accepted Skill is invalid") from exc
    if current_sha256 != state.current_accepted_sha256:
        raise AcceptedSkillReleaseError("current accepted Skill hash mismatch")

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        packaged_skill = temporary / _RUNTIME_DIRECTORY
        installation = install_skill(
            current_source,
            packaged_skill,
            version=runtime_manifest.version,
        )
        shutil.copyfile(
            current_source / _RUNTIME_MANIFEST,
            packaged_skill / _RUNTIME_MANIFEST,
            follow_symlinks=False,
        )
        os.chmod(packaged_skill / _RUNTIME_MANIFEST, 0o600)
        if installation.sha256 != state.current_accepted_sha256:
            raise AcceptedSkillReleaseError("release package hash mismatch")
        intended_runtime_manifest = destination / _RUNTIME_DIRECTORY / _RUNTIME_MANIFEST
        runtime_manifest_ref = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=intended_runtime_manifest.relative_to(workspace).as_posix(),
            sha256=hashlib.sha256(
                (packaged_skill / _RUNTIME_MANIFEST).read_bytes()
            ).hexdigest(),
        )
        final_ref = _workspace_ref(
            workspace, final_path, label="capstone final receipt"
        )
        release_identity = hashlib.sha256(
            (
                state.current_accepted_sha256
                + state.events[-1].event_sha256
                + final_ref.sha256
            ).encode("ascii")
        ).hexdigest()[:20]
        release = AcceptedSkillReleaseManifest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="accepted_skill_release_manifest",
            release_id=f"release-{release_identity}",
            registry_id=state.registry_id,
            lineage_id=state.lineage_id,
            accepted_skill_sha256=state.current_accepted_sha256,
            package_sha256=installation.sha256,
            current_event_sha256=state.events[-1].event_sha256,
            name=runtime_manifest.name,
            version=runtime_manifest.version,
            measurement_kind=final.measurement_kind,
            result_origin=final.result_origin,
            released_at=released_at,
            registry_events=events_ref,
            registry_checkpoint=checkpoint_ref,
            gate_decision=gate_ref,
            final_receipt=final_ref,
            runtime_manifest=runtime_manifest_ref,
            runtime_files=runtime_manifest.files,
        )
        (temporary / _RELEASE_MANIFEST).write_bytes(artifact_json_bytes(release))
        _verify_package_inventory(temporary, release)
        final_state, final_events_ref, final_checkpoint_ref = _audit_registry(
            registry, workspace=workspace
        )
        if (
            final_state.current_accepted_sha256 != state.current_accepted_sha256
            or final_state.events[-1].event_sha256 != state.events[-1].event_sha256
            or final_events_ref != events_ref
            or final_checkpoint_ref != checkpoint_ref
        ):
            raise AcceptedSkillReleaseError("Registry changed while packaging")
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return release


def _release_manifest(path: Path, *, workspace: Path) -> AcceptedSkillReleaseManifest:
    resolved = _workspace_file(workspace, path, label="accepted release manifest")
    if resolved.name != _RELEASE_MANIFEST:
        raise AcceptedSkillReleaseError(
            "installation requires an accepted release manifest"
        )
    return _canonical_record(
        resolved,
        AcceptedSkillReleaseManifest,
        label="accepted release manifest",
    )


def _registry_from_release(
    workspace: Path,
    release: AcceptedSkillReleaseManifest,
    *,
    supplied_registry: SkillRegistry | None = None,
) -> SkillRegistry:
    events_path = _path_from_ref(
        workspace, release.registry_events, label="release Registry events"
    )
    if events_path.name != "events.jsonl":
        raise AcceptedSkillReleaseError("release Registry event reference is invalid")
    checkpoint_path = _path_from_ref(
        workspace, release.registry_checkpoint, label="release Registry checkpoint"
    )
    if supplied_registry is not None:
        try:
            registry_root = supplied_registry.root.resolve(strict=True)
            supplied_checkpoint = supplied_registry.checkpoint_path.resolve(strict=True)
        except OSError as exc:
            raise AcceptedSkillReleaseError(
                "supplied Registry paths are invalid"
            ) from exc
        if (
            registry_root != events_path.parent
            or supplied_registry.registry_id != release.registry_id
            or supplied_checkpoint != checkpoint_path
        ):
            raise AcceptedSkillReleaseError(
                "supplied Registry does not match the accepted release"
            )
        return supplied_registry
    try:
        return SkillRegistry(
            events_path.parent,
            registry_id=release.registry_id,
            checkpoint_path=checkpoint_path,
        )
    except RegistryError as exc:
        raise AcceptedSkillReleaseError("release Registry identity is invalid") from exc


def install_current_accepted(
    *,
    workspace_root: Path,
    release_manifest: Path,
    destination: Path,
    registry: SkillRegistry | None = None,
    expected_profile_sha256: str | None = None,
) -> SkillInstallation:
    """Install only a verified accepted package into its name-derived directory."""

    workspace = _workspace_root(workspace_root)
    release_path = _workspace_file(
        workspace, release_manifest, label="accepted release manifest"
    )
    release = _release_manifest(release_path, workspace=workspace)
    package_root = release_path.parent
    packaged_skill = package_root / _RUNTIME_DIRECTORY
    expected_runtime_manifest = packaged_skill / _RUNTIME_MANIFEST
    expected_runtime_ref = _workspace_ref(
        workspace, expected_runtime_manifest, label="packaged runtime manifest"
    )
    if release.runtime_manifest != expected_runtime_ref:
        raise AcceptedSkillReleaseError(
            "release manifest points outside its packaged runtime"
        )
    _verify_package_inventory(package_root, release)

    registry = _registry_from_release(
        workspace,
        release,
        supplied_registry=registry,
    )
    state, events_ref, checkpoint_ref = _audit_registry(registry, workspace=workspace)
    if (
        state.registry_id != release.registry_id
        or state.lineage_id != release.lineage_id
        or state.current_accepted_sha256 != release.accepted_skill_sha256
        or state.events[-1].event_sha256 != release.current_event_sha256
        or events_ref != release.registry_events
        or checkpoint_ref != release.registry_checkpoint
    ):
        raise AcceptedSkillReleaseError(
            "release is not bound to the Registry current accepted Skill"
        )
    _, gate_ref = _accepted_gate(registry, state, workspace=workspace)
    if gate_ref != release.gate_decision:
        raise AcceptedSkillReleaseError("release Gate evidence does not match Registry")
    current = state.versions[state.current_accepted_sha256]
    registry_runtime_manifest = _workspace_ref(
        workspace,
        registry.root / current.manifest.path,
        label="Registry runtime manifest",
    )
    if registry_runtime_manifest.sha256 != release.runtime_manifest.sha256:
        raise AcceptedSkillReleaseError(
            "packaged runtime manifest differs from the Registry version"
        )
    final_path = _path_from_ref(
        workspace, release.final_receipt, label="release final receipt"
    )
    final = _verify_capstone_final(
        workspace,
        final_path,
        state=state,
        expected_profile_sha256=expected_profile_sha256,
    )
    if (
        final.measurement_kind is not release.measurement_kind
        or final.result_origin != release.result_origin
    ):
        raise AcceptedSkillReleaseError("release final measurement does not match")

    try:
        runtime_manifest = load_skill_manifest(packaged_skill)
        package_sha256 = normalized_skill_sha256(packaged_skill)
    except (OSError, ValueError) as exc:
        raise AcceptedSkillReleaseError("packaged runtime is invalid") from exc
    if (
        runtime_manifest.name != release.name
        or runtime_manifest.version != release.version
        or runtime_manifest.files != release.runtime_files
        or package_sha256 != release.package_sha256
        or package_sha256 != state.current_accepted_sha256
    ):
        raise AcceptedSkillReleaseError(
            "packaged runtime does not match the accepted release"
        )
    return install_skill(
        packaged_skill,
        destination / PurePosixPath(release.name),
        version=release.version,
    )


__all__ = [
    "AcceptedSkillReleaseError",
    "install_current_accepted",
    "package_current_accepted",
]
