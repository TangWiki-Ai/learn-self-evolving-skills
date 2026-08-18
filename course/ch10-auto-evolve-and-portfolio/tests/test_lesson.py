from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from ses.automation.portfolio import export_portfolio, portfolio_semantic_sha256
from ses.contracts import (
    AutoEvolveState,
    AutoLoopStatus,
    FinalAggregateReport,
    GateOutcome,
    PortfolioManifest,
)
from ses.evolution.registry import SkillRegistry

LESSON = Path(__file__).parents[1]
ROOT = LESSON.parents[1]
REFERENCE = LESSON / "artifacts/fixed-reference"
CREATED_AT = datetime(2026, 8, 19, 9, 30, tzinfo=UTC)
REFERENCE_SHA256 = "e17b6531f685d87cc498a5deac98cbea473018ec84a66c24e9a826c9bc06db88"


def _variant(name: str) -> ModuleType:
    path = LESSON / name / "automation.py"
    spec = importlib.util.spec_from_file_location(f"lesson_10_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generator() -> ModuleType:
    path = LESSON / "scripts/generate_fixed_reference.py"
    spec = importlib.util.spec_from_file_location("lesson_10_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(output: Path) -> AutoEvolveState:
    return cast(
        AutoEvolveState,
        _variant("solution").run_bounded(
            project_root=ROOT,
            output_root=output,
        ),
    )


@pytest.fixture(scope="module")
def fixed_experiment(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("lesson-10") / "experiment"
    state = _run(output)
    assert state.status is AutoLoopStatus.FINAL_COMPLETE
    return output


def test_solution_completes_accept_reject_and_one_final(
    fixed_experiment: Path,
) -> None:
    state = AutoEvolveState.model_validate_json(
        (fixed_experiment / "state.json").read_bytes()
    )
    registry = SkillRegistry(fixed_experiment / "registry").audit()
    final = FinalAggregateReport.model_validate_json(
        (fixed_experiment / "final/final-aggregate.json").read_bytes()
    )

    assert tuple(row.gate_outcome for row in state.rounds) == (
        GateOutcome.ACCEPTED,
        GateOutcome.REJECTED,
    )
    assert state.completed_rounds == 2
    assert state.current_accepted_skill_sha256 == registry.current_accepted_sha256
    assert len(registry.events) == 6
    assert final.case_count == 12 and final.pass_count == 10
    assert final.result_source == "fixed_reference"
    assert final.network_used is False
    assert (
        state.total_cost_amount
        == state.rounds[0].cost_amount + state.rounds[1].cost_amount
    )


def test_solution_resume_is_idempotent(fixed_experiment: Path) -> None:
    events = fixed_experiment / "registry/events.jsonl"
    final = fixed_experiment / "final/final-aggregate.json"
    before_events = events.read_bytes()
    before_final = final.read_bytes()

    resumed = _run(fixed_experiment)

    assert resumed.status is AutoLoopStatus.FINAL_COMPLETE
    assert events.read_bytes() == before_events
    assert final.read_bytes() == before_final


def test_solution_renders_l3_and_exports_the_allowlisted_portfolio(
    fixed_experiment: Path,
    tmp_path: Path,
) -> None:
    solution = _variant("solution")
    l3 = cast(
        Path,
        solution.render_l3(
            experiment_root=fixed_experiment,
            destination=tmp_path / "l3.html",
        ),
    )
    manifest = cast(
        PortfolioManifest,
        solution.export_public_portfolio(
            experiment_root=fixed_experiment,
            destination=tmp_path / "portfolio",
            created_at=CREATED_AT,
        ),
    )

    rendered = l3.read_text(encoding="utf-8").casefold()
    assert "version dag and rejected branches" in rendered
    assert "capability and cost curve" in rendered
    assert "final aggregate — isolated after the loop" in rendered
    assert "http://" not in rendered and "https://" not in rendered
    assert l3.stat().st_size < 2_000_000
    assert {row.kind for row in manifest.files} >= {
        "skill",
        "registry",
        "gate",
        "loop_state",
        "l3_report",
        "final_aggregate",
        "architecture",
        "system_summary",
    }
    joined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "portfolio").rglob("*")
        if path.is_file()
    ).casefold()
    assert "case_passes" not in joined
    assert "private_results_sha256" not in joined
    assert "private/selection" not in joined
    assert str(ROOT).casefold() not in joined
    summary = (tmp_path / "portfolio/system-summary.md").read_text(encoding="utf-8")
    assert "fixed synthetic adapter (not a Provider measurement)" in summary
    assert "Provider spend 0" in summary
    assert "Selection lock:" in summary and "Final lock:" in summary


def test_fixed_reference_is_an_exact_repeat_of_the_production_path(
    fixed_experiment: Path,
    tmp_path: Path,
) -> None:
    regenerated = tmp_path / "fixed-reference"
    export_portfolio(
        fixed_experiment,
        regenerated,
        created_at=CREATED_AT,
    )

    assert portfolio_semantic_sha256(REFERENCE) == REFERENCE_SHA256
    assert portfolio_semantic_sha256(regenerated) == REFERENCE_SHA256
    assert (regenerated / "manifest.json").read_bytes() == (
        REFERENCE / "manifest.json"
    ).read_bytes()


def test_fixed_reference_generator_accepts_a_fresh_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "fresh-reference"
    generator = _generator()
    temporary = tmp_path / "generator-temporary"
    temporary.mkdir()
    if tmp_path.as_posix().startswith("/private/var/"):
        aliased = Path(tmp_path.as_posix().replace("/private/var/", "/var/", 1))
        aliased = aliased / temporary.name

        @contextmanager
        def aliased_temporary_directory(*, prefix: str) -> Iterator[str]:
            del prefix
            yield aliased.as_posix()

        monkeypatch.setattr(
            generator,
            "TemporaryDirectory",
            aliased_temporary_directory,
        )

    generator.main(["--output-root", str(destination)])

    assert (destination / "manifest.json").is_file()
    assert (destination / "l3.html").is_file()
    assert (destination / "final-aggregate.json").is_file()


def test_reference_final_is_aggregate_only_and_explicitly_offline() -> None:
    final = json.loads((REFERENCE / "final-aggregate.json").read_text(encoding="utf-8"))

    assert final["case_count"] == 12
    assert final["pass_count"] == 10
    assert final["measurement_kind"] == "synthetic_offline"
    assert final["result_source"] == "fixed_reference"
    assert final["network_used"] is False
    assert "case_passes" not in final
    assert "private_results_sha256" not in final


@pytest.mark.parametrize(
    "function",
    ["run_bounded", "render_l3", "export_public_portfolio", "run_and_export"],
)
def test_starter_keeps_the_four_automation_gaps(function: str) -> None:
    with pytest.raises(NotImplementedError, match="Lesson 10"):
        getattr(_variant("starter"), function)()
