from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ses.contracts import RunnerStatus
from ses.foundation.config import (
    ModelRole,
    ProviderId,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import read_provider_credentials
from ses.foundation.doctor import run_doctor
from ses.reporting.baseline import build_baseline_report
from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    DevelopCatalogEvaluator,
    LiveDevelopConfig,
    develop_catalog_sha256,
    load_develop_catalog,
)
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256

_REPRESENTATIVE_CASE = "develop-return-65a595515e9a2273cdab"


@pytest.mark.live
def test_explicit_live_doctor_smoke() -> None:
    if os.environ.get("SES_RUN_LIVE") != "1":
        pytest.skip("set SES_RUN_LIVE=1 to authorize the paid live smoke")
    config = os.environ.get("SES_LIVE_CONFIG")
    if not config:
        pytest.skip("set SES_LIVE_CONFIG to the strict runtime config path")
    provider_name = os.environ.get("SES_LIVE_PROVIDER")
    if not provider_name:
        pytest.skip("set SES_LIVE_PROVIDER to the explicitly authorized provider")

    config_path = Path(config).resolve()
    results = run_doctor(
        project_root=config_path.parent,
        config_path=config_path,
        live=True,
        timeout=120,
        provider=ProviderId(provider_name),
    )

    assert all(result.status in {"PASS", "WARN"} for result in results)


@pytest.mark.live
def test_explicit_live_representative_skill_shop_and_judge_smoke(
    tmp_path: Path,
) -> None:
    if os.environ.get("SES_RUN_LIVE") != "1":
        pytest.skip("set SES_RUN_LIVE=1 to authorize the paid live smoke")
    config_name = os.environ.get("SES_LIVE_CONFIG")
    provider_name = os.environ.get("SES_LIVE_PROVIDER")
    if not config_name or not provider_name:
        pytest.skip("set SES_LIVE_CONFIG and SES_LIVE_PROVIDER")

    config_path = Path(config_name).resolve()
    project_root = config_path.parent
    provider = ProviderId(provider_name)
    runtime = load_runtime_config(config_path)
    lock_path = project_root / runtime.models_lock_for(provider)
    lock = load_model_lock(lock_path)
    credentials = read_provider_credentials(provider, os.environ)
    catalog = load_develop_catalog(mode="fixed")
    case = catalog[_REPRESENTATIVE_CASE]
    skill_source = project_root / "fixtures/seed/skill/v0"
    manifest = load_skill_manifest(skill_source)
    skill_files = tuple(
        (skill_source / item.path, f"resolve-product-returns/{item.path}")
        for item in manifest.files
    )
    lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    evaluator = DevelopCatalogEvaluator(
        {_REPRESENTATIVE_CASE: case},
        skill_files=skill_files,
        live_config=LiveDevelopConfig(
            model=lock.roles[ModelRole.MAIN],
            credentials=credentials,
            executable=runtime.claude_executable,
            environ=os.environ,
            timeout_seconds=300,
            provider=provider,
            model_lock_sha256=lock_sha256,
            cost_currency=("CNY" if provider is ProviderId.CHATANYWHERE else "USD"),
        ),
    )
    completed = BaselineRunner(tmp_path, evaluator).run(
        run_id=f"run-live-smoke-{provider.value}",
        case_ids=(_REPRESENTATIVE_CASE,),
        iterations=1,
        budgets=BudgetLimits(
            max_cases=1,
            max_turns_per_case=3,
            cost_currency=("CNY" if provider is ProviderId.CHATANYWHERE else "USD"),
        ),
        data_version=develop_catalog_sha256({_REPRESENTATIVE_CASE: case}),
        model_lock_hash=lock_sha256,
        skill_hash=normalized_skill_sha256(skill_source),
        protocol_version="ses-chatanywhere-live-smoke-v1",
    )
    report = build_baseline_report(completed.events_path)
    cases = report["cases"]
    totals = report["totals"]

    assert isinstance(cases, list)
    assert cases and isinstance(cases[0], dict)
    assert isinstance(totals, dict)
    assert cases[0]["first_status"] in {
        RunnerStatus.PASS.value,
        RunnerStatus.AGENT_FAIL.value,
    }
    repetitions = cases[0]["repetitions"]
    assert isinstance(repetitions, list)
    assert repetitions and isinstance(repetitions[0], dict)
    artifacts = repetitions[0]["artifacts"]
    assert isinstance(artifacts, dict)
    assert artifacts["grade"] is not None
    assert artifacts["state_diff"] is not None
    assert artifacts["traces"]
    assert repetitions[0]["error"] is None
    timeline = repetitions[0]["tool_timeline"]
    assert isinstance(timeline, list)
    tool_names = {
        row["tool_name"]
        for row in timeline
        if isinstance(row, dict) and "tool_name" in row
    }
    assert "Skill" in tool_names
    assert any(str(name).startswith("mcp__shop__") for name in tool_names)
    assert totals["input_tokens"] > 0
    assert totals["output_tokens"] > 0
    if provider is ProviderId.CHATANYWHERE:
        assert totals["cost_complete"] is False
