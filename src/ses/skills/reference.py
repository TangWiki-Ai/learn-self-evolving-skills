"""The checked-in Lesson 1 fallback Skill."""

from __future__ import annotations

from pathlib import Path

REFERENCE_SKILL_VERSION = "reference-v1"


def reference_skill_source(repo_root: Path) -> Path:
    """Return the explicit course reference Skill, not a hidden runtime copy."""
    return repo_root / "course" / "ch01-see-the-difference" / "reference-skill"
