"""Private hash-chain parser and state replay for the Skill Registry."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ses.contracts import (
    GateOutcome,
    RegistryEvent,
    RegistryEventType,
    VersionStatus,
    artifact_json_bytes,
)
from ses.evolution.registry_decision import _GateDecisionAuditor
from ses.evolution.registry_evidence import _InitialEvidenceAuditor
from ses.evolution.registry_internal import (
    _SAFE_COMMAND,
    _ZERO_HASH,
    RegistryError,
    RegistryState,
    _canonical_digest,
    _freeze_state,
    _MutableVersion,
    _protocol_identity,
)
from ses.evolution.registry_store import _RegistryStore


class _RegistryReplay:
    """Authenticate the event log and derive its complete Registry state."""

    def __init__(
        self,
        store: _RegistryStore,
        initial: _InitialEvidenceAuditor,
        decision: _GateDecisionAuditor,
    ) -> None:
        self._store = store
        self._initial = initial
        self._decision = decision

    def audit(self) -> RegistryState:
        if (
            not self._store.events_path.is_file()
            or self._store.events_path.is_symlink()
        ):
            raise RegistryError("registry is not initialized")
        try:
            lines = self._store.events_path.read_text(encoding="utf-8").splitlines()
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
            if event.registry_id != self._store.registry_id:
                raise RegistryError("registry event belongs to another Registry")
            if event.command_id in command_ids or event.event_id in event_ids:
                raise RegistryError("registry event or command ID is duplicated")
            command_ids.add(event.command_id)
            event_ids.add(event.event_id)
            previous_hash = event.event_sha256
            events.append(event)
        state = self.replay(tuple(events))
        self._store.verify_checkpoint(state.events)
        return state

    def replay(self, events: tuple[RegistryEvent, ...]) -> RegistryState:
        versions: dict[str, _MutableVersion] = {}
        current: str | None = None
        lineage_id = events[0].lineage_id
        protocol_identity: tuple[str, str, str, str] | None = None
        selection_nonces: set[str] = set()
        selection_run_ids: set[str] = set()
        for event in events:
            if event.lineage_id != lineage_id:
                raise RegistryError("registry events cross experiment lineages")
            self._store.verify_ref(event.version_manifest)
            for reference in (
                *event.evidence,
                *(() if event.candidate is None else (event.candidate,)),
                *(() if event.gate_decision is None else (event.gate_decision,)),
            ):
                self._store.verify_ref(reference)
            if event.event_type is RegistryEventType.INITIALIZED:
                if versions or current is not None:
                    raise RegistryError("registry contains duplicate initialization")
                self._initial.verify_initial_event_evidence(event)
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
                self._store.verify_version(versions[event.version_sha256].freeze())
                self._initial.verify_initial_static_gate(
                    self._store.version_path(event.version_sha256),
                    skill_hash=event.version_sha256,
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
                ):
                    raise RegistryError("invalid candidate registration transition")
                candidate = self._decision.candidate_from_ref(
                    event.candidate,
                    require_registry_snapshot=True,
                )
                expected_candidate_evidence = (
                    self._decision.candidate_evidence_from_ref(
                        event.candidate,
                        require_registry_snapshot=True,
                    )
                )
                if (
                    candidate.candidate_id != event.version_id
                    or candidate.content_sha256 != event.version_sha256
                    or candidate.parent_skill_sha256 != current
                    or event.evidence != expected_candidate_evidence
                ):
                    raise RegistryError("candidate object does not match its event")
                manifest = self._decision.manifest_from_ref(event.version_manifest)
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
                decision = self._decision.decision_from_ref(event.gate_decision)
                if decision.mode == "live" and not self._store.checkpoint_authenticated:
                    raise RegistryError(
                        "live governance requires an authenticated Registry checkpoint"
                    )
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
                if not self._decision.candidate_snapshots_match(
                    version.candidate,
                    decision.candidate,
                ):
                    raise RegistryError(
                        "gate candidate audit snapshot differs from its registration"
                    )
                pair = self._decision.verify_gate_decision(decision)
                self._decision.claim_pair_identity(
                    pair,
                    nonces=selection_nonces,
                    run_ids=selection_run_ids,
                )
                expected_evidence = self._decision.decision_evidence(decision)
                if event.evidence != expected_evidence or event.reason != ",".join(
                    reason.value for reason in decision.reason_codes
                ):
                    raise RegistryError(
                        "candidate decision event evidence is inconsistent"
                    )
                decision_protocol = _protocol_identity(decision)
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
            self._verify_command_identity(event)
            if current != event.current_accepted_skill_sha256:
                raise RegistryError("event accepted pointer does not match replay")
            self._store.verify_version(versions[event.version_sha256].freeze())
        assert current is not None
        return _freeze_state(
            registry_id=self._store.registry_id,
            lineage_id=lineage_id,
            current=current,
            versions=versions,
            events=events,
        )

    def _verify_command_identity(self, event: RegistryEvent) -> None:
        if (
            not _SAFE_COMMAND.fullmatch(event.command_id)
            or event.event_id
            != f"event-{event.sequence:06d}-{event.command_sha256[:8]}"
        ):
            raise RegistryError("registry event command identity is invalid")
        if event.event_type is RegistryEventType.INITIALIZED:
            payload: dict[str, object] = {
                "action": "initialize",
                "evidence_sha256": [reference.sha256 for reference in event.evidence],
                "skill_sha256": event.version_sha256,
            }
        elif event.event_type is RegistryEventType.CANDIDATE_REGISTERED:
            payload = {
                "action": "register_candidate",
                "candidate_id": event.version_id,
                "candidate_sha256": event.version_sha256,
                "parent_sha256": event.parent_skill_sha256,
            }
        elif event.event_type in {
            RegistryEventType.CANDIDATE_ACCEPTED,
            RegistryEventType.CANDIDATE_REJECTED,
        }:
            decision = self._decision.decision_from_ref(event.gate_decision)
            payload = {
                "action": "record_decision",
                "candidate_id": decision.candidate_id,
                "decision_sha256": event.gate_decision.sha256
                if event.gate_decision is not None
                else "",
                "outcome": decision.outcome.value,
            }
        elif event.event_type is RegistryEventType.PROMOTED:
            payload = {"action": "promote", "candidate_id": event.version_id}
        elif event.event_type is RegistryEventType.ROLLED_BACK:
            payload = {"action": "rollback", "target_sha256": event.version_sha256}
        else:
            raise RegistryError("unsupported registry transition")
        if event.command_sha256 != _canonical_digest(payload):
            raise RegistryError("registry event command fingerprint is invalid")


__all__: tuple[str, ...] = ()
