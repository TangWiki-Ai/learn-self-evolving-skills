#!/usr/bin/env python3
"""Build replayed evidence from nine trajectories at a pinned STATE-Bench commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from ses.contracts import (  # noqa: E402
    ArtifactRef,
    ArtifactRoot,
    AssertionResult,
    CaseGrade,
    CompletedPayload,
    CreatorReplayCall,
    CreatorSourceProvenance,
    CreatorSourceReplay,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    EvidenceRef,
    GradeStatus,
    JudgeKind,
    RecordType,
    SchemaVersion,
    StateChange,
    StateDiff,
    TextDeltaPayload,
    ToolCallPayload,
    ToolResultPayload,
    Trace,
    artifact_json_bytes,
)
from ses.skills.seeds import CreatorSeedProjection  # noqa: E402

REPOSITORY = "https://github.com/microsoft/STATE-Bench"
COMMIT = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
SOURCE_VERSION = f"state-bench:{COMMIT}:creator-audit-v4-pending"
SEEDS = (
    "2-return_defective_electronics",
    "3-hard_voluntary_store_credit_return_method_update",
    "8-return_high_value",
    "4-return_promo_recalculation",
    "6-return_wrong_item",
    "66-challenge_seasonal_electronics",
    "69-challenge_silver_restock_plus_shipping",
    "10-return_full_order",
    "58-challenge_store_credit_only_pushback",
)
PROJECTIONS = (
    CreatorSeedProjection(
        scenario="defective electronics return",
        reusable_steps=(
            "inspect the order and classify the defect",
            "read the current return policy",
            "preview the return and explain the outcome",
            "confirm only after approval",
            "verify the completed return",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "process_return preview",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="changed-mind return with store-credit destination",
        reusable_steps=(
            "inspect the order and requested refund destination",
            "read both return and refund policies",
            "preview the return and refund-method update",
            "get explicit approval before either mutation",
            "confirm both operations and verify the destination",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "get_policies",
            "process_return preview",
            "process_refund preview",
            "process_return confirm",
            "process_refund confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="high-value return with membership waiver",
        reusable_steps=(
            "inspect the order and membership-sensitive facts",
            "read the current return policy",
            "preview all deductions and waivers",
            "confirm only the approved return",
            "verify the completed amount and state",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "process_return preview",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="promotional order return recalculation",
        reusable_steps=(
            "inspect the full order and promotion",
            "read the return policy",
            "preview the item-specific recalculation",
            "explain the computed adjustment before consent",
            "confirm the same item and verify the result",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "process_return preview",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="wrong-item shipment return",
        reusable_steps=(
            "inspect the order and received product facts",
            "read the relevant return and refund policies",
            "preserve the fulfillment-error classification",
            "preview before changing state",
            "confirm after approval and verify the outcome",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "get_policies",
            "get_policies",
            "get_product_details",
            "process_return preview",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="seasonal return-window extension",
        reusable_steps=(
            "inspect purchase and delivery timing",
            "read the seasonal extension policy",
            "preview eligibility and deductions",
            "confirm only after approval",
            "verify the completed return",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "process_return preview",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="return with stacked fee adjustments",
        reusable_steps=(
            "inspect the order and customer tier",
            "read every applicable return rule",
            "preview each fee and discount separately",
            "explain the net amount before consent",
            "confirm and verify all adjustments",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "process_return preview",
            "get_customer",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="complete multi-item order return",
        reusable_steps=(
            "inspect every item in the requested order",
            "read the current return policy",
            "preview each item without skipping lines",
            "confirm every approved item",
            "verify the order is fully returned",
        ),
        tool_sequence=(
            "get_order",
            "get_policies",
            "process_return preview",
            "process_return confirm",
            "process_return preview",
            "process_return preview",
            "process_return confirm",
            "process_return confirm",
        ),
    ),
    CreatorSeedProjection(
        scenario="store-credit-only return under customer pushback",
        reusable_steps=(
            "inspect order timing and customer status",
            "read return and refund policies",
            "preview the permitted refund destination",
            "hold the policy boundary when challenged",
            "confirm and verify the store-credit result",
        ),
        tool_sequence=(
            "get_order",
            "get_customer",
            "get_policies",
            "get_policies",
            "process_return preview",
            "process_return confirm",
        ),
    ),
)
EPOCH = datetime(2026, 8, 17, tzinfo=UTC)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return cast(dict[str, Any], value)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(path: Path, value: object) -> bytes:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _record(path: Path, value: object) -> bytes:
    payload = artifact_json_bytes(value)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _ref(path: str, payload: bytes) -> ArtifactRef:
    return ArtifactRef(root=ArtifactRoot.RUN, path=path, sha256=_sha256(payload))


def _git(source_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _verify_source_root(source_root: Path, selected: tuple[Path, ...]) -> None:
    if _git(source_root, "rev-parse", "HEAD") != COMMIT:
        raise ValueError("STATE-Bench source is not at the pinned commit")
    origin = _git(source_root, "remote", "get-url", "origin")
    normalized = origin.removesuffix(".git").replace(
        "git@github.com:", "https://github.com/"
    )
    if normalized != REPOSITORY:
        raise ValueError("STATE-Bench source has an unexpected origin")
    relative = tuple(path.relative_to(source_root).as_posix() for path in selected)
    if _git(source_root, "status", "--porcelain", "--", *relative):
        raise ValueError("selected STATE-Bench source files contain local changes")


def _trace(seed_id: str, trajectory: dict[str, Any]) -> Trace:
    conversation = trajectory["conversation"]
    opening = next(row["content"] for row in conversation if row.get("role") == "user")
    request_id = f"{seed_id}-request"
    calls = [
        call
        for message in conversation
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    request = EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id=request_id,
        prompt=opening,
        allowed_tools=tuple(dict.fromkeys(str(call["name"]) for call in calls)),
        timeout_seconds=300,
    )
    events: list[EngineEvent] = []

    def add(payload: object) -> None:
        sequence = len(events)
        events.append(
            EngineEvent(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type=RecordType.ENGINE_EVENT,
                event_id=f"{seed_id}-event-{sequence:03d}",
                request_id=request_id,
                sequence=sequence,
                occurred_at=EPOCH + timedelta(seconds=sequence),
                payload=payload,
            )
        )

    call_index = 0
    for message in conversation:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            tool_call_id = f"{seed_id}-tool-{call_index:03d}"
            add(
                ToolCallPayload(
                    message_id=f"{seed_id}-message-{call_index:03d}",
                    tool_call_id=tool_call_id,
                    tool_name=call["name"],
                    arguments=call["arguments"],
                )
            )
            add(
                ToolResultPayload(
                    tool_call_id=tool_call_id,
                    content=call["result"],
                    is_error=False,
                )
            )
            call_index += 1
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            add(
                TextDeltaPayload(
                    message_id=f"{seed_id}-assistant-{len(events):03d}",
                    text=content,
                )
            )
    session_id = f"{seed_id}-state-bench-session"
    add(CompletedPayload(exit_status=EngineExitStatus.SUCCESS, session_id=session_id))
    return Trace(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TRACE,
        trace_id=f"{seed_id}-trace",
        run_id="run-creator-seed-audit",
        case_id=seed_id,
        iteration_id="iteration-0",
        session_id=session_id,
        request=request,
        events=tuple(events),
        exit_status=EngineExitStatus.SUCCESS,
    )


def _pointer(*tokens: str) -> str:
    return "/" + "/".join(
        token.replace("~", "~0").replace("/", "~1") for token in tokens
    )


def _ses_diff(seed_id: str, upstream: Any) -> StateDiff:
    created = cast(dict[str, dict[str, Any]], upstream.created)
    modified = cast(dict[str, dict[str, Any]], upstream.modified)
    deleted = cast(dict[str, dict[str, Any]], upstream.deleted)
    added = {
        _pointer(entity, record_id): record
        for entity, records in created.items()
        for record_id, record in records.items()
    }
    removed = {
        _pointer(entity, record_id): record
        for entity, records in deleted.items()
        for record_id, record in records.items()
    }
    changed = {
        _pointer(entity, record_id, field): StateChange(
            before=values["old"], after=values["new"]
        )
        for entity, records in modified.items()
        for record_id, fields in records.items()
        for field, values in fields.items()
    }
    if not (added or removed or changed):
        raise ValueError(f"replayed creator trajectory has no state change: {seed_id}")
    return StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id=f"{seed_id}-state-diff",
        before_snapshot_id=f"{seed_id}-before",
        after_snapshot_id=f"{seed_id}-after",
        added=added,
        removed=removed,
        changed=changed,
        summary=(
            "Computed from before and after snapshots of the pinned trajectory replay."
        ),
    )


def _replay(
    *,
    source_root: Path,
    seed_id: str,
    source_ref: ArtifactRef,
    task_path: Path,
    env_path: Path,
    trajectory: dict[str, Any],
) -> tuple[CreatorSourceReplay, StateDiff]:
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from state_bench.domains.customer_support.environment import (  # type: ignore[import-not-found]
        CustomerSupportEnvironment,
    )
    from state_bench.domains.customer_support.schemas import (  # type: ignore[import-not-found]
        CSEnvironmentData,
    )
    from state_bench.schemas import (  # type: ignore[import-not-found]
        StateDiff as UpstreamStateDiff,
    )
    from state_bench.schemas import TaskDefinition  # type: ignore[import-not-found]
    from state_bench.scoring import (  # type: ignore[import-not-found]
        evaluate_state_requirements,
    )

    task = TaskDefinition.load(task_path)
    environment = CustomerSupportEnvironment(CSEnvironmentData.load(env_path), task.now)
    before = environment.get_full_snapshot()
    calls: list[CreatorReplayCall] = []
    source_calls = [
        call
        for message in trajectory["conversation"]
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    for sequence, call in enumerate(source_calls):
        name = str(call["name"])
        handler = environment.tool_handlers.get(name)
        if handler is None:
            raise ValueError(f"pinned trajectory names an unknown tool: {name}")
        actual = handler(call["arguments"])
        expected_bytes = _json_bytes(call["result"])
        actual_bytes = _json_bytes(actual)
        if expected_bytes != actual_bytes:
            raise ValueError(f"pinned trajectory replay mismatch: {seed_id}:{name}")
        calls.append(
            CreatorReplayCall(
                sequence=sequence,
                tool_name=name,
                arguments=call["arguments"],
                expected_result_sha256=_sha256(expected_bytes),
                actual_result_sha256=_sha256(actual_bytes),
                matched=True,
            )
        )
    after = environment.get_full_snapshot()
    upstream_diff = UpstreamStateDiff.compute(before, after)
    score = evaluate_state_requirements(task, upstream_diff)
    if score is None or score.score != 1:
        reason = "missing state score" if score is None else score.reasoning
        raise ValueError(f"pinned trajectory failed state requirements: {reason}")
    replay = CreatorSourceReplay(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="creator_source_replay",
        seed_id=seed_id,
        source=source_ref,
        before_snapshot_sha256=_sha256(_json_bytes(before)),
        after_snapshot_sha256=_sha256(_json_bytes(after)),
        upstream_state_diff_sha256=_sha256(_json_bytes(upstream_diff.to_dict())),
        state_score=1,
        state_reason=score.reasoning,
        calls=tuple(calls),
    )
    return replay, _ses_diff(seed_id, upstream_diff)


def build(source_root: Path, output: Path) -> None:
    tasks = source_root / "state_bench/domains/customer_support/tasks"
    envs = source_root / "state_bench/domains/customer_support/task_envs"
    trajectories = source_root / "datasets/train_task_trajectories/customer_support"
    source_paths = tuple(
        path
        for source_id in SEEDS
        for path in (
            tasks / f"{source_id}.json",
            envs / f"{source_id}.json",
            trajectories / f"{source_id}.json",
        )
    )
    _verify_source_root(source_root, source_paths)
    previous: dict[str, dict[str, object]] = {}
    packet_path = output / "review-packet.json"
    if packet_path.is_file():
        prior = _json(packet_path)
        previous = {
            str(row["seed_id"]): row
            for row in prior.get("records", [])
            if isinstance(row, dict) and "seed_id" in row
        }
    packet: list[dict[str, object]] = []
    for index, (source_id, projection) in enumerate(
        zip(SEEDS, PROJECTIONS, strict=True), 1
    ):
        seed_id = f"creator-seed-{index:03d}"
        task_path = tasks / f"{source_id}.json"
        env_path = envs / f"{source_id}.json"
        trajectory_path = trajectories / f"{source_id}.json"
        task = _json(task_path)
        trajectory = _json(trajectory_path)
        if task.get("task_type") != "return_item":
            raise ValueError(f"not a return_item creator source: {source_id}")

        source_record = CreatorSourceProvenance(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="creator_source_provenance",
            repository=REPOSITORY,
            commit=COMMIT,
            task_id=source_id,
            task_sha256=_sha256(task_path.read_bytes()),
            environment_sha256=_sha256(env_path.read_bytes()),
            trajectory_sha256=_sha256(trajectory_path.read_bytes()),
        )
        source_rel = f"private/sources/source-{index:03d}.json"
        source_payload = _record(output / source_rel, source_record)
        source_ref = _ref(source_rel, source_payload)
        replay, diff = _replay(
            source_root=source_root,
            seed_id=seed_id,
            source_ref=source_ref,
            task_path=task_path,
            env_path=env_path,
            trajectory=trajectory,
        )
        replay_rel = f"private/replays/replay-{index:03d}.json"
        replay_payload = _record(output / replay_rel, replay)
        replay_ref = _ref(replay_rel, replay_payload)
        trace = _trace(seed_id, trajectory)
        trace_rel = f"private/traces/trace-{index:03d}.json"
        diff_rel = f"private/state-diffs/state-diff-{index:03d}.json"
        trace_payload = _record(output / trace_rel, trace)
        diff_payload = _record(output / diff_rel, diff)
        trace_ref = _ref(trace_rel, trace_payload)
        diff_ref = _ref(diff_rel, diff_payload)
        bucket = "changed" if diff.changed else "added" if diff.added else "removed"
        assertion = AssertionResult(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ASSERTION_RESULT,
            assertion_id=f"{seed_id}-replayed-state",
            judge=JudgeKind.STATE,
            judge_version="state-bench-replay-state-requirements-v1",
            required=True,
            status=GradeStatus.PASS,
            reason=replay.state_reason,
            evidence=(EvidenceRef(artifact=diff_ref, json_pointer=f"/{bucket}"),),
        )
        grade = CaseGrade(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.CASE_GRADE,
            grade_id=f"{seed_id}-state-grade",
            run_id="run-creator-seed-audit",
            case_id=seed_id,
            iteration_id="iteration-0",
            status=GradeStatus.PASS,
            assertions=(assertion,),
        )
        grade_rel = f"private/judges/state/state-grade-{index:03d}.json"
        grade_payload = _record(output / grade_rel, grade)
        projection_rel = f"projections/seed-{index:03d}.json"
        projection_payload = _write(
            output / projection_rel, projection.model_dump(mode="json")
        )
        row: dict[str, object] = {
            "seed_id": seed_id,
            "source_id": source_id,
            "scenario": projection.scenario,
            "source": source_ref.model_dump(mode="json"),
            "replay": replay_ref.model_dump(mode="json"),
            "trace": trace_ref.model_dump(mode="json"),
            "state_diff": diff_ref.model_dump(mode="json"),
            "state_grade": _ref(grade_rel, grade_payload).model_dump(mode="json"),
            "projection": _ref(projection_rel, projection_payload).model_dump(
                mode="json"
            ),
            "model_judge": {"status": "pending"},
            "course_attestation": {"status": "course_authored_pending_human_review"},
        }
        prior_row = previous.get(seed_id)
        if prior_row is not None and all(
            prior_row.get(name) == row[name]
            for name in (
                "source",
                "replay",
                "trace",
                "state_diff",
                "state_grade",
                "projection",
            )
        ):
            prior_judge = prior_row.get("model_judge")
            if (
                isinstance(prior_judge, dict)
                and prior_judge.get("status") == "pass"
                and prior_judge.get("prompt_version") == "rubric-prompt-v3"
                and prior_judge.get("extractor_version") == "evidence-extractor-v2"
                and prior_judge.get("rubric_version") == "2.0.0"
            ):
                row["model_judge"] = prior_judge
        packet.append(row)
    _write(
        packet_path,
        {
            "schema_version": "v1alpha1",
            "record_type": "creator_seed_review_packet",
            "source_version": SOURCE_VERSION,
            "source_repository": REPOSITORY,
            "source_commit": COMMIT,
            "review_status": "course_authored_pending_human_review",
            "review_packet": "docs/release/human-review-packet.md",
            "records": packet,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source_root.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
