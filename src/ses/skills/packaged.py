"""Materialize immutable Skill artifacts shipped as package resources."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path


def _copy_tree(resource: Traversable, destination: Path) -> None:
    destination.mkdir()
    for child in resource.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_tree(child, target)
        elif child.is_file():
            target.write_bytes(child.read_bytes())


def materialize_packaged_skill(resource_name: str, destination: Path) -> Path:
    """Copy one packaged Skill artifact to a new writable directory."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Skill destination already exists: {destination}")
    resource = files("ses.skills.resources").joinpath(resource_name)
    if not resource.is_dir():
        raise FileNotFoundError(
            f"packaged Skill resource does not exist: {resource_name}"
        )
    _copy_tree(resource, destination)
    return destination
