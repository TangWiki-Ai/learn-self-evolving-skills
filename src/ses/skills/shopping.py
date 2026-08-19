"""Packaged shopping-assistant Skill installation helpers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from .installer import SkillInstallation, install_skill
from .packaged import materialize_packaged_skill

SHOPPING_ASSISTANT_NAME = "shopping-assistant"
SHOPPING_ASSISTANT_VERSION = "shopping-assistant-v1"


def materialize_shopping_assistant_skill(destination: Path) -> Path:
    """Copy the packaged shopping-assistant artifact for inspection."""
    return materialize_packaged_skill("shopping_assistant", destination)


def install_shopping_assistant_skill(destination: Path) -> SkillInstallation:
    """Install only the manifest-declared shopping-assistant runtime files."""
    with TemporaryDirectory(prefix="ses-shopping-assistant-") as temporary:
        source = materialize_shopping_assistant_skill(Path(temporary) / "source")
        return install_skill(
            source,
            destination,
            version=SHOPPING_ASSISTANT_VERSION,
        )
