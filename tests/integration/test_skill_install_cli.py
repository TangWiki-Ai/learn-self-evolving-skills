from __future__ import annotations

import json
from pathlib import Path

import pytest

from ses.cli.app import main


def test_skill_install_cli_installs_the_packaged_shopping_assistant(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "workspace" / ".claude" / "skills" / "shopping"

    exit_code = main(
        [
            "skill-install",
            "shopping-assistant",
            "--destination",
            str(destination),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "shopping-assistant"
    assert payload["version"] == "shopping-assistant-v1"
    assert payload["source_kind"] == "reference_fallback"
    assert payload["installed_files"] == ["SKILL.md"]
    assert payload["destination"] == destination.as_posix()
    assert len(payload["sha256"]) == 64
    assert (destination / "SKILL.md").is_file()


def test_skill_install_cli_uses_the_native_skill_directory_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(["skill-install", "shopping-assistant", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["destination"] == ".claude/skills/shopping-assistant"
    assert payload["source_kind"] == "reference_fallback"
    assert (
        tmp_path / ".claude" / "skills" / "shopping-assistant" / "SKILL.md"
    ).is_file()


def test_skill_install_cli_never_overwrites_an_existing_destination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    exit_code = main(
        [
            "skill-install",
            "shopping-assistant",
            "--destination",
            str(destination),
        ]
    )

    assert exit_code == 1
    assert "must be empty" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "keep"
