"""Validate and apply evidence-linked Skill patches atomically."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

from ses.contracts import (
    AddPatchOperation,
    ArtifactRef,
    DeletePatchOperation,
    EvidenceRef,
    FailureCard,
    Patch,
    RunnerStatus,
    UpdatePatchOperation,
)
from ses.evolution.diagnosis import (
    DiagnosisError,
    analyze_fixture,
    require_skill_root_cards,
)
from ses.evolution.evidence import EvidenceError, load_failure_evidence_verified


class PatchValidationError(ValueError):
    """The Patch cannot be applied without risking an invalid candidate."""


EMPTY_CONTENT_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_PATCH_OPERATIONS = 3
MAX_CHANGED_LINES_PER_OPERATION = 12
MAX_CHANGED_LINES_PER_PATCH = 24


def file_content_sha256(content: str) -> str:
    """Hash one runtime file after normalizing line endings."""
    return hashlib.sha256(
        content.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


def validate_runtime_path(target: str) -> None:
    """Accept canonical parent runtime paths without widening patch targets."""
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
    ):
        raise PatchValidationError(f"unsafe Skill runtime path: {target}")


def validate_target(target: str) -> None:
    """Only allow Markdown Skill instructions as patch targets."""
    validate_runtime_path(target)
    if PurePosixPath(target).suffix != ".md":
        raise PatchValidationError(f"unsafe or non-runtime patch target: {target}")


def _pointer_value(value: object, pointer: str) -> object:
    if not pointer.startswith("/"):
        raise PatchValidationError("patch evidence must use a JSON pointer")
    current = value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise PatchValidationError("patch evidence JSON pointer does not exist")
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                raise PatchValidationError("patch evidence JSON pointer does not exist")
            current = current[index]
        else:
            raise PatchValidationError("patch evidence JSON pointer does not exist")
    return current


def _ref_key(reference: EvidenceRef) -> tuple[str, str, str, str]:
    return (
        reference.artifact.root.value,
        reference.artifact.path,
        reference.artifact.sha256,
        reference.json_pointer,
    )


def _verify_refs(
    refs: tuple[EvidenceRef, ...],
    *,
    evidence_artifact: ArtifactRef,
    evidence_bytes: bytes,
    evidence_value: object,
    kind: str,
    expected_pointers: set[str] | None = None,
) -> None:
    if not refs:
        raise PatchValidationError(f"patch operation has no {kind} evidence")
    for reference in refs:
        if reference.artifact != evidence_artifact:
            raise PatchValidationError(
                "patch evidence does not match the verified fixture"
            )
        try:
            reference.artifact.verify_bytes(evidence_bytes)
        except ValueError as exc:
            raise PatchValidationError(
                "patch evidence bytes do not match hash"
            ) from exc
        if (
            expected_pointers is not None
            and reference.json_pointer not in expected_pointers
        ):
            raise PatchValidationError(f"{kind} evidence points at the wrong case")
        pointed = _pointer_value(evidence_value, reference.json_pointer)
        expected_kind = kind.casefold()
        if not isinstance(pointed, Mapping) or pointed.get("kind") != expected_kind:
            raise PatchValidationError(
                f"{kind} evidence pointer does not identify {expected_kind} evidence"
            )


def validate_patch(
    patch: Patch,
    *,
    cards: tuple[FailureCard, ...],
    evidence_path: Path,
) -> object:
    """Validate diagnosis, evidence, targets, and operation identity before apply."""
    try:
        require_skill_root_cards(cards)
    except DiagnosisError as exc:
        raise PatchValidationError(str(exc)) from exc
    try:
        evidence, evidence_bytes, evidence_artifact = load_failure_evidence_verified(
            evidence_path
        )
    except EvidenceError as exc:
        raise PatchValidationError(str(exc)) from exc
    evidence_value = evidence.model_dump(mode="json")
    if evidence.source.skill_sha256 != patch.parent_skill_sha256:
        raise PatchValidationError(
            "evidence Skill hash does not match the Patch parent"
        )
    analysis = analyze_fixture(evidence)
    if not analysis.patch_allowed:
        raise PatchValidationError(
            f"evidence cannot justify a Skill patch: {analysis.reason}"
        )
    if any(card.provenance is not evidence.provenance for card in cards):
        raise PatchValidationError("Failure Card provenance does not match evidence")
    card_by_id = {card.failure_id: card for card in cards}
    if len(card_by_id) != len(cards):
        raise PatchValidationError("Failure Card IDs must be unique")
    case_indexes = {case.case_key: index for index, case in enumerate(evidence.cases)}
    for card in cards:
        if card.case_key not in case_indexes:
            raise PatchValidationError(
                "Failure Card references an unknown evidence case"
            )
        index = case_indexes[card.case_key]
        case = evidence.cases[index]
        if case.skill_status is not RunnerStatus.AGENT_FAIL:
            raise PatchValidationError(
                "Failure Card must reference an observed agent_fail case"
            )
        _verify_refs(
            card.trace_evidence,
            evidence_artifact=evidence_artifact,
            evidence_bytes=evidence_bytes,
            evidence_value=evidence_value,
            kind="Trace",
            expected_pointers={f"/cases/{index}/trace"},
        )
        _verify_refs(
            card.assertion_evidence,
            evidence_artifact=evidence_artifact,
            evidence_bytes=evidence_bytes,
            evidence_value=evidence_value,
            kind="Assertion",
            expected_pointers={f"/cases/{index}/assertion"},
        )
        if case.failure_categories and card.category not in case.failure_categories:
            raise PatchValidationError(
                "Failure Card category conflicts with its explicit evidence label"
            )
    for operation in patch.operations:
        validate_target(operation.target)
        if not operation.failure_card_ids:
            raise PatchValidationError("every operation must name a failure card")
        if len(set(operation.failure_card_ids)) != len(operation.failure_card_ids):
            raise PatchValidationError("operation Failure Card IDs must be unique")
        if any(
            identifier not in card_by_id for identifier in operation.failure_card_ids
        ):
            raise PatchValidationError("operation references an unknown failure card")
        linked_cards = tuple(card_by_id[item] for item in operation.failure_card_ids)
        expected_trace = {
            _ref_key(reference)
            for card in linked_cards
            for reference in card.trace_evidence
        }
        expected_assertion = {
            _ref_key(reference)
            for card in linked_cards
            for reference in card.assertion_evidence
        }
        if {_ref_key(item) for item in operation.trace_evidence} != expected_trace:
            raise PatchValidationError(
                "operation Trace evidence does not match its Failure Cards"
            )
        if {
            _ref_key(item) for item in operation.assertion_evidence
        } != expected_assertion:
            raise PatchValidationError(
                "operation Assertion evidence does not match its Failure Cards"
            )
        _verify_refs(
            operation.trace_evidence,
            evidence_artifact=evidence_artifact,
            evidence_bytes=evidence_bytes,
            evidence_value=evidence_value,
            kind="Trace",
        )
        _verify_refs(
            operation.assertion_evidence,
            evidence_artifact=evidence_artifact,
            evidence_bytes=evidence_bytes,
            evidence_value=evidence_value,
            kind="Assertion",
        )
    return evidence


def _changed_lines(before: str, after: str) -> int:
    matcher = SequenceMatcher(
        a=before.splitlines(), b=after.splitlines(), autojunk=False
    )
    return sum(
        max(a2 - a1, b2 - b1)
        for tag, a1, a2, b1, b2 in matcher.get_opcodes()
        if tag != "equal"
    )


def apply_patch(
    parent_files: Mapping[str, str],
    patch: Patch,
    *,
    cards: tuple[FailureCard, ...],
    evidence_path: Path,
) -> dict[str, str]:
    """Return a new file map, leaving ``parent_files`` untouched on every error."""
    validate_patch(patch, cards=cards, evidence_path=evidence_path)
    if len(patch.operations) > MAX_PATCH_OPERATIONS:
        raise PatchValidationError(
            f"patch exceeds the {MAX_PATCH_OPERATIONS}-operation teaching budget"
        )
    current = dict(parent_files)
    total_changed_lines = 0
    for operation in patch.operations:
        target = operation.target
        actual = (
            file_content_sha256(current[target])
            if target in current
            else EMPTY_CONTENT_SHA256
        )
        if actual != operation.precondition_sha256:
            raise PatchValidationError(f"stale precondition for {target}")
        before = current.get(target, "")
        after = (
            operation.content
            if isinstance(operation, (AddPatchOperation, UpdatePatchOperation))
            else ""
        )
        changed_lines = _changed_lines(before, after)
        if changed_lines > MAX_CHANGED_LINES_PER_OPERATION:
            raise PatchValidationError(
                f"operation for {target} exceeds the "
                f"{MAX_CHANGED_LINES_PER_OPERATION}-line teaching budget"
            )
        total_changed_lines += changed_lines
        if total_changed_lines > MAX_CHANGED_LINES_PER_PATCH:
            raise PatchValidationError(
                f"patch exceeds the {MAX_CHANGED_LINES_PER_PATCH}-line teaching budget"
            )
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
        validate_runtime_path(target)
    return current
