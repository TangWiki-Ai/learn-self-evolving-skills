"""Construct the narrowly allowlisted Updater workspace."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from ses.contracts import FailureCardSet
from ses.foundation.workspace import CaseWorkspace, WorkspaceError, WorkspaceFactory
from ses.skills.installer import load_skill_manifest


class UpdaterWorkspaceError(ValueError):
    """The requested Updater inputs would cross a visibility boundary."""


@dataclass(frozen=True, slots=True)
class UpdaterWorkspace:
    workspace: CaseWorkspace
    visible_files: tuple[str, ...]

    def cleanup(self) -> None:
        if (
            self.workspace.cleanup_root is not None
            and self.workspace.cleanup_root.exists()
        ):
            shutil.rmtree(self.workspace.cleanup_root)


_PRIVATE_EXACT_PARTS = frozenset(
    {
        ".env",
        "credentials",
        "final",
        "gold",
        "judge-private",
        "protected",
        "selection",
    }
)
_PRIVATE_SPLIT_QUALIFIERS = frozenset(
    {"cases", "catalog", "gold", "hidden", "holdout", "locked", "manifest", "split"}
)
_SUPPORTED_SKILL_SPEC_NAMES = frozenset(
    {
        ".updater-skill-spec",
        ".updater-skill-spec.md",
        "updater-spec",
        "updater-spec.md",
    }
)


def _is_private_path_part(value: str) -> bool:
    normalized = value.casefold().replace("_", "-")
    if normalized in _PRIVATE_EXACT_PARTS or normalized.startswith(".env."):
        return True
    tokens = frozenset(re.findall(r"[a-z0-9]+", normalized))
    if tokens & {"credentials", "credential", "gold", "protected", "secrets"}:
        return True
    split = tokens & {"selection", "final"}
    return bool(split and tokens & _PRIVATE_SPLIT_QUALIFIERS)


def _validated_input_file(path: Path, *, label: str) -> Path:
    """Return one real, non-private file without following a symlink component."""

    if ".." in path.parts:
        raise UpdaterWorkspaceError(f"Updater {label} path must be canonical")
    lexical = path if path.is_absolute() else Path.cwd() / path
    if any(component.is_symlink() for component in (lexical, *lexical.parents)):
        raise UpdaterWorkspaceError(f"Updater {label} path cannot contain a symlink")
    if any(_is_private_path_part(part) for part in lexical.parts):
        raise UpdaterWorkspaceError(f"Updater cannot read private {label} paths")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise UpdaterWorkspaceError(f"Updater {label} must be a regular file") from exc
    if any(_is_private_path_part(part) for part in resolved.parts):
        raise UpdaterWorkspaceError(f"Updater cannot read private {label} paths")
    if not resolved.is_file() or resolved.is_symlink():
        raise UpdaterWorkspaceError(f"Updater {label} must be a regular file")
    return resolved


def create_updater_workspace(
    *,
    failure_cards_path: Path,
    skill_spec_path: Path,
    parent_dir: Path,
    root: Path | None = None,
) -> UpdaterWorkspace:
    """Expose only reviewed cards, the Skill spec, and installable parent files."""
    failure_cards_path = _validated_input_file(
        failure_cards_path,
        label="Failure Card set",
    )
    skill_spec_path = _validated_input_file(skill_spec_path, label="Skill spec")
    if skill_spec_path.name.casefold() not in _SUPPORTED_SKILL_SPEC_NAMES:
        raise UpdaterWorkspaceError(
            "Updater Skill spec must use an approved updater spec filename"
        )
    try:
        FailureCardSet.model_validate_json(
            failure_cards_path.read_text(encoding="utf-8")
        )
        if not skill_spec_path.read_text(encoding="utf-8").strip():
            raise UpdaterWorkspaceError("Updater Skill spec must not be empty")
        manifest = load_skill_manifest(parent_dir)
    except (OSError, UnicodeError, ValidationError, ValueError) as exc:
        raise UpdaterWorkspaceError(
            "Updater inputs failed visibility validation"
        ) from exc

    files: list[tuple[Path, str]] = [
        (failure_cards_path, "inputs/failure-cards.json"),
        (skill_spec_path, "inputs/skill-spec.md"),
    ]
    manifest_path = parent_dir / "skill-manifest.json"
    files.append((manifest_path, "parent-skill/skill-manifest.json"))
    for item in manifest.files:
        files.append(
            (parent_dir / PurePosixPath(item.path), f"parent-skill/{item.path}")
        )
    try:
        workspace = WorkspaceFactory(root).create(
            run_id="evolution",
            case_id="updater",
            iteration_id="v1",
            files=files,
        )
    except (OSError, WorkspaceError) as exc:
        raise UpdaterWorkspaceError(
            "could not create isolated Updater workspace"
        ) from exc
    visible = tuple(
        sorted(
            path.relative_to(workspace.root).as_posix()
            for path in workspace.root.rglob("*")
            if path.is_file()
        )
    )
    if any(PurePosixPath(item).is_absolute() for item in visible):
        if workspace.cleanup_root is not None:
            shutil.rmtree(workspace.cleanup_root)
        raise UpdaterWorkspaceError("Updater workspace exposed an absolute path")
    return UpdaterWorkspace(workspace=workspace, visible_files=visible)
