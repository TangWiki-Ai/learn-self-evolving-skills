from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ses.contracts import (
    PairedComparison,
    SkillV0PipelineSummary,
    artifact_json_bytes,
)
from ses.contracts.shopping import ShoppingPairMetrics, ShoppingScenario
from ses.evolution.registry import RegistryError, SkillRegistry
from ses.reporting.l2 import render_l2_html
from ses.skills.static_gate import StaticGatePolicy, StaticGateReport, run_static_gate
from tests.shopping._fixed_v0_pipeline import build_fixed_v0_pipeline


def _shopping_static_policy() -> StaticGatePolicy:
    return StaticGatePolicy(
        supported_tools=frozenset(
            {
                "mcp__shop_simulator__search",
                "mcp__shop_simulator__click",
                "mcp__shop_simulator__ask_shopper",
                "mcp__shop_simulator__purchase",
                "mcp__shop_simulator__finish_without_purchase",
            }
        ),
        identifier_patterns=(re.compile(r"\bB0[A-Z0-9]{8}\b", re.IGNORECASE),),
    )


def test_l2_validates_and_renders_shopping_v1alpha2_metrics(tmp_path: Path) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)

    rendered = render_l2_html(
        pipeline.paired.comparison,
        pipeline.trigger.evaluation,
        artifact_root=pipeline.root,
    )

    lowered = rendered.casefold()
    assert "shopping metrics" in lowered
    assert "full-success" in lowered
    assert "mean strict" in lowered
    assert "safety violations" in lowered
    assert "cost delta" in lowered
    assert all(scenario.value in rendered for scenario in ShoppingScenario)
    marker = '<script type="application/json" id="l2-data">'
    payload = json.loads(rendered.split(marker, 1)[1].split("</script>", 1)[0])
    metrics = ShoppingPairMetrics.model_validate(payload["shopping_metrics"])
    assert (
        metrics.pair_execution_sha256
        == pipeline.paired.comparison.pair_execution_sha256
    )


def test_l2_rejects_tampered_shopping_metric_projection(tmp_path: Path) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    comparison = pipeline.paired.comparison
    assert comparison.shopping_metrics is not None
    metrics_path = pipeline.root / comparison.shopping_metrics.path
    metrics = ShoppingPairMetrics.model_validate_json(metrics_path.read_bytes())
    forged = metrics.model_copy(
        update={
            "baseline_full_success_count": metrics.baseline_full_success_count - 1,
            "strata": (
                metrics.strata[0].model_copy(
                    update={
                        "baseline_full_success_count": (
                            metrics.strata[0].baseline_full_success_count - 1
                        )
                    }
                ),
                *metrics.strata[1:],
            ),
        }
    )
    metrics_path.write_text(forged.model_dump_json(), encoding="utf-8")
    forged_ref = comparison.shopping_metrics.model_copy(
        update={"sha256": hashlib.sha256(metrics_path.read_bytes()).hexdigest()}
    )
    forged_pair = comparison.model_copy(update={"shopping_metrics": forged_ref})

    with pytest.raises(ValueError, match="shopping metrics"):
        render_l2_html(
            forged_pair,
            pipeline.trigger.evaluation,
            artifact_root=pipeline.root,
        )


def test_registry_initializes_shopping_v0_with_policy_and_explicit_lineage(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    calls: list[Path] = []

    def shopping_static_gate(source: Path) -> StaticGateReport:
        calls.append(source)
        return run_static_gate(source, policy=_shopping_static_policy())

    registry = SkillRegistry(
        tmp_path / "registry",
        initial_static_gate=shopping_static_gate,
    )
    event = registry.initialize(
        command_id="command-shopping-initialize",
        accepted_skill=pipeline.skill_source,
        evidence_paths=(pipeline.summary_path,),
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
        lineage_id="lineage-shopping-fixed-v1",
    )

    state = registry.audit()
    assert state.lineage_id == "lineage-shopping-fixed-v1"
    assert calls
    legacy_payload = {
        "action": "initialize",
        "evidence_sha256": [reference.sha256 for reference in event.evidence],
        "skill_sha256": event.version_sha256,
    }
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert event.command_sha256 != legacy_digest
    lineage_payload = {**legacy_payload, "lineage_id": "lineage-shopping-fixed-v1"}
    assert (
        event.command_sha256
        == hashlib.sha256(
            json.dumps(lineage_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    stored_summary = registry.root / event.evidence[0].path
    summary = SkillV0PipelineSummary.model_validate_json(stored_summary.read_bytes())
    stored_pair = PairedComparison.model_validate_json(
        (stored_summary.parent / summary.paired_comparison.path).read_bytes()
    )
    assert stored_pair.shopping_metrics is not None
    assert (stored_summary.parent / stored_pair.shopping_metrics.path).is_file()
    assert all(
        reference is not None and (stored_summary.parent / reference.path).is_file()
        for row in stored_pair.cases
        for reference in (row.baseline_domain_result, row.skill_domain_result)
    )


def test_registry_rejects_tampered_shopping_metrics_before_initialization(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    metrics_ref = pipeline.paired.comparison.shopping_metrics
    assert metrics_ref is not None
    (pipeline.root / metrics_ref.path).write_text("{}", encoding="utf-8")
    registry = SkillRegistry(
        tmp_path / "registry",
        initial_static_gate=lambda source: run_static_gate(
            source,
            policy=_shopping_static_policy(),
        ),
    )

    with pytest.raises(RegistryError, match="evidence"):
        registry.initialize(
            command_id="command-shopping-tampered",
            accepted_skill=pipeline.skill_source,
            evidence_paths=(pipeline.summary_path,),
            occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
            lineage_id="lineage-shopping-fixed-v1",
        )


def test_registry_rejects_unreviewed_shopping_creator_seeds(tmp_path: Path) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    summary = SkillV0PipelineSummary.model_validate_json(
        pipeline.summary_path.read_bytes()
    )
    pipeline.summary_path.write_bytes(
        artifact_json_bytes(
            summary.model_copy(
                update={"seed_review_status": "course_authored_pending_human_review"}
            )
        )
    )
    registry = SkillRegistry(
        tmp_path / "registry",
        initial_static_gate=lambda source: run_static_gate(
            source,
            policy=_shopping_static_policy(),
        ),
    )

    with pytest.raises(RegistryError, match="reviewed Creator seeds"):
        registry.initialize(
            command_id="command-shopping-unreviewed-seeds",
            accepted_skill=pipeline.skill_source,
            evidence_paths=(pipeline.summary_path,),
            occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
            lineage_id="lineage-shopping-fixed-v1",
        )


def test_registry_recomputes_shopping_metrics_from_domain_evidence(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    comparison = pipeline.paired.comparison
    metrics_ref = comparison.shopping_metrics
    assert metrics_ref is not None
    metrics_path = pipeline.root / metrics_ref.path
    metrics = ShoppingPairMetrics.model_validate_json(metrics_path.read_bytes())
    forged_metrics = metrics.model_copy(
        update={
            "baseline_full_success_count": metrics.baseline_full_success_count - 1,
            "strata": (
                metrics.strata[0].model_copy(
                    update={
                        "baseline_full_success_count": (
                            metrics.strata[0].baseline_full_success_count - 1
                        )
                    }
                ),
                *metrics.strata[1:],
            ),
        }
    )
    metrics_path.write_bytes(artifact_json_bytes(forged_metrics))
    forged_metrics_ref = metrics_ref.model_copy(
        update={"sha256": hashlib.sha256(metrics_path.read_bytes()).hexdigest()}
    )
    pair_path = pipeline.paired.comparison_path
    pair_path.write_bytes(
        artifact_json_bytes(
            comparison.model_copy(update={"shopping_metrics": forged_metrics_ref})
        )
    )
    summary = SkillV0PipelineSummary.model_validate_json(
        pipeline.summary_path.read_bytes()
    )
    pair_ref = summary.paired_comparison.model_copy(
        update={"sha256": hashlib.sha256(pair_path.read_bytes()).hexdigest()}
    )
    pipeline.summary_path.write_bytes(
        artifact_json_bytes(summary.model_copy(update={"paired_comparison": pair_ref}))
    )
    registry = SkillRegistry(
        tmp_path / "registry",
        initial_static_gate=lambda source: run_static_gate(
            source,
            policy=_shopping_static_policy(),
        ),
    )

    with pytest.raises(RegistryError, match="metrics disagree with domain evidence"):
        registry.initialize(
            command_id="command-shopping-semantic-tamper",
            accepted_skill=pipeline.skill_source,
            evidence_paths=(pipeline.summary_path,),
            occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
            lineage_id="lineage-shopping-fixed-v1",
        )


def test_registry_keeps_legacy_lineage_default_and_rejects_unsafe_explicit_id(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)
    registry = SkillRegistry(
        tmp_path / "registry",
        initial_static_gate=lambda source: run_static_gate(
            source,
            policy=_shopping_static_policy(),
        ),
    )
    skill_hash = pipeline.paired.comparison.skill_sha256

    with pytest.raises(RegistryError, match="lineage_id"):
        registry.initialize(
            command_id="command-unsafe-lineage",
            accepted_skill=pipeline.skill_source,
            evidence_paths=(pipeline.summary_path,),
            occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
            lineage_id="../lineage-live",
        )

    event = registry.initialize(
        command_id="command-default-lineage",
        accepted_skill=pipeline.skill_source,
        evidence_paths=(pipeline.summary_path,),
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert event.lineage_id == f"lineage-{skill_hash[:16]}"
    legacy_payload = {
        "action": "initialize",
        "evidence_sha256": [reference.sha256 for reference in event.evidence],
        "skill_sha256": event.version_sha256,
    }
    assert (
        event.command_sha256
        == hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
