"""Capture and verify the complete evidence-bound candidate audit bundle."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    EvolutionPipelineSummary,
    FailureCardSet,
    FailureEvidenceFixture,
    Patch,
    artifact_json_bytes,
)
from ses.evolution.candidate import load_runtime_files
from ses.evolution.patches import PatchValidationError, validate_patch
from ses.foundation.credentials import credential_values, is_sensitive_name, redact
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256

_REQUIRED_AUDIT_FILES = frozenset(
    {
        "candidate.json",
        "failure-cards.json",
        "failure-evidence.json",
        "patch.json",
    }
)
_OPTIONAL_AUDIT_FILES = frozenset({"summary.json"})


class CandidateBundleError(ValueError):
    """The candidate or one of its evidence sidecars is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class CandidateAuditSnapshot:
    """Captured bytes that remain meaningful under the candidate record's root."""

    candidate: CandidateArtifact
    files: Mapping[str, bytes]

    @property
    def candidate_bytes(self) -> bytes:
        return self.files["candidate.json"]


def _contains_secret_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "",
            "[redacted]",
            "redacted",
            "none",
            "not_present",
            "not-present",
            "scrubbed",
        }
    if isinstance(value, Mapping):
        return any(_contains_secret_value(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_value(child) for child in value)
    return True


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                isinstance(key, str)
                and is_sensitive_name(key)
                and _contains_secret_value(child)
            )
            or _contains_sensitive_field(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_field(child) for child in value)
    return False


def _assert_credential_free(content: bytes) -> None:
    text = content.decode("utf-8", errors="replace")
    if redact(text, credential_values(os.environ)) != text:
        raise CandidateBundleError("candidate audit material contains credentials")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return
    if _contains_sensitive_field(value):
        raise CandidateBundleError(
            "candidate audit material contains credential fields"
        )


def _open_controlled_directory(path: Path, *, label: str) -> int:
    if ".." in path.parts:
        raise CandidateBundleError(f"{label} must be a canonical real directory")
    lexical = path if path.is_absolute() else Path.cwd() / path
    components = lexical.parts[1:]
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(lexical.anchor, flags)
        for component in components:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise CandidateBundleError(f"{label} must be a real directory")
        return descriptor
    except CandidateBundleError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise CandidateBundleError(
            f"{label} cannot contain symlink ancestors or unsafe components"
        ) from exc


def _directory_inventory(descriptor: int, *, label: str) -> Mapping[str, int]:
    try:
        names = os.listdir(descriptor)
        inventory = {
            name: os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
            for name in names
        }
    except OSError as exc:
        raise CandidateBundleError(f"{label} inventory cannot be read") from exc
    if any(PurePosixPath(name).parts != (name,) for name in inventory):
        raise CandidateBundleError(f"{label} inventory is not canonical")
    return MappingProxyType(inventory)


def _read_regular_file(descriptor: int, name: str) -> bytes:
    if PurePosixPath(name).parts != (name,):
        raise CandidateBundleError("candidate sidecar name is not canonical")
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(name, flags, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CandidateBundleError(
                    f"candidate sidecar must be a regular file: {name}"
                )
            content = stream.read()
    except OSError as exc:
        raise CandidateBundleError(
            f"candidate sidecar cannot be captured: {name}"
        ) from exc
    _assert_credential_free(content)
    return content


def _verify_reference(
    reference: ArtifactRef,
    *,
    expected_path: str,
    files: Mapping[str, bytes],
) -> None:
    if reference.root is not ArtifactRoot.WORKSPACE or reference.path != expected_path:
        raise CandidateBundleError(
            "candidate evidence reference does not use its snapshot root"
        )
    try:
        reference.verify_bytes(files[expected_path])
    except (KeyError, ValueError) as exc:
        raise CandidateBundleError(
            "candidate evidence reference checksum does not match its sidecar"
        ) from exc


def _canonical_record_bytes(value: object, content: bytes, *, label: str) -> None:
    if not hasattr(value, "model_dump") or artifact_json_bytes(value) != content:  # type: ignore[arg-type]
        raise CandidateBundleError(f"candidate {label} record is not canonical")


def _validate_records(files: Mapping[str, bytes]) -> CandidateArtifact:
    try:
        candidate = CandidateArtifact.model_validate_json(files["candidate.json"])
        patch = Patch.model_validate_json(files["patch.json"])
        cards = FailureCardSet.model_validate_json(files["failure-cards.json"])
        evidence = FailureEvidenceFixture.model_validate_json(
            files["failure-evidence.json"]
        )
    except (KeyError, UnicodeError, ValueError) as exc:
        raise CandidateBundleError("candidate audit records are invalid") from exc
    _canonical_record_bytes(
        candidate,
        files["candidate.json"],
        label="artifact",
    )
    _canonical_record_bytes(patch, files["patch.json"], label="Patch")
    _canonical_record_bytes(
        cards,
        files["failure-cards.json"],
        label="Failure Card",
    )
    if candidate.patch != patch:
        raise CandidateBundleError("candidate embedded Patch differs from patch.json")
    _verify_reference(
        cards.evidence_fixture,
        expected_path="failure-evidence.json",
        files=files,
    )
    if evidence.source.skill_sha256 != candidate.parent_skill_sha256:
        raise CandidateBundleError(
            "candidate failure evidence does not bind its accepted parent"
        )

    with tempfile.TemporaryDirectory(prefix="ses-candidate-evidence-audit-") as temp:
        evidence_path = Path(temp) / "failure-evidence.json"
        evidence_path.write_bytes(files["failure-evidence.json"])
        try:
            validate_patch(
                candidate.patch,
                cards=cards.cards,
                evidence_path=evidence_path,
            )
        except PatchValidationError as exc:
            raise CandidateBundleError(
                "candidate Patch evidence is incomplete or inconsistent"
            ) from exc

    summary_bytes = files.get("summary.json")
    if summary_bytes is not None:
        try:
            summary = EvolutionPipelineSummary.model_validate_json(summary_bytes)
        except (UnicodeError, ValueError) as exc:
            raise CandidateBundleError("candidate summary is invalid") from exc
        _canonical_record_bytes(summary, summary_bytes, label="summary")
        for reference, name in (
            (summary.failure_cards, "failure-cards.json"),
            (summary.patch, "patch.json"),
            (summary.candidate, "candidate.json"),
        ):
            _verify_reference(reference, expected_path=name, files=files)
        if (
            summary.parent_skill_sha256 != candidate.parent_skill_sha256
            or summary.candidate_skill_sha256 != candidate.content_sha256
            or summary.failure_card_count != len(cards.cards)
            or summary.patch_operation_count != len(patch.operations)
            or summary.evidence_provenance is not cards.provenance
        ):
            raise CandidateBundleError(
                "candidate summary does not bind its complete audit bundle"
            )
    return candidate


def _capture_candidate_audit_snapshot(
    descriptor: int,
    inventory: Mapping[str, int],
    *,
    exact_inventory: bool,
) -> CandidateAuditSnapshot:
    names = set(_REQUIRED_AUDIT_FILES)
    if "summary.json" in inventory:
        names.add("summary.json")
    files = {name: _read_regular_file(descriptor, name) for name in sorted(names)}
    if exact_inventory:
        if any(not stat.S_ISREG(mode) for mode in inventory.values()):
            raise CandidateBundleError(
                "candidate snapshot contains undeclared material"
            )
        if set(inventory) != set(files):
            raise CandidateBundleError(
                "candidate snapshot contains undeclared material"
            )
    candidate = _validate_records(files)
    return CandidateAuditSnapshot(
        candidate=candidate,
        files=MappingProxyType(files),
    )


def capture_candidate_audit_snapshot(
    root: Path,
    *,
    exact_inventory: bool,
) -> CandidateAuditSnapshot:
    """Capture and deeply verify sidecars rooted beside ``candidate.json``."""

    descriptor = _open_controlled_directory(root, label="candidate snapshot root")
    try:
        inventory = _directory_inventory(
            descriptor,
            label="candidate snapshot",
        )
        return _capture_candidate_audit_snapshot(
            descriptor,
            inventory,
            exact_inventory=exact_inventory,
        )
    finally:
        os.close(descriptor)


def capture_candidate_bundle(
    bundle: Path,
    *,
    verify_runtime: bool = True,
) -> CandidateAuditSnapshot:
    """Read a complete source bundle once and reject undeclared or mutable input."""

    descriptor = _open_controlled_directory(bundle, label="candidate bundle")
    try:
        inventory = _directory_inventory(descriptor, label="candidate bundle")
        expected = set(_REQUIRED_AUDIT_FILES) | {"skill"}
        if "summary.json" in inventory:
            expected.add("summary.json")
        if set(inventory) != expected:
            raise CandidateBundleError("candidate bundle contains undeclared material")
        if any(stat.S_ISLNK(mode) for mode in inventory.values()):
            raise CandidateBundleError("candidate bundle cannot contain symlinks")
        if not stat.S_ISDIR(inventory["skill"]):
            raise CandidateBundleError("candidate bundle has no runtime Skill")
        snapshot = _capture_candidate_audit_snapshot(
            descriptor,
            inventory,
            exact_inventory=False,
        )
    finally:
        os.close(descriptor)
    if not verify_runtime:
        return snapshot
    try:
        manifest = load_skill_manifest(bundle / "skill")
        runtime_hash = normalized_skill_sha256(bundle / "skill")
        runtime_files = load_runtime_files(bundle / "skill")
        declared = {item.path for item in manifest.files} | {"skill-manifest.json"}
        actual: set[str] = set()
        for path in (bundle / "skill").rglob("*"):
            if path.is_symlink():
                raise CandidateBundleError("candidate Skill cannot contain symlinks")
            if path.is_file():
                actual.add(path.relative_to(bundle / "skill").as_posix())
    except (OSError, UnicodeError, ValueError) as exc:
        raise CandidateBundleError("candidate runtime Skill is invalid") from exc
    if actual != declared:
        raise CandidateBundleError("candidate Skill contains undeclared files")
    if (
        runtime_hash != snapshot.candidate.content_sha256
        or runtime_files != dict(snapshot.candidate.files)
        or manifest != snapshot.candidate.manifest
    ):
        raise CandidateBundleError("candidate runtime differs from its record")
    return snapshot


def store_candidate_audit_snapshot(
    parent: Path,
    snapshot: CandidateAuditSnapshot,
) -> Path:
    """Atomically persist one isolated content-addressed candidate audit root."""

    target = parent / snapshot.candidate.content_sha256
    if target.exists() or target.is_symlink():
        try:
            stored = capture_candidate_audit_snapshot(
                target,
                exact_inventory=True,
            )
        except CandidateBundleError as exc:
            raise CandidateBundleError(
                "content-addressed candidate snapshot collision"
            ) from exc
        if stored.files != snapshot.files:
            raise CandidateBundleError("content-addressed candidate snapshot collision")
        return target / "candidate.json"

    staging = Path(tempfile.mkdtemp(prefix=".candidate-audit-", dir=parent))
    try:
        for name, content in snapshot.files.items():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(staging / name, flags, 0o444)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(staging, target)
        stored = capture_candidate_audit_snapshot(target, exact_inventory=True)
        if stored.files != snapshot.files:
            raise CandidateBundleError("stored candidate snapshot failed verification")
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return target / "candidate.json"


__all__ = (
    "CandidateAuditSnapshot",
    "CandidateBundleError",
    "capture_candidate_audit_snapshot",
    "capture_candidate_bundle",
    "store_candidate_audit_snapshot",
)
