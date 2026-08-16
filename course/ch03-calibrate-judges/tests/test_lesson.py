from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from ses.evaluation.calibration import execute_fixed_calibration

LESSON = Path(__file__).parents[1]
ROOT = LESSON.parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "judges" / "calibration.json"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("evidence_extractor", "extract_evidence"),
        ("llm_judge", "judge_llm"),
        ("agent_judge", "judge_agent"),
        ("calibration", "execute_fixed_calibration"),
    ],
)
def test_starter_retains_each_core_learning_gap(
    module_name: str, function_name: str
) -> None:
    module = _module(
        LESSON / "starter" / f"{module_name}.py",
        f"lesson_03_starter_{module_name}",
    )
    function = getattr(module, function_name)

    if asyncio.iscoroutinefunction(function):
        with pytest.raises(NotImplementedError, match="Lesson 3"):
            asyncio.run(function())
    else:
        with pytest.raises(NotImplementedError, match="Lesson 3"):
            function(None, None)


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("evidence_extractor", "extract_evidence"),
        ("llm_judge", "judge_llm"),
        ("agent_judge", "judge_agent"),
        ("calibration", "execute_fixed_calibration"),
    ],
)
def test_solution_exports_each_core_implementation(
    module_name: str, function_name: str
) -> None:
    module = _module(
        LESSON / "solution" / f"{module_name}.py",
        f"lesson_03_solution_{module_name}",
    )
    assert callable(getattr(module, function_name))


def test_checked_in_agreement_artifact_is_protocol_traceable() -> None:
    artifact = json.loads(
        (LESSON / "agreement-experiment.json").read_text(encoding="utf-8")
    )

    assert artifact["measured"] is True
    assert artifact["fixed_offline_protocol_executed"] is True
    assert artifact["live_model_measured"] is False
    assert artifact["human_label_version"] == "human-labels-v1"
    assert len(artifact["measurements"]) == 8
    required = {
        "raw_fixed_response",
        "evidence_sha256",
        "rubric_sha256",
        "prompt_sha256",
        "extractor_sha256",
        "judge_model_id",
        "model_lock_version",
        "model_config_sha256",
        "model_protocol_sha256",
        "protocol_sha256",
    }
    assert all(required <= set(item) for item in artifact["measurements"])


def test_solution_is_the_protocol_used_to_create_the_artifact() -> None:
    from ses.evaluation.calibration import load_calibration_fixture

    expected = json.loads(
        (LESSON / "agreement-experiment.json").read_text(encoding="utf-8")
    )
    actual = asyncio.run(
        execute_fixed_calibration(load_calibration_fixture(FIXTURE))
    ).model_dump(mode="json")

    assert actual == expected
