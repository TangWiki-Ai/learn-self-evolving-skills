from __future__ import annotations

from pathlib import Path

import pytest

from ses.skills.installer import install_skill, normalized_skill_sha256


def test_install_skill_copies_only_skill_markdown_and_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
    (source / "references" / "policy.md").write_text(
        "Return policy checklist.\n", encoding="utf-8"
    )
    (source / "eval" / "gold").mkdir(parents=True)
    (source / "eval" / "gold" / "answer.json").write_text(
        '{"hidden": true}\n', encoding="utf-8"
    )
    (source / "Trace.json").write_text("private trace", encoding="utf-8")
    (source / "notes.txt").write_text("not installable", encoding="utf-8")

    destination = tmp_path / "workspace" / ".claude" / "skills" / "demo"
    result = install_skill(source, destination)

    assert result.installed_files == ("SKILL.md", "references/policy.md")
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "# Demo skill\n"
    assert (destination / "references" / "policy.md").exists()
    assert not (destination / "eval").exists()
    assert not (destination / "Trace.json").exists()
    assert not (destination / "notes.txt").exists()
    assert len(result.sha256) == 64


def test_install_skill_rejects_symlinks_even_inside_references(tmp_path: Path) -> None:
    source = tmp_path / "candidate"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (source / "references" / "escape.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        install_skill(source, tmp_path / "workspace")


def test_install_skill_excludes_hidden_and_eval_named_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
    for name in (".hidden.md", "gold-answer.json", "eval-notes.md", "Trace.log"):
        (source / "references" / name).write_text("private", encoding="utf-8")
    (source / "references" / "safe.md").write_text("safe", encoding="utf-8")

    result = install_skill(source, tmp_path / "workspace")

    assert result.installed_files == ("SKILL.md", "references/safe.md")


def test_normalized_hash_ignores_line_endings_and_excluded_root_files(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for source, content in ((first, "# Demo\r\n"), (second, "# Demo\n")):
        (source / "references").mkdir(parents=True)
        (source / "SKILL.md").write_text(content, encoding="utf-8", newline="")
        (source / "eval.json").write_text("first", encoding="utf-8")

    assert normalized_skill_sha256(first) == normalized_skill_sha256(second)
