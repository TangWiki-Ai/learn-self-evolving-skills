"""Verify mined signals before admitting executable cases to develop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    GradeStatus,
    ToolResultStatus,
    artifact_json_bytes,
)
from ses.contracts.security import validate_public_data
from ses.evaluation import aggregate_status, judge_state
from ses.evaluation.calibration import (
    execute_fixed_calibration,
    load_calibration_fixture,
)
from ses.shop import (
    CaseEnvironment,
    ReturnCaseFixture,
    ReturnReason,
    compute_return_policy,
    state_diff,
)
from ses.shop.fixture import PINNED_CASE_FIXTURE
from ses.testset.curation import (
    CurationBundle,
    CurationModel,
    FixedCurationModel,
    curate_sources,
    invocation_cost,
)

VARIANT_VERSION = "ses-controlled-variant-v1"
ORACLE_VERSION = "ses-shop-oracle-v1"
REPLAY_VERSION = "ses-environment-replay-v1"
CALIBRATION_VERSION = "ses-case-calibration-v1"
QUALIFICATION_VERSION = "ses-case-qualification-v2"


class PrivateModel(BaseModel):
    """Strict model for private records that intentionally contain oracle data."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CandidateSeed(PrivateModel):
    candidate_id: str
    source_id: str
    semantic_group_id: str
    flow: str
    subflow: str
    difficulty_bucket: Literal["hard", "medium", "easy"] | None = None
    public_intent: str


class VariantDimensions(PrivateModel):
    membership_tier: Literal["standard", "silver", "gold", "platinum"]
    has_prime_shipping: bool
    days_since_delivery: int = Field(ge=0, le=365)
    return_window_days: int = Field(ge=0, le=90)
    return_reason: ReturnReason
    price_minor: int = Field(gt=0)
    order_subtotal_minor: int = Field(gt=0)
    restocking_fee_pct: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _validate_supported_combination(self) -> VariantDimensions:
        if self.order_subtotal_minor < self.price_minor:
            raise ValueError("order subtotal cannot be lower than the item price")
        if self.return_reason is not ReturnReason.CHANGED_MIND and (
            self.restocking_fee_pct != 0
        ):
            raise ValueError(
                "restocking fee is supported only for changed-mind variants"
            )
        return self


class VariantPlan(PrivateModel):
    candidate_id: str
    dimensions: VariantDimensions


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class HumanReview(PrivateModel):
    case_id: str
    reviewed_hash: str
    decision: ReviewStatus
    reason: str
    reviewed_at: str | None = None
    reviewer: str | None = None

    @model_validator(mode="after")
    def _validate_review(self) -> HumanReview:
        if self.decision is ReviewStatus.PENDING:
            if self.reviewed_at is not None or self.reviewer is not None:
                raise ValueError("pending review cannot identify a reviewer or time")
        elif self.reviewed_at is None or self.reviewer is None:
            raise ValueError("completed review requires reviewer and reviewed_at")
        return self


class QualificationStage(StrEnum):
    CANDIDATE = "candidate"
    REPLAY_VERIFIED = "replay_verified"
    JUDGE_CALIBRATED = "judge_calibrated"
    HUMAN_REVIEW_PENDING = "human_review_pending"
    QUALIFIED = "qualified"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ControlledVariant:
    candidate: CandidateSeed
    dimensions: VariantDimensions
    fixture: ReturnCaseFixture
    lineage_hash: str


@dataclass(frozen=True, slots=True)
class VerifiedCase:
    variant: ControlledVariant
    expected_actions: tuple[tuple[str, Mapping[str, JsonValue]], ...]
    oracle: Mapping[str, object]
    replay: Mapping[str, object]
    calibration: Mapping[str, object]
    reviewed_hash: str


@dataclass(frozen=True, slots=True)
class QualificationSummary:
    output: Path
    candidate_count: int
    source_candidate_count: int
    selected_source_count: int
    qualified_count: int
    rejected_count: int
    pending_count: int
    data_version: str | None
    response_source: str
    network_used: bool
    live_provider_used: bool
    input_tokens: int
    output_tokens: int


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _plain(value: object) -> object:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, _canonical_bytes(value) + b"\n")


def _reference(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _artifact_ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def load_candidate_seeds(path: Path) -> tuple[CandidateSeed, ...]:
    rows = tuple(
        CandidateSeed.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not rows or len({row.candidate_id for row in rows}) != len(rows):
        raise ValueError("candidate seeds must be nonempty and unique")
    return rows


def load_variant_plan(path: Path) -> tuple[VariantPlan, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("variant plan must be a JSON list")
    rows = tuple(VariantPlan.model_validate(item) for item in raw)
    if not rows:
        raise ValueError("variant plan cannot be empty")
    return rows


def load_human_reviews(path: Path) -> Mapping[str, HumanReview]:
    if not path.exists():
        return {}
    rows = tuple(
        HumanReview.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len({row.case_id for row in rows}) != len(rows):
        raise ValueError("human review case IDs must be unique")
    return {row.case_id: row for row in rows}


def generate_controlled_variant(
    candidate: CandidateSeed,
    dimensions: VariantDimensions,
    *,
    base: ReturnCaseFixture = PINNED_CASE_FIXTURE,
    public_request_template: str | None = None,
) -> ControlledVariant:
    """Map one candidate signal to a schema-supported deterministic fixture."""

    identity = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "dimensions": dimensions.model_dump(mode="json"),
        "transformation_version": VARIANT_VERSION,
    }
    digest = _sha(identity)
    short = digest[:20]
    case_id = f"develop-return-{short}"
    reason_text = {
        ReturnReason.DEFECTIVE: "is defective",
        ReturnReason.WRONG_ITEM: "is the wrong item",
        ReturnReason.NOT_AS_DESCRIBED: "is not as described",
        ReturnReason.CHANGED_MIND: "is no longer wanted",
        ReturnReason.DAMAGED_IN_TRANSIT: "arrived damaged in transit",
    }[dimensions.return_reason]
    order_id = f"ORD-{digest[:8].upper()}"
    item_id = f"ITEM-{digest[8:16].upper()}"
    product_id = f"PROD-{digest[16:24].upper()}"
    customer_id = f"CUST-{digest[24:32].upper()}"
    delivery_at = base.task_now - timedelta(days=dimensions.days_since_delivery)
    template = public_request_template or (
        "The item in order {order_id} {reason}. "
        "Please check the return policy and complete the return if allowed."
    )
    try:
        user_prompt = template.format(order_id=order_id, reason=reason_text)
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid public request template") from exc
    fixture = ReturnCaseFixture.model_validate(
        {
            **base.model_dump(mode="json"),
            "fixture_id": f"fixture-{short}",
            "case_id": case_id,
            "source_id": candidate.source_id,
            "transformation_version": VARIANT_VERSION,
            "task_id": f"ticket07-{short}",
            "user_prompt": user_prompt,
            "product": {
                **base.product.model_dump(mode="json"),
                "product_id": product_id,
                "price": {"amount_minor": dimensions.price_minor, "currency": "USD"},
                "return_window_days": dimensions.return_window_days,
                "restocking_fee_pct": dimensions.restocking_fee_pct,
            },
            "order": {
                **base.order.model_dump(mode="json"),
                "order_id": order_id,
                "customer_id": customer_id,
                "delivery_at": delivery_at.isoformat().replace("+00:00", "Z"),
                "subtotal": {
                    "amount_minor": dimensions.order_subtotal_minor,
                    "currency": "USD",
                },
            },
            "item": {
                **base.item.model_dump(mode="json"),
                "item_id": item_id,
                "order_id": order_id,
                "product_id": product_id,
            },
            "customer": {
                **base.customer.model_dump(mode="json"),
                "customer_id": customer_id,
                "membership_tier": dimensions.membership_tier,
                "has_prime_shipping": dimensions.has_prime_shipping,
            },
        }
    )
    return ControlledVariant(candidate, dimensions, fixture, digest)


def _actions(
    fixture: ReturnCaseFixture, reason: ReturnReason, amount_minor: int
) -> tuple[tuple[str, Mapping[str, JsonValue]], ...]:
    return (
        ("get_order", {"order_id": fixture.order.order_id}),
        ("get_policies", {"topic": "return"}),
        ("process_return", {"item_id": fixture.item.item_id, "reason": reason.value}),
        (
            "process_return",
            {
                "item_id": fixture.item.item_id,
                "reason": reason.value,
                "confirm": True,
                "amount_minor": amount_minor,
            },
        ),
    )


def _execute_actions(
    fixture: ReturnCaseFixture,
    actions: Sequence[tuple[str, Mapping[str, JsonValue]]],
) -> tuple[object, object, object, tuple[Mapping[str, object], ...]]:
    environment = CaseEnvironment(fixture)
    try:
        before = environment.snapshot()
        results: list[Mapping[str, object]] = []
        for tool_name, arguments in actions:
            result = environment.execute(tool_name, arguments)
            results.append(cast(Mapping[str, object], result.model_dump(mode="json")))
            if result.status is not ToolResultStatus.SUCCESS:
                raise ValueError(f"standard operation failed at {tool_name}")
        after = environment.snapshot()
        return before, after, state_diff(before, after), tuple(results)
    finally:
        environment.close()


def _public_case(variant: ControlledVariant) -> dict[str, object]:
    payload = cast(
        dict[str, object], variant.fixture.case_definition().model_dump(mode="json")
    )
    validate_public_data(payload)
    return payload


def _verify_case(
    variant: ControlledVariant,
    root: Path,
    *,
    model_calibration_ref: Mapping[str, str],
) -> VerifiedCase:
    fixture = variant.fixture
    decision = compute_return_policy(fixture, variant.dimensions.return_reason)
    if not decision.eligible:
        raise ValueError("policy_denied")
    actions = _actions(
        fixture, variant.dimensions.return_reason, decision.refund_amount.amount_minor
    )
    before, after, diff, results = _execute_actions(fixture, actions)

    case_root = root / "private" / fixture.case_id
    before_path = case_root / "before.json"
    after_path = case_root / "after.json"
    diff_path = case_root / "state-diff.json"
    _write_bytes(before_path, artifact_json_bytes(cast(Any, before)))
    _write_bytes(after_path, artifact_json_bytes(cast(Any, after)))
    _write_bytes(diff_path, artifact_json_bytes(cast(Any, diff)))
    tool_path = case_root / "tool-results.json"
    _write_json(tool_path, results)

    oracle_input = {
        "membership_tier": variant.dimensions.membership_tier,
        "has_prime_shipping": variant.dimensions.has_prime_shipping,
        "days_since_delivery": variant.dimensions.days_since_delivery,
        "return_window_days": variant.dimensions.return_window_days,
        "return_reason": variant.dimensions.return_reason.value,
        "price_minor": variant.dimensions.price_minor,
        "order_subtotal_minor": variant.dimensions.order_subtotal_minor,
        "restocking_fee_pct": variant.dimensions.restocking_fee_pct,
    }
    oracle_body = {
        "oracle_input": oracle_input,
        "policy_version": fixture.policy_version,
        "expected_terminal_business_state": cast(Any, after).state,
        "expected_tool_action_constraints": [
            {"tool_name": name, "arguments": dict(arguments)}
            for name, arguments in actions
        ],
        "source_candidate": variant.candidate.candidate_id,
        "source_id": variant.candidate.source_id,
        "semantic_group_id": variant.candidate.semantic_group_id,
        "variant_lineage_hash": variant.lineage_hash,
        "transformation_version": VARIANT_VERSION,
        "oracle_version": ORACLE_VERSION,
    }
    oracle = {**oracle_body, "gold_hash": _sha(oracle_body)}
    oracle_path = case_root / "oracle.json"
    _write_json(oracle_path, oracle)

    replay_body = {
        "protocol_version": REPLAY_VERSION,
        "case_id": fixture.case_id,
        "policy_version": fixture.policy_version,
        "oracle_hash": oracle["gold_hash"],
        "status": "pass",
        "amount_reconciled": True,
        "terminal_state_reconciled": True,
        "artifacts": {
            "before": _reference(root, before_path),
            "after": _reference(root, after_path),
            "state_diff": _reference(root, diff_path),
            "tool_results": _reference(root, tool_path),
            "oracle": _reference(root, oracle_path),
        },
    }
    replay = {**replay_body, "replay_hash": _sha(replay_body)}
    replay_path = case_root / "replay.json"
    _write_json(replay_path, replay)

    evidence = _artifact_ref(root, diff_path)
    correct_assertions = judge_state(
        cast(Any, diff), cast(Any, diff), evidence_artifact=evidence
    )
    wrong_amount = decision.refund_amount.amount_minor + 1
    _, _, wrong_diff, _ = _execute_actions(
        fixture,
        _actions(fixture, variant.dimensions.return_reason, wrong_amount),
    )
    wrong_path = case_root / "deliberate-incorrect-state-diff.json"
    _write_bytes(wrong_path, artifact_json_bytes(cast(Any, wrong_diff)))
    wrong_assertions = judge_state(
        cast(Any, diff),
        cast(Any, wrong_diff),
        evidence_artifact=_artifact_ref(root, wrong_path),
    )
    insufficient_assertions = judge_state(
        cast(Any, diff), cast(Any, diff), evidence_artifact=None
    )
    statuses = {
        "deliberate_correct": aggregate_status(correct_assertions).value,
        "deliberate_incorrect": aggregate_status(wrong_assertions).value,
        "evidence_insufficient": aggregate_status(insufficient_assertions).value,
    }
    if statuses != {
        "deliberate_correct": GradeStatus.PASS.value,
        "deliberate_incorrect": GradeStatus.FAIL.value,
        "evidence_insufficient": GradeStatus.NOT_EVALUATED.value,
    }:
        raise ValueError("judge_calibration_mismatch")
    calibration = {
        "protocol_version": CALIBRATION_VERSION,
        "case_id": fixture.case_id,
        "statuses": statuses,
        "response_source": "fixed_response",
        "live_model_measured": False,
        "fixed_response_fixture": dict(model_calibration_ref),
        "assertions": {
            "deliberate_correct": [
                item.model_dump(mode="json") for item in correct_assertions
            ],
            "deliberate_incorrect": [
                item.model_dump(mode="json") for item in wrong_assertions
            ],
            "evidence_insufficient": [
                item.model_dump(mode="json") for item in insufficient_assertions
            ],
        },
    }
    calibration_path = case_root / "calibration.json"
    _write_json(calibration_path, calibration)

    reviewed_hash = _sha(
        {
            "case_definition": _public_case(variant),
            "variant_lineage_hash": variant.lineage_hash,
            "oracle_hash": oracle["gold_hash"],
            "replay_hash": replay["replay_hash"],
            "calibration_hash": _sha(calibration),
        }
    )
    return VerifiedCase(
        variant=variant,
        expected_actions=actions,
        oracle=oracle,
        replay=replay,
        calibration=calibration,
        reviewed_hash=reviewed_hash,
    )


def _copy_tree_atomic(source: Path, destination: Path) -> None:
    if destination.exists():
        backup = destination.with_name(destination.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        destination.replace(backup)
        try:
            source.replace(destination)
        except Exception:
            backup.replace(destination)
            raise
        shutil.rmtree(backup)
    else:
        source.replace(destination)


def _load_locked_manifest(path: Path) -> tuple[set[str], set[str], set[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("locked") is not True:
        raise ValueError("protected split manifest must be locked")
    records = raw.get("records")
    if not isinstance(records, list):
        raise ValueError("protected split manifest records must be a list")
    ids: set[str] = set()
    hashes: set[str] = set()
    semantic: set[str] = set()
    for item in records:
        if not isinstance(item, Mapping):
            raise ValueError("protected split record must be an object")
        ids.add(str(item["case_id"]))
        hashes.add(str(item["content_hash"]))
        semantic.add(str(item["semantic_group_id"]))
    return ids, hashes, semantic


def assert_split_safe(
    cases: Sequence[VerifiedCase], protected_manifests: Iterable[Path]
) -> None:
    """Fail before persistence on ID, public content, or semantic overlap."""

    protected_ids: set[str] = set()
    protected_hashes: set[str] = set()
    protected_semantic: set[str] = set()
    for path in protected_manifests:
        ids, hashes, semantic = _load_locked_manifest(path)
        protected_ids.update(ids)
        protected_hashes.update(hashes)
        protected_semantic.update(semantic)
    for case in cases:
        case_id = case.variant.fixture.case_id
        content_hash = _sha(_public_case(case.variant))
        semantic_group = case.variant.candidate.semantic_group_id
        if case_id in protected_ids:
            raise ValueError(f"split_id_conflict:{case_id}")
        if content_hash in protected_hashes:
            raise ValueError(f"split_content_conflict:{case_id}")
        if semantic_group in protected_semantic:
            raise ValueError(f"split_semantic_conflict:{case_id}")


def reject_protected_split_write(split: str) -> None:
    if split in {"selection", "final"}:
        raise PermissionError(f"split_write_protected:{split}")
    if split != "develop":
        raise ValueError(f"unsupported_split:{split}")


def public_role_view(case: VerifiedCase, role: str) -> Mapping[str, object]:
    """Expose only fields allowed to reach runtime roles or reports."""

    public = _public_case(case.variant)
    if role == "agent":
        return public
    if role == "report":
        return {
            "case_id": public["case_id"],
            "source_id": public["source_id"],
            "split": public["split"],
        }
    if role in {"creator", "updater"}:
        return {"case_id": public["case_id"], "split": public["split"]}
    raise ValueError(f"unknown_role:{role}")


def _persist_curation(
    root: Path, bundle: CurationBundle
) -> tuple[Path, Mapping[str, Mapping[str, object]]]:
    """Persist private source evidence and a public-safe reference manifest."""

    references: dict[str, Mapping[str, object]] = {}
    manifest_sources: list[dict[str, object]] = []
    for item in bundle.sources:
        source_id = item.source.source_id
        source_root = root / "private" / "curation" / _sha(source_id)[:20]
        source_path = source_root / "source-evidence.json"
        signals_path = source_root / "deterministic-signals.json"
        triage_path = source_root / "model-triage.json"
        _write_json(source_path, item.source.model_dump(mode="json"))
        _write_json(signals_path, item.signals.model_dump(mode="json"))
        _write_json(
            triage_path,
            {
                "decision": item.triage.model_dump(mode="json"),
                "invocation": item.triage_invocation.model_dump(mode="json"),
            },
        )
        rubric_ref: Mapping[str, str] | None = None
        if item.rubric_draft is not None and item.rubric_invocation is not None:
            rubric_path = source_root / "rubric-draft.json"
            _write_json(
                rubric_path,
                {
                    "draft": item.rubric_draft.model_dump(mode="json"),
                    "invocation": item.rubric_invocation.model_dump(mode="json"),
                    "status": "model_draft_requires_human_activation",
                },
            )
            rubric_ref = _reference(root, rubric_path)
        item_refs: dict[str, object] = {
            "source_evidence": _reference(root, source_path),
            "deterministic_signals": _reference(root, signals_path),
            "model_triage": _reference(root, triage_path),
            "rubric_draft": rubric_ref,
        }
        references[source_id] = item_refs
        manifest_sources.append(
            {
                "source_id": source_id,
                "source_kind": item.source.source_kind,
                "selected": item.selected,
                "selection_reason": item.selection_reason,
                "artifacts": item_refs,
            }
        )
    input_tokens, output_tokens, cost, cost_currency = invocation_cost(bundle)
    invocations = tuple(
        invocation
        for item in bundle.sources
        for invocation in (item.triage_invocation, item.rubric_invocation)
        if invocation is not None
    )
    response_sources = sorted({item.response_source.value for item in invocations})
    manifest_path = root / "curation-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": "v1alpha1",
            "record_type": "curation_manifest",
            "curation_version": bundle.curation_version,
            "source_candidate_count": len(bundle.sources),
            "selected_source_count": sum(item.selected for item in bundle.sources),
            "response_sources": response_sources,
            "network_used": bundle.network_used,
            "live_provider_used": bundle.live_provider_used,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_amount": str(cost),
                "cost_currency": cost_currency,
            },
            "sources": manifest_sources,
        },
    )
    return manifest_path, references


def _response_source(bundle: CurationBundle) -> str:
    values = {
        invocation.response_source.value
        for item in bundle.sources
        for invocation in (item.triage_invocation, item.rubric_invocation)
        if invocation is not None
    }
    if len(values) != 1:
        return "mixed"
    return next(iter(values))


def qualify_cases(
    *,
    candidate_path: Path,
    variant_plan_path: Path,
    reviews_path: Path,
    protected_manifests: Sequence[Path],
    model_calibration_fixture: Path,
    output: Path,
    split: str = "develop",
    source_evidence_path: Path | None = None,
    curation_fixture_path: Path | None = None,
    curation_model: CurationModel | None = None,
) -> QualificationSummary:
    """Run LLM-assisted candidate verification into one atomic output tree."""

    reject_protected_split_write(split)
    seeds = {item.candidate_id: item for item in load_candidate_seeds(candidate_path)}
    plans = load_variant_plan(variant_plan_path)
    reviews = load_human_reviews(reviews_path)
    for plan in plans:
        if plan.candidate_id not in seeds:
            raise ValueError(
                f"variant references unknown candidate:{plan.candidate_id}"
            )

    temp = output.with_name(output.name + ".building")
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    try:
        project_root = Path(__file__).resolve().parents[3]
        evidence_path = source_evidence_path or (
            project_root
            / "data"
            / "upstream"
            / "abcd"
            / "fixture"
            / "conversations.json"
        )
        active_curation_model = curation_model or FixedCurationModel.from_path(
            curation_fixture_path
            or project_root
            / "data"
            / "testset"
            / "ticket07"
            / "curation-responses.json"
        )
        source_ids = tuple(dict.fromkeys(seed.source_id for seed in seeds.values()))
        curation = asyncio.run(
            curate_sources(
                source_ids=source_ids,
                source_path=evidence_path,
                model=active_curation_model,
            )
        )
        curation_manifest_path, curation_refs = _persist_curation(temp, curation)
        curated_by_source = curation.by_source_id

        fixture = load_calibration_fixture(model_calibration_fixture)
        model_report = asyncio.run(execute_fixed_calibration(fixture))
        model_report_path = temp / "private" / "judge-model-calibration.json"
        _write_bytes(model_report_path, artifact_json_bytes(model_report))
        model_calibration_ref = _reference(temp, model_report_path)

        verified: list[VerifiedCase] = []
        failures: list[dict[str, object]] = []
        for plan in plans:
            candidate = seeds[plan.candidate_id]
            curated = curated_by_source[candidate.source_id]
            variant = generate_controlled_variant(
                candidate,
                plan.dimensions,
                public_request_template=(
                    curated.rubric_draft.public_request_template
                    if curated.rubric_draft is not None
                    else None
                ),
            )
            if not curated.selected:
                failures.append(
                    {
                        "case_id": variant.fixture.case_id,
                        "stage": QualificationStage.REJECTED.value,
                        "reason_code": "source_not_mappable",
                        "reason": curated.selection_reason,
                        "pipeline_version": QUALIFICATION_VERSION,
                        "input_artifacts": [
                            value
                            for value in curation_refs[candidate.source_id].values()
                            if value is not None
                        ],
                        "output_artifacts": [],
                    }
                )
                continue
            try:
                verified.append(
                    _verify_case(
                        variant,
                        temp,
                        model_calibration_ref=model_calibration_ref,
                    )
                )
            except ValueError as exc:
                failures.append(
                    {
                        "case_id": variant.fixture.case_id,
                        "stage": QualificationStage.REJECTED.value,
                        "reason_code": str(exc),
                        "reason": "automatic verification rejected the case",
                        "pipeline_version": QUALIFICATION_VERSION,
                        "input_artifacts": [],
                        "output_artifacts": [],
                    }
                )
        assert_split_safe(verified, protected_manifests)

        review_packet: list[dict[str, object]] = []
        qualifications: list[dict[str, object]] = list(failures)
        catalog_cases: list[dict[str, object]] = []
        qualified = rejected = pending = 0
        active_case_ids = {case.variant.fixture.case_id for case in verified}
        failed_case_ids = {str(item["case_id"]) for item in failures}
        for retired_case_id in sorted(set(reviews) - active_case_ids - failed_case_ids):
            retired_review = reviews[retired_case_id]
            if retired_review.decision is not ReviewStatus.REJECTED:
                raise ValueError(f"inactive review must be rejected:{retired_case_id}")
            qualifications.append(
                {
                    "case_id": retired_case_id,
                    "stage": QualificationStage.REJECTED.value,
                    "reason_code": "human_rejected_source_mapping",
                    "reason": retired_review.reason,
                    "review": retired_review.model_dump(mode="json"),
                    "input_artifacts": [],
                    "output_artifacts": [],
                    "pipeline_version": QUALIFICATION_VERSION,
                }
            )
            rejected += 1
        for case in verified:
            variant = case.variant
            case_id = variant.fixture.case_id
            curated = curated_by_source[variant.candidate.source_id]
            if curated.rubric_draft is None or curated.rubric_invocation is None:
                raise ValueError(f"selected source has no rubric draft:{case_id}")
            source_refs = curation_refs[variant.candidate.source_id]
            public_path = temp / "public" / "cases" / f"{case_id}.json"
            _write_json(public_path, _public_case(variant))
            fixture_path = (
                temp / "private" / "fixtures" / f"{variant.fixture.fixture_id}.json"
            )
            _write_json(fixture_path, variant.fixture.model_dump(mode="json"))
            review = reviews.get(case_id)
            if review is None:
                review = HumanReview(
                    case_id=case_id,
                    reviewed_hash=case.reviewed_hash,
                    decision=ReviewStatus.PENDING,
                    reason="owner review required",
                )
            if review.reviewed_hash != case.reviewed_hash:
                review = HumanReview(
                    case_id=case_id,
                    reviewed_hash=case.reviewed_hash,
                    decision=ReviewStatus.PENDING,
                    reason="reviewed version changed; owner re-review required",
                )
            review_packet.append(
                {
                    "case_id": case_id,
                    "reviewed_hash": case.reviewed_hash,
                    "public_intent": variant.fixture.user_prompt,
                    "source_candidate": variant.candidate.candidate_id,
                    "source_evidence": curated.source.model_dump(mode="json"),
                    "deterministic_signals": curated.signals.model_dump(mode="json"),
                    "llm_triage": curated.triage.model_dump(mode="json"),
                    "llm_triage_provenance": (
                        curated.triage_invocation.model_dump(mode="json")
                    ),
                    "rubric_draft": curated.rubric_draft.model_dump(mode="json"),
                    "rubric_draft_provenance": (
                        curated.rubric_invocation.model_dump(mode="json")
                    ),
                    "rubric_draft_status": "advisory_not_activated",
                    "review_scope": ("case_source_oracle_replay_and_judge_calibration"),
                    "variant_dimensions": variant.dimensions.model_dump(mode="json"),
                    "oracle_summary": {
                        "policy_version": variant.fixture.policy_version,
                        "refund_amount_minor": cast(Mapping[str, Any], case.oracle)[
                            "expected_terminal_business_state"
                        ]["order_items"][variant.fixture.item.item_id]["refund_amount"][
                            "amount_minor"
                        ],
                        "refund_method": cast(Mapping[str, Any], case.oracle)[
                            "expected_terminal_business_state"
                        ]["order_items"][variant.fixture.item.item_id]["refund_method"],
                    },
                    "replay_status": case.replay["status"],
                    "judge_statuses": case.calibration["statuses"],
                    "current_review": review.model_dump(mode="json"),
                }
            )
            private_case_root = temp / "private" / case_id
            refs = [
                _reference(temp, path) for path in sorted(private_case_root.iterdir())
            ]
            if review.decision is ReviewStatus.APPROVED:
                stage = QualificationStage.QUALIFIED
                qualified += 1
                catalog_cases.append(
                    {
                        "case_id": case_id,
                        "fixture": _reference(temp, fixture_path),
                        "public_case": _reference(temp, public_path),
                        "expected_actions": [
                            {"tool_name": name, "arguments": dict(arguments)}
                            for name, arguments in case.expected_actions
                        ],
                        "policy_version": variant.fixture.policy_version,
                        "qualification_hash": case.reviewed_hash,
                        "curation": {
                            "source_id": variant.candidate.source_id,
                            "artifacts": source_refs,
                        },
                        "rubric_draft": curated.rubric_draft.model_dump(mode="json"),
                        "rubric_status": "model_draft_requires_human_activation",
                    }
                )
                reason_code = "all_checks_passed"
                reason = (
                    "source triage, replay, Judge calibration, and human approval "
                    "passed"
                )
            elif review.decision is ReviewStatus.REJECTED:
                stage = QualificationStage.REJECTED
                rejected += 1
                reason_code = "human_rejected"
                reason = review.reason
            else:
                stage = QualificationStage.HUMAN_REVIEW_PENDING
                pending += 1
                reason_code = "human_review_required"
                reason = review.reason
            qualifications.append(
                {
                    "case_id": case_id,
                    "stage": stage.value,
                    "reason_code": reason_code,
                    "reason": reason,
                    "review": review.model_dump(mode="json"),
                    "input_artifacts": [
                        _reference(temp, public_path),
                        _reference(temp, fixture_path),
                        _reference(temp, curation_manifest_path),
                        *(value for value in source_refs.values() if value is not None),
                    ],
                    "output_artifacts": refs,
                    "pipeline_version": QUALIFICATION_VERSION,
                }
            )

        _write_json(temp / "review-packet.json", review_packet)
        qualification_path = temp / "qualification-manifest.jsonl"
        _write_bytes(
            qualification_path,
            b"".join(_canonical_bytes(item) + b"\n" for item in qualifications),
        )
        data_version: str | None = None
        if catalog_cases:
            catalog_body = {
                "schema_version": "v1alpha1",
                "record_type": "develop_catalog",
                "qualification_version": QUALIFICATION_VERSION,
                "policy_version": PINNED_CASE_FIXTURE.policy_version,
                "curation_manifest": _reference(temp, curation_manifest_path),
                "cases": sorted(catalog_cases, key=lambda item: str(item["case_id"])),
                "qualification_manifest": _reference(temp, qualification_path),
            }
            data_version = _sha(catalog_body)
            _write_json(
                temp / "develop-manifest.json",
                {**catalog_body, "data_version": data_version},
            )
        input_tokens, output_tokens, _, _ = invocation_cost(curation)
        response_source = _response_source(curation)
        summary_payload = {
            "candidate_count": len(plans),
            "source_candidate_count": len(curation.sources),
            "selected_source_count": sum(item.selected for item in curation.sources),
            "qualified_count": qualified,
            "rejected_count": rejected + len(failures),
            "pending_count": pending,
            "data_version": data_version,
            "curation_response_source": response_source,
            "curation_input_tokens": input_tokens,
            "curation_output_tokens": output_tokens,
            "network_used": curation.network_used,
            "live_provider_used": curation.live_provider_used,
        }
        _write_json(
            temp / "summary.json",
            summary_payload,
        )
        _copy_tree_atomic(temp, output)
        return QualificationSummary(
            output=output,
            candidate_count=len(plans),
            source_candidate_count=len(curation.sources),
            selected_source_count=sum(item.selected for item in curation.sources),
            qualified_count=qualified,
            rejected_count=rejected + len(failures),
            pending_count=pending,
            data_version=data_version,
            response_source=response_source,
            network_used=curation.network_used,
            live_provider_used=curation.live_provider_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
