from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

from ses.skills.comparison import SkillDemoComparison
from ses.skills.installer import normalized_skill_sha256
from ses.skills.reference import materialize_reference_skill

LESSON = Path(__file__).parents[1]


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _comparison() -> SkillDemoComparison:
    return SkillDemoComparison.model_validate_json(
        (LESSON / "comparison-artifact.json").read_bytes()
    )


def _chooser(variant: str):
    module = _module(LESSON / variant / "skill_choice.py", f"lesson_01_{variant}")
    return module.choose_skill


def test_solution_prefers_a_safe_generated_skill() -> None:
    choose_skill = _chooser("solution")
    generated = {"source": "generated", "installable": True}
    reference = {"source": "reference", "installable": True}

    assert choose_skill(generated, reference) == generated


def test_solution_marks_reference_fallback_when_generation_is_weak() -> None:
    choose_skill = _chooser("solution")
    reference = {"source": "reference", "installable": True}

    assert choose_skill(None, reference) == {
        "source": "reference_fallback",
        "installable": True,
        "reference": True,
    }


def test_checked_in_comparison_uses_the_runtime_schema() -> None:
    comparison = _comparison()

    assert comparison.schema_version == "v1alpha1"
    assert comparison.record_type == "lesson_1_skill_demo_comparison"
    assert comparison.measured is False
    assert comparison.source.kind == "checked_in_reference"
    assert "stable improvement" in comparison.notice
    assert comparison.protocol.same_for_both_runs is True
    assert comparison.runs.without_skill["messages"]
    assert comparison.runs.without_skill["tool_calls"]
    assert comparison.runs.with_skill["messages"]
    assert comparison.runs.with_skill["tool_calls"]


def test_course_reference_matches_the_packaged_runtime_reference(
    tmp_path: Path,
) -> None:
    packaged = materialize_reference_skill(tmp_path / "reference")
    course_reference = LESSON / "reference-skill"

    assert normalized_skill_sha256(course_reference) == normalized_skill_sha256(
        packaged
    )


def test_lesson_one_output_carries_terminal_state_evidence_into_lesson_two() -> None:
    comparison = _comparison()
    with_skill = comparison.runs.with_skill
    state_diff = with_skill["state_diff"]

    assert isinstance(state_diff, Mapping)
    assert state_diff["changed"]
    next_lesson = LESSON.parent / "ch02-grade-terminal-state" / "README.md"
    assert "StateDiff" in next_lesson.read_text(encoding="utf-8")


def test_lesson_has_every_required_teaching_section() -> None:
    lesson = (LESSON / "README.md").read_text(encoding="utf-8")
    for heading in (
        "## 困惑",
        "## 方法",
        "## 业界做法",
        "## 关键 insight",
        "## Starter",
        "## 实现任务",
        "## 测试",
        "## 对照产物",
        "## 拓展阅读",
        "## 预算",
    ):
        assert heading in lesson
    assert "预计费用" in lesson
    assert "实测费用" in lesson
    assert "../ch02-grade-terminal-state/README.md" in lesson


def test_starter_retains_the_lesson_gap() -> None:
    with pytest.raises(NotImplementedError, match="Lesson 1"):
        _chooser("starter")(
            {"source": "generated", "installable": True},
            {"source": "reference", "installable": True},
        )
