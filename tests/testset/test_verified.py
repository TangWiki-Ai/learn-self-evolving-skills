from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.runner import develop_catalog_sha256, load_develop_catalog
from ses.testset.split_guard import (
    DevelopSplitIdentity,
    FixedOfflineSplitVerifier,
    SplitIdentityDimension,
    SplitValidationStatus,
)
from ses.testset.verified import (
    CandidateSeed,
    QualificationSummary,
    VariantDimensions,
    assert_split_safe,
    enforce_course_attestation_boundary,
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


def _run(output: Path, attestations: Path) -> QualificationSummary:
    return qualify_cases(
        candidate_path=TICKET / "candidate-seeds.jsonl",
        variant_plan_path=TICKET / "variant-plan.json",
        attestations_path=attestations,
        protected_manifests=_protected(),
        model_calibration_fixture=JUDGE_FIXTURE,
        output=output,
        mode="fixed",
        protected_split_verifier=FixedOfflineSplitVerifier(),
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_fixed_pipeline_builds_pending_course_catalog_without_human_acceptance(
    tmp_path: Path,
) -> None:
    attestations = TICKET / "course-attestations.jsonl"
    output = tmp_path / "generated"

    result = _run(output, attestations)
    assert result.fixed_course_count == 15
    assert result.excluded_count == 7
    assert result.pending_count == 15
    assert result.qualified_count == 0
    assert result.rejected_count == 0
    assert result.source_candidate_count == 2
    assert result.selected_source_count == 1
    assert result.response_source == "fixed_response"
    assert result.network_used is False
    assert result.live_provider_used is False
    assert (
        result.protected_split_validation_status
        is SplitValidationStatus.FIXED_OFFLINE_UNVERIFIED
    )
    assert result.protected_split_provenance_sha256 is None
    curation = json.loads((output / "curation-manifest.json").read_text())
    assert curation["source_candidate_count"] == 2
    assert curation["selected_source_count"] == 1
    assert curation["response_sources"] == ["fixed_response"]
    packet = json.loads((output / "review-packet.json").read_text())
    assert {item["llm_triage"]["intent"] for item in packet} == {"initiate_return"}
    assert {item["rubric_draft_status"] for item in packet} == {
        "advisory_not_activated"
    }
    assert {item["source_evidence"]["source_kind"] for item in packet} == {
        "benchmark_proxy"
    }
    assert {item["judge_statuses"]["deliberate_correct"] for item in packet} == {"pass"}
    assert {item["judge_statuses"]["deliberate_incorrect"] for item in packet} == {
        "fail"
    }
    assert {item["judge_statuses"]["evidence_insufficient"] for item in packet} == {
        "not_evaluated"
    }
    assert {item["course_attestation"]["status"] for item in packet} == {
        "course_authored_pending_human_review"
    }
    catalog = load_develop_catalog(output / "develop-manifest.json")
    assert len(catalog) == 15
    assert result.data_version == next(iter(catalog.values())).manifest_data_version
    assert len(develop_catalog_sha256(catalog)) == 64
    manifest = json.loads((output / "develop-manifest.json").read_text())
    assert manifest["review_status"] == "course_authored_pending_human_review"
    assert manifest["intended_use"] == "fixed_offline_course_only"

    stable = _tree_hash(output)
    _run(output, attestations)
    assert _tree_hash(output) == stable


def test_qualification_rejects_legacy_unsigned_human_review_claim(
    tmp_path: Path,
) -> None:
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("", encoding="utf-8")
    output = tmp_path / "generated"
    _run(output, reviews)
    packet = json.loads((output / "review-packet.json").read_text())
    reviews.write_text(
        json.dumps(
            {
                "case_id": packet[0]["case_id"],
                "reviewed_hash": packet[0]["qualification_hash"],
                "decision": "approved",
                "reason": "unsigned legacy claim",
                "reviewed_at": "2026-08-16T12:00:00Z",
                "reviewer": "ticket-owner",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="legacy unsigned human review claim"):
        _run(output, reviews)


def test_course_attestations_are_unsigned_and_non_accepting() -> None:
    rows = [
        json.loads(line)
        for line in (TICKET / "course-attestations.jsonl").read_text().splitlines()
    ]
    forbidden = {
        "approved",
        "decision",
        "human_reviewed",
        "reviewed_at",
        "reviewed_hash",
        "reviewer",
        "signature",
    }
    assert len(rows) == 22
    assert all(not forbidden.intersection(row) for row in rows)
    assert {row["status"] for row in rows} == {"course_authored_pending_human_review"}


@pytest.mark.parametrize("mode", ["live", "release"])
def test_pending_course_attestations_fail_closed_for_acceptance(mode: str) -> None:
    with pytest.raises(ValueError, match="independent signed human review"):
        enforce_course_attestation_boundary(mode)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["live", "release"])
def test_live_and_release_require_a_trusted_holdout_verifier_before_writes(
    tmp_path: Path, mode: str
) -> None:
    output = tmp_path / "generated"

    with pytest.raises(ValueError, match="trusted external holdout verifier"):
        qualify_cases(
            candidate_path=TICKET / "candidate-seeds.jsonl",
            variant_plan_path=TICKET / "variant-plan.json",
            attestations_path=TICKET / "course-attestations.jsonl",
            protected_manifests=_protected(),
            model_calibration_fixture=JUDGE_FIXTURE,
            output=output,
            mode=mode,  # type: ignore[arg-type]
        )

    assert not output.exists()


def test_fixed_qualification_requires_an_explicit_unverified_adapter(
    tmp_path: Path,
) -> None:
    output = tmp_path / "generated"

    with pytest.raises(ValueError, match="explicit fixed/offline holdout verifier"):
        qualify_cases(
            candidate_path=TICKET / "candidate-seeds.jsonl",
            variant_plan_path=TICKET / "variant-plan.json",
            attestations_path=TICKET / "course-attestations.jsonl",
            protected_manifests=_protected(),
            model_calibration_fixture=JUDGE_FIXTURE,
            output=output,
            mode="fixed",
        )

    assert not output.exists()


def test_fixed_course_exclusion_is_not_a_human_rejection_claim(
    tmp_path: Path,
) -> None:
    output = tmp_path / "generated"
    result = _run(output, TICKET / "course-attestations.jsonl")
    audit = [
        json.loads(line)
        for line in (output / "qualification-manifest.jsonl").read_text().splitlines()
    ]
    exclusions = [
        row
        for row in audit
        if row["stage"] == "course_fixed_excluded_pending_human_review"
    ]
    assert result.excluded_count == 7
    assert len(exclusions) == 7
    serialized = json.dumps(exclusions, ensure_ascii=False).casefold()
    for forbidden in (
        '"reviewer"',
        '"reviewed_at"',
        '"decision"',
        '"reviewed_hash"',
        "ticket-owner",
        "human_rejected",
    ):
        assert forbidden not in serialized


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


@pytest.mark.parametrize(
    ("dimension", "reason"),
    [
        (SplitIdentityDimension.SOURCE_ID, "split_source_conflict"),
        (SplitIdentityDimension.SEMANTIC_GROUP_ID, "split_semantic_conflict"),
        (SplitIdentityDimension.CASE_ID, "split_id_conflict"),
        (SplitIdentityDimension.CONTENT_HASH, "split_content_conflict"),
    ],
)
def test_trusted_holdout_verifier_blocks_all_four_identity_dimensions(
    dimension: SplitIdentityDimension, reason: str
) -> None:
    variant = generate_controlled_variant(_seed(), _dimensions())
    fake_case = type("Case", (), {"variant": variant})()
    expected_identity = DevelopSplitIdentity(
        source_id=_seed().source_id,
        semantic_group_id=_seed().semantic_group_id,
        case_id=variant.fixture.case_id,
        content_hash=hashlib.sha256(variant.fixture.user_prompt.encode()).hexdigest(),
    )

    class ConflictVerifier:
        status = SplitValidationStatus.EXTERNAL_INVENTORY_COMMITMENT_VERIFIED
        provenance_sha256 = "a" * 64

        def conflict_dimension(
            self, identity: DevelopSplitIdentity
        ) -> SplitIdentityDimension | None:
            assert identity == expected_identity
            return dimension

    with pytest.raises(ValueError, match=reason):
        assert_split_safe(
            [fake_case],
            [],
            protected_split_verifier=ConflictVerifier(),
        )


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
