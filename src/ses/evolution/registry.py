"""Tamper-evident append-only Registry for immutable Skill versions."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ses.contracts import (
    GateOutcome,
    RegistryEvent,
    RegistryEventType,
    VersionStatus,
)
from ses.evolution.candidate_bundle import (
    CandidateBundleError,
    store_candidate_audit_snapshot,
)
from ses.evolution.registry_decision import _GateDecisionAuditor
from ses.evolution.registry_evidence import _InitialEvidenceAuditor
from ses.evolution.registry_internal import (
    _SAFE_COMMAND,
    RegistryError,
    RegistryState,
    RegistryVersion,
    _canonical_digest,
    _idempotent,
    _protocol_identity,
    _RegistryTransition,
)
from ses.evolution.registry_replay import _RegistryReplay
from ses.evolution.registry_store import _RegistryStore
from ses.skills.installer import normalized_skill_sha256
from ses.skills.static_gate import run_static_gate


class SkillRegistry:
    """Own immutable version storage and every legal governance transition."""

    def __init__(
        self,
        root: Path,
        *,
        registry_id: str = "registry-primary",
        checkpoint_path: Path | None = None,
        checkpoint_key: bytes | None = None,
    ) -> None:
        self._store = _RegistryStore(
            root,
            registry_id=registry_id,
            checkpoint_path=checkpoint_path,
            checkpoint_key=checkpoint_key,
        )
        self._initial = _InitialEvidenceAuditor(
            self._store,
            static_gate=lambda source: run_static_gate(source),
        )
        self._decision = _GateDecisionAuditor(self._store)
        self._replay = _RegistryReplay(
            self._store,
            self._initial,
            self._decision,
        )

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def registry_id(self) -> str:
        return self._store.registry_id

    @property
    def checkpoint_path(self) -> Path:
        return self._store.checkpoint_path

    @property
    def checkpoint_authenticated(self) -> bool:
        """Report integrity authentication, not monotonic replay protection."""

        return self._store.checkpoint_authenticated

    @property
    def events_path(self) -> Path:
        return self._store.events_path

    def version_path(self, skill_sha256: str) -> Path:
        return self._store.version_path(skill_sha256)

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
        captured_evidence = tuple(
            (path, self._initial.read_evidence_source(path)) for path in evidence_paths
        )
        evidence_hashes = tuple(
            hashlib.sha256(content).hexdigest() for _, content in captured_evidence
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
            existing = _idempotent(state, command_id, command_sha)
            if existing is not None:
                return existing
            raise RegistryError("registry is already initialized")
        if self.checkpoint_path.exists():
            raise RegistryError("Registry checkpoint already exists")

        self.root.mkdir(parents=True, exist_ok=True)
        version_dir = self._store.store_skill(accepted_skill, skill_hash)
        self._initial.verify_initial_static_gate(
            version_dir,
            skill_hash=skill_hash,
        )
        evidence = tuple(
            self._initial.store_evidence(path, content)
            for path, content in captured_evidence
        )
        self._initial.validate_initial_evidence(evidence, skill_hash=skill_hash)
        lineage_id = f"lineage-{skill_hash[:16]}"
        transition = _RegistryTransition(
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
            version_manifest=self._store.ref(version_dir / "skill-manifest.json"),
            candidate=None,
            gate_decision=None,
            evidence=evidence,
            reason="initial accepted Skill verified by prior gate evidence",
        )
        return self._store.append(
            transition,
            expected_events=(),
            audit=self.audit,
        )

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
        snapshot = self._decision.load_candidate(candidate_bundle)
        candidate = snapshot.candidate
        command_sha = _canonical_digest(
            {
                "action": "register_candidate",
                "candidate_id": candidate.candidate_id,
                "candidate_sha256": candidate.content_sha256,
                "parent_sha256": candidate.parent_skill_sha256,
            }
        )
        existing = _idempotent(state, command_id, command_sha)
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

        version_dir = self._store.store_skill(
            candidate_bundle / "skill",
            candidate.content_sha256,
        )
        try:
            candidate_path = store_candidate_audit_snapshot(
                self._store.storage_directory("objects", "candidates"),
                snapshot,
            )
        except CandidateBundleError as exc:
            raise RegistryError("candidate audit snapshot cannot be stored") from exc
        candidate_ref = self._store.ref(candidate_path)
        candidate_evidence = self._decision.candidate_evidence_from_ref(
            candidate_ref,
            require_registry_snapshot=True,
        )
        transition = _RegistryTransition(
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
            version_manifest=self._store.ref(version_dir / "skill-manifest.json"),
            candidate=candidate_ref,
            gate_decision=None,
            evidence=candidate_evidence,
            reason="immutable candidate registered",
        )
        return self._store.append(
            transition,
            expected_events=state.events,
            audit=self.audit,
        )

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
        decision, decision_ref, selection_pair = self._decision.load_decision(
            decision_path
        )
        if decision.mode == "live" and not self.checkpoint_authenticated:
            raise RegistryError(
                "live governance requires an authenticated Registry checkpoint"
            )
        command_sha = _canonical_digest(
            {
                "action": "record_decision",
                "candidate_id": decision.candidate_id,
                "decision_sha256": decision_ref.sha256,
                "outcome": decision.outcome.value,
            }
        )
        existing = _idempotent(state, command_id, command_sha)
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
        existing_protocol = self._decision.lineage_protocol_identity(state)
        if existing_protocol is not None and existing_protocol != _protocol_identity(
            decision
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
        if not self._decision.candidate_snapshots_match(
            candidate_ref,
            decision.candidate,
        ):
            raise RegistryError(
                "gate candidate audit snapshot differs from its registration"
            )
        self._decision.ensure_pair_identity_is_fresh(selection_pair, state=state)
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
        evidence = self._decision.decision_evidence(decision)
        transition = _RegistryTransition(
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
        )
        return self._store.append(
            transition,
            expected_events=state.events,
            audit=self.audit,
        )

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
        existing = _idempotent(state, command_id, command_sha)
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
        self._store.verify_version(version)
        self._store.verify_ref(version.gate_decision)
        transition = _RegistryTransition(
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
        )
        return self._store.append(
            transition,
            expected_events=state.events,
            audit=self.audit,
        )

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
        existing = _idempotent(state, command_id, command_sha)
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
        self._store.verify_version(target)
        evidence = (
            (target.gate_decision,)
            if target.gate_decision is not None
            else target.evidence
        )
        if not evidence:
            raise RegistryError("rollback target has no verification evidence")
        for reference in evidence:
            self._store.verify_ref(reference)
        transition = _RegistryTransition(
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
        )
        return self._store.append(
            transition,
            expected_events=state.events,
            audit=self.audit,
        )

    def audit(self) -> RegistryState:
        """Verify the full hash chain, every artifact, and every transition."""

        return self._replay.audit()

    @staticmethod
    def _validate_command_id(command_id: str) -> None:
        if not _SAFE_COMMAND.fullmatch(command_id):
            raise RegistryError("command_id must be a safe command identifier")


__all__ = [
    "RegistryError",
    "RegistryState",
    "RegistryVersion",
    "SkillRegistry",
]
