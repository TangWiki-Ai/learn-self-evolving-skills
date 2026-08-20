from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

CAPSTONE = Path(__file__).parents[1]
ROOT = CAPSTONE.parents[1]
MILESTONES = ("create", "eval", "evolve", "gate", "automation")


def _variant(variant: str, milestone: str) -> ModuleType:
    path = CAPSTONE / variant / f"{milestone}.py"
    spec = importlib.util.spec_from_file_location(
        f"shopping_capstone_{variant}_{milestone}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _PolicyProbeExecution:
    def __init__(self, expected: object) -> None:
        self.expected = expected
        self.calls: list[str] = []

    def validate(self, result: object) -> str:
        self.calls.append("validate")
        assert result == self.expected
        return "policy-result-sha256"

    def execute(self) -> int:
        self.calls.append("execute")
        return 0


@pytest.mark.parametrize(
    "milestone,function",
    [
        ("create", "create_stage"),
        ("create", "static_stage"),
        ("create", "trigger_stage"),
        ("eval", "grade_policy"),
        ("eval", "paired_stage"),
        ("evolve", "diagnosis_policy"),
        ("evolve", "updater_policy"),
        ("evolve", "evolution_stage"),
        ("gate", "gate_policy"),
        ("gate", "candidate_gate"),
        ("gate", "registry_branch"),
        ("automation", "build_loop"),
        ("automation", "build_completion_index"),
        ("automation", "package_accepted"),
        ("automation", "install_accepted"),
    ],
)
def test_starter_keeps_each_learner_decision_open(
    milestone: str, function: str
) -> None:
    if os.environ.get("SES_CAPSTONE_IMPLEMENTATION_VARIANT") == "starter":
        pytest.skip("the clean room selected the learner-owned starter implementation")
    with pytest.raises(NotImplementedError, match=f"Capstone {milestone.title()}"):
        getattr(_variant("starter", milestone), function)()


@pytest.mark.parametrize(
    "milestone,function",
    [
        ("create", "project_seed"),
        ("create", "static_decision"),
        ("create", "trigger_decision"),
        ("eval", "project_grade"),
        ("eval", "compare_pair"),
        ("evolve", "diagnose_failure"),
        ("evolve", "propose_patch"),
        ("gate", "project_gate_metrics"),
        ("gate", "apply_guardrails"),
        ("gate", "select_registry_branch"),
        ("automation", "plan_loop"),
        ("automation", "final_eligibility"),
        ("automation", "package_eligibility"),
    ],
)
def test_starter_keeps_each_policy_decision_open(milestone: str, function: str) -> None:
    if os.environ.get("SES_CAPSTONE_IMPLEMENTATION_VARIANT") == "starter":
        pytest.skip("the clean room selected the learner-owned starter implementation")
    with pytest.raises(NotImplementedError, match=f"Capstone {milestone.title()}"):
        getattr(_variant("starter", milestone), function)({})


def test_selected_implementation_has_no_open_gaps() -> None:
    selected = os.environ.get("SES_CAPSTONE_IMPLEMENTATION_VARIANT")
    if selected not in {"starter", "solution"}:
        pytest.skip("no clean-room milestone implementation was selected")
    for milestone in MILESTONES:
        source = (CAPSTONE / selected / f"{milestone}.py").read_text(encoding="utf-8")
        assert "NotImplementedError" not in source, (
            f"{selected}/{milestone}.py still contains an open learner decision"
        )


def test_solution_delegates_to_production_seams() -> None:
    create = _variant("solution", "create")
    evaluation = _variant("solution", "eval")
    evolve = _variant("solution", "evolve")
    gate = _variant("solution", "gate")
    automation = _variant("solution", "automation")

    assert create.create_stage.__module__ == "ses.shopping.course_workflow"
    assert create.static_stage.__module__ == "ses.shopping.course_workflow"
    assert create.trigger_stage.__module__ == "ses.shopping.course_workflow"
    assert evaluation.grade_policy().__class__.__module__ == "ses.shopping.grading"
    assert evaluation.paired_stage.__module__ == "ses.shopping.course_workflow"
    assert evolve.diagnosis_policy().policy_id == "shopping-v1"
    assert evolve.updater_policy().policy_id == "shopping-v1"
    assert evolve.evolution_stage.__module__ == "ses.evolution.workflow"
    assert gate.gate_policy.__module__ == "ses.shopping.gate"
    assert gate.candidate_gate.__module__ == "ses.evolution.gate"
    assert gate.registry_branch.__module__ == "ses.evolution.governance"
    assert automation.build_loop.__module__ == "ses.shopping.automation"
    assert automation.build_completion_index.__module__ == "ses.automation.capstone"
    assert automation.package_accepted.__module__ == "ses.skills.release"
    assert automation.install_accepted.__module__ == "ses.skills.release"


def test_solution_validates_each_locked_policy_probe_before_one_target_call() -> None:
    fixture = json.loads(
        (CAPSTONE / "fixtures/milestone-policy-v1.json").read_text(encoding="utf-8")
    )
    for milestone in MILESTONES:
        module = _variant("solution", milestone)
        row = fixture["milestones"][milestone]
        execution = _PolicyProbeExecution(row["expected"])

        assert (
            module.execute_target(
                f"{milestone}.probe",
                row["probe"],
                execution.validate,
                execution.execute,
            )
            == 0
        )
        assert execution.calls == ["validate", "execute"]


def test_fixed_profile_has_ten_groups_and_forty_slots_across_four_scenarios() -> None:
    profile = json.loads(
        (CAPSTONE / "profiles/fixed-v1.json").read_text(encoding="utf-8")
    )

    assert sum(profile["source_group_counts"].values()) == 10
    assert sum(profile["episode_slot_counts"].values()) == 40
    assert profile["scenarios"] == [
        "single",
        "single_persona",
        "multi",
        "multi_persona",
    ]
    assert profile["mode"] == "fixed"
    assert profile["measurement_level"] == "synthetic_offline"


def test_course_manifest_keeps_five_milestones_and_reference_out_of_truth() -> None:
    manifest = json.loads(
        (CAPSTONE / "course-manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["course_kind"] == "independent_capstone"
    assert [row["id"] for row in manifest["milestones"]] == list(MILESTONES)
    assert manifest["live_execution_policy"] == "blocked_phase0_no_go"
    assert manifest["reference_fallback"] == {
        "runtime_path": "src/ses/skills/resources/shopping_assistant",
        "creator_seed": False,
        "gold": False,
        "default_accepted": False,
        "completion_evidence": False,
    }
    assert manifest["milestone_execution"] == {
        "default_variant": "starter",
        "reference_variant": "solution",
        "entrypoint": "execute_target",
        "policy_fixture": "fixtures/milestone-policy-v1.json",
        "policy_validation": "before_target_exactly_once",
        "target_execution": "exactly_once",
    }
    commands = {row["id"]: row["command"] for row in manifest["target_commands"]}
    assert tuple(
        command_id for command_id in commands if ".inspect_" in command_id
    ) == (
        "eval.inspect_paired_trace",
        "evolve.inspect_failure_evidence",
        "evolve.inspect_failure_card",
        "gate.inspect_rejected_decision",
        "gate.inspect_registry_history",
    )
    assert list(commands)[-3:] == [
        "automation.package",
        "automation.capstone_index",
        "automation.install",
    ]
    assert all(
        command.startswith("uv run --offline --frozen ")
        for command in commands.values()
    )


def test_phase0_no_go_keeps_every_live_asset_closed() -> None:
    source = json.loads(
        (CAPSTONE / "sources/shop-simulator-live-no-go.json").read_text(
            encoding="utf-8"
        )
    )

    assert source["decision"] == "no_go"
    assert len(source["assets"]) == 8
    assert all(row["reviewer"] == source["reviewer"] for row in source["assets"])
    assert {row["status"] for row in source["assets"]} <= {
        "unknown",
        "prohibited",
    }
