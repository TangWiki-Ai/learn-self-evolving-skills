"""Canonical records for an installable Skill artifact."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from ses.contracts.artifact import RelativeArtifactPath, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.primitives import NonEmptyStr


class SkillManifestFile(ContractModel):
    """One content-addressed runtime file in a Skill artifact."""

    path: RelativeArtifactPath
    sha256: Sha256Digest

    @field_validator("path")
    @classmethod
    def _installable_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if any(part.startswith(".") for part in path.parts):
            raise ValueError("manifest file path cannot contain hidden segments")
        if value != "SKILL.md" and (
            len(path.parts) < 2 or path.parts[0] != "references"
        ):
            raise ValueError("manifest may declare only SKILL.md and references files")
        return value


class SkillArtifactManifest(VersionedRecord):
    """Canonical inventory and identity of one installable Skill artifact."""

    record_type: Literal["skill_artifact_manifest"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: NonEmptyStr
    source_version: NonEmptyStr = "unspecified"
    content_sha256: Sha256Digest | None = None
    provider_compatibility: tuple[NonEmptyStr, ...] = ("claude-code-native",)
    files: tuple[SkillManifestFile, ...]

    @field_validator("provider_compatibility")
    @classmethod
    def _provider_compatibility_not_empty(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("provider compatibility must be nonempty and unique")
        return value

    @field_validator("files")
    @classmethod
    def _complete_unique_inventory(
        cls, value: tuple[SkillManifestFile, ...]
    ) -> tuple[SkillManifestFile, ...]:
        paths = [item.path for item in value]
        if paths.count("SKILL.md") != 1:
            raise ValueError("manifest must declare SKILL.md exactly once")
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique")
        return value
