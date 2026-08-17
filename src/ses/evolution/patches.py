"""Validate and apply evidence-linked Skill patches atomically."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from ses.contracts import (
    AddPatchOperation,
    DeletePatchOperation,
    EvidenceRef,
    FailureCard,
    FailureProvenance,
    Patch,
    PatchOperation,
    UpdatePatchOperation,
)
from ses.contracts.artifact import ArtifactRoot
from ses.evolution.diagnosis import (
    DiagnosisError,
    analyze_fixture,
    require_skill_root_cards,
)
from ses.evolution.evidence import EvidenceError, load_failure_evidence, sha256_file


class PatchValidationError(ValueError):
    """The Patch cannot be applied without risking an invalid candidate."""


EMPTY_CONTENT_SHA256 = hashlib.sha256(b"").hexdigest()


def file_content_sha256(content: str) -> str:
    """Hash one runtime file after normalizing line endings."""
    return hashlib.sha256(
        content.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


def validate_target(target: str) -> None:
    """Only allow installable Skill runtime files as patch targets."""
    path = PurePosixPath(target)
    if (
        target != path.as_posix()
        or path.is_absolute()
        or "\\" in target
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
        or target == "skill-manifest.json"
        or (
            target != "SKILL.md"
            and (len(path.parts) < 2 or path.parts[0] != "references")
        )
        or path.suffix != ".md"
    ):
        raise PatchValidationError(f"unsafe or non-runtime patch target: {target}")


def _pointer_exists(value: object, pointer: str) -> bool:
    if not pointer.startswith("/"):
        return False
    current = value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _verify_refs(
    refs: tuple[EvidenceRef, ...],
    *,
    evidence_path: Any,
    evidence_value: object,
    kind: str,
) -> None:
    if not refs:
        raise PatchValidationError(f"patch operation has no {kind} evidence")
    expected_hash = sha256_file(evidence_path)
    for reference in refs:
        if reference.artifact.root is not ArtifactRoot.WORKSPACE:
            raise PatchValidationError("patch evidence must use a workspace reference")
        if PurePosixPath(reference.artifact.path).name != evidence_path.name:
            raise PatchValidationError("patch evidence points at another fixture")
        if reference.artifact.sha256 != expected_hash:
            raise PatchValidationError("patch evidence hash is stale or tampered")
        try:
            reference.artifact.verify_bytes(evidence_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise PatchValidationError(
                "patch evidence bytes do not match hash"
            ) from exc
        if not _pointer_exists(evidence_value, reference.json_pointer):
            raise PatchValidationError("patch evidence JSON pointer does not exist")


def validate_patch(
    patch: Patch,
    *,
    cards: tuple[FailureCard, ...],
    evidence_path: Any,
) -> object:
    """Validate diagnosis, evidence, targets, and operation identity before apply."""
    try:
        require_skill_root_cards(cards)
    except DiagnosisError as exc:
        raise PatchValidationError(str(exc)) from exc
    try:
        actual_evidence_hash = sha256_file(evidence_path)
        for operation in patch.operations:
            for reference in (*operation.trace_evidence, *operation.assertion_evidence):
                if reference.artifact.sha256 != actual_evidence_hash:
                    raise PatchValidationError(
                        "patch evidence hash is stale or tampered"
                    )
        evidence = load_failure_evidence(evidence_path)
    except (EvidenceError, OSError) as exc:
        raise PatchValidationError(str(exc)) from exc
    evidence_value = evidence.model_dump(mode="json")
    analysis = analyze_fixture(evidence)
    if evidence.provenance is FailureProvenance.LIVE and not analysis.patch_allowed:
        raise PatchValidationError(
            f"live evidence cannot justify a Skill patch: {analysis.reason}"
        )
    if any(card.provenance is not evidence.provenance for card in cards):
        raise PatchValidationError("Failure Card provenance does not match evidence")
    card_ids = {card.failure_id for card in cards}
    for operation in patch.operations:
        validate_target(operation.target)
        if not operation.failure_card_ids:
            raise PatchValidationError("every operation must name a failure card")
        if any(identifier not in card_ids for identifier in operation.failure_card_ids):
            raise PatchValidationError("operation references an unknown failure card")
        _verify_refs(
            operation.trace_evidence,
            evidence_path=evidence_path,
            evidence_value=evidence_value,
            kind="Trace",
        )
        _verify_refs(
            operation.assertion_evidence,
            evidence_path=evidence_path,
            evidence_value=evidence_value,
            kind="Assertion",
        )
    return evidence


def _operation_content(operation: PatchOperation) -> str | None:
    if isinstance(operation, (AddPatchOperation, UpdatePatchOperation)):
        return operation.content
    if isinstance(operation, DeletePatchOperation):
        return None
    raise PatchValidationError("unknown patch operation")


def apply_patch(
    parent_files: Mapping[str, str],
    patch: Patch,
    *,
    cards: tuple[FailureCard, ...],
    evidence_path: Any,
) -> dict[str, str]:
    """Return a new file map, leaving ``parent_files`` untouched on every error."""
    validate_patch(patch, cards=cards, evidence_path=evidence_path)
    current = dict(parent_files)
    for operation in patch.operations:
        target = operation.target
        actual = (
            file_content_sha256(current[target])
            if target in current
            else EMPTY_CONTENT_SHA256
        )
        if actual != operation.precondition_sha256:
            raise PatchValidationError(f"stale precondition for {target}")
        if isinstance(operation, AddPatchOperation):
            if target in current:
                raise PatchValidationError(f"add target already exists: {target}")
            current[target] = operation.content
        elif isinstance(operation, UpdatePatchOperation):
            if target not in current:
                raise PatchValidationError(f"update target does not exist: {target}")
            current[target] = operation.content
        elif isinstance(operation, DeletePatchOperation):
            if target not in current:
                raise PatchValidationError(f"delete target does not exist: {target}")
            del current[target]
        else:
            raise PatchValidationError("unknown patch operation")
    if "SKILL.md" not in current:
        raise PatchValidationError("patch would remove the Skill entrypoint")
    for target in current:
        validate_target(target)
    return current
