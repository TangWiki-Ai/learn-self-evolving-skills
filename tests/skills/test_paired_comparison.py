from __future__ import annotations

from pathlib import Path

import pytest

from ses.skills.paired import PairCategory, compare_run_events, run_fresh_paired
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.v0 import FakeV0Creator, create_skill_v0

ROOT = Path(__file__).parents[2]


def _v0(tmp_path: Path) -> Path:
    pack = load_creator_seed_pack(
        ROOT / "data" / "skill-v0" / "creator" / "seed-manifest.json"
    )
    return create_skill_v0(
        seed_pack=pack,
        output_dir=tmp_path / "v0",
        creator=FakeV0Creator(),
        workspace_root=tmp_path / "creator-workspaces",
    ).source


def test_paired_comparison_runs_fresh_isolated_sides_and_classifies_all_flips(
    tmp_path: Path,
) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )

    assert len(comparison.cases) == 15
    assert {row.category for row in comparison.cases} == set(PairCategory)
    assert comparison.baseline_run_id != comparison.skill_run_id
    assert comparison.fresh_baseline is True
    assert comparison.fresh_skill is True
    assert comparison.compatible is True
    assert comparison.category_counts[PairCategory.FAIL_TO_PASS] == 1
    assert comparison.category_counts[PairCategory.PASS_TO_FAIL] == 1
    assert comparison.category_counts[PairCategory.BOTH_FAIL] == 1
    assert comparison.category_counts[PairCategory.BOTH_PASS] == 12
    assert comparison.skill_input_tokens > comparison.baseline_input_tokens
    assert comparison.skill_cost_amount > comparison.baseline_cost_amount
    assert all(row.baseline_trace and row.skill_trace for row in comparison.cases)
    assert all(
        row.baseline_state_diff and row.skill_state_diff for row in comparison.cases
    )
    assert all(row.baseline_grade and row.skill_grade for row in comparison.cases)
    workspaces = list((tmp_path / "paired").glob("run-*/workspaces/case-*/workspace"))
    assert len(workspaces) == 30


def test_paired_comparison_rejects_protocol_mismatch(tmp_path: Path) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    baseline_events = tmp_path / "paired" / comparison.baseline_run_id / "events.jsonl"
    skill_events = tmp_path / "paired" / comparison.skill_run_id / "events.jsonl"
    lines = skill_events.read_text(encoding="utf-8").splitlines()
    import json

    started = json.loads(lines[0])
    started["config"]["protocol_version"] = "incompatible-v2"
    lines[0] = json.dumps(started, sort_keys=True, separators=(",", ":"))
    skill_events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="protocol"):
        compare_run_events(baseline_events, skill_events)
