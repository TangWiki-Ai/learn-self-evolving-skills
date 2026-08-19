"""Skill creation and safe installation primitives for the course demo."""

from ses.contracts import TriggerEvalResult

from .creator import COURSE_CREATOR_PROMPT, CreatorError, FakeCreator, SkillCandidate
from .installer import SkillInstallation, install_skill, normalized_skill_sha256
from .reference import REFERENCE_SKILL_VERSION, materialize_reference_skill
from .seeds import CreatorSeedPack, load_creator_seed_pack
from .shopping import (
    SHOPPING_ASSISTANT_NAME,
    SHOPPING_ASSISTANT_VERSION,
    install_shopping_assistant_skill,
    materialize_shopping_assistant_skill,
)
from .static_gate import StaticGateReport, run_static_gate
from .trigger_eval import evaluate_triggers
from .v0 import FakeV0Creator, LiveV0Creator, create_skill_v0

__all__ = [
    "COURSE_CREATOR_PROMPT",
    "REFERENCE_SKILL_VERSION",
    "SHOPPING_ASSISTANT_NAME",
    "SHOPPING_ASSISTANT_VERSION",
    "CreatorError",
    "CreatorSeedPack",
    "FakeCreator",
    "FakeV0Creator",
    "LiveV0Creator",
    "SkillCandidate",
    "SkillInstallation",
    "StaticGateReport",
    "TriggerEvalResult",
    "create_skill_v0",
    "evaluate_triggers",
    "install_shopping_assistant_skill",
    "install_skill",
    "load_creator_seed_pack",
    "materialize_reference_skill",
    "materialize_shopping_assistant_skill",
    "normalized_skill_sha256",
    "run_static_gate",
]
