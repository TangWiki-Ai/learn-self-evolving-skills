"""Lesson 1 solution: choose a safe generated Skill or reference fallback."""

from __future__ import annotations

from collections.abc import Mapping


def choose_skill(
    generated: Mapping[str, object] | None,
    reference: Mapping[str, object],
) -> dict[str, object]:
    """Prefer an installable candidate and label reference fallback use."""
    if generated is not None and generated.get("installable") is True:
        return dict(generated)
    selected = dict(reference)
    selected["source"] = "reference_fallback"
    selected["reference"] = True
    return selected
