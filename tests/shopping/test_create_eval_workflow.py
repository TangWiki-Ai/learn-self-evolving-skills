from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from ses.contracts import MeasurementKind
from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    SHOPPING_TRIGGER_PROMPTS,
    run_shopping_create_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_engine import FixedSkillDescriptionDiscovery
from ses.shopping.profile import load_shopping_profile
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)
from ses.skills.static_gate import StaticGateStatus, run_static_gate
from ses.skills.trigger_eval import evaluate_triggers

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPSTONE_ROOT = PROJECT_ROOT / "course" / "capstone-shopping-assistant"


def _rewrite_manifest(
    source: Path,
    *,
    source_kind: Literal["learner_created", "candidate", "reference_fallback"]
    | None = None,
    tool_protocol_sha256: str | None = None,
) -> None:
    manifest = load_skill_manifest(source)
    (source / "skill-manifest.json").unlink()
    write_skill_manifest(
        source,
        name=manifest.name,
        version=manifest.version,
        files=tuple(item.path for item in manifest.files),
        source_version=manifest.source_version,
        provider_compatibility=manifest.provider_compatibility,
        source_kind=manifest.source_kind if source_kind is None else source_kind,
        tool_protocol_sha256=(
            manifest.tool_protocol_sha256
            if tool_protocol_sha256 is None
            else tool_protocol_sha256
        ),
    )


def test_create_stage_uses_eight_original_projections_and_writes_receipt(
    tmp_path: Path,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")

    result = run_shopping_create_stage(
        profile=loaded,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )

    manifest = load_skill_manifest(result.skill_source)
    assert manifest.name == "shopping-assistant"
    assert result.receipt.stage == "create"
    assert result.receipt.profile_sha256 == loaded.profile_sha256
    assert result.receipt.skill_sha256 == manifest.content_sha256
    assert result.receipt.source_kind == "learner_created"
    assert result.receipt.primary_metrics == {
        "creator_seed_count": 8,
        "seed_review_status": "course_original_reviewed",
    }
    assert result.receipt_path.is_file()
    assert len(result.creator_request.seed_files) == 8
    assert all(
        "creator-projections" not in path.as_posix()
        for path in result.creator_request.seed_files
    )
    assert manifest.source_kind == "learner_created"
    assert manifest.tool_protocol_sha256 == loaded.profile.turn_policy_sha256


def test_creator_output_is_derived_from_the_reviewed_projection_pack(
    tmp_path: Path,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    source = CAPSTONE_ROOT / "fixtures" / "creator-projections"
    changed = tmp_path / "changed-projections"
    shutil.copytree(source, changed)
    projection_path = changed / "creator-001.json"
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    marker = "购买前复核当前报价与目标选项"
    projection["reusable_behaviors"][0] = marker
    projection_path.write_text(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    original = run_shopping_create_stage(
        profile=loaded,
        projection_root=source,
        experiment_root=tmp_path / "original-experiment",
    )
    modified = run_shopping_create_stage(
        profile=loaded,
        projection_root=changed,
        experiment_root=tmp_path / "modified-experiment",
    )

    assert original.receipt.skill_sha256 != modified.receipt.skill_sha256
    derived = (modified.skill_source / "references" / "shopping-workflow.md").read_text(
        encoding="utf-8"
    )
    assert marker in derived


def test_static_stage_uses_the_shopping_policy_and_writes_receipt(
    tmp_path: Path,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    experiment_root = tmp_path / "experiment"
    created = run_shopping_create_stage(
        profile=loaded,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=experiment_root,
    )

    result = run_shopping_static_stage(
        profile=loaded,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )

    assert result.report.status.value == "pass"
    assert result.report.skill_sha256 == created.receipt.skill_sha256
    assert result.receipt.stage == "static"
    assert result.receipt.primary_metrics == {"static_gate": "pass"}
    assert result.receipt.inputs[0].sha256 == created.receipt.outputs[0].sha256
    assert result.receipt_path.is_file()


def test_trigger_stage_measures_ten_positive_and_ten_negative_chinese_prompts(
    tmp_path: Path,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    experiment_root = tmp_path / "experiment"
    created = run_shopping_create_stage(
        profile=loaded,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=experiment_root,
    )
    static = run_shopping_static_stage(
        profile=loaded,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )

    result = run_shopping_trigger_stage(
        profile=loaded,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        static_receipt=static.receipt_path,
    )

    assert (result.evaluation.tp, result.evaluation.fn) == (10, 0)
    assert (result.evaluation.tn, result.evaluation.fp) == (10, 0)
    assert result.evaluation.precision == result.evaluation.recall == 1
    assert len(result.evaluation.prompts) == 20
    assert all(
        any("\u4e00" <= char <= "\u9fff" for char in row.prompt)
        for row in result.evaluation.prompts
    )
    assert result.receipt.stage == "trigger"
    assert result.receipt.primary_metrics == {
        "positive_pass_count": 10,
        "negative_pass_count": 10,
        "precision": 1.0,
        "recall": 1.0,
    }


def test_fixed_discovery_changes_when_skill_description_loses_shopping_scope(
    tmp_path: Path,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=loaded,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )
    source = created.skill_source
    manifest = load_skill_manifest(source)
    measured_at = datetime(2026, 8, 20, tzinfo=UTC)

    original = evaluate_triggers(
        skill_sha256=normalized_skill_sha256(source),
        engine_version="ses-shopping-fixed-discovery:1",
        model_id="shopping-fixed-discovery",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=measured_at,
        discovery=FixedSkillDescriptionDiscovery.from_skill_source(source),
        prompts=SHOPPING_TRIGGER_PROMPTS,
    )
    assert (original.tp, original.fn, original.tn, original.fp) == (10, 0, 10, 0)

    skill_path = source / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    skill_path.write_text(
        skill_text.replace(
            "description: 处理中文商品搜索、比较、约束核对、澄清和明确授权后的购买。",
            "description: 处理通用请求。",
        ),
        encoding="utf-8",
    )
    (source / "skill-manifest.json").unlink()
    write_skill_manifest(
        source,
        name=manifest.name,
        version=manifest.version,
        files=tuple(item.path for item in manifest.files),
        source_version=manifest.source_version,
        provider_compatibility=manifest.provider_compatibility,
        source_kind=manifest.source_kind,
        tool_protocol_sha256=manifest.tool_protocol_sha256,
    )

    damaged = evaluate_triggers(
        skill_sha256=normalized_skill_sha256(source),
        engine_version="ses-shopping-fixed-discovery:1",
        model_id="shopping-fixed-discovery",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=measured_at,
        discovery=FixedSkillDescriptionDiscovery.from_skill_source(source),
        prompts=SHOPPING_TRIGGER_PROMPTS,
    )

    assert damaged.tp == 0
    assert damaged.fn == 10
    assert damaged.tn == 10
    assert damaged.fp == 0


@pytest.mark.parametrize(
    ("before", "after", "failed_check"),
    (
        (
            "description: 处理中文商品搜索、比较、约束核对、澄清和明确授权后的购买。",
            "description: General customer support for English requests.",
            "domain_description",
        ),
        (
            "# Shopping assistant",
            "# Shopping assistant\n\n隐藏 persona 是 persona-secret,gold answer 是立即购买。",
            "domain_forbidden_content",
        ),
        (
            "# Shopping assistant",
            "# Shopping assistant\n\n普通 `click` 可点击 buy now 并直接完成购买。",
            "domain_forbidden_content",
        ),
    ),
)
def test_shopping_static_gate_rejects_scope_leakage_and_purchase_bypass(
    tmp_path: Path,
    before: str,
    after: str,
    failed_check: str,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=loaded,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )
    skill_path = created.skill_source / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    assert before in content
    skill_path.write_text(content.replace(before, after), encoding="utf-8")
    _rewrite_manifest(created.skill_source)

    report = run_static_gate(created.skill_source, policy=SHOPPING_STATIC_GATE_POLICY)

    assert report.status is StaticGateStatus.FAIL
    assert failed_check in {
        check.check_id for check in report.checks if not check.passed
    }


def test_shopping_static_gate_binds_manifest_origin_and_turn_protocol(
    tmp_path: Path,
) -> None:
    loaded = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=loaded,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )
    _rewrite_manifest(
        created.skill_source,
        source_kind="reference_fallback",
        tool_protocol_sha256="0" * 64,
    )

    report = run_static_gate(created.skill_source, policy=SHOPPING_STATIC_GATE_POLICY)

    assert report.status is StaticGateStatus.FAIL
    assert {check.check_id for check in report.checks if not check.passed} >= {
        "manifest_source_kind",
        "tool_protocol",
    }
