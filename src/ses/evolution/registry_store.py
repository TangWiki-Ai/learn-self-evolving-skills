"""Private secure storage and append primitives for the Skill Registry."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    RegistryCheckpoint,
    RegistryEvent,
    SchemaVersion,
    Sha256Digest,
    StrictNonNegativeInt,
    VersionedRecord,
    artifact_json_bytes,
)
from ses.evolution.registry_internal import (
    RegistryError,
    RegistryState,
    RegistryVersion,
    _idempotent,
    _RegistryTransition,
)
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_KEY_ENV = "SES_REGISTRY_CHECKPOINT_HMAC_KEY"


class _RegistryAppendIntent(VersionedRecord):
    """External authorization for exactly one pending event-log append."""

    record_type: Literal["registry_append_intent"]
    registry_id: str = Field(pattern=r"^registry-[a-z0-9-]+$")
    lineage_id: str = Field(pattern=r"^lineage-[a-z0-9-]+$")
    prior_event_count: StrictNonNegativeInt
    prior_head_event_sha256: Sha256Digest
    next_event_count: StrictNonNegativeInt
    next_head_event_sha256: Sha256Digest
    command_id: str = Field(pattern=r"^command-[a-z0-9-]+$")
    command_sha256: Sha256Digest
    integrity_mode: Literal["hmac_sha256", "local_untrusted"]
    integrity_sha256: Sha256Digest | None = None

    @model_validator(mode="after")
    def _valid_transition_and_authentication(self) -> _RegistryAppendIntent:
        if self.next_event_count != self.prior_event_count + 1:
            raise ValueError("append intent event count is not contiguous")
        if (self.integrity_mode == "hmac_sha256") != (
            self.integrity_sha256 is not None
        ):
            raise ValueError("append intent authentication fields do not match")
        return self


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint_message(checkpoint: RegistryCheckpoint) -> bytes:
    payload = checkpoint.model_dump(mode="json", exclude={"integrity_sha256"})
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _intent_message(intent: _RegistryAppendIntent) -> bytes:
    payload = intent.model_dump(mode="json", exclude={"integrity_sha256"})
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _RegistryStore:
    """Own safe paths, immutable objects, append locking, and checkpoints."""

    def __init__(
        self,
        root: Path,
        *,
        registry_id: str,
        checkpoint_path: Path | None,
        checkpoint_key: bytes | None,
    ) -> None:
        lexical_root = root.absolute()
        resolved_root = root.resolve()
        if ".." in root.parts or root.is_symlink() or lexical_root != resolved_root:
            raise RegistryError("registry root must be a canonical real path")
        if not re.fullmatch(r"registry-[a-z0-9-]+", registry_id):
            raise RegistryError("registry_id must be a safe registry identifier")
        self.root = resolved_root
        self.registry_id = registry_id
        checkpoint = checkpoint_path or self.root.with_name(
            f"{self.root.name}.checkpoint.json"
        )
        if ".." in checkpoint.parts or checkpoint.is_symlink():
            raise RegistryError("Registry checkpoint must be a canonical real path")
        lexical_checkpoint = checkpoint.absolute()
        resolved_checkpoint = checkpoint.resolve()
        if lexical_checkpoint != resolved_checkpoint:
            raise RegistryError("Registry checkpoint must be a canonical real path")
        if resolved_checkpoint.is_relative_to(self.root):
            raise RegistryError("Registry checkpoint must be outside the Registry")
        self.checkpoint_path = resolved_checkpoint
        self._intent_path = resolved_checkpoint.with_name(
            f"{resolved_checkpoint.name}.append-intent.json"
        )
        environment_key = os.environ.get(_CHECKPOINT_KEY_ENV)
        effective_key = (
            checkpoint_key
            if checkpoint_key is not None
            else environment_key.encode("utf-8")
            if environment_key
            else None
        )
        if effective_key is not None and len(effective_key) < 32:
            raise RegistryError("Registry checkpoint HMAC key is too short")
        self._checkpoint_key = effective_key

    @property
    def checkpoint_authenticated(self) -> bool:
        return self._checkpoint_key is not None

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def version_path(self, skill_sha256: str) -> Path:
        if not _SHA256.fullmatch(skill_sha256):
            raise RegistryError("version hash must be lowercase SHA-256")
        return self.root / "versions" / skill_sha256

    def append(
        self,
        transition: _RegistryTransition,
        *,
        expected_events: tuple[RegistryEvent, ...],
        audit: Callable[[], RegistryState],
    ) -> RegistryEvent:
        self.storage_directory()
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
            prior_events: tuple[RegistryEvent, ...] = ()
            if self.events_path.is_symlink():
                raise RegistryError("registry event log cannot be a symlink")
            if self.events_path.exists():
                state = audit()
                prior_events = state.events
                existing = _idempotent(
                    state,
                    transition.command_id,
                    transition.command_sha256,
                )
                if existing is not None:
                    return existing
                if prior_events != expected_events:
                    raise RegistryError("Registry changed during the command")
            elif expected_events:
                raise RegistryError("Registry changed during the command")
            else:
                self._clear_abandoned_initial_intent()
            event = transition.event(
                registry_id=self.registry_id,
                prior_events=prior_events,
            )
            self._write_intent(prior_events, event)
            with self.events_path.open("ab") as stream:
                stream.write(artifact_json_bytes(event) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.write_checkpoint((*prior_events, event))
            self._clear_intent()
            return event

    def storage_directory(self, *parts: str) -> Path:
        """Create a Registry-owned directory without following symlink ancestors."""

        current = self.root
        if current.is_symlink():
            raise RegistryError("Registry storage cannot contain symlinks")
        current.mkdir(parents=True, exist_ok=True)
        resolved_root = self.root.resolve(strict=True)
        for part in parts:
            if not part or PurePosixPath(part).parts != (part,):
                raise RegistryError("Registry storage component is invalid")
            current = current / part
            if current.is_symlink():
                raise RegistryError("Registry storage cannot contain symlinks")
            current.mkdir(exist_ok=True)
            try:
                current.resolve(strict=True).relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise RegistryError("Registry storage escapes its root") from exc
        return current

    def write_checkpoint(self, events: Sequence[RegistryEvent]) -> None:
        """Atomically advance the external authenticated or offline anchor."""

        if not events:
            raise RegistryError("Registry checkpoint requires at least one event")
        parent = self.checkpoint_path.parent
        if parent.is_symlink() or parent.absolute() != parent.resolve():
            raise RegistryError("Registry checkpoint parent is not canonical")
        parent.mkdir(parents=True, exist_ok=True)
        if self.checkpoint_path.is_symlink():
            raise RegistryError("Registry checkpoint cannot be a symlink")
        authenticated = self._checkpoint_key is not None
        checkpoint = RegistryCheckpoint(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="registry_checkpoint",
            registry_id=self.registry_id,
            lineage_id=events[0].lineage_id,
            event_count=len(events),
            head_event_sha256=events[-1].event_sha256,
            integrity_mode=("hmac_sha256" if authenticated else "local_untrusted"),
            integrity_sha256=("0" * 64 if authenticated else None),
        )
        if self._checkpoint_key is not None:
            integrity_sha256 = hmac.new(
                self._checkpoint_key,
                _checkpoint_message(checkpoint),
                hashlib.sha256,
            ).hexdigest()
            checkpoint = checkpoint.model_copy(
                update={"integrity_sha256": integrity_sha256}
            )
        self._write_external(
            self.checkpoint_path,
            artifact_json_bytes(checkpoint),
        )

    def _write_intent(
        self,
        prior_events: Sequence[RegistryEvent],
        event: RegistryEvent,
    ) -> None:
        authenticated = self._checkpoint_key is not None
        intent = _RegistryAppendIntent(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="registry_append_intent",
            registry_id=self.registry_id,
            lineage_id=event.lineage_id,
            prior_event_count=len(prior_events),
            prior_head_event_sha256=(
                prior_events[-1].event_sha256 if prior_events else "0" * 64
            ),
            next_event_count=len(prior_events) + 1,
            next_head_event_sha256=event.event_sha256,
            command_id=event.command_id,
            command_sha256=event.command_sha256,
            integrity_mode=("hmac_sha256" if authenticated else "local_untrusted"),
            integrity_sha256=("0" * 64 if authenticated else None),
        )
        if self._checkpoint_key is not None:
            intent = intent.model_copy(
                update={
                    "integrity_sha256": hmac.new(
                        self._checkpoint_key,
                        _intent_message(intent),
                        hashlib.sha256,
                    ).hexdigest()
                }
            )
        self._write_external(self._intent_path, artifact_json_bytes(intent))

    def _write_external(self, path: Path, content: bytes) -> None:
        """Atomically replace one external checkpoint-side record."""

        parent = path.parent
        if parent.is_symlink() or parent.absolute() != parent.resolve():
            raise RegistryError("Registry checkpoint parent is not canonical")
        parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RegistryError("Registry checkpoint records cannot be symlinks")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            self._sync_external_parent(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _sync_external_parent(path: Path) -> None:
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError as exc:
            raise RegistryError("Registry checkpoint parent cannot be opened") from exc
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_intent(self) -> _RegistryAppendIntent | None:
        if self._intent_path.is_symlink() or (
            self._intent_path.exists() and not self._intent_path.is_file()
        ):
            raise RegistryError("Registry append intent is invalid")
        if not self._intent_path.exists():
            return None
        try:
            payload = self._intent_path.read_bytes()
            intent = _RegistryAppendIntent.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise RegistryError("Registry append intent is invalid") from exc
        if artifact_json_bytes(intent) != payload:
            raise RegistryError("Registry append intent is not canonical")
        if intent.integrity_mode == "hmac_sha256":
            if self._checkpoint_key is None:
                raise RegistryError(
                    "Registry append intent authentication key is missing"
                )
            expected_integrity = hmac.new(
                self._checkpoint_key,
                _intent_message(intent),
                hashlib.sha256,
            ).hexdigest()
            if intent.integrity_sha256 is None or not hmac.compare_digest(
                intent.integrity_sha256,
                expected_integrity,
            ):
                raise RegistryError("Registry append intent authentication failed")
        elif self._checkpoint_key is not None:
            raise RegistryError("Registry append intent authentication was downgraded")
        if intent.registry_id != self.registry_id:
            raise RegistryError("Registry append intent belongs to another Registry")
        return intent

    def _clear_intent(self) -> None:
        if self._intent_path.is_symlink() or (
            self._intent_path.exists() and not self._intent_path.is_file()
        ):
            raise RegistryError("Registry append intent is invalid")
        if self._intent_path.exists():
            self._intent_path.unlink()
            self._sync_external_parent(self._intent_path)

    def _clear_abandoned_initial_intent(self) -> None:
        intent = self._read_intent()
        if intent is None:
            return
        if (
            intent.prior_event_count != 0
            or intent.prior_head_event_sha256 != "0" * 64
            or self.checkpoint_path.exists()
            or self.events_path.exists()
        ):
            raise RegistryError("Registry append intent does not match empty history")
        self._clear_intent()

    @staticmethod
    def _intent_matches_completed_event(
        intent: _RegistryAppendIntent,
        events: Sequence[RegistryEvent],
    ) -> bool:
        tail = events[-1]
        prior_head = events[-2].event_sha256 if len(events) > 1 else "0" * 64
        return (
            intent.lineage_id == events[0].lineage_id
            and intent.prior_event_count == len(events) - 1
            and intent.prior_head_event_sha256 == prior_head
            and intent.next_event_count == len(events)
            and intent.next_head_event_sha256 == tail.event_sha256
            and intent.command_id == tail.command_id
            and intent.command_sha256 == tail.command_sha256
            and tail.sequence == intent.prior_event_count
            and tail.previous_event_sha256 == prior_head
        )

    @staticmethod
    def _intent_matches_unwritten_event(
        intent: _RegistryAppendIntent,
        events: Sequence[RegistryEvent],
    ) -> bool:
        return (
            intent.lineage_id == events[0].lineage_id
            and intent.prior_event_count == len(events)
            and intent.prior_head_event_sha256 == events[-1].event_sha256
            and intent.next_event_count == len(events) + 1
        )

    def _read_checkpoint(self) -> RegistryCheckpoint | None:
        if self.checkpoint_path.is_symlink() or (
            self.checkpoint_path.exists() and not self.checkpoint_path.is_file()
        ):
            raise RegistryError("Registry checkpoint is invalid")
        if not self.checkpoint_path.exists():
            return None
        try:
            payload = self.checkpoint_path.read_bytes()
            checkpoint = RegistryCheckpoint.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise RegistryError("Registry checkpoint is invalid") from exc
        if artifact_json_bytes(checkpoint) != payload:
            raise RegistryError("Registry checkpoint is not canonical")
        if checkpoint.integrity_mode == "hmac_sha256":
            if self._checkpoint_key is None:
                raise RegistryError("Registry checkpoint authentication key is missing")
            expected_integrity = hmac.new(
                self._checkpoint_key,
                _checkpoint_message(checkpoint),
                hashlib.sha256,
            ).hexdigest()
            if checkpoint.integrity_sha256 is None or not hmac.compare_digest(
                checkpoint.integrity_sha256,
                expected_integrity,
            ):
                raise RegistryError("Registry checkpoint authentication failed")
        elif self._checkpoint_key is not None:
            raise RegistryError("Registry checkpoint authentication was downgraded")
        return checkpoint

    def verify_checkpoint(self, events: Sequence[RegistryEvent]) -> None:
        intent = self._read_intent()
        checkpoint = self._read_checkpoint()
        if checkpoint is not None and (
            checkpoint.registry_id == self.registry_id
            and checkpoint.lineage_id == events[0].lineage_id
            and checkpoint.event_count == len(events)
            and checkpoint.head_event_sha256 == events[-1].event_sha256
        ):
            if intent is None:
                return
            if self._intent_matches_completed_event(
                intent,
                events,
            ) or self._intent_matches_unwritten_event(intent, events):
                self._clear_intent()
                return
            raise RegistryError("Registry append intent disagrees with its checkpoint")

        if intent is None:
            raise RegistryError(
                "Registry append intent is missing for checkpoint recovery"
            )
        if not self._intent_matches_completed_event(intent, events):
            raise RegistryError("Registry append intent does not match the event tail")
        if checkpoint is None:
            if len(events) != 1 or intent.prior_event_count != 0:
                raise RegistryError("Registry checkpoint is missing")
        elif (
            checkpoint.registry_id != self.registry_id
            or checkpoint.lineage_id != events[0].lineage_id
            or checkpoint.event_count != len(events) - 1
            or checkpoint.head_event_sha256 != intent.prior_head_event_sha256
        ):
            raise RegistryError("Registry event log differs from its checkpoint")
        self.write_checkpoint(events)
        repaired = self._read_checkpoint()
        if repaired is None or (
            repaired.registry_id != self.registry_id
            or repaired.lineage_id != events[0].lineage_id
            or repaired.event_count != len(events)
            or repaired.head_event_sha256 != events[-1].event_sha256
        ):
            raise RegistryError("Registry checkpoint recovery did not persist")
        self._clear_intent()

    def store_skill(self, source: Path, expected_hash: str) -> Path:
        target = self.version_path(expected_hash)
        if target.exists():
            if target.is_symlink() or normalized_skill_sha256(target) != expected_hash:
                raise RegistryError("stored version content was tampered with")
            return target
        try:
            manifest = load_skill_manifest(source)
            actual_hash = normalized_skill_sha256(source)
        except (OSError, ValueError) as exc:
            raise RegistryError("Skill version cannot be stored") from exc
        if actual_hash != expected_hash:
            raise RegistryError("Skill version hash does not match its record")
        target_parent = self.storage_directory("versions")
        if target.parent != target_parent:
            raise RegistryError("stored version path is not canonical")
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

    def store_object(self, family: str, name: str, content: bytes) -> ArtifactRef:
        if PurePosixPath(name).parts != (name,):
            raise RegistryError("Registry object name is invalid")
        target = self.storage_directory("objects", family) / name
        if target.exists():
            if target.is_symlink() or target.read_bytes() != content:
                raise RegistryError("immutable Registry object already differs")
        else:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return self.ref(target)

    def ref(self, path: Path) -> ArtifactRef:
        try:
            relative = path.resolve(strict=True).relative_to(self.root.resolve())
        except (OSError, ValueError) as exc:
            raise RegistryError("Registry artifact escapes its root") from exc
        return ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=relative.as_posix(),
            sha256=_file_sha256(path),
        )

    def verify_ref(self, reference: ArtifactRef) -> Path:
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

    def verify_version(self, version: RegistryVersion) -> None:
        manifest_path = self.verify_ref(version.manifest)
        expected = self.version_path(version.skill_sha256)
        if manifest_path.parent != expected:
            raise RegistryError("version manifest path does not match its content hash")
        try:
            manifest = load_skill_manifest(expected)
            actual = normalized_skill_sha256(expected)
        except (OSError, ValueError) as exc:
            raise RegistryError("stored version failed integrity validation") from exc
        if actual != version.skill_sha256:
            raise RegistryError("stored version content hash mismatch")
        declared = {item.path for item in manifest.files} | {"skill-manifest.json"}
        actual_files: set[str] = set()
        for path in expected.rglob("*"):
            if path.is_symlink():
                raise RegistryError("stored version contains a symlink")
            if path.is_file():
                actual_files.add(path.relative_to(expected).as_posix())
        if actual_files != declared:
            raise RegistryError("stored version contains undeclared files")


__all__: tuple[str, ...] = ()
