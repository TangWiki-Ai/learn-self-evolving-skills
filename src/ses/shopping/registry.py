"""Shopping-domain construction seam for the shared append-only Registry."""

from __future__ import annotations

from pathlib import Path

from ses.evolution.registry import SkillRegistry
from ses.shopping.course_workflow import SHOPPING_STATIC_GATE_POLICY
from ses.skills.static_gate import run_static_gate


def open_shopping_registry(
    root: Path,
    *,
    registry_id: str = "registry-primary",
    checkpoint_path: Path | None = None,
) -> SkillRegistry:
    """Open the shared Registry with the locked shopping initial-evidence policy."""

    return SkillRegistry(
        root,
        registry_id=registry_id,
        checkpoint_path=checkpoint_path,
        initial_static_gate=lambda source: run_static_gate(
            source,
            policy=SHOPPING_STATIC_GATE_POLICY,
        ),
    )


__all__ = ["open_shopping_registry"]
