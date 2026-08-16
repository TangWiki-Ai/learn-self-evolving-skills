"""Strict schema shared by current and checked-in Lesson 1 comparisons."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ComparisonSource(_StrictModel):
    kind: Literal["current_run", "checked_in_reference"]
    engine: str = Field(min_length=1)
    description: str = Field(min_length=1)
    runtime_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ComparisonProtocol(_StrictModel):
    case_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    timeout_seconds: float = Field(gt=0)
    engine: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    same_for_both_runs: bool

    @classmethod
    def from_json_values(cls, **values: object) -> ComparisonProtocol:
        allowed_tools = values.get("allowed_tools")
        if isinstance(allowed_tools, list):
            values["allowed_tools"] = tuple(allowed_tools)
        return cls.model_validate(values)


class ComparisonSkill(_StrictModel):
    source: Literal["generated", "candidate", "reference", "reference_fallback"]
    reference: bool
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fallback_reason: str | None = None


class ComparisonRuns(_StrictModel):
    without_skill: dict[str, JsonValue]
    with_skill: dict[str, JsonValue]


class QualitativeResult(_StrictModel):
    outcome: str = Field(min_length=1)
    without_skill_state_changed: bool
    with_skill_state_changed: bool


class SkillDemoComparison(_StrictModel):
    """Versioned qualitative comparison; not the future Runner ComparisonRecord."""

    schema_version: Literal["v1alpha1"]
    record_type: Literal["lesson_1_skill_demo_comparison"]
    case_id: str = Field(min_length=1)
    claim: Literal["qualitative_demo_only"]
    measured: bool
    notice: str = Field(min_length=1)
    source: ComparisonSource
    protocol: ComparisonProtocol
    skill: ComparisonSkill
    runs: ComparisonRuns
    qualitative_result: QualitativeResult
    comparison_artifact: str = Field(min_length=1)
