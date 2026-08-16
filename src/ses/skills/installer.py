"""Install exactly the runtime files declared by a Skill artifact manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_MANIFEST = "skill-manifest.json"
_ENTRYPOINT = "SKILL.md"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SkillInstallError(ValueError):
    """The candidate Skill cannot be safely inspected or installed."""


class _ManifestFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    sha256: str

    @field_validator("path")
    @classmethod
    def _installable_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or path.is_absolute()
            or value != path.as_posix()
            or any(
                part in {"", ".", ".."} or part.startswith(".") for part in path.parts
            )
        ):
            raise ValueError("manifest file path must be a safe relative POSIX path")
        if value != _ENTRYPOINT and (
            len(path.parts) < 2 or path.parts[0] != "references"
        ):
            raise ValueError("manifest may declare only SKILL.md and references files")
        return value

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("manifest file sha256 must be 64 lowercase hex characters")
        return value


class SkillManifest(BaseModel):
    """Strict source manifest for one installable Skill artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = Field(pattern=r"^v1alpha1$")
    record_type: str = Field(pattern=r"^skill_artifact_manifest$")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: str = Field(min_length=1)
    files: tuple[_ManifestFile, ...]

    @field_validator("files", mode="before")
    @classmethod
    def _json_files_to_tuple(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("files")
    @classmethod
    def _complete_unique_inventory(
        cls, value: tuple[_ManifestFile, ...]
    ) -> tuple[_ManifestFile, ...]:
        paths = [item.path for item in value]
        if paths.count(_ENTRYPOINT) != 1:
            raise ValueError("manifest must declare SKILL.md exactly once")
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique")
        return value


@dataclass(frozen=True, slots=True)
class SkillInstallation:
    """The exact files and semantic identity installed for one run."""

    destination: Path
    installed_files: tuple[str, ...]
    name: str
    version: str
    sha256: str

    @property
    def skill_version(self) -> str:
        return self.version

    @property
    def skill_sha256(self) -> str:
        return self.sha256


def _regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise SkillInstallError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise SkillInstallError(f"{label} must be a regular file: {path}")


def load_skill_manifest(source: Path) -> SkillManifest:
    """Load and validate the explicit artifact manifest without following links."""
    if source.is_symlink() or not source.is_dir():
        raise SkillInstallError(f"Skill source must be a real directory: {source}")
    manifest_path = source / _MANIFEST
    _regular_file(manifest_path, label="Skill manifest")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        return SkillManifest.model_validate(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise SkillInstallError(f"invalid Skill manifest: {exc}") from exc


def _declared_files(
    source: Path, manifest: SkillManifest
) -> tuple[tuple[str, Path], ...]:
    files: list[tuple[str, Path]] = []
    source_root = source.resolve()
    for item in manifest.files:
        path = source / PurePosixPath(item.path)
        _regular_file(path, label=f"declared Skill file {item.path}")
        try:
            path.resolve(strict=True).relative_to(source_root)
        except (OSError, ValueError) as exc:
            raise SkillInstallError(
                f"manifest file path escapes the Skill source: {item.path}"
            ) from exc
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != item.sha256:
            raise SkillInstallError(f"manifest hash mismatch for {item.path}")
        files.append((item.path, path))
    return tuple(files)


def _canonical_bytes(path: Path) -> bytes:
    payload = path.read_bytes()
    if b"\x00" not in payload:
        return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return payload


def _digest(files: tuple[tuple[str, Path], ...]) -> str:
    digest = hashlib.sha256()
    for relative, path in sorted(files):
        payload = _canonical_bytes(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def normalized_skill_sha256(source: Path) -> str:
    """Hash the normalized contents of every manifest-declared runtime file."""
    manifest = load_skill_manifest(source)
    return _digest(_declared_files(source, manifest))


def write_skill_manifest(
    source: Path,
    *,
    name: str,
    version: str,
    files: tuple[str, ...],
) -> Path:
    """Write a strict manifest for files already created below ``source``."""
    payload = {
        "schema_version": "v1alpha1",
        "record_type": "skill_artifact_manifest",
        "name": name,
        "version": version,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(
                    (source / PurePosixPath(relative)).read_bytes()
                ).hexdigest(),
            }
            for relative in files
        ],
    }
    manifest = SkillManifest.model_validate(payload)
    destination = source / _MANIFEST
    destination.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def _prepare_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise SkillInstallError(
            f"Skill destination must not be a symlink: {destination}"
        )
    current = destination.parent
    while current != current.parent:
        if current.is_symlink():
            raise SkillInstallError(f"Skill destination contains a symlink: {current}")
        current = current.parent
    if destination.exists():
        if not destination.is_dir():
            raise SkillInstallError(
                f"Skill destination must be a directory: {destination}"
            )
        if any(destination.iterdir()):
            raise SkillInstallError("Skill destination must be empty")
    else:
        destination.mkdir(parents=True)


def _verify_installation(
    destination: Path, files: tuple[tuple[str, Path], ...]
) -> None:
    expected = tuple(sorted(relative for relative, _ in files))
    actual_files: list[str] = []
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise SkillInstallError(f"installed Skill contains a symlink: {path}")
        if path.is_file():
            actual_files.append(path.relative_to(destination).as_posix())
    if tuple(sorted(actual_files)) != expected:
        raise SkillInstallError(
            "installed Skill file inventory does not match manifest"
        )
    source_hashes = {
        relative: hashlib.sha256(source.read_bytes()).hexdigest()
        for relative, source in files
    }
    for relative in expected:
        target = destination / PurePosixPath(relative)
        if hashlib.sha256(target.read_bytes()).hexdigest() != source_hashes[relative]:
            raise SkillInstallError(f"installed Skill hash mismatch for {relative}")


def install_skill(
    source: Path,
    destination: Path,
    *,
    version: str | None = None,
) -> SkillInstallation:
    """Copy only manifest-declared runtime files and verify the installed tree."""
    manifest = load_skill_manifest(source)
    if version is not None and version != manifest.version:
        raise SkillInstallError("requested Skill version does not match manifest")
    files = _declared_files(source, manifest)
    _prepare_destination(destination)
    for relative, source_path in files:
        target = destination / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target, follow_symlinks=False)
        os.chmod(target, 0o600)
    _verify_installation(destination, files)
    installed_files = tuple(
        (relative, destination / PurePosixPath(relative)) for relative, _ in files
    )
    return SkillInstallation(
        destination=destination,
        installed_files=tuple(relative for relative, _ in files),
        name=manifest.name,
        version=manifest.version,
        sha256=_digest(installed_files),
    )
