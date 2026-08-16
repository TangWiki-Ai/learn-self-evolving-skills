from __future__ import annotations

from pathlib import Path

import pytest

from ses.skills.creator import COURSE_CREATOR_PROMPT, CreatorError, FakeCreator
from ses.skills.installer import install_skill


def test_default_fake_creator_is_offline_and_returns_installable_candidate(
    tmp_path: Path,
) -> None:
    result = FakeCreator().create(tmp_path / "candidate", seed_traces=())

    assert result.version == "demo-v1"
    assert result.source.is_dir()
    assert (
        "do not invent case-specific answers"
        in result.source.joinpath("SKILL.md").read_text(encoding="utf-8").lower()
    )
    installed = install_skill(
        result.source,
        tmp_path / "workspace" / ".claude" / "skills" / "return-demo",
        version=result.version,
    )
    assert installed.sha256 == result.sha256
    assert installed.installed_files == ("SKILL.md", "references/return-checklist.md")
    assert "ORD-6006" not in result.source.joinpath("SKILL.md").read_text(
        encoding="utf-8"
    )


def test_fake_creator_can_expose_a_deterministic_failure(tmp_path: Path) -> None:
    with pytest.raises(CreatorError, match="offline creator failure"):
        FakeCreator(failure="offline creator failure").create(
            tmp_path / "candidate", seed_traces=()
        )


def test_course_creator_prompt_states_the_visibility_and_safety_boundary() -> None:
    assert "seed traces" in COURSE_CREATOR_PROMPT
    assert "gold" in COURSE_CREATOR_PROMPT
    assert "SKILL.md" in COURSE_CREATOR_PROMPT
