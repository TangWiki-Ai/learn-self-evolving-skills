"""Materialize the checked-in reference Skill from installed package resources."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

REFERENCE_SKILL_VERSION = "reference-v1"


def _copy_tree(resource: Traversable, destination: Path) -> None:
    destination.mkdir()
    for child in resource.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_tree(child, target)
        elif child.is_file():
            target.write_bytes(child.read_bytes())


def materialize_reference_skill(destination: Path) -> Path:
    """Copy the packaged reference artifact to a new writable directory."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"reference destination already exists: {destination}")
    resource = files("ses.skills.resources").joinpath("reference_skill")
    _copy_tree(resource, destination)
    return destination
