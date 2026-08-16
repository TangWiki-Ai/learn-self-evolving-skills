"""Choose a generated, learner-supplied, or packaged reference demo Skill."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeAlias

from .creator import CreatorError, FakeCreator
from .installer import (
    SkillInstallError,
    SkillManifest,
    load_skill_manifest,
    normalized_skill_sha256,
)
from .reference import materialize_reference_skill

_REQUIRED_WORKFLOW_TERMS = ("inspect", "preview", "confirm", "verify")
SkillSource: TypeAlias = Literal[
    "generated", "candidate", "reference", "reference_fallback"
]


class CandidateMode(StrEnum):
    GENERATE = "generate"
    CANDIDATE = "candidate"
    REFERENCE = "reference"


class CandidateQualityError(ValueError):
    """A structurally installable candidate is too weak for the lesson."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category


@dataclass(frozen=True, slots=True)
class SelectedSkill:
    source: Path
    source_label: SkillSource
    fallback_reason: str | None
    manifest: SkillManifest
    sha256: str


def _validate_quality(source: Path, manifest: SkillManifest) -> None:
    try:
        content = (source / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CandidateQualityError("invalid_structure", str(exc)) from exc
    if len(content.strip()) < 160 or any(
        term not in content.lower() for term in _REQUIRED_WORKFLOW_TERMS
    ):
        raise CandidateQualityError(
            "weak_content",
            "SKILL.md must give a substantive inspect-preview-confirm-verify workflow",
        )
    match = re.match(r"\A---\n(?P<header>.*?)\n---\n", content, flags=re.DOTALL)
    if match is None:
        raise CandidateQualityError(
            "invalid_structure", "SKILL.md must start with YAML front matter"
        )
    metadata: dict[str, str] = {}
    for line in match.group("header").splitlines():
        if ":" not in line:
            raise CandidateQualityError(
                "invalid_structure", "SKILL.md front matter contains an invalid line"
            )
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    required = {"name", "description", "version"}
    if not required.issubset(metadata) or any(not metadata[key] for key in required):
        raise CandidateQualityError(
            "invalid_structure", "SKILL.md front matter is missing required metadata"
        )
    if metadata["name"] != manifest.name or metadata["version"] != manifest.version:
        raise CandidateQualityError(
            "invalid_structure", "SKILL.md metadata does not match the manifest"
        )


def _checked(source: Path, source_label: SkillSource) -> SelectedSkill:
    manifest = load_skill_manifest(source)
    digest = normalized_skill_sha256(source)
    _validate_quality(source, manifest)
    return SelectedSkill(
        source=source,
        source_label=source_label,
        fallback_reason=None,
        manifest=manifest,
        sha256=digest,
    )


def _reference(
    output_dir: Path, *, label: SkillSource, reason: str | None
) -> SelectedSkill:
    source = materialize_reference_skill(output_dir / "reference")
    selected = _checked(source, label)
    return SelectedSkill(
        source=selected.source,
        source_label=label,
        fallback_reason=reason,
        manifest=selected.manifest,
        sha256=selected.sha256,
    )


def select_demo_skill(
    output_dir: Path,
    *,
    mode: CandidateMode,
    candidate_source: Path | None = None,
    creator: FakeCreator | None = None,
) -> SelectedSkill:
    """Select one candidate, falling back only for quality or install errors."""
    output_dir.mkdir(parents=True, exist_ok=False)
    if mode is CandidateMode.REFERENCE:
        return _reference(output_dir, label="reference", reason=None)
    try:
        if mode is CandidateMode.GENERATE:
            candidate = (creator or FakeCreator()).create(
                output_dir / "generated", seed_traces=()
            )
            selected = _checked(candidate.source, "generated")
            if selected.sha256 != candidate.sha256:
                raise SkillInstallError("generated candidate hash changed")
            return selected
        if candidate_source is None:
            raise ValueError("candidate mode requires candidate_source")
        return _checked(candidate_source, "candidate")
    except CandidateQualityError as exc:
        reason = f"{exc.category}: {exc}"
    except (CreatorError, SkillInstallError, OSError, UnicodeError) as exc:
        reason = f"uninstallable: {exc or type(exc).__name__}"
    return _reference(output_dir, label="reference_fallback", reason=reason)
