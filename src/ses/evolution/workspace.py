"""Construct the narrowly allowlisted Updater workspace."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ses.evolution.evidence import EvidenceError, load_failure_evidence
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


_PRIVATE_NAME_PARTS = frozenset(
    {"selection", "final", "gold", "judge-private", "credentials", ".env"}
)


def create_updater_workspace(
    *,
    evidence_path: Path,
    parent_dir: Path,
    root: Path | None = None,
) -> UpdaterWorkspace:
    """Expose only redacted failure evidence and installable parent files."""
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise UpdaterWorkspaceError("Updater evidence must be a regular file")
    if any(part.casefold() in _PRIVATE_NAME_PARTS for part in evidence_path.parts):
        raise UpdaterWorkspaceError("Updater cannot read private evidence paths")
    try:
        load_failure_evidence(evidence_path)
        manifest = load_skill_manifest(parent_dir)
    except (EvidenceError, ValueError) as exc:
        raise UpdaterWorkspaceError(
            "Updater inputs failed visibility validation"
        ) from exc

    files: list[tuple[Path, str]] = [(evidence_path, f"inputs/{evidence_path.name}")]
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
