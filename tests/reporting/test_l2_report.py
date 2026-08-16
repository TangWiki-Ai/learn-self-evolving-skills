from __future__ import annotations

import json
from pathlib import Path

from ses.contracts.runner import PairedComparison
from ses.reporting.l2 import render_l2_html, write_l2_html
from ses.skills.paired import run_fresh_paired
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.trigger_eval import (
    FixedNativeDiscovery,
    TriggerEvalResult,
    evaluate_triggers,
)
from ses.skills.v0 import FakeV0Creator, create_skill_v0

ROOT = Path(__file__).parents[2]


def _artifacts(tmp_path: Path) -> tuple[PairedComparison, TriggerEvalResult]:
    pack = load_creator_seed_pack(
        ROOT / "data" / "skill-v0" / "creator" / "seed-manifest.json"
    )
    skill = create_skill_v0(
        seed_pack=pack,
        output_dir=tmp_path / "v0",
        creator=FakeV0Creator(),
        workspace_root=tmp_path / "creator-workspaces",
    )
    trigger = evaluate_triggers(
        skill_sha256=skill.sha256,
        engine_version="claude-code:2.1.220",
        discovery=FixedNativeDiscovery(),
    )
    paired = run_fresh_paired(
        skill_source=skill.source,
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
        result_kind="fixed_offline_reference",
    )

    html = destination.read_text(encoding="utf-8")
    lowered = html.casefold()
    assert "paired l2 comparison" in lowered
    assert "trigger precision" in lowered and "trigger recall" in lowered
    assert "fail-to-pass" in lowered and "pass-to-fail" in lowered
    assert "score distribution" in lowered and "cost difference" in lowered
    assert all(row.case_id in html for row in paired.cases)
    assert all(row.baseline_trace in html for row in paired.cases)
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
        result_kind="live_measured",
    )

    marker = '<script type="application/json" id="l2-data">'
    payload = html.split(marker, 1)[1].split("</script>", 1)[0]
    data = json.loads(payload)
    assert data["result_kind"] == "live_measured"
    assert len(data["paired"]["cases"]) == 15
    assert data["trigger"]["precision"] == 1.0
    assert "live measured" in html.casefold()
