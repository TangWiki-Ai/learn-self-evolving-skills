from __future__ import annotations

import stat
from pathlib import Path

from ses.skills.applicability import parse_skill_front_matter
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256
from ses.skills.shopping import (
    SHOPPING_ASSISTANT_NAME,
    SHOPPING_ASSISTANT_VERSION,
    install_shopping_assistant_skill,
    materialize_shopping_assistant_skill,
)


def test_packaged_shopping_assistant_is_a_valid_skill_artifact(
    tmp_path: Path,
) -> None:
    source = materialize_shopping_assistant_skill(tmp_path / "source")
    manifest = load_skill_manifest(source)
    content = (source / "SKILL.md").read_text(encoding="utf-8")
    metadata = parse_skill_front_matter(content)

    assert manifest.name == SHOPPING_ASSISTANT_NAME
    assert manifest.version == SHOPPING_ASSISTANT_VERSION
    assert tuple(item.path for item in manifest.files) == ("SKILL.md",)
    assert metadata is not None
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == SHOPPING_ASSISTANT_NAME
    assert "Chinese or English" in metadata["description"]
    assert "pre-purchase" in metadata["description"]
    assert "Do not use for existing orders" in metadata["description"]
    assert "benchmark analysis" in metadata["description"]
    assert len(normalized_skill_sha256(source)) == 64


def test_shopping_assistant_installs_only_runtime_content(tmp_path: Path) -> None:
    destination = tmp_path / "workspace" / ".claude" / "skills" / "shopping"

    result = install_shopping_assistant_skill(destination)

    assert result.name == SHOPPING_ASSISTANT_NAME
    assert result.version == SHOPPING_ASSISTANT_VERSION
    assert result.installed_files == ("SKILL.md",)
    assert (destination / "SKILL.md").is_file()
    assert not (destination / "skill-manifest.json").exists()
    assert stat.S_IMODE((destination / "SKILL.md").stat().st_mode) == 0o600


def test_shopping_assistant_covers_the_pre_purchase_workflow(tmp_path: Path) -> None:
    source = materialize_shopping_assistant_skill(tmp_path / "source")
    content = (source / "SKILL.md").read_text(encoding="utf-8").casefold()

    for required_behavior in (
        "constraint ledger",
        "ask one focused question",
        "search",
        "exact variant",
        "compare only valid candidates",
        "explicitly authorizes",
        "never invent",
        "untrusted catalog data",
    ):
        assert required_behavior in content


def test_shopping_assistant_excludes_post_purchase_support(tmp_path: Path) -> None:
    source = materialize_shopping_assistant_skill(tmp_path / "source")
    content = (source / "SKILL.md").read_text(encoding="utf-8").casefold()

    for excluded_request in (
        "delivery tracking",
        "cancellation",
        "returns",
        "exchanges",
        "refunds",
        "repairs",
        "warranties",
        "complaints",
        "account support",
        "benchmark analysis",
    ):
        assert excluded_request in content
