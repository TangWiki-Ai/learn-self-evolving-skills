"""Shared applicability checks for the offline return-Skill lesson."""

from __future__ import annotations

import re
from collections.abc import Mapping

REQUIRED_WORKFLOW_TERMS = ("inspect", "preview", "confirm", "verify")


def parse_skill_front_matter(content: str) -> Mapping[str, str] | None:
    """Parse the lesson's deliberately small front-matter subset."""

    match = re.match(r"\A---\n(?P<header>.*?)\n---\n", content, flags=re.DOTALL)
    if match is None:
        return None
    metadata: dict[str, str] = {}
    for line in match.group("header").splitlines():
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def is_applicable_return_skill(
    content: str, metadata: Mapping[str, str] | None = None
) -> bool:
    """Return whether the Skill declares and implements the lesson workflow."""

    values = metadata if metadata is not None else parse_skill_front_matter(content)
    if values is None:
        return False
    description = values.get("description", "").lower()
    lowered = content.lower()
    return "return" in description and all(
        term in lowered for term in REQUIRED_WORKFLOW_TERMS
    )
