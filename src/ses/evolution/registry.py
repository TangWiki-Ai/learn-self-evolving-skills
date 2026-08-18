"""Tamper-evident append-only Registry for immutable Skill versions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from ses.contracts import (
    SELECTION_ITERATION_ID,
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    GateAggregateMetrics,
    GateDecision,
    GateErrorEvidence,
    GateOutcome,
    GatePolicy,
    GateReason,
    GateStage,
    GateStepStatus,
    RegistryEvent,
    RegistryEventType,
    RunnerStatus,
    SchemaVersion,
    SelectionPairEvaluation,
    SkillArtifactManifest,
    SkillV0PipelineSummary,
    TriggerEvalResult,
    VersionStatus,
    artifact_json_bytes,
    content_sha256,
)
from ses.evolution.candidate import load_runtime_files
from ses.evolution.gate import validate_trigger_evidence
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256
from ses.skills.static_gate import StaticGateReport, StaticGateStatus

_SAFE_COMMAND = re.compile(r"^command-[a-z0-9][a-z0-9-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO_HASH = "0" * 64


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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SkillRegistry:
    """Own immutable version storage and every legal governance transition."""

    def __init__(self, root: Path, *, registry_id: str = "registry-primary") -> None:
        if ".." in root.parts or root.is_symlink():
            raise RegistryError("registry root must be a canonical real path")
        if not re.fullmatch(r"registry-[a-z0-9-]+", registry_id):
            raise RegistryError("registry_id must be a safe registry identifier")
        self.root = root.resolve()
        self.registry_id = registry_id

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def version_path(self, skill_sha256: str) -> Path:
        if not _SHA256.fullmatch(skill_sha256):
            raise RegistryError("version hash must be lowercase SHA-256")
        return self.root / "versions" / skill_sha256

    def initialize(
        self,
        *,
        command_id: str,
        accepted_skill: Path,
        evidence_paths: Sequence[Path],
        occurred_at: datetime,
    ) -> RegistryEvent:
        """Create a lineage from one previously verified accepted Skill."""

        self._validate_command_id(command_id)
        if not evidence_paths:
            raise RegistryError(
                "registry initialization requires verification evidence"
            )
        try:
            skill_hash = normalized_skill_sha256(accepted_skill)
        except (OSError, ValueError) as exc:
            raise RegistryError("initial accepted Skill is invalid") from exc
        evidence_hashes = self._validate_initial_evidence(
            evidence_paths,
            skill_hash=skill_hash,
        )
        command_sha = _canonical_digest(
            {
                "action": "initialize",
                "evidence_sha256": list(evidence_hashes),
                "skill_sha256": skill_hash,
            }
        )
        if self.events_path.exists():
            state = self.audit()
            existing = self._idempotent(state, command_id, command_sha)
            if existing is not None:
                return existing
            raise RegistryError("registry is already initialized")

        self.root.mkdir(parents=True, exist_ok=True)
        version_dir = self._store_skill(accepted_skill, skill_hash)
        evidence = tuple(self._store_evidence(path) for path in evidence_paths)
        lineage_id = f"lineage-{skill_hash[:16]}"
        event = self._new_event(
            lineage_id=lineage_id,
            command_id=command_id,
            command_sha256=command_sha,
            occurred_at=occurred_at,
            event_type=RegistryEventType.INITIALIZED,
            version_id=f"accepted-{skill_hash[:16]}",
            version_sha256=skill_hash,
            parent_skill_sha256=None,
            previous_accepted_skill_sha256=None,
            current_accepted_skill_sha256=skill_hash,
            status=VersionStatus.ACCEPTED,
            version_manifest=self._ref(version_dir / "skill-manifest.json"),
            candidate=None,
            gate_decision=None,
            evidence=evidence,
            reason="initial accepted Skill verified by prior gate evidence",
            prior_events=(),
        )
        return self._append(event)

    def register_candidate(
        self,
        *,
        command_id: str,
        candidate_bundle: Path,
        occurred_at: datetime,
    ) -> RegistryEvent:
        """Register a candidate and its complete immutable runtime snapshot."""

        self._validate_command_id(command_id)
        state = self.audit()
        candidate, source = self._load_candidate(candidate_bundle)
        command_sha = _canonical_digest(
            {
                "action": "register_candidate",
                "candidate_id": candidate.candidate_id,
                "candidate_sha256": candidate.content_sha256,
                "parent_sha256": candidate.parent_skill_sha256,
            }
        )
        existing = self._idempotent(state, command_id, command_sha)
        if existing is not None:
            return existing
        if candidate.content_sha256 in state.versions:
            raise RegistryError("candidate version is already registered")
        if any(
            version.version_id == candidate.candidate_id
            for version in state.versions.values()
        ):
            raise RegistryError("candidate ID is already registered")
        if candidate.parent_skill_sha256 != state.current_accepted_sha256:
            raise RegistryError("candidate parent is not the current accepted version")

        version_dir = self._store_skill(
            candidate_bundle / "skill", candidate.content_sha256
        )
        candidate_ref = self._store_object(
            "candidates", candidate.candidate_id + ".json", source.read_bytes()
        )
        event = self._new_event(
            lineage_id=state.lineage_id,
            command_id=command_id,
            command_sha256=command_sha,
            occurred_at=occurred_at,
            event_type=RegistryEventType.CANDIDATE_REGISTERED,
            version_id=candidate.candidate_id,
            version_sha256=candidate.content_sha256,
            parent_skill_sha256=candidate.parent_skill_sha256,
            previous_accepted_skill_sha256=state.current_accepted_sha256,
            current_accepted_skill_sha256=state.current_accepted_sha256,
            status=VersionStatus.CANDIDATE,
            version_manifest=self._ref(version_dir / "skill-manifest.json"),
            candidate=candidate_ref,
            gate_decision=None,
            evidence=(),
            reason="immutable candidate registered",
            prior_events=state.events,
        )
        return self._append(event)

    def record_decision(
        self,
        *,
        command_id: str,
        decision_path: Path,
        occurred_at: datetime,
    ) -> RegistryEvent:
        """Append exactly one accepted or rejected terminal candidate decision."""

        self._validate_command_id(command_id)
        state = self.audit()
        decision, decision_ref, selection_pair = self._load_decision(decision_path)
        command_sha = _canonical_digest(
            {
                "action": "record_decision",
                "candidate_id": decision.candidate_id,
                "decision_sha256": decision_ref.sha256,
                "outcome": decision.outcome.value,
            }
        )
        existing = self._idempotent(state, command_id, command_sha)
        if existing is not None:
            return existing
        version = state.versions.get(decision.candidate_skill_sha256)
        if version is None or version.status is not VersionStatus.CANDIDATE:
            raise RegistryError("gate decision requires a registered candidate")
        if (
            version.version_id != decision.candidate_id
            or version.parent_skill_sha256 != decision.accepted_skill_sha256
            or decision.accepted_skill_sha256 != state.current_accepted_sha256
            or decision.lineage_id != state.lineage_id
        ):
            raise RegistryError("gate decision does not match the Registry lineage")
        existing_protocol = self._lineage_protocol_identity(state)
        if (
            existing_protocol is not None
            and existing_protocol != self._protocol_identity(decision)
        ):
            raise RegistryError(
                "gate decision changes the locked protocol within a lineage"
            )
        candidate_ref = version.candidate
        if candidate_ref is None:
            raise RegistryError("registered candidate evidence is missing")
        if decision.candidate.sha256 != candidate_ref.sha256:
            raise RegistryError(
                "gate decision candidate differs from the registered candidate"
            )
        self._ensure_pair_identity_is_fresh(selection_pair, state=state)
        event_type = (
            RegistryEventType.CANDIDATE_ACCEPTED
            if decision.outcome is GateOutcome.ACCEPTED
            else RegistryEventType.CANDIDATE_REJECTED
        )
        status = (
            VersionStatus.ACCEPTED
            if decision.outcome is GateOutcome.ACCEPTED
            else VersionStatus.REJECTED
        )
        evidence = self._decision_evidence(decision)
        event = self._new_event(
            lineage_id=state.lineage_id,
            command_id=command_id,
            command_sha256=command_sha,
            occurred_at=occurred_at,
            event_type=event_type,
            version_id=version.version_id,
            version_sha256=version.skill_sha256,
            parent_skill_sha256=version.parent_skill_sha256,
            previous_accepted_skill_sha256=state.current_accepted_sha256,
            current_accepted_skill_sha256=state.current_accepted_sha256,
            status=status,
            version_manifest=version.manifest,
            candidate=candidate_ref,
            gate_decision=decision_ref,
            evidence=evidence,
            reason=",".join(reason.value for reason in decision.reason_codes),
            prior_events=state.events,
        )
        return self._append(event)

    def promote(
        self,
        *,
        command_id: str,
        candidate_id: str,
        occurred_at: datetime,
    ) -> RegistryEvent:
        """Move the accepted pointer only to a fully gated candidate."""

        self._validate_command_id(command_id)
        state = self.audit()
        command_sha = _canonical_digest(
            {"action": "promote", "candidate_id": candidate_id}
        )
        existing = self._idempotent(state, command_id, command_sha)
        if existing is not None:
            return existing
        matches = [
            version
            for version in state.versions.values()
            if version.version_id == candidate_id
        ]
        if len(matches) != 1:
            raise RegistryError("promotion target is not a registered candidate")
        version = matches[0]
        if (
            version.status is not VersionStatus.ACCEPTED
            or not version.verified
            or version.gate_decision is None
        ):
            raise RegistryError("promotion requires an accepted candidate")
        if version.parent_skill_sha256 != state.current_accepted_sha256:
            raise RegistryError("accepted candidate decision is stale")
        self._verify_version(version)
        self._verify_ref(version.gate_decision)
        event = self._new_event(
            lineage_id=state.lineage_id,
            command_id=command_id,
            command_sha256=command_sha,
            occurred_at=occurred_at,
            event_type=RegistryEventType.PROMOTED,
            version_id=version.version_id,
            version_sha256=version.skill_sha256,
            parent_skill_sha256=version.parent_skill_sha256,
            previous_accepted_skill_sha256=state.current_accepted_sha256,
            current_accepted_skill_sha256=version.skill_sha256,
            status=VersionStatus.ACCEPTED,
            version_manifest=version.manifest,
            candidate=version.candidate,
            gate_decision=version.gate_decision,
            evidence=(version.gate_decision,),
            reason="fully gated candidate promoted",
            prior_events=state.events,
        )
        return self._append(event)

    def rollback(
        self,
        *,
        command_id: str,
        target_skill_sha256: str,
        occurred_at: datetime,
    ) -> RegistryEvent:
        """Move the pointer to a verified version that was accepted before."""

        self._validate_command_id(command_id)
        state = self.audit()
        command_sha = _canonical_digest(
            {"action": "rollback", "target_sha256": target_skill_sha256}
        )
        existing = self._idempotent(state, command_id, command_sha)
        if existing is not None:
            return existing
        target = state.versions.get(target_skill_sha256)
        if (
            target is None
            or not target.verified
            or not target.was_current
            or target.status in {VersionStatus.CANDIDATE, VersionStatus.REJECTED}
            or target.skill_sha256 == state.current_accepted_sha256
        ):
            raise RegistryError("rollback requires a verified historical version")
        self._verify_version(target)
        evidence = (
            (target.gate_decision,)
            if target.gate_decision is not None
            else target.evidence
        )
        if not evidence:
            raise RegistryError("rollback target has no verification evidence")
        for reference in evidence:
            self._verify_ref(reference)
        event = self._new_event(
            lineage_id=state.lineage_id,
            command_id=command_id,
            command_sha256=command_sha,
            occurred_at=occurred_at,
            event_type=RegistryEventType.ROLLED_BACK,
            version_id=target.version_id,
            version_sha256=target.skill_sha256,
            parent_skill_sha256=target.parent_skill_sha256,
            previous_accepted_skill_sha256=state.current_accepted_sha256,
            current_accepted_skill_sha256=target.skill_sha256,
            status=VersionStatus.ACCEPTED,
            version_manifest=target.manifest,
            candidate=target.candidate,
            gate_decision=target.gate_decision,
            evidence=evidence,
            reason="accepted pointer rolled back to verified history",
            prior_events=state.events,
        )
        return self._append(event)

    def audit(self) -> RegistryState:
        """Verify the full hash chain, every artifact, and every transition."""

        if not self.events_path.is_file() or self.events_path.is_symlink():
            raise RegistryError("registry is not initialized")
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RegistryError("registry event log cannot be read") from exc
        if not lines:
            raise RegistryError("registry event log is empty")
        events: list[RegistryEvent] = []
        previous_hash = _ZERO_HASH
        command_ids: set[str] = set()
        event_ids: set[str] = set()
        for sequence, line in enumerate(lines):
            try:
                raw = json.loads(line)
                if (
                    not isinstance(raw, Mapping)
                    or raw.get("event_sha256") == _ZERO_HASH
                ):
                    raise ValueError("stored event hash is missing")
                event = RegistryEvent.model_validate_json(line)
                if artifact_json_bytes(event).decode("utf-8") != line:
                    raise ValueError("event is not canonical wire JSON")
            except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                raise RegistryError(
                    f"invalid registry event or hash at line {sequence + 1}"
                ) from exc
            if event.sequence != sequence:
                raise RegistryError("registry event sequence is not contiguous")
            if event.previous_event_sha256 != previous_hash:
                raise RegistryError("registry previous-event hash chain is broken")
            if event.registry_id != self.registry_id:
                raise RegistryError("registry event belongs to another Registry")
            if event.command_id in command_ids or event.event_id in event_ids:
                raise RegistryError("registry event or command ID is duplicated")
            command_ids.add(event.command_id)
            event_ids.add(event.event_id)
            previous_hash = event.event_sha256
            events.append(event)
        return self._replay(tuple(events))

    def _replay(self, events: tuple[RegistryEvent, ...]) -> RegistryState:
        versions: dict[str, _MutableVersion] = {}
        current: str | None = None
        lineage_id = events[0].lineage_id
        protocol_identity: tuple[str, str, str, str] | None = None
        selection_nonces: set[str] = set()
        selection_run_ids: set[str] = set()
        for event in events:
            if event.lineage_id != lineage_id:
                raise RegistryError("registry events cross experiment lineages")
            self._verify_ref(event.version_manifest)
            for reference in (
                *(event.evidence),
                *(() if event.candidate is None else (event.candidate,)),
                *(() if event.gate_decision is None else (event.gate_decision,)),
            ):
                self._verify_ref(reference)
            if event.event_type is RegistryEventType.INITIALIZED:
                if versions or current is not None:
                    raise RegistryError("registry contains duplicate initialization")
                self._verify_initial_event_evidence(event)
                versions[event.version_sha256] = _MutableVersion(
                    version_id=event.version_id,
                    skill_sha256=event.version_sha256,
                    parent_skill_sha256=None,
                    status=VersionStatus.ACCEPTED,
                    manifest=event.version_manifest,
                    candidate=None,
                    gate_decision=None,
                    evidence=event.evidence,
                    verified=True,
                    was_current=True,
                )
                current = event.version_sha256
            elif current is None:
                raise RegistryError("registry transition precedes initialization")
            elif event.previous_accepted_skill_sha256 != current:
                raise RegistryError("registry transition used a stale accepted pointer")
            elif event.event_type is RegistryEventType.CANDIDATE_REGISTERED:
                if (
                    event.version_sha256 in versions
                    or any(
                        version.version_id == event.version_id
                        for version in versions.values()
                    )
                    or event.parent_skill_sha256 != current
                    or event.current_accepted_skill_sha256 != current
                    or event.candidate is None
                    or event.evidence
                ):
                    raise RegistryError("invalid candidate registration transition")
                candidate = self._candidate_from_ref(event.candidate)
                if (
                    candidate.candidate_id != event.version_id
                    or candidate.content_sha256 != event.version_sha256
                    or candidate.parent_skill_sha256 != current
                ):
                    raise RegistryError("candidate object does not match its event")
                manifest = self._manifest_from_ref(event.version_manifest)
                if manifest != candidate.manifest:
                    raise RegistryError(
                        "candidate manifest does not match its registered object"
                    )
                versions[event.version_sha256] = _MutableVersion(
                    version_id=event.version_id,
                    skill_sha256=event.version_sha256,
                    parent_skill_sha256=current,
                    status=VersionStatus.CANDIDATE,
                    manifest=event.version_manifest,
                    candidate=event.candidate,
                    gate_decision=None,
                    evidence=event.evidence,
                    verified=False,
                    was_current=False,
                )
            elif event.event_type in {
                RegistryEventType.CANDIDATE_ACCEPTED,
                RegistryEventType.CANDIDATE_REJECTED,
            }:
                version = versions.get(event.version_sha256)
                if version is None or version.status is not VersionStatus.CANDIDATE:
                    raise RegistryError("candidate decision transition is invalid")
                decision = self._decision_from_ref(event.gate_decision)
                expected_outcome = (
                    GateOutcome.ACCEPTED
                    if event.event_type is RegistryEventType.CANDIDATE_ACCEPTED
                    else GateOutcome.REJECTED
                )
                if (
                    decision.outcome is not expected_outcome
                    or decision.candidate_id != version.version_id
                    or decision.candidate_skill_sha256 != version.skill_sha256
                    or decision.accepted_skill_sha256 != current
                    or decision.lineage_id != lineage_id
                    or event.current_accepted_skill_sha256 != current
                    or event.version_id != version.version_id
                    or event.parent_skill_sha256 != version.parent_skill_sha256
                    or event.version_manifest != version.manifest
                    or event.candidate != version.candidate
                    or event.status
                    is not (
                        VersionStatus.ACCEPTED
                        if expected_outcome is GateOutcome.ACCEPTED
                        else VersionStatus.REJECTED
                    )
                ):
                    raise RegistryError("gate decision does not match its transition")
                if version.candidate is None or (
                    decision.candidate.sha256 != version.candidate.sha256
                ):
                    raise RegistryError(
                        "gate decision candidate differs from the registered candidate"
                    )
                pair = self._verify_gate_decision(decision)
                self._claim_pair_identity(
                    pair,
                    nonces=selection_nonces,
                    run_ids=selection_run_ids,
                )
                expected_evidence = self._decision_evidence(decision)
                if event.evidence != expected_evidence or event.reason != ",".join(
                    reason.value for reason in decision.reason_codes
                ):
                    raise RegistryError(
                        "candidate decision event evidence is inconsistent"
                    )
                decision_protocol = self._protocol_identity(decision)
                if (
                    protocol_identity is not None
                    and protocol_identity != decision_protocol
                ):
                    raise RegistryError(
                        "gate decisions change protocol within one lineage"
                    )
                protocol_identity = decision_protocol
                version.status = event.status
                version.gate_decision = event.gate_decision
                version.evidence = event.evidence
                version.verified = event.status is VersionStatus.ACCEPTED
            elif event.event_type is RegistryEventType.PROMOTED:
                version = versions.get(event.version_sha256)
                if (
                    version is None
                    or version.status is not VersionStatus.ACCEPTED
                    or not version.verified
                    or version.parent_skill_sha256 != current
                    or event.current_accepted_skill_sha256 != version.skill_sha256
                    or event.version_id != version.version_id
                    or event.parent_skill_sha256 != version.parent_skill_sha256
                    or event.version_manifest != version.manifest
                    or event.candidate != version.candidate
                    or event.gate_decision != version.gate_decision
                    or event.evidence != (version.gate_decision,)
                    or event.reason != "fully gated candidate promoted"
                ):
                    raise RegistryError("promotion transition is invalid")
                current = version.skill_sha256
                version.was_current = True
            elif event.event_type is RegistryEventType.ROLLED_BACK:
                target = versions.get(event.version_sha256)
                source = versions.get(current)
                if (
                    target is None
                    or source is None
                    or not target.verified
                    or not target.was_current
                    or target.status
                    in {
                        VersionStatus.CANDIDATE,
                        VersionStatus.REJECTED,
                    }
                    or target.skill_sha256 == current
                    or event.current_accepted_skill_sha256 != target.skill_sha256
                    or event.version_id != target.version_id
                    or event.parent_skill_sha256 != target.parent_skill_sha256
                    or event.version_manifest != target.manifest
                    or event.candidate != target.candidate
                    or event.gate_decision != target.gate_decision
                    or event.evidence
                    != (
                        (target.gate_decision,)
                        if target.gate_decision is not None
                        else target.evidence
                    )
                    or event.reason
                    != "accepted pointer rolled back to verified history"
                ):
                    raise RegistryError("rollback transition is invalid")
                source.status = VersionStatus.ROLLED_BACK
                target.status = VersionStatus.ACCEPTED
                current = target.skill_sha256
            else:
                raise RegistryError("unsupported registry transition")
            if current != event.current_accepted_skill_sha256:
                raise RegistryError("event accepted pointer does not match replay")
            self._verify_version(versions[event.version_sha256].freeze())
        assert current is not None
        frozen = {key: value.freeze() for key, value in versions.items()}
        return RegistryState(
            registry_id=self.registry_id,
            lineage_id=lineage_id,
            current_accepted_sha256=current,
            versions=MappingProxyType(frozen),
            events=events,
        )

    def _validate_command_id(self, command_id: str) -> None:
        if not _SAFE_COMMAND.fullmatch(command_id):
            raise RegistryError("command_id must be a safe command identifier")

    def _validate_initial_evidence(
        self,
        evidence_paths: Sequence[Path],
        *,
        skill_hash: str,
    ) -> tuple[str, ...]:
        hashes: list[str] = []
        verified = False
        for path in evidence_paths:
            if path.is_symlink() or not path.is_file():
                raise RegistryError("verification evidence must be a regular file")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise RegistryError("verification evidence cannot be read") from exc
            hashes.append(hashlib.sha256(content).hexdigest())
            try:
                summary = SkillV0PipelineSummary.model_validate_json(content)
            except (OSError, ValueError):
                continue
            if summary.skill_sha256 == skill_hash and summary.static_gate == "pass":
                verified = True
        if not verified:
            raise RegistryError(
                "initial evidence does not identify a verified accepted Skill"
            )
        return tuple(hashes)

    def _verify_initial_event_evidence(self, event: RegistryEvent) -> None:
        verified = False
        for reference in event.evidence:
            path = self._verify_ref(reference)
            try:
                summary = SkillV0PipelineSummary.model_validate_json(path.read_bytes())
            except ValueError:
                continue
            if (
                summary.skill_sha256 == event.version_sha256
                and summary.static_gate == "pass"
            ):
                verified = True
        if not verified:
            raise RegistryError(
                "registry initialization evidence does not verify its Skill"
            )

    def _idempotent(
        self, state: RegistryState, command_id: str, command_sha256: str
    ) -> RegistryEvent | None:
        for event in state.events:
            if event.command_id != command_id:
                continue
            if event.command_sha256 != command_sha256:
                raise RegistryError("command_id conflict with an earlier command")
            return event
        return None

    @staticmethod
    def _protocol_identity(decision: GateDecision) -> tuple[str, str, str, str]:
        return (
            decision.gate_policy_sha256,
            decision.selection_lock_sha256,
            decision.evaluation_protocol_sha256,
            decision.model_lock_sha256,
        )

    def _lineage_protocol_identity(
        self,
        state: RegistryState,
    ) -> tuple[str, str, str, str] | None:
        identity: tuple[str, str, str, str] | None = None
        for version in state.versions.values():
            if version.gate_decision is None:
                continue
            current = self._protocol_identity(
                self._decision_from_ref(version.gate_decision)
            )
            if identity is not None and identity != current:
                raise RegistryError("Registry lineage contains mixed gate protocols")
            identity = current
        return identity

    @staticmethod
    def _claim_pair_identity(
        pair: SelectionPairEvaluation | None,
        *,
        nonces: set[str],
        run_ids: set[str],
    ) -> None:
        if pair is None:
            return
        pair_run_ids = {pair.accepted_run_id, pair.candidate_run_id}
        if pair.evaluation_nonce in nonces or pair_run_ids & run_ids:
            raise RegistryError(
                "selection nonce and run IDs must be fresh within the lineage"
            )
        nonces.add(pair.evaluation_nonce)
        run_ids.update(pair_run_ids)

    def _ensure_pair_identity_is_fresh(
        self,
        pair: SelectionPairEvaluation | None,
        *,
        state: RegistryState,
    ) -> None:
        if pair is None:
            return
        nonces: set[str] = set()
        run_ids: set[str] = set()
        for version in state.versions.values():
            if version.gate_decision is None:
                continue
            decision = self._decision_from_ref(version.gate_decision)
            self._claim_pair_identity(
                self._selection_pair_from_decision(decision),
                nonces=nonces,
                run_ids=run_ids,
            )
        self._claim_pair_identity(pair, nonces=nonces, run_ids=run_ids)

    def _new_event(
        self,
        *,
        lineage_id: str,
        command_id: str,
        command_sha256: str,
        occurred_at: datetime,
        event_type: RegistryEventType,
        version_id: str,
        version_sha256: str,
        parent_skill_sha256: str | None,
        previous_accepted_skill_sha256: str | None,
        current_accepted_skill_sha256: str,
        status: VersionStatus,
        version_manifest: ArtifactRef,
        candidate: ArtifactRef | None,
        gate_decision: ArtifactRef | None,
        evidence: tuple[ArtifactRef, ...],
        reason: str,
        prior_events: tuple[RegistryEvent, ...],
    ) -> RegistryEvent:
        sequence = len(prior_events)
        return RegistryEvent(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="registry_event",
            registry_id=self.registry_id,
            lineage_id=lineage_id,
            event_id=f"event-{sequence:06d}-{command_sha256[:8]}",
            command_id=command_id,
            command_sha256=command_sha256,
            sequence=sequence,
            occurred_at=occurred_at,
            event_type=event_type,
            version_id=version_id,
            version_sha256=version_sha256,
            parent_skill_sha256=parent_skill_sha256,
            previous_accepted_skill_sha256=previous_accepted_skill_sha256,
            current_accepted_skill_sha256=current_accepted_skill_sha256,
            status=status,
            version_manifest=version_manifest,
            candidate=candidate,
            gate_decision=gate_decision,
            evidence=evidence,
            reason=reason,
            previous_event_sha256=(
                prior_events[-1].event_sha256 if prior_events else _ZERO_HASH
            ),
        )

    def _append(self, event: RegistryEvent) -> RegistryEvent:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".append.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise RegistryError("Registry append lock cannot be opened") from exc
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if self.events_path.is_symlink():
                raise RegistryError("registry event log cannot be a symlink")
            if self.events_path.exists():
                state = self.audit()
                existing = self._idempotent(
                    state,
                    event.command_id,
                    event.command_sha256,
                )
                if existing is not None:
                    return existing
                if (
                    event.sequence != len(state.events)
                    or event.previous_event_sha256 != state.events[-1].event_sha256
                ):
                    raise RegistryError("Registry changed during the command")
            elif event.sequence != 0 or event.previous_event_sha256 != _ZERO_HASH:
                raise RegistryError("Registry changed during the command")
            with self.events_path.open("ab") as stream:
                stream.write(artifact_json_bytes(event) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            return event

    def _store_skill(self, source: Path, expected_hash: str) -> Path:
        target = self.version_path(expected_hash)
        if target.exists():
            if normalized_skill_sha256(target) != expected_hash:
                raise RegistryError("stored version content was tampered with")
            return target
        try:
            manifest = load_skill_manifest(source)
            actual_hash = normalized_skill_sha256(source)
        except (OSError, ValueError) as exc:
            raise RegistryError("Skill version cannot be stored") from exc
        if actual_hash != expected_hash:
            raise RegistryError("Skill version hash does not match its record")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".version-", dir=target.parent))
        try:
            for item in manifest.files:
                relative = PurePosixPath(item.path)
                source_file = source / relative
                if source_file.is_symlink() or not source_file.is_file():
                    raise RegistryError("Skill runtime file is not immutable")
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, destination, follow_symlinks=False)
            shutil.copyfile(
                source / "skill-manifest.json",
                staging / "skill-manifest.json",
                follow_symlinks=False,
            )
            if normalized_skill_sha256(staging) != expected_hash:
                raise RegistryError("stored Skill snapshot failed verification")
            os.replace(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return target

    def _store_object(self, family: str, name: str, content: bytes) -> ArtifactRef:
        target = self.root / "objects" / family / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_symlink() or target.read_bytes() != content:
                raise RegistryError("immutable Registry object already differs")
        else:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return self._ref(target)

    def _store_evidence(self, source: Path) -> ArtifactRef:
        if source.is_symlink() or not source.is_file():
            raise RegistryError("verification evidence must be a regular file")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        suffix = source.suffix if source.suffix in {".json", ".jsonl"} else ".bin"
        return self._store_object("evidence", digest + suffix, content)

    def _ref(self, path: Path) -> ArtifactRef:
        try:
            relative = path.resolve(strict=True).relative_to(self.root.resolve())
        except (OSError, ValueError) as exc:
            raise RegistryError("Registry artifact escapes its root") from exc
        return ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=relative.as_posix(),
            sha256=_file_sha256(path),
        )

    def _verify_ref(self, reference: ArtifactRef) -> Path:
        if reference.root is not ArtifactRoot.WORKSPACE:
            raise RegistryError("Registry artifacts must use the workspace root")
        path = self.root / reference.path
        try:
            path.resolve(strict=True).relative_to(self.root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise RegistryError("Registry artifact escapes its root") from exc
        if path.is_symlink() or not path.is_file():
            raise RegistryError("Registry artifact is not a regular file")
        try:
            reference.verify_bytes(path.read_bytes())
        except ValueError as exc:
            raise RegistryError("Registry artifact hash mismatch") from exc
        return path

    def _verify_version(self, version: RegistryVersion) -> None:
        manifest_path = self._verify_ref(version.manifest)
        expected = self.version_path(version.skill_sha256)
        if manifest_path.parent != expected:
            raise RegistryError("version manifest path does not match its content hash")
        try:
            actual = normalized_skill_sha256(expected)
        except (OSError, ValueError) as exc:
            raise RegistryError("stored version failed integrity validation") from exc
        if actual != version.skill_sha256:
            raise RegistryError("stored version content hash mismatch")

    def _load_candidate(self, bundle: Path) -> tuple[CandidateArtifact, Path]:
        source = bundle / "candidate.json"
        skill = bundle / "skill"
        try:
            candidate = CandidateArtifact.model_validate_json(
                source.read_text(encoding="utf-8")
            )
            actual_hash = normalized_skill_sha256(skill)
            files = load_runtime_files(skill)
            manifest = load_skill_manifest(skill)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("candidate bundle is invalid") from exc
        if (
            actual_hash != candidate.content_sha256
            or files != dict(candidate.files)
            or manifest != candidate.manifest
        ):
            raise RegistryError("candidate bundle differs from its record")
        return candidate, source

    def _candidate_from_ref(self, reference: ArtifactRef) -> CandidateArtifact:
        path = self._verify_ref(reference)
        try:
            return CandidateArtifact.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("stored candidate object is invalid") from exc

    def _load_decision(
        self, path: Path
    ) -> tuple[GateDecision, ArtifactRef, SelectionPairEvaluation | None]:
        try:
            path.resolve(strict=True).relative_to(self.root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise RegistryError(
                "gate decision must be inside the Registry root"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise RegistryError("gate decision must be a regular file")
        try:
            decision = GateDecision.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate decision is invalid") from exc
        pair = self._verify_gate_decision(decision)
        return decision, self._ref(path), pair

    def _decision_from_ref(self, reference: ArtifactRef | None) -> GateDecision:
        if reference is None:
            raise RegistryError("candidate transition is missing its gate decision")
        path = self._verify_ref(reference)
        try:
            return GateDecision.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("stored gate decision is invalid") from exc

    def _manifest_from_ref(self, reference: ArtifactRef) -> SkillArtifactManifest:
        path = self._verify_ref(reference)
        try:
            return SkillArtifactManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("stored Skill manifest is invalid") from exc

    def _verify_error_evidence(
        self,
        reference: ArtifactRef,
        *,
        stage: GateStage,
    ) -> None:
        path = self._verify_ref(reference)
        try:
            evidence = GateErrorEvidence.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate error evidence is invalid") from exc
        if evidence.stage is not stage:
            raise RegistryError("gate error evidence does not match its stage")

    def _selection_pair_from_decision(
        self, decision: GateDecision
    ) -> SelectionPairEvaluation | None:
        selection = next(
            step for step in decision.steps if step.stage is GateStage.SELECTION
        )
        if len(selection.evidence) == 1 and selection.status in {
            GateStepStatus.FAIL,
            GateStepStatus.ERROR,
        }:
            self._verify_error_evidence(
                selection.evidence[0],
                stage=GateStage.SELECTION,
            )
            return None
        if selection.status not in {
            GateStepStatus.PASS,
            GateStepStatus.ERROR,
            GateStepStatus.BUDGET_STOP,
        }:
            return None
        if not selection.evidence:
            raise RegistryError("gate selection evidence is missing")
        pair_path = self._verify_ref(selection.evidence[0])
        try:
            return SelectionPairEvaluation.model_validate_json(
                pair_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate selection pair evidence is invalid") from exc

    def _verify_gate_decision(
        self, decision: GateDecision
    ) -> SelectionPairEvaluation | None:
        references = [
            decision.candidate,
            decision.accepted_manifest,
            decision.gate_policy,
        ]
        references.extend(
            reference for step in decision.steps for reference in step.evidence
        )
        for reference in references:
            self._verify_ref(reference)

        candidate = self._candidate_from_ref(decision.candidate)
        if (
            candidate.candidate_id != decision.candidate_id
            or candidate.content_sha256 != decision.candidate_skill_sha256
            or candidate.parent_skill_sha256 != decision.accepted_skill_sha256
        ):
            raise RegistryError("gate decision candidate evidence does not match")

        manifest = self._manifest_from_ref(decision.accepted_manifest)
        if manifest.content_sha256 != decision.accepted_skill_sha256:
            raise RegistryError("gate accepted manifest does not match its version")

        policy_path = self._verify_ref(decision.gate_policy)
        try:
            policy = GatePolicy.model_validate_json(
                policy_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("gate policy evidence is invalid") from exc
        if (
            content_sha256(policy) != decision.gate_policy_sha256
            or policy.selection_lock_sha256 != decision.selection_lock_sha256
            or policy.evaluation_protocol_sha256 != decision.evaluation_protocol_sha256
            or policy.model_lock_sha256 != decision.model_lock_sha256
        ):
            raise RegistryError("gate decision does not match its locked policy")

        candidate_step = next(
            step
            for step in decision.steps
            if step.stage is GateStage.CANDIDATE_VALIDATION
        )
        candidate_evidence = (decision.candidate, decision.accepted_manifest)
        if candidate_step.status is GateStepStatus.PASS:
            if candidate_step.evidence != candidate_evidence:
                raise RegistryError("candidate validation evidence is incomplete")
        elif candidate_step.status is GateStepStatus.FAIL:
            if (
                len(candidate_step.evidence) != 3
                or candidate_step.evidence[:2] != candidate_evidence
            ):
                raise RegistryError("candidate rejection evidence is incomplete")
            self._verify_error_evidence(
                candidate_step.evidence[2],
                stage=GateStage.CANDIDATE_VALIDATION,
            )

        static_step = next(
            step for step in decision.steps if step.stage is GateStage.STATIC
        )
        static_report: StaticGateReport | None = None
        if static_step.status is GateStepStatus.ERROR:
            if len(static_step.evidence) != 1:
                raise RegistryError("gate Static error evidence is incomplete")
            self._verify_error_evidence(
                static_step.evidence[0],
                stage=GateStage.STATIC,
            )
        elif static_step.evidence:
            if len(static_step.evidence) != 1:
                raise RegistryError("gate Static step must bind exactly one report")
            static_path = self._verify_ref(static_step.evidence[0])
            try:
                static_report = StaticGateReport.model_validate_json(
                    static_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise RegistryError("gate Static evidence is invalid") from exc
        if static_step.status is GateStepStatus.PASS and (
            static_report is None
            or static_report.status is not StaticGateStatus.PASS
            or static_report.skill_sha256 != decision.candidate_skill_sha256
            or not static_report.checks
            or not all(check.passed for check in static_report.checks)
        ):
            raise RegistryError(
                "passing gate Static evidence does not verify the candidate"
            )
        if static_step.status is not GateStepStatus.PASS and static_report is not None:
            report_failed = (
                static_report.status is StaticGateStatus.FAIL
                or static_report.skill_sha256 != decision.candidate_skill_sha256
                or not static_report.checks
                or not all(check.passed for check in static_report.checks)
            )
            if not report_failed:
                raise RegistryError("failed gate Static step carries a passing report")

        trigger_step = next(
            step for step in decision.steps if step.stage is GateStage.TRIGGER
        )
        trigger: TriggerEvalResult | None = None
        trigger_cost = Decimal(0)
        expected_unpaired_metrics = GateAggregateMetrics(
            cost_currency=policy.cost_currency
        )
        if trigger_step.status is GateStepStatus.PASS:
            if len(trigger_step.evidence) != 1:
                raise RegistryError("gate trigger evidence is missing")
            trigger_path = self._verify_ref(trigger_step.evidence[0])
            try:
                trigger = TriggerEvalResult.model_validate_json(
                    trigger_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise RegistryError("gate trigger evidence is invalid") from exc
            try:
                trigger_cost = validate_trigger_evidence(
                    trigger,
                    policy=policy,
                    skill_sha256=decision.candidate_skill_sha256,
                    measurement_kind=decision.measurement_kind,
                    measured_at=decision.decided_at,
                    mode=decision.mode,
                )
            except ValueError as exc:
                raise RegistryError(
                    "gate trigger evidence does not match its decision"
                ) from exc
            if (
                trigger.precision < policy.min_trigger_precision
                or trigger.recall < policy.min_trigger_recall
                or trigger.indeterminate_count > policy.max_trigger_indeterminate
            ):
                raise RegistryError("passing gate Trigger violates its locked policy")
            expected_unpaired_metrics = GateAggregateMetrics(
                trigger_precision=trigger.precision,
                trigger_recall=trigger.recall,
                trigger_indeterminate_count=trigger.indeterminate_count,
                trigger_cost_amount=trigger_cost,
                total_cost_amount=trigger_cost,
                cost_currency=policy.cost_currency,
                total_input_tokens=trigger.usage.input_tokens,
                total_output_tokens=trigger.usage.output_tokens,
            )
        elif trigger_step.status is GateStepStatus.FAIL:
            trigger_path = self._verify_ref(trigger_step.evidence[0])
            try:
                failed_trigger = TriggerEvalResult.model_validate_json(
                    trigger_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise RegistryError("failed gate Trigger evidence is invalid") from exc
            failed_cost = Decimal(0)
            evidence_failed = False
            try:
                failed_cost = validate_trigger_evidence(
                    failed_trigger,
                    policy=policy,
                    skill_sha256=decision.candidate_skill_sha256,
                    measurement_kind=decision.measurement_kind,
                    measured_at=decision.decided_at,
                    mode=decision.mode,
                )
            except ValueError:
                evidence_failed = True
            threshold_failed = (
                failed_trigger.precision < policy.min_trigger_precision
                or failed_trigger.recall < policy.min_trigger_recall
                or failed_trigger.indeterminate_count > policy.max_trigger_indeterminate
            )
            if not evidence_failed and not threshold_failed:
                raise RegistryError("failed gate Trigger satisfies its locked policy")
            expected_unpaired_metrics = GateAggregateMetrics(
                trigger_precision=failed_trigger.precision,
                trigger_recall=failed_trigger.recall,
                trigger_indeterminate_count=failed_trigger.indeterminate_count,
                trigger_cost_amount=failed_cost,
                total_cost_amount=failed_cost,
                cost_currency=policy.cost_currency,
                total_input_tokens=failed_trigger.usage.input_tokens,
                total_output_tokens=failed_trigger.usage.output_tokens,
            )
        elif trigger_step.status is GateStepStatus.ERROR:
            if len(trigger_step.evidence) != 1:
                raise RegistryError("gate Trigger error evidence is incomplete")
            self._verify_error_evidence(
                trigger_step.evidence[0],
                stage=GateStage.TRIGGER,
            )

        selection = next(
            step for step in decision.steps if step.stage is GateStage.SELECTION
        )
        pair = self._selection_pair_from_decision(decision)
        if pair is not None:
            expected_nonce = hashlib.sha256(
                (
                    decision.gate_id
                    + decision.candidate_skill_sha256
                    + decision.decided_at.isoformat()
                ).encode("utf-8")
            ).hexdigest()
            if (
                pair.gate_id != decision.gate_id
                or pair.evaluation_nonce != expected_nonce
                or pair.iteration_id != SELECTION_ITERATION_ID
                or pair.accepted_skill_sha256 != decision.accepted_skill_sha256
                or pair.candidate_skill_sha256 != decision.candidate_skill_sha256
                or pair.selection_lock_sha256 != decision.selection_lock_sha256
                or pair.evaluation_protocol_sha256
                != decision.evaluation_protocol_sha256
                or pair.model_lock_sha256 != decision.model_lock_sha256
                or pair.measurement_kind is not decision.measurement_kind
                or pair.measured_at != decision.decided_at
                or pair.cost_currency != policy.cost_currency
                or len(pair.cases) != policy.selection_case_count
                or tuple(row.slot for row in pair.cases) != policy.selection_slots
                or tuple(row.slot for row in pair.cases if row.critical)
                != policy.critical_slots
            ):
                raise RegistryError("gate selection pair does not match its decision")
            expected_selection_evidence = (
                selection.evidence[0],
                pair.accepted_events,
                pair.candidate_events,
            )
            if selection.evidence != expected_selection_evidence:
                raise RegistryError("gate selection evidence references are incomplete")
            pair_ref = selection.evidence[0]
            for stage in (
                GateStage.CRITICAL_REGRESSION,
                GateStage.OVERALL_QUALITY,
                GateStage.COST,
                GateStage.BUDGET,
            ):
                step = next(item for item in decision.steps if item.stage is stage)
                if (
                    step.status is not GateStepStatus.NOT_EVALUATED
                    and step.evidence != (pair_ref,)
                ):
                    raise RegistryError(
                        "downstream gate evidence does not bind the selection pair"
                    )
            self._verify_pair_event_log(pair, side="accepted")
            self._verify_pair_event_log(pair, side="candidate")
        if pair is not None:
            if trigger is None:
                raise RegistryError("paired gate decision lacks Trigger evidence")
            self._verify_measured_decision(
                decision,
                policy=policy,
                trigger=trigger,
                trigger_cost=trigger_cost,
                pair=pair,
            )
        elif decision.outcome is GateOutcome.ACCEPTED:
            raise RegistryError("accepted gate decision lacks measured evidence")
        elif decision.metrics != expected_unpaired_metrics:
            raise RegistryError("unpaired gate metrics do not match their evidence")
        return pair

    def _verify_measured_decision(
        self,
        decision: GateDecision,
        *,
        policy: GatePolicy,
        trigger: TriggerEvalResult,
        trigger_cost: Decimal,
        pair: SelectionPairEvaluation,
    ) -> None:
        metrics = decision.metrics
        count = len(pair.cases)
        accepted_passes = sum(
            row.accepted_status is RunnerStatus.PASS for row in pair.cases
        )
        candidate_passes = sum(
            row.candidate_status is RunnerStatus.PASS for row in pair.cases
        )
        accepted_cost = sum(
            (row.accepted_cost_amount for row in pair.cases),
            Decimal(0),
        )
        candidate_cost = sum(
            (row.candidate_cost_amount for row in pair.cases),
            Decimal(0),
        )
        relative_cost = (
            (candidate_cost - accepted_cost) / accepted_cost
            if accepted_cost > 0 and candidate_cost > accepted_cost
            else Decimal(0)
            if candidate_cost <= accepted_cost
            else None
        )
        critical_regressions = sum(
            row.critical
            and row.accepted_status is RunnerStatus.PASS
            and row.candidate_status is not RunnerStatus.PASS
            for row in pair.cases
        )
        input_tokens = trigger.usage.input_tokens + sum(
            row.accepted_input_tokens + row.candidate_input_tokens for row in pair.cases
        )
        output_tokens = trigger.usage.output_tokens + sum(
            row.accepted_output_tokens + row.candidate_output_tokens
            for row in pair.cases
        )
        expected = (
            trigger.precision,
            trigger.recall,
            trigger.indeterminate_count,
            count,
            accepted_passes,
            candidate_passes,
            accepted_passes / count,
            candidate_passes / count,
            (candidate_passes - accepted_passes) / count,
            critical_regressions,
            trigger_cost,
            accepted_cost,
            candidate_cost,
            trigger_cost + accepted_cost + candidate_cost,
            relative_cost,
            pair.cost_currency,
            input_tokens,
            output_tokens,
        )
        actual = (
            metrics.trigger_precision,
            metrics.trigger_recall,
            metrics.trigger_indeterminate_count,
            metrics.selection_case_count,
            metrics.accepted_pass_count,
            metrics.candidate_pass_count,
            metrics.accepted_pass_rate,
            metrics.candidate_pass_rate,
            metrics.quality_delta,
            metrics.critical_regression_count,
            metrics.trigger_cost_amount,
            metrics.accepted_cost_amount,
            metrics.candidate_cost_amount,
            metrics.total_cost_amount,
            metrics.relative_cost_increase,
            metrics.cost_currency,
            metrics.total_input_tokens,
            metrics.total_output_tokens,
        )
        if actual != expected:
            raise RegistryError("gate metrics do not match their measured evidence")
        statuses = {row.accepted_status for row in pair.cases} | {
            row.candidate_status for row in pair.cases
        }
        expected_terminal: (
            tuple[
                GateStage,
                GateStepStatus,
                tuple[GateReason, ...],
            ]
            | None
        ) = None
        if RunnerStatus.BUDGET_STOP in statuses:
            expected_terminal = (
                GateStage.SELECTION,
                GateStepStatus.BUDGET_STOP,
                (GateReason.BUDGET_STOP,),
            )
        elif RunnerStatus.JUDGE_ERROR in statuses:
            expected_terminal = (
                GateStage.SELECTION,
                GateStepStatus.ERROR,
                (GateReason.JUDGE_ERROR,),
            )
        elif statuses - {RunnerStatus.PASS, RunnerStatus.AGENT_FAIL}:
            expected_terminal = (
                GateStage.SELECTION,
                GateStepStatus.ERROR,
                (GateReason.EVALUATION_ERROR,),
            )
        elif critical_regressions > policy.max_critical_regressions:
            expected_terminal = (
                GateStage.CRITICAL_REGRESSION,
                GateStepStatus.FAIL,
                (GateReason.CRITICAL_REGRESSION,),
            )
        elif metrics.quality_delta <= policy.min_quality_delta:
            reason = (
                GateReason.TIE
                if metrics.quality_delta == 0
                else GateReason.OVERALL_REGRESSION
            )
            expected_terminal = (
                GateStage.OVERALL_QUALITY,
                GateStepStatus.FAIL,
                (reason,),
            )
        else:
            cost_reasons: list[GateReason] = []
            if candidate_cost > policy.max_candidate_cost_amount:
                cost_reasons.append(GateReason.COST_LIMIT)
            if (
                relative_cost is None
                or relative_cost > policy.max_relative_cost_increase
            ):
                cost_reasons.append(GateReason.COST_GROWTH)
            if cost_reasons:
                expected_terminal = (
                    GateStage.COST,
                    GateStepStatus.FAIL,
                    tuple(cost_reasons),
                )
            else:
                budget_reasons: list[GateReason] = []
                if (
                    trigger_cost + accepted_cost + candidate_cost
                    > policy.max_gate_cost_amount
                ):
                    budget_reasons.append(GateReason.COST_LIMIT)
                if (
                    input_tokens > policy.max_gate_input_tokens
                    or output_tokens > policy.max_gate_output_tokens
                ):
                    budget_reasons.append(GateReason.TOKEN_BUDGET)
                if budget_reasons:
                    expected_terminal = (
                        GateStage.BUDGET,
                        GateStepStatus.FAIL,
                        tuple(budget_reasons),
                    )

        terminal = next(
            (step for step in decision.steps if step.status is not GateStepStatus.PASS),
            None,
        )
        actual_terminal = (
            None
            if terminal is None
            else (
                terminal.stage,
                terminal.status,
                terminal.reason_codes,
            )
        )
        if actual_terminal != expected_terminal:
            raise RegistryError("gate outcome does not match its locked policy")

    def _verify_pair_event_log(
        self,
        pair: SelectionPairEvaluation,
        *,
        side: str,
    ) -> None:
        reference = (
            pair.accepted_events if side == "accepted" else pair.candidate_events
        )
        path = self._verify_ref(reference)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RegistryError("selection event evidence cannot be read") from exc
        if len(lines) != len(pair.cases):
            raise RegistryError("selection event evidence is incomplete")
        run_id = pair.accepted_run_id if side == "accepted" else pair.candidate_run_id
        skill_sha256 = (
            pair.accepted_skill_sha256
            if side == "accepted"
            else pair.candidate_skill_sha256
        )
        for sequence, (line, row) in enumerate(zip(lines, pair.cases, strict=True)):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RegistryError("selection event evidence is invalid JSON") from exc
            status = row.accepted_status if side == "accepted" else row.candidate_status
            score = row.accepted_score if side == "accepted" else row.candidate_score
            input_tokens = (
                row.accepted_input_tokens
                if side == "accepted"
                else row.candidate_input_tokens
            )
            output_tokens = (
                row.accepted_output_tokens
                if side == "accepted"
                else row.candidate_output_tokens
            )
            cost = (
                row.accepted_cost_amount
                if side == "accepted"
                else row.candidate_cost_amount
            )
            expected: dict[str, object] = {
                "cost_amount": str(cost),
                "cost_currency": pair.cost_currency,
                "evaluation_nonce": pair.evaluation_nonce,
                "input_tokens": input_tokens,
                "iteration_id": pair.iteration_id,
                "measurement_kind": pair.measurement_kind.value,
                "output_tokens": output_tokens,
                "run_id": run_id,
                "score": score,
                "sequence": sequence,
                "skill_sha256": skill_sha256,
                "slot": row.slot,
                "status": status.value,
            }
            if payload != expected:
                raise RegistryError(
                    "selection event evidence disagrees with its paired summary"
                )

    def _decision_evidence(self, decision: GateDecision) -> tuple[ArtifactRef, ...]:
        ordered = [
            decision.candidate,
            decision.accepted_manifest,
            decision.gate_policy,
        ]
        ordered.extend(
            reference for step in decision.steps for reference in step.evidence
        )
        unique: dict[tuple[str, str, str], ArtifactRef] = {}
        for reference in ordered:
            key = (reference.root.value, reference.path, reference.sha256)
            unique.setdefault(key, reference)
        return tuple(unique.values())


__all__ = [
    "RegistryError",
    "RegistryState",
    "RegistryVersion",
    "SkillRegistry",
]
