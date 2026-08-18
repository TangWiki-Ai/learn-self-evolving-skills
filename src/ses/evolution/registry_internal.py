"""Private records shared by Registry storage, replay, and audit modules."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from ses.contracts import (
    ArtifactRef,
    GateDecision,
    RegistryEvent,
    RegistryEventType,
    SchemaVersion,
    VersionStatus,
)

_ZERO_HASH = "0" * 64
_SAFE_COMMAND = re.compile(r"^command-[a-z0-9][a-z0-9-]{0,95}$")


class RegistryError(ValueError):
    """Registry integrity or state transition validation failed."""


@dataclass(frozen=True, slots=True)
class RegistryVersion:
    """One replayed immutable version node."""

    version_id: str
    skill_sha256: str
    parent_skill_sha256: str | None
    status: VersionStatus
    manifest: ArtifactRef
    candidate: ArtifactRef | None
    gate_decision: ArtifactRef | None
    evidence: tuple[ArtifactRef, ...]
    verified: bool
    was_current: bool


@dataclass(frozen=True, slots=True)
class RegistryState:
    """Complete state rebuilt exclusively from validated events."""

    registry_id: str
    lineage_id: str
    current_accepted_sha256: str
    versions: Mapping[str, RegistryVersion]
    events: tuple[RegistryEvent, ...]


@dataclass(slots=True)
class _MutableVersion:
    version_id: str
    skill_sha256: str
    parent_skill_sha256: str | None
    status: VersionStatus
    manifest: ArtifactRef
    candidate: ArtifactRef | None
    gate_decision: ArtifactRef | None
    evidence: tuple[ArtifactRef, ...]
    verified: bool
    was_current: bool

    def freeze(self) -> RegistryVersion:
        return RegistryVersion(
            version_id=self.version_id,
            skill_sha256=self.skill_sha256,
            parent_skill_sha256=self.parent_skill_sha256,
            status=self.status,
            manifest=self.manifest,
            candidate=self.candidate,
            gate_decision=self.gate_decision,
            evidence=self.evidence,
            verified=self.verified,
            was_current=self.was_current,
        )


@dataclass(frozen=True, slots=True)
class _RegistryTransition:
    """One command intent before sequence and hash-chain fields are assigned."""

    lineage_id: str
    command_id: str
    command_sha256: str
    occurred_at: datetime
    event_type: RegistryEventType
    version_id: str
    version_sha256: str
    parent_skill_sha256: str | None
    previous_accepted_skill_sha256: str | None
    current_accepted_skill_sha256: str
    status: VersionStatus
    version_manifest: ArtifactRef
    candidate: ArtifactRef | None
    gate_decision: ArtifactRef | None
    evidence: tuple[ArtifactRef, ...]
    reason: str

    def event(
        self,
        *,
        registry_id: str,
        prior_events: tuple[RegistryEvent, ...],
    ) -> RegistryEvent:
        sequence = len(prior_events)
        return RegistryEvent(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="registry_event",
            registry_id=registry_id,
            lineage_id=self.lineage_id,
            event_id=f"event-{sequence:06d}-{self.command_sha256[:8]}",
            command_id=self.command_id,
            command_sha256=self.command_sha256,
            sequence=sequence,
            occurred_at=self.occurred_at,
            event_type=self.event_type,
            version_id=self.version_id,
            version_sha256=self.version_sha256,
            parent_skill_sha256=self.parent_skill_sha256,
            previous_accepted_skill_sha256=self.previous_accepted_skill_sha256,
            current_accepted_skill_sha256=self.current_accepted_skill_sha256,
            status=self.status,
            version_manifest=self.version_manifest,
            candidate=self.candidate,
            gate_decision=self.gate_decision,
            evidence=self.evidence,
            reason=self.reason,
            previous_event_sha256=(
                prior_events[-1].event_sha256 if prior_events else _ZERO_HASH
            ),
        )


def _canonical_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _idempotent(
    state: RegistryState,
    command_id: str,
    command_sha256: str,
) -> RegistryEvent | None:
    for event in state.events:
        if event.command_id != command_id:
            continue
        if event.command_sha256 != command_sha256:
            raise RegistryError("command_id conflict with an earlier command")
        return event
    return None


def _protocol_identity(decision: GateDecision) -> tuple[str, str, str, str]:
    return (
        decision.gate_policy_sha256,
        decision.selection_lock_sha256,
        decision.evaluation_protocol_sha256,
        decision.model_lock_sha256,
    )


def _freeze_state(
    *,
    registry_id: str,
    lineage_id: str,
    current: str,
    versions: Mapping[str, _MutableVersion],
    events: tuple[RegistryEvent, ...],
) -> RegistryState:
    frozen = {key: value.freeze() for key, value in versions.items()}
    return RegistryState(
        registry_id=registry_id,
        lineage_id=lineage_id,
        current_accepted_sha256=current,
        versions=MappingProxyType(frozen),
        events=events,
    )


__all__: tuple[str, ...] = ()
