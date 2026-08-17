from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ses.contracts import MeasurementKind, TriggerEvalResult
from ses.contracts.runner import PairedComparison
from ses.reporting.l2 import render_l2_html, write_l2_html
from ses.skills.paired import run_fresh_paired
from ses.skills.trigger_eval import (
    SyntheticDiscoveryFixture,
    evaluate_triggers,
)

ROOT = Path(__file__).parents[2]


def _artifacts(tmp_path: Path) -> tuple[PairedComparison, TriggerEvalResult]:
    skill_source = Path(
        shutil.copytree(
            ROOT / "course" / "ch07-create-v0" / "artifacts" / "skill" / "v0",
            tmp_path / "v0",
        )
    )
    from ses.skills.installer import normalized_skill_sha256

    skill_hash = normalized_skill_sha256(skill_source)
    trigger = evaluate_triggers(
        skill_sha256=skill_hash,
        engine_version="claude-code:2.1.220",
        model_id="fixture-model",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=datetime(2026, 8, 17, tzinfo=UTC),
        discovery=SyntheticDiscoveryFixture(),
    )
    paired = run_fresh_paired(
        skill_source=skill_source,
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    return paired, trigger


def test_l2_html_is_offline_small_and_contains_metrics_and_case_evidence(
    tmp_path: Path,
) -> None:
    paired, trigger = _artifacts(tmp_path)
    destination = tmp_path / "l2.html"

    write_l2_html(
        paired,
        trigger,
        destination,
        artifact_root=tmp_path / "paired",
    )

    html = destination.read_text(encoding="utf-8")
    lowered = html.casefold()
    assert "paired l2 comparison" in lowered
    assert "trigger precision" in lowered and "trigger recall" in lowered
    assert "fail-to-pass" in lowered and "pass-to-fail" in lowered
    assert "score distribution" in lowered and "cost difference" in lowered
    assert all(row.case_id in html for row in paired.cases)
    assert all(
        row.baseline_trace is not None and row.baseline_trace.path in html
        for row in paired.cases
    )
    links = re.findall(r'href="([^"]+)"', html)
    assert links
    assert all((tmp_path / "paired" / link).is_file() for link in links)
    assert "http://" not in lowered and "https://" not in lowered
    assert "<script src=" not in lowered and "<link rel=" not in lowered
    assert destination.stat().st_size < 2_000_000
    assert str(ROOT) not in html
    assert "gold" not in lowered and "siliconflow_api_key" not in lowered
    assert "fixed/offline reference" in lowered


def test_l2_html_embeds_structured_source_data_without_private_fields(
    tmp_path: Path,
) -> None:
    paired, trigger = _artifacts(tmp_path)

    html = render_l2_html(
        paired,
        trigger,
        artifact_root=tmp_path / "paired",
    )

    marker = '<script type="application/json" id="l2-data">'
    payload = html.split(marker, 1)[1].split("</script>", 1)[0]
    data = json.loads(payload)
    assert data["result_kind"] == "fixed_offline_reference"
    assert len(data["paired"]["cases"]) == 15
    assert data["trigger"]["precision"] == 1.0
    assert "fixed/offline reference" in html.casefold()
    assert paired.baseline_events.sha256 in html
    assert paired.skill_events.sha256 in html
    assert paired.model_lock_sha256 in html


def test_l2_rejects_tampered_case_evidence(tmp_path: Path) -> None:
    paired, trigger = _artifacts(tmp_path)
    trace_ref = paired.cases[0].baseline_trace
    assert trace_ref is not None
    trace = tmp_path / "paired" / trace_ref.path
    trace.write_text(trace.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum"):
        render_l2_html(paired, trigger, artifact_root=tmp_path / "paired")


def test_l2_rejects_mismatched_skill_identity(tmp_path: Path) -> None:
    paired, trigger = _artifacts(tmp_path)

    with pytest.raises(ValueError, match="different Skill"):
        render_l2_html(
            paired,
            trigger.model_copy(update={"skill_sha256": "0" * 64}),
            artifact_root=tmp_path / "paired",
        )
