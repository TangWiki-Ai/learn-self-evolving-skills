"""Trusted pre-persistence checks against opaque protected holdouts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ses.testset.holdout import (
    FINAL_COUNT,
    SELECTION_COUNT,
    STATE_BENCH_ARCHIVE_SHA256,
    HoldoutCommitments,
    HoldoutInventory,
    HoldoutManifest,
)
from ses.testset.secure_files import read_regular_file_snapshot


class SplitIdentityDimension(StrEnum):
    SOURCE_ID = "source_id"
    SEMANTIC_GROUP_ID = "semantic_group_id"
    CASE_ID = "case_id"
    CONTENT_HASH = "content_hash"


class SplitValidationStatus(StrEnum):
    FIXED_OFFLINE_UNVERIFIED = "fixed_offline_unverified"
    EXTERNAL_INVENTORY_COMMITMENT_VERIFIED = "external_inventory_commitment_verified"


@dataclass(frozen=True, slots=True)
class DevelopSplitIdentity:
    """Develop identity presented to the trusted verifier, never holdout identity."""

    source_id: str
    semantic_group_id: str
    case_id: str
    content_hash: str


class ProtectedSplitVerifier(Protocol):
    """Verifier seam that never returns protected split identities."""

    @property
    def status(self) -> SplitValidationStatus: ...

    @property
    def provenance_sha256(self) -> str | None: ...

    def conflict_dimension(
        self, identity: DevelopSplitIdentity
    ) -> SplitIdentityDimension | None: ...


def _regular_file_bytes(root: Path, relative: str) -> bytes:
    try:
        return read_regular_file_snapshot(root, relative).data
    except ValueError as exc:
        raise ValueError(
            "protected holdout path has a symlink ancestor or unreadable component"
        ) from exc


@dataclass(frozen=True, slots=True)
class _HoldoutLockSnapshot:
    selection_bytes: bytes
    final_bytes: bytes
    commitments_bytes: bytes
    selection: HoldoutManifest
    final: HoldoutManifest
    inventory_sha256: str


def _snapshot_public_locks(root: Path) -> _HoldoutLockSnapshot:
    selection_bytes = _regular_file_bytes(root, "selection-manifest.json")
    final_bytes = _regular_file_bytes(root, "final-manifest.json")
    commitments_bytes = _regular_file_bytes(root, "holdout-commitments.json")
    selection = HoldoutManifest.model_validate_json(selection_bytes)
    final = HoldoutManifest.model_validate_json(final_bytes)
    commitments = HoldoutCommitments.model_validate_json(commitments_bytes)
    if selection.split != "selection" or final.split != "final":
        raise ValueError("holdout locks identify the wrong split")
    if any(
        manifest.upstream_archive_sha256 != STATE_BENCH_ARCHIVE_SHA256
        for manifest in (selection, final)
    ):
        raise ValueError("holdout locks do not identify the pinned archive")
    if selection.inventory_commitment_sha256 != final.inventory_commitment_sha256:
        raise ValueError("holdout locks disagree on the private inventory")
    if (
        commitments.selection_manifest_sha256
        != hashlib.sha256(selection_bytes).hexdigest()
        or commitments.final_manifest_sha256 != hashlib.sha256(final_bytes).hexdigest()
    ):
        raise ValueError("holdout commitments do not match the opaque locks")
    return _HoldoutLockSnapshot(
        selection_bytes=selection_bytes,
        final_bytes=final_bytes,
        commitments_bytes=commitments_bytes,
        selection=selection,
        final=final,
        inventory_sha256=selection.inventory_commitment_sha256,
    )


class ExternalHoldoutSplitVerifier:
    """Check develop identities against a commitment-verified external inventory."""

    __slots__ = ("_identity_values", "_provenance_sha256")

    def __init__(
        self,
        *,
        identity_values: dict[SplitIdentityDimension, frozenset[str]],
        provenance_sha256: str,
    ) -> None:
        self._identity_values = identity_values
        self._provenance_sha256 = provenance_sha256

    @classmethod
    def from_bundle(
        cls,
        *,
        bundle_root: Path,
        public_lock_root: Path,
    ) -> ExternalHoldoutSplitVerifier:
        public = _snapshot_public_locks(public_lock_root)
        external_locks = (
            _regular_file_bytes(bundle_root, "selection-manifest.json"),
            _regular_file_bytes(bundle_root, "final-manifest.json"),
            _regular_file_bytes(bundle_root, "holdout-commitments.json"),
        )
        if external_locks != (
            public.selection_bytes,
            public.final_bytes,
            public.commitments_bytes,
        ):
            raise ValueError(
                "external holdout locks differ from the public commitments"
            )

        inventory_bytes = _regular_file_bytes(
            bundle_root, "private/holdout-inventory.json"
        )
        inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
        if inventory_sha256 != public.inventory_sha256:
            raise ValueError(
                "external holdout inventory differs from its public commitment"
            )
        inventory = HoldoutInventory.model_validate_json(inventory_bytes)
        selection = public.selection
        final = public.final
        if any(
            (
                inventory.source_commit != manifest.source_commit
                or inventory.upstream_archive_sha256 != manifest.upstream_archive_sha256
            )
            for manifest in (selection, final)
        ):
            raise ValueError("external holdout inventory provenance drifted")

        records = tuple(inventory.records)
        if len(records) != SELECTION_COUNT + FINAL_COUNT:
            raise ValueError("external holdout inventory has the wrong case count")
        for manifest in (selection, final):
            case_ids = tuple(
                record.case_id for record in records if record.split == manifest.split
            )
            if case_ids != manifest.slots:
                raise ValueError("external holdout inventory differs from opaque slots")

        field_by_dimension = {
            SplitIdentityDimension.SOURCE_ID: "source_id",
            SplitIdentityDimension.SEMANTIC_GROUP_ID: "semantic_group_id",
            SplitIdentityDimension.CASE_ID: "case_id",
            SplitIdentityDimension.CONTENT_HASH: "content_hash",
        }
        identity_values = {
            dimension: frozenset(str(getattr(record, field)) for record in records)
            for dimension, field in field_by_dimension.items()
        }
        if any(len(values) != len(records) for values in identity_values.values()):
            raise ValueError("external holdout identities are not four-way unique")
        return cls(
            identity_values=identity_values,
            provenance_sha256=inventory_sha256,
        )

    @property
    def status(self) -> SplitValidationStatus:
        return SplitValidationStatus.EXTERNAL_INVENTORY_COMMITMENT_VERIFIED

    @property
    def provenance_sha256(self) -> str:
        return self._provenance_sha256

    def conflict_dimension(
        self, identity: DevelopSplitIdentity
    ) -> SplitIdentityDimension | None:
        values = {
            SplitIdentityDimension.SOURCE_ID: identity.source_id,
            SplitIdentityDimension.SEMANTIC_GROUP_ID: identity.semantic_group_id,
            SplitIdentityDimension.CASE_ID: identity.case_id,
            SplitIdentityDimension.CONTENT_HASH: identity.content_hash,
        }
        return next(
            (
                dimension
                for dimension, value in values.items()
                if value in self._identity_values[dimension]
            ),
            None,
        )


class FixedOfflineSplitVerifier:
    """Explicitly make no protected-identity claim for fixed course generation."""

    @property
    def status(self) -> SplitValidationStatus:
        return SplitValidationStatus.FIXED_OFFLINE_UNVERIFIED

    @property
    def provenance_sha256(self) -> None:
        return None

    def conflict_dimension(
        self, identity: DevelopSplitIdentity
    ) -> SplitIdentityDimension | None:
        del identity
        return None


def require_trusted_holdout_verifier(
    verifier: ProtectedSplitVerifier | None,
) -> ProtectedSplitVerifier:
    if (
        verifier is None
        or verifier.status
        is not SplitValidationStatus.EXTERNAL_INVENTORY_COMMITMENT_VERIFIED
    ):
        raise ValueError(
            "live/release qualification requires a trusted external holdout verifier"
        )
    return verifier
