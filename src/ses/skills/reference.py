"""Materialize the checked-in reference Skill from installed package resources."""

from __future__ import annotations

from pathlib import Path

from .packaged import materialize_packaged_skill

REFERENCE_SKILL_VERSION = "reference-v1"


def materialize_reference_skill(destination: Path) -> Path:
    """Copy the packaged reference artifact to a new writable directory."""
    return materialize_packaged_skill("reference_skill", destination)
