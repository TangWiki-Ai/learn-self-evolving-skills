from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.runner import develop_catalog_sha256, load_develop_catalog
from ses.testset.verified import (
    CandidateSeed,
    QualificationSummary,
    ReviewStatus,
    VariantDimensions,
    assert_split_safe,
    generate_controlled_variant,
    qualify_cases,
    reject_protected_split_write,
)

ROOT = Path(__file__).parents[2]
TICKET = ROOT / "data" / "testset" / "ticket07"
PROTECTED = ROOT / "data" / "testset" / "protected"
JUDGE_FIXTURE = ROOT / "tests" / "fixtures" / "judges" / "calibration.json"


def _seed() -> CandidateSeed:
    return CandidateSeed(
        candidate_id="candidate:test",
        source_id="abcd:test",
        semantic_group_id="semantic:test",
        flow="product_defect",
        subflow="return_size",
        difficulty_bucket="medium",
        public_intent="Return a benchmark item.",
    )


def _dimensions(**updates: object) -> VariantDimensions:
    values: dict[str, object] = {
        "membership_tier": "standard",
        "has_prime_shipping": False,
        "days_since_delivery": 15,
        "return_window_days": 15,
        "return_reason": "changed_mind",
        "price_minor": 20000,
        "order_subtotal_minor": 20000,
        "restocking_fee_pct": 15,
    }
    values.update(updates)
    return VariantDimensions.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("membership_tier", "gold"),
        ("has_prime_shipping", True),
        ("days_since_delivery", 30),
        ("return_window_days", 30),
        ("restocking_fee_pct", 25),
        ("order_subtotal_minor", 25000),
    ],
)
def test_supported_policy_dimensions_change_stable_variant_identity(
    field: str, value: object
) -> None:
    baseline = generate_controlled_variant(_seed(), _dimensions())
    changed = generate_controlled_variant(_seed(), _dimensions(**{field: value}))

    assert changed.fixture.case_id != baseline.fixture.case_id
    assert changed.lineage_hash != baseline.lineage_hash


def test_variant_rejects_contradictory_or_unsupported_values() -> None:
    with pytest.raises(ValidationError, match="subtotal"):
        _dimensions(price_minor=20000, order_subtotal_minor=19999)
    with pytest.raises(ValidationError, match="changed-mind"):
        _dimensions(return_reason="defective", restocking_fee_pct=15)
    with pytest.raises(ValidationError):
        _dimensions(membership_tier="diamond")


def test_variant_id_hash_and_public_prompt_are_deterministic_without_answer() -> None:
    first = generate_controlled_variant(_seed(), _dimensions())
    second = generate_controlled_variant(_seed(), _dimensions())

    assert first.fixture == second.fixture
    assert first.lineage_hash == second.lineage_hash
    assert str(_dimensions().price_minor) not in first.fixture.user_prompt
    assert "refund_amount" not in first.fixture.user_prompt


def _protected() -> list[Path]:
    return [
        PROTECTED / "creator-manifest.json",
        PROTECTED / "selection-manifest.json",
        PROTECTED / "final-manifest.json",
    ]


def _run(output: Path, reviews: Path) -> QualificationSummary:
    return qualify_cases(
        candidate_path=TICKET / "candidate-seeds.jsonl",
        variant_plan_path=TICKET / "variant-plan.json",
        reviews_path=reviews,
        protected_manifests=_protected(),
        model_calibration_fixture=JUDGE_FIXTURE,
        output=output,
    )


def _approve_pending(output: Path, reviews: Path) -> None:
    packet = json.loads((output / "review-packet.json").read_text())
    rows = [
        {
            "case_id": item["case_id"],
            "reviewed_hash": item["reviewed_hash"],
            "decision": ReviewStatus.APPROVED.value,
            "reason": "synthetic test reviewer approved protocol fixture",
            "reviewed_at": "2026-08-16T12:00:00Z",
            "reviewer": "synthetic-test-reviewer",
        }
        for item in packet
    ]
    reviews.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_pipeline_preserves_pending_then_qualifies_fifteen_after_synthetic_review(
    tmp_path: Path,
) -> None:
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("", encoding="utf-8")
    output = tmp_path / "generated"

    pending = _run(output, reviews)
    assert pending.pending_count == 15
    assert pending.qualified_count == 0
    assert not (output / "develop-manifest.json").exists()
    packet = json.loads((output / "review-packet.json").read_text())
    assert {item["judge_statuses"]["deliberate_correct"] for item in packet} == {"pass"}
    assert {item["judge_statuses"]["deliberate_incorrect"] for item in packet} == {
        "fail"
    }
    assert {item["judge_statuses"]["evidence_insufficient"] for item in packet} == {
        "not_evaluated"
    }

    _approve_pending(output, reviews)
    completed = _run(output, reviews)
    assert completed.qualified_count == 15
    assert completed.rejected_count == 0
    catalog = load_develop_catalog(output / "develop-manifest.json")
    assert len(catalog) == 15
    assert completed.data_version == next(iter(catalog.values())).manifest_data_version
    assert len(develop_catalog_sha256(catalog)) == 64

    stable = _tree_hash(output)
    _run(output, reviews)
    assert _tree_hash(output) == stable


def test_rejected_review_is_retained_in_audit_manifest(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("", encoding="utf-8")
    output = tmp_path / "generated"
    _run(output, reviews)
    _approve_pending(output, reviews)
    rows = [json.loads(line) for line in reviews.read_text().splitlines()]
    rows[0].update({"decision": "rejected", "reason": "ambiguous public intent"})
    reviews.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = _run(output, reviews)
    audit = [
        json.loads(line)
        for line in (output / "qualification-manifest.jsonl").read_text().splitlines()
    ]
    assert result.qualified_count == 14
    assert result.rejected_count == 1
    assert any(
        row["stage"] == "rejected" and row["reason_code"] == "human_rejected"
        for row in audit
    )


def test_protected_split_write_fails_without_modifying_files(tmp_path: Path) -> None:
    protected_file = tmp_path / "locked.json"
    protected_file.write_text("locked", encoding="utf-8")
    before = protected_file.read_bytes()

    with pytest.raises(PermissionError, match="split_write_protected:selection"):
        reject_protected_split_write("selection")
    with pytest.raises(PermissionError, match="split_write_protected:final"):
        reject_protected_split_write("final")

    assert protected_file.read_bytes() == before


def test_split_conflicts_cover_id_content_and_semantics(tmp_path: Path) -> None:
    variant = generate_controlled_variant(_seed(), _dimensions())
    fake = type("Case", (), {"variant": variant})()
    public = variant.fixture.case_definition().model_dump(mode="json")
    content_hash = hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    for field, value, expected in [
        ("case_id", variant.fixture.case_id, "split_id_conflict"),
        ("content_hash", content_hash, "split_content_conflict"),
        ("semantic_group_id", _seed().semantic_group_id, "split_semantic_conflict"),
    ]:
        manifest = tmp_path / f"{field}.json"
        row = {
            "case_id": "other",
            "content_hash": "0" * 64,
            "semantic_group_id": "other",
        }
        row[field] = value
        manifest.write_text(
            json.dumps({"locked": True, "records": [row], "split": "final"}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=expected):
            assert_split_safe([fake], [manifest])


def test_role_views_never_expose_oracle_or_review_fields(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("", encoding="utf-8")
    output = tmp_path / "generated"
    _run(output, reviews)
    packet = json.loads((output / "review-packet.json").read_text())
    case_id = packet[0]["case_id"]
    # The public persisted case is the same shape consumed by every runtime role.
    public = json.loads((output / "public" / "cases" / f"{case_id}.json").read_text())
    serialized = json.dumps(public).casefold()
    assert "gold" not in serialized
    assert "review" not in serialized
    assert "oracle" not in serialized
