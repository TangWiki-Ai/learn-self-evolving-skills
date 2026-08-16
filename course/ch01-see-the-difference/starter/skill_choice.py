"""Lesson 1 starter: choose a generated Skill or the marked reference fallback."""

from __future__ import annotations

from collections.abc import Mapping


def choose_skill(
    generated: Mapping[str, object] | None,
    reference: Mapping[str, object],
) -> dict[str, object]:
    """Return an installable candidate, marking fallback use explicitly."""
    raise NotImplementedError("Lesson 1: implement generated/reference Skill choice")
