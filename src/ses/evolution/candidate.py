"""Create immutable candidates from accepted Skill content."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from ses.contracts import (
    CandidateArtifact,
    FailureCard,
    Patch,
    SchemaVersion,
    Sha256Digest,
    normalized_files_sha256,
)
from ses.evolution.patches import PatchValidationError, apply_patch
from ses.skills.installer import (
    SkillInstallError,
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)
from ses.skills.static_gate import StaticGateStatus, run_static_gate


class CandidateError(ValueError):
    """Candidate creation failed before an invalid artifact could be published."""


def _runtime_files(source: Path) -> dict[str, str]:
    manifest = load_skill_manifest(source)
    files: dict[str, str] = {}
    for item in manifest.files:
        path = source / PurePosixPath(item.path)
        if path.is_symlink() or not path.is_file():
            raise CandidateError(f"parent runtime file is not regular: {item.path}")
        try:
            files[item.path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CandidateError(
                f"parent runtime file is not UTF-8: {item.path}"
            ) from exc
    return files


def create_candidate(
    *,
    parent_dir: Path,
    patch: Patch,
    cards: tuple[FailureCard, ...],
    evidence_path: Path,
    output_dir: Path,
    expected_parent_sha256: Sha256Digest | None = None,
) -> CandidateArtifact:
    """Apply, gate, and atomically publish one candidate directory."""
    if output_dir.exists() or output_dir.is_symlink():
        raise CandidateError("candidate output must not already exist")
    if parent_dir.is_symlink() or not parent_dir.is_dir():
        raise CandidateError("accepted parent must be a real directory")
    try:
        parent_manifest = load_skill_manifest(parent_dir)
        parent_hash = normalized_skill_sha256(parent_dir)
    except SkillInstallError as exc:
        raise CandidateError(
            "accepted parent failed Skill schema or hash validation"
        ) from exc
    if expected_parent_sha256 is not None and parent_hash != expected_parent_sha256:
        raise CandidateError("accepted parent hash does not match the expected hash")
    if patch.parent_skill_sha256 != parent_hash:
        raise CandidateError("Patch parent hash does not match accepted parent")
    try:
        changed_files = apply_patch(
            _runtime_files(parent_dir),
            patch,
            cards=cards,
            evidence_path=evidence_path,
        )
    except PatchValidationError as exc:
        raise CandidateError(str(exc)) from exc

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=output_dir.parent))
    try:
        for relative, content in sorted(changed_files.items()):
            target = staging / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        version = f"candidate-{patch.patch_sha256[:12]}"
        write_skill_manifest(
            staging,
            name=parent_manifest.name,
            version=version,
            files=tuple(sorted(changed_files)),
            source_version=f"parent:{parent_hash}",
            provider_compatibility=parent_manifest.provider_compatibility,
        )
        report = run_static_gate(staging)
        if report.status is not StaticGateStatus.PASS:
            raise CandidateError("candidate failed Ticket 08 Static Gate")
        content_hash = normalized_skill_sha256(staging)
        expected_content_hash = normalized_files_sha256(changed_files)
        if content_hash != expected_content_hash:
            raise CandidateError("candidate content hash is not deterministic")
        manifest = load_skill_manifest(staging)
        candidate = CandidateArtifact(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="skill_candidate",
            candidate_id=f"candidate-{patch.patch_sha256[:12]}",
            parent_skill_sha256=parent_hash,
            patch_sha256=patch.patch_sha256,
            content_sha256=content_hash,
            version=manifest.version,
            static_gate_status="pass",
            patch=patch,
            files=changed_files,
            manifest=manifest.model_dump(mode="json"),
            creation_protocol="evidence-linked-patch-v1",
        )
        os.replace(staging, output_dir)
        return candidate
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def write_candidate_record(path: Path, candidate: CandidateArtifact) -> None:
    """Persist a candidate record separately from its installable directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            candidate.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def load_patch(path: Path) -> Patch:
    """Load one strict Patch record from JSON."""
    try:
        from pydantic import ValidationError

        return Patch.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise CandidateError("invalid Patch JSON") from exc
