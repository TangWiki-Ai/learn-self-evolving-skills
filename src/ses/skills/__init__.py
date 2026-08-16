"""Skill creation and safe installation primitives for the course demo."""

from .creator import COURSE_CREATOR_PROMPT, CreatorError, FakeCreator, SkillCandidate
from .installer import SkillInstallation, install_skill, normalized_skill_sha256
from .reference import REFERENCE_SKILL_VERSION, reference_skill_source

__all__ = [
    "COURSE_CREATOR_PROMPT",
    "REFERENCE_SKILL_VERSION",
    "CreatorError",
    "FakeCreator",
    "SkillCandidate",
    "SkillInstallation",
    "install_skill",
    "normalized_skill_sha256",
    "reference_skill_source",
]
