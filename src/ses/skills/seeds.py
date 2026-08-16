"""Validated creator-only seed projections for Skill v0."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CreatorSeedError(ValueError):
    """The creator seed set is incomplete, unapproved, or crosses a split."""


class CreatorSeedRecord(BaseModel):
    """One audited successful trajectory and its safe creator projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    seed_id: str = Field(pattern=r"^creator-seed-[0-9]{3}$")
    split: str
    source_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection: str = Field(min_length=1)
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_judge: str
    model_judge: str
    human_review: str


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


def _safe_projection(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or relative != pure.as_posix()
        or not pure.parts
        or pure.parts[0] != "projections"
        or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
    ):
        raise CreatorSeedError("seed projection must be a safe projections/ path")
    path = root / pure
    if path.is_symlink() or not path.is_file():
        raise CreatorSeedError("seed projection must be a regular file")
    try:
        path.resolve(strict=True).relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise CreatorSeedError("seed projection escapes its manifest root") from exc
    return path


def _safe_source(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or relative != pure.as_posix()
        or len(pure.parts) < 3
        or pure.parts[:2] != ("private", "traces")
        or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
    ):
        raise CreatorSeedError("seed source must be a safe private/traces path")
    path = root / pure
    if path.is_symlink() or not path.is_file():
        raise CreatorSeedError("seed source must be a regular file")
    return path


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
    for record in manifest.records:
        if record.split != "creator":
            raise CreatorSeedError("every seed must belong to the creator split")
        if (
            record.state_judge != "pass"
            or record.model_judge != "pass"
            or record.human_review != "approved"
        ):
            raise CreatorSeedError("every seed must pass all three review gates")
        source = _safe_source(manifest_path.parent, record.source)
        if hashlib.sha256(source.read_bytes()).hexdigest() != record.source_sha256:
            raise CreatorSeedError("seed source hash does not match its manifest")
        path = _safe_projection(manifest_path.parent, record.projection)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != record.projection_sha256:
            raise CreatorSeedError("seed projection hash does not match its manifest")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CreatorSeedError("seed projection must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise CreatorSeedError("seed projection must be a JSON object")
        projections.append(path)
    return CreatorSeedPack(
        manifest=manifest,
        manifest_path=manifest_path,
        projections=tuple(projections),
    )
