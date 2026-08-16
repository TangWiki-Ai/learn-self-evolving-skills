"""Copy only the learner-facing portion of a Skill into a fresh workspace."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_ENTRYPOINT = "SKILL.md"
_REFERENCES = "references"
_EXCLUDED_NAME_PARTS = frozenset(
    {
        "eval",
        "gold",
        "trace",
        "traces",
        "hidden",
        "private",
        "secret",
    }
)


class SkillInstallError(ValueError):
    """The candidate Skill cannot be safely inspected or installed."""


@dataclass(frozen=True, slots=True)
class SkillInstallation:
    """The exact files and semantic identity installed for one run."""

    destination: Path
    installed_files: tuple[str, ...]
    version: str
    sha256: str

    @property
    def skill_version(self) -> str:
        """Expose the Trace field name without duplicating stored state."""
        return self.version

    @property
    def skill_sha256(self) -> str:
        """Expose the Trace field name without duplicating stored state."""
        return self.sha256


def _regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise SkillInstallError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise SkillInstallError(f"{label} must be a regular file: {path}")


def _safe_name(path: str) -> bool:
    return not any(
        part.startswith(".")
        or any(marker in part.lower() for marker in _EXCLUDED_NAME_PARTS)
        for part in Path(path).parts
    )


def _source_files(source: Path) -> tuple[tuple[str, Path], ...]:
    if source.is_symlink() or not source.is_dir():
        raise SkillInstallError(f"Skill source must be a directory: {source}")

    entrypoint = source / _ENTRYPOINT
    _regular_file(entrypoint, label="SKILL.md")
    files: list[tuple[str, Path]] = [(_ENTRYPOINT, entrypoint)]
    references = source / _REFERENCES
    if references.exists():
        if references.is_symlink() or not references.is_dir():
            raise SkillInstallError("references must be a real directory")
        for candidate in sorted(references.rglob("*")):
            relative = candidate.relative_to(source).as_posix()
            if candidate.is_symlink():
                raise SkillInstallError(
                    f"Skill references must not contain a symlink: {relative}"
                )
            if candidate.is_dir():
                continue
            if not _safe_name(relative):
                continue
            _regular_file(candidate, label="Skill reference")
            files.append((relative, candidate))
    return tuple(files)


def _canonical_bytes(path: Path) -> bytes:
    """Normalize text line endings while keeping binary references byte-stable."""
    payload = path.read_bytes()
    if b"\x00" not in payload:
        return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return payload


def _digest(files: tuple[tuple[str, Path], ...]) -> str:
    digest = hashlib.sha256()
    for relative, path in files:
        payload = _canonical_bytes(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def normalized_skill_sha256(source: Path) -> str:
    """Hash the sorted, installable Skill files, excluding non-runtime material."""
    return _digest(_source_files(source))


def _safe_destination(destination: Path) -> None:
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise SkillInstallError(
            f"Skill destination must be a real directory: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    current = destination
    while current != current.parent:
        if current.is_symlink():
            raise SkillInstallError(f"Skill destination contains a symlink: {current}")
        current = current.parent


def install_skill(
    source: Path,
    destination: Path,
    *,
    version: str = "v0",
) -> SkillInstallation:
    """Install only ``SKILL.md`` and safe files below ``references``.

    The installer never traverses or copies any other source entry. It refuses
    symlinks in the allowlisted tree so a candidate cannot smuggle outside data
    into the Agent workspace.
    """
    if not isinstance(version, str) or not version.strip():
        raise SkillInstallError("Skill version must be a non-empty string")
    files = _source_files(source)
    _safe_destination(destination)
    for relative, source_path in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise SkillInstallError(
                    f"Skill destination is not a new file: {target}"
                )
            raise SkillInstallError(f"Skill destination already contains: {relative}")
        shutil.copyfile(source_path, target, follow_symlinks=False)
        os.chmod(target, 0o600)
    installed_files = tuple((relative, destination / relative) for relative, _ in files)
    return SkillInstallation(
        destination=destination,
        installed_files=tuple(relative for relative, _ in files),
        version=version.strip(),
        sha256=_digest(installed_files),
    )
