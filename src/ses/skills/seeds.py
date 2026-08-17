"""Validated creator-only seed projections for Skill v0."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CaseGrade,
    CreatorHumanReview,
    CreatorSourceProvenance,
    CreatorSourceReplay,
    EngineExitStatus,
    GradeStatus,
    JudgeKind,
    StateDiff,
    Trace,
)
from ses.evaluation.evidence_extractor import EvidenceBundle
from ses.evaluation.judges.llm import (
    RUBRIC_PROMPT_VERSION,
    JudgeResponseSource,
    ModelJudgeRun,
)


class CreatorSeedError(ValueError):
    """The creator seed set is incomplete, unapproved, or crosses a split."""


_PROJECTION_LEAK = re.compile(
    r"\b(?:develop|selection|final|gold|eval|trace|case)[-_ /]|"
    r"\b(?:hidden[_ -]?gold|reference answer|api[_ -]?key|credentials?|secrets?)\b|"
    r"\b(?:ORD|CUST(?:OMER)?)-[A-Z0-9-]+\b",
    re.IGNORECASE,
)


class CreatorSeedProjection(BaseModel):
    """The complete, deliberately small information surface visible to Creator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scenario: str = Field(min_length=3, max_length=80)
    reusable_steps: tuple[str, ...] = Field(min_length=2, max_length=8)
    tool_sequence: tuple[
        Literal[
            "get_order",
            "get_customer",
            "get_policies",
            "get_product_details",
            "process_return preview",
            "process_return confirm",
            "process_refund preview",
            "process_refund confirm",
        ],
        ...,
    ] = Field(min_length=2, max_length=10)

    @field_validator("reusable_steps", "tool_sequence", mode="before")
    @classmethod
    def _json_arrays_to_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("reusable_steps")
    @classmethod
    def _bounded_generic_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 120 for item in value):
            raise ValueError("projection steps must be short and nonempty")
        return value

    @model_validator(mode="after")
    def _contains_no_private_vocabulary(self) -> CreatorSeedProjection:
        visible = "\n".join((self.scenario, *self.reusable_steps))
        if _PROJECTION_LEAK.search(visible):
            raise ValueError("projection contains private or credential vocabulary")
        return self


class CreatorSeedRecord(BaseModel):
    """One audited successful trajectory and its safe creator projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    seed_id: str = Field(pattern=r"^creator-seed-[0-9]{3}$")
    split: str
    source_id: str = Field(min_length=1)
    source: ArtifactRef
    replay: ArtifactRef
    trace: ArtifactRef
    state_diff: ArtifactRef
    state_grade: ArtifactRef
    model_evidence: ArtifactRef
    model_grade: ArtifactRef
    model_judge_run: ArtifactRef
    human_review: ArtifactRef
    projection: ArtifactRef


class CreatorSeedManifest(BaseModel):
    """The fixed nine-trace input boundary for v0 creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = Field(pattern=r"^v1alpha1$")
    record_type: str = Field(pattern=r"^creator_seed_manifest$")
    source_version: str = Field(min_length=1)
    records: tuple[CreatorSeedRecord, ...]


@dataclass(frozen=True, slots=True)
class CreatorSeedPack:
    """Validated manifest plus the only files a Creator may read."""

    manifest: CreatorSeedManifest
    manifest_path: Path
    projections: tuple[Path, ...]

    @property
    def records(self) -> tuple[CreatorSeedRecord, ...]:
        return self.manifest.records


def _resolve_artifact(root: Path, ref: ArtifactRef, prefix: tuple[str, ...]) -> Path:
    pure = PurePosixPath(ref.path)
    if (
        ref.root is not ArtifactRoot.RUN
        or pure.parts[: len(prefix)] != prefix
        or any(part.startswith(".") for part in pure.parts)
    ):
        raise CreatorSeedError("creator audit artifact has an invalid controlled path")
    path = root / pure
    if path.is_symlink() or not path.is_file():
        raise CreatorSeedError("creator audit artifact must be a regular file")
    try:
        path.resolve(strict=True).relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise CreatorSeedError(
            "creator audit artifact escapes its manifest root"
        ) from exc
    try:
        ref.verify_bytes(path.read_bytes())
    except ValueError as exc:
        raise CreatorSeedError("creator audit artifact hash does not match") from exc
    return path


def _validate_grade_evidence(
    root: Path, grade: CaseGrade, required_artifact: ArtifactRef
) -> None:
    seen = False
    for assertion in grade.assertions:
        for evidence in assertion.evidence:
            _resolve_artifact(root, evidence.artifact, tuple())
            if evidence.artifact != required_artifact:
                raise CreatorSeedError("creator grade evidence is unrelated")
            seen = True
    if not seen:
        raise CreatorSeedError("creator grade evidence is missing")


def load_creator_seed_pack(manifest_path: Path) -> CreatorSeedPack:
    """Load exactly nine triply-approved creator records and verify projections."""

    try:
        manifest = CreatorSeedManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise CreatorSeedError("invalid creator seed manifest") from exc
    if len(manifest.records) != 9:
        raise CreatorSeedError("creator seed manifest must contain exactly 9 records")
    seed_ids = tuple(record.seed_id for record in manifest.records)
    sources = tuple(record.source_id for record in manifest.records)
    if len(set(seed_ids)) != 9 or len(set(sources)) != 9:
        raise CreatorSeedError("creator seed records and sources must be unique")
    projections: list[Path] = []
    source_commits: set[str] = set()
    for record in manifest.records:
        if record.split != "creator":
            raise CreatorSeedError("every seed must belong to the creator split")
        root = manifest_path.parent
        source_path = _resolve_artifact(root, record.source, ("private", "sources"))
        replay_path = _resolve_artifact(root, record.replay, ("private", "replays"))
        source = _resolve_artifact(root, record.trace, ("private", "traces"))
        try:
            trace = Trace.model_validate_json(source.read_text(encoding="utf-8"))
        except (UnicodeError, ValidationError) as exc:
            raise CreatorSeedError("seed source must be a canonical Trace") from exc
        if (
            trace.case_id != record.seed_id
            or trace.exit_status is not EngineExitStatus.SUCCESS
        ):
            raise CreatorSeedError(
                "seed canonical Trace must identify the seed and complete successfully"
            )
        state_path = _resolve_artifact(
            root, record.state_diff, ("private", "state-diffs")
        )
        state_grade_path = _resolve_artifact(
            root, record.state_grade, ("private", "judges", "state")
        )
        model_grade_path = _resolve_artifact(
            root, record.model_grade, ("private", "judges", "model")
        )
        model_evidence_path = _resolve_artifact(
            root,
            record.model_evidence,
            ("private", "judges", "model", "evidence"),
        )
        model_run_path = _resolve_artifact(
            root,
            record.model_judge_run,
            ("private", "judges", "model", "judge-runs"),
        )
        review_path = _resolve_artifact(
            root, record.human_review, ("private", "reviews")
        )
        try:
            source_record = CreatorSourceProvenance.model_validate_json(
                source_path.read_text(encoding="utf-8")
            )
            replay = CreatorSourceReplay.model_validate_json(
                replay_path.read_text(encoding="utf-8")
            )
            state_diff = StateDiff.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
            state_grade = CaseGrade.model_validate_json(
                state_grade_path.read_text(encoding="utf-8")
            )
            model_grade = CaseGrade.model_validate_json(
                model_grade_path.read_text(encoding="utf-8")
            )
            model_evidence = EvidenceBundle.model_validate_json(
                model_evidence_path.read_text(encoding="utf-8")
            )
            model_run = ModelJudgeRun.model_validate_json(
                model_run_path.read_text(encoding="utf-8")
            )
            review = CreatorHumanReview.model_validate_json(
                review_path.read_text(encoding="utf-8")
            )
        except (UnicodeError, ValidationError) as exc:
            raise CreatorSeedError("creator audit evidence is not canonical") from exc
        if (
            source_record.repository != "https://github.com/microsoft/STATE-Bench"
            or source_record.task_id != record.source_id
            or replay.seed_id != record.seed_id
            or replay.source != record.source
            or replay.state_score != 1
            or any(not call.matched for call in replay.calls)
        ):
            raise CreatorSeedError("creator replay does not match its pinned source")
        source_commits.add(source_record.commit)
        if not (state_diff.added or state_diff.removed or state_diff.changed):
            raise CreatorSeedError("creator StateDiff must record a real state change")
        for grade, judge, required_artifact in (
            (state_grade, JudgeKind.STATE, record.state_diff),
            (model_grade, JudgeKind.LLM, record.model_evidence),
        ):
            _validate_grade_evidence(root, grade, required_artifact)
            if (
                grade.case_id != record.seed_id
                or grade.status is not GradeStatus.PASS
                or not grade.assertions
                or any(
                    assertion.judge is not judge
                    or assertion.status is not GradeStatus.PASS
                    for assertion in grade.assertions
                )
            ):
                raise CreatorSeedError("creator State and model grades must pass")
        if (
            model_evidence.trace_id != trace.trace_id
            or model_evidence.diff_id != state_diff.diff_id
            or model_run.assertion not in model_grade.assertions
            or model_run.protocol.evidence_sha256 != record.model_evidence.sha256
            or model_run.protocol.prompt_version != RUBRIC_PROMPT_VERSION
            or model_run.protocol.extractor_version != model_evidence.extractor_version
            or model_run.protocol.extractor_sha256 != model_evidence.extractor_sha256
            or model_run.protocol.response_source is not JudgeResponseSource.LIVE_ENGINE
            or model_run.request.allowed_tools
        ):
            raise CreatorSeedError("creator model Judge provenance is inconsistent")
        if (
            review.seed_id != record.seed_id
            or review.reviewed_source_sha256 != record.source.sha256
            or review.reviewed_trace_sha256 != record.trace.sha256
            or review.reviewed_replay_sha256 != record.replay.sha256
            or review.reviewed_state_diff_sha256 != record.state_diff.sha256
            or review.reviewed_state_grade_sha256 != record.state_grade.sha256
            or review.reviewed_model_evidence_sha256 != record.model_evidence.sha256
            or review.reviewed_model_grade_sha256 != record.model_grade.sha256
            or review.reviewed_model_run_sha256 != record.model_judge_run.sha256
            or review.reviewed_projection_sha256 != record.projection.sha256
            or review.decision != "approved"
        ):
            raise CreatorSeedError(
                "creator human review must approve the exact evidence"
            )
        path = _resolve_artifact(root, record.projection, ("projections",))
        try:
            CreatorSeedProjection.model_validate_json(path.read_text(encoding="utf-8"))
        except (UnicodeError, ValidationError) as exc:
            raise CreatorSeedError("seed projection violates the safe schema") from exc
        projections.append(path)
    if len(source_commits) != 1:
        raise CreatorSeedError("creator seeds must use one pinned source commit")
    source_commit = source_commits.pop()
    expected_source_version = f"state-bench:{source_commit}:creator-audit-v3"
    if manifest.source_version != expected_source_version:
        raise CreatorSeedError("creator manifest source version is inconsistent")
    return CreatorSeedPack(
        manifest=manifest,
        manifest_path=manifest_path,
        projections=tuple(projections),
    )
