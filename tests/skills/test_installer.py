from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ses.contracts import artifact_json_bytes
from ses.skills.installer import (
    SkillInstallError,
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)


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


def test_normalized_hash_uses_only_manifest_declared_files(tmp_path: Path) -> None:
    source = _candidate(tmp_path)
    first = normalized_skill_sha256(source)
    (source / "undeclared.md").write_text("not runtime material", encoding="utf-8")

    assert normalized_skill_sha256(source) == first


def test_manifest_validation_rejects_hash_mismatch(tmp_path: Path) -> None:
    source = _candidate(tmp_path)
    (source / "references" / "policy.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(SkillInstallError, match="hash mismatch"):
        normalized_skill_sha256(source)


@pytest.mark.parametrize(
    "declared_path",
    [
        "../outside.md",
        "/tmp/outside.md",
        "references/../../outside.md",
        "references/.hidden.md",
    ],
)
def test_manifest_validation_rejects_path_escape(
    tmp_path: Path, declared_path: str
) -> None:
    source = _candidate(tmp_path)
    manifest = json.loads((source / "skill-manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append({"path": declared_path, "sha256": "0" * 64})
    (source / "skill-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SkillInstallError, match="path"):
        normalized_skill_sha256(source)


def test_manifest_validation_rejects_source_symlinks(tmp_path: Path) -> None:
    source = _candidate(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (source / "references" / "policy.md").unlink()
    (source / "references" / "policy.md").symlink_to(outside)

    with pytest.raises(SkillInstallError, match="symlink"):
        normalized_skill_sha256(source)


def test_manifest_writer_is_canonical_and_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    (source / "references/policy.md").write_text("Policy.\n", encoding="utf-8")

    destination = write_skill_manifest(
        source,
        name="demo",
        version="v1",
        files=("SKILL.md", "references/policy.md"),
    )
    manifest = load_skill_manifest(source)

    assert destination.read_bytes() == artifact_json_bytes(manifest)
    with pytest.raises(FileExistsError):
        write_skill_manifest(
            source,
            name="demo",
            version="v2",
            files=("SKILL.md", "references/policy.md"),
        )
