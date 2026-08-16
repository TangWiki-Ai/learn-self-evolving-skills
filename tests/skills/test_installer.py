from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ses.skills.installer import SkillInstallError, install_skill


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(source: Path, files: list[str]) -> None:
    payload = {
        "schema_version": "v1alpha1",
        "record_type": "skill_artifact_manifest",
        "name": "demo",
        "version": "demo-v1",
        "files": [
            {"path": relative, "sha256": _sha256(source / relative)}
            for relative in files
        ],
    }
    (source / "skill-manifest.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def _candidate(tmp_path: Path) -> Path:
    source = tmp_path / "candidate"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
    (source / "references" / "policy.md").write_text(
        "Return policy checklist.\n", encoding="utf-8"
    )
    _write_manifest(source, ["SKILL.md", "references/policy.md"])
    return source


def test_install_skill_copies_exactly_the_manifest_declared_files(
    tmp_path: Path,
) -> None:
    source = _candidate(tmp_path)
    (source / "eval" / "gold").mkdir(parents=True)
    (source / "eval" / "gold" / "answer.json").write_text(
        '{"hidden": true}\n', encoding="utf-8"
    )
    (source / "references" / "undeclared.md").write_text(
        "not installable", encoding="utf-8"
    )

    destination = tmp_path / "workspace" / ".claude" / "skills" / "demo"
    result = install_skill(source, destination)

    assert result.installed_files == ("SKILL.md", "references/policy.md")
    assert result.version == "demo-v1"
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == ("# Demo skill\n")
    assert (destination / "references" / "policy.md").is_file()
    assert not (destination / "skill-manifest.json").exists()
    assert not (destination / "eval").exists()
    assert not (destination / "references" / "undeclared.md").exists()
    assert len(result.sha256) == 64


def test_install_skill_rejects_manifest_hash_mismatch(tmp_path: Path) -> None:
    source = _candidate(tmp_path)
    (source / "references" / "policy.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(SkillInstallError, match="hash mismatch"):
        install_skill(source, tmp_path / "workspace")


@pytest.mark.parametrize(
    "declared_path",
    ["../outside.md", "/tmp/outside.md", "references/../../outside.md"],
)
def test_install_skill_rejects_manifest_path_escape(
    tmp_path: Path, declared_path: str
) -> None:
    source = _candidate(tmp_path)
    manifest = json.loads((source / "skill-manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append({"path": declared_path, "sha256": "0" * 64})
    (source / "skill-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SkillInstallError, match="path"):
        install_skill(source, tmp_path / "workspace")


def test_install_skill_rejects_declared_files_outside_the_allowlist(
    tmp_path: Path,
) -> None:
    source = _candidate(tmp_path)
    (source / "notes.md").write_text("not runtime material", encoding="utf-8")
    _write_manifest(source, ["SKILL.md", "references/policy.md", "notes.md"])

    with pytest.raises(SkillInstallError, match=r"SKILL\.md and references"):
        install_skill(source, tmp_path / "workspace")


def test_install_skill_rejects_symlinks_in_source_and_destination(
    tmp_path: Path,
) -> None:
    source = _candidate(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (source / "references" / "policy.md").unlink()
    (source / "references" / "policy.md").symlink_to(outside)

    with pytest.raises(SkillInstallError, match="symlink"):
        install_skill(source, tmp_path / "workspace")

    source = _candidate(tmp_path / "second")
    real_destination = tmp_path / "real-destination"
    real_destination.mkdir()
    destination = tmp_path / "linked-destination"
    destination.symlink_to(real_destination, target_is_directory=True)

    with pytest.raises(SkillInstallError, match="symlink"):
        install_skill(source, destination)
