"""Atomic persistence and lifecycle operations for ``workspace/.ses/status.json``."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ses.contracts import ArtifactRef, Usage, artifact_json_bytes
from ses.contracts.primitives import UtcDateTime
from ses.foundation.config import ProviderId
from ses.foundation.credentials import credential_values, redact
from ses.journey.models import (
    DEFAULT_STATION_COMMANDS,
    STATION_COUNT,
    ExperimentCostSource,
    ExperimentMode,
    ExperimentUsage,
    JourneyProgressStatus,
    JourneyStatus,
    StationProgress,
    initial_journey_status,
)

_UTC_DATETIME_ADAPTER = TypeAdapter(UtcDateTime)
_STATUS_DIRECTORY = ".ses"
_STATUS_FILENAME = "status.json"


class JourneyStateError(ValueError):
    """A journey transition or persisted status is invalid or unsafe."""


def _transition_time(value: datetime | None, state: JourneyStatus | None) -> datetime:
    try:
        timestamp = _UTC_DATETIME_ADAPTER.validate_python(value or datetime.now(UTC))
    except (TypeError, ValidationError, ValueError) as exc:
        raise JourneyStateError("journey timestamp must be an aware datetime") from exc
    if state is not None and timestamp < state.updated_at:
        raise JourneyStateError("journey timestamps cannot move backwards")
    return timestamp


def _experiment_mode(value: ExperimentMode | str) -> ExperimentMode:
    try:
        return ExperimentMode(value)
    except (TypeError, ValueError) as exc:
        raise JourneyStateError("experiment mode must be 'live' or 'fixed'") from exc


def _provider(value: ProviderId | str | None) -> ProviderId | None:
    if value is None:
        return None
    try:
        return ProviderId(value)
    except (TypeError, ValueError) as exc:
        raise JourneyStateError(
            "experiment provider must be 'siliconflow' or 'chatanywhere'"
        ) from exc


def _cost_source(
    value: ExperimentCostSource | str | None,
    *,
    mode: ExperimentMode,
) -> ExperimentCostSource:
    if value is None:
        return (
            ExperimentCostSource.SYNTHETIC_CI
            if mode is ExperimentMode.FIXED
            else ExperimentCostSource.CLAUDE_CODE_ESTIMATE
        )
    try:
        return ExperimentCostSource(value)
    except (TypeError, ValueError) as exc:
        raise JourneyStateError("experiment cost source is invalid") from exc


def _merge_refs(
    current: tuple[ArtifactRef, ...], additions: Sequence[ArtifactRef]
) -> tuple[ArtifactRef, ...]:
    """Replace a stale checksum at one logical path while retaining link order."""

    result = list(current)
    positions = {
        (reference.root.value, reference.path): index
        for index, reference in enumerate(result)
    }
    for reference in additions:
        key = (reference.root.value, reference.path)
        position = positions.get(key)
        if position is None:
            positions[key] = len(result)
            result.append(reference)
        else:
            result[position] = reference
    return tuple(result)


def _copy_station(
    station: StationProgress, update: Mapping[str, Any]
) -> StationProgress:
    try:
        return station.model_copy(update=update)
    except (TypeError, ValidationError, ValueError) as exc:
        raise JourneyStateError("station transition is invalid") from exc


def _copy_status(state: JourneyStatus, update: Mapping[str, Any]) -> JourneyStatus:
    try:
        return state.model_copy(update=update)
    except (TypeError, ValidationError, ValueError) as exc:
        raise JourneyStateError("journey transition is invalid") from exc


def _replace_station(
    state: JourneyStatus,
    station: StationProgress,
    *,
    status: JourneyProgressStatus,
    current_station: int,
    updated_at: datetime,
) -> JourneyStatus:
    stations = list(state.stations)
    stations[station.number] = station
    return _copy_status(
        state,
        update={
            "status": status,
            "current_station": current_station,
            "stations": tuple(stations),
            "updated_at": updated_at,
        },
    )


def _projection_after_completion(
    stations: Sequence[StationProgress],
) -> tuple[JourneyProgressStatus, int]:
    if all(station.status is JourneyProgressStatus.COMPLETED for station in stations):
        return JourneyProgressStatus.COMPLETED, STATION_COUNT - 1
    for station in stations:
        if station.status is JourneyProgressStatus.NEEDS_ATTENTION:
            return JourneyProgressStatus.NEEDS_ATTENTION, station.number
    for station in stations:
        if station.status is JourneyProgressStatus.RUNNING:
            return JourneyProgressStatus.RUNNING, station.number
    for station in stations:
        if station.status is JourneyProgressStatus.PENDING:
            return JourneyProgressStatus.RUNNING, station.number
    raise JourneyStateError("journey has no valid station to resume")


class JourneyStatusStore:
    """Own the dashboard status snapshot beneath one trusted workspace."""

    def __init__(self, workspace: Path) -> None:
        raw = Path(workspace)
        if ".." in raw.parts:
            raise JourneyStateError("workspace path cannot contain parent traversal")
        absolute = raw if raw.is_absolute() else Path.cwd() / raw
        if absolute.is_symlink():
            raise JourneyStateError("workspace path cannot be a symlink")
        try:
            self.workspace = absolute.resolve(strict=False)
        except OSError as exc:
            raise JourneyStateError("workspace path cannot be resolved safely") from exc
        self.status_directory = self.workspace / _STATUS_DIRECTORY
        self.status_path = self.status_directory / _STATUS_FILENAME
        self._validate_boundary(allow_missing=True)

    def initialize(
        self,
        commands: Sequence[str] = DEFAULT_STATION_COMMANDS,
        *,
        cost_currency: str = "CNY",
        experiment_mode: ExperimentMode | str = ExperimentMode.LIVE,
        experiment_provider: ProviderId | str | None = None,
        model_lock_sha256: str | None = None,
        cost_source: ExperimentCostSource | str | None = None,
        now: datetime | None = None,
    ) -> JourneyStatus:
        """Create an initial snapshot or safely recover the existing one."""

        command_tuple = tuple(commands)
        normalized_mode = _experiment_mode(experiment_mode)
        normalized_provider = _provider(experiment_provider)
        normalized_lock = model_lock_sha256
        if normalized_mode is ExperimentMode.LIVE:
            normalized_provider = normalized_provider or ProviderId.SILICONFLOW
            normalized_lock = normalized_lock or "0" * 64
        else:
            if normalized_provider is not None or normalized_lock is not None:
                raise JourneyStateError(
                    "fixed journey cannot bind a live provider or model lock"
                )
        normalized_cost_source = _cost_source(cost_source, mode=normalized_mode)
        if self.status_path.is_symlink():
            raise JourneyStateError("journey status cannot be a symlink")
        if self.status_path.exists():
            state = self.load()
            if tuple(station.command for station in state.stations) != command_tuple:
                raise JourneyStateError(
                    "existing journey uses a different station command set"
                )
            if state.experiment_usage.cost_currency != cost_currency:
                raise JourneyStateError(
                    "existing journey uses a different experiment currency"
                )
            if state.experiment_mode is not normalized_mode:
                raise JourneyStateError(
                    "existing journey uses a different experiment mode"
                )
            if state.experiment_provider is not normalized_provider:
                raise JourneyStateError(
                    "existing journey uses a different experiment provider"
                )
            if state.model_lock_sha256 != normalized_lock:
                raise JourneyStateError("existing journey uses a different model lock")
            if state.cost_source is not normalized_cost_source:
                raise JourneyStateError(
                    "existing journey uses a different experiment cost source"
                )
            return state

        timestamp = _transition_time(now, None)
        try:
            state = initial_journey_status(
                commands=command_tuple,
                now=timestamp,
                cost_currency=cost_currency,
                experiment_mode=normalized_mode,
                experiment_provider=normalized_provider,
                model_lock_sha256=normalized_lock,
                cost_source=normalized_cost_source,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise JourneyStateError("initial journey configuration is invalid") from exc
        self.save(state)
        return state

    def load(self) -> JourneyStatus:
        """Recover and validate the last complete atomic snapshot."""

        try:
            payload = self._read_status_bytes()
            self._assert_credential_free(payload)
            state = JourneyStatus.model_validate_json(payload)
            if self._status_bytes(state) != payload:
                raise JourneyStateError("journey status is not canonical JSON")
            return state
        except JourneyStateError:
            raise
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise JourneyStateError("journey status is invalid") from exc

    def save(self, state: JourneyStatus) -> None:
        """Validate then atomically replace the dashboard snapshot."""

        try:
            validated = JourneyStatus.model_validate(state)
            payload = self._status_bytes(validated)
            self._assert_credential_free(payload)
        except JourneyStateError:
            raise
        except (TypeError, UnicodeError, ValidationError, ValueError) as exc:
            raise JourneyStateError("journey status is invalid") from exc
        self._atomic_write(payload)

    def start_station(
        self, number: int, *, now: datetime | None = None
    ) -> JourneyStatus:
        """Start or reopen any station, including station 7 out of sequence."""

        state = self.load()
        station = self._station(state, number)
        if (
            station.status is JourneyProgressStatus.RUNNING
            and state.current_station == number
            and state.status is JourneyProgressStatus.RUNNING
        ):
            return state

        timestamp = _transition_time(now, state)
        stations = list(state.stations)
        for index, existing in enumerate(stations):
            if index != number and existing.status is JourneyProgressStatus.RUNNING:
                stations[index] = _copy_station(
                    existing,
                    {
                        "status": JourneyProgressStatus.NEEDS_ATTENTION,
                        "completed_at": None,
                        "updated_at": timestamp,
                        "attention_reason": (
                            f"Station {existing.number} was interrupted when "
                            f"station {number} started."
                        ),
                    },
                )
        stations[number] = _copy_station(
            station,
            {
                "status": JourneyProgressStatus.RUNNING,
                "started_at": timestamp,
                "completed_at": None,
                "updated_at": timestamp,
                "attention_reason": None,
            },
        )
        updated = _copy_status(
            state,
            {
                "status": JourneyProgressStatus.RUNNING,
                "current_station": number,
                "stations": tuple(stations),
                "updated_at": timestamp,
            },
        )
        self.save(updated)
        return updated

    def record_station_refs(
        self,
        number: int,
        *,
        decision_refs: Sequence[ArtifactRef] = (),
        artifact_refs: Sequence[ArtifactRef] = (),
        now: datetime | None = None,
    ) -> JourneyStatus:
        """Attach decision and output evidence without changing station status."""

        state = self.load()
        station = self._station(state, number)
        if station.status is JourneyProgressStatus.PENDING:
            raise JourneyStateError("cannot attach references to a pending station")
        timestamp = _transition_time(now, state)
        updated_station = _copy_station(
            station,
            {
                "decision_refs": _merge_refs(
                    station.decision_refs, tuple(decision_refs)
                ),
                "artifact_refs": _merge_refs(
                    station.artifact_refs, tuple(artifact_refs)
                ),
                "updated_at": timestamp,
            },
        )
        updated = _replace_station(
            state,
            updated_station,
            status=state.status,
            current_station=state.current_station,
            updated_at=timestamp,
        )
        self.save(updated)
        return updated

    def complete_station(
        self,
        number: int,
        *,
        decision_refs: Sequence[ArtifactRef] = (),
        artifact_refs: Sequence[ArtifactRef] = (),
        now: datetime | None = None,
    ) -> JourneyStatus:
        """Complete one started station and project the next resumable station."""

        state = self.load()
        station = self._station(state, number)
        if station.status is JourneyProgressStatus.PENDING:
            raise JourneyStateError("cannot complete a station that has not started")
        timestamp = _transition_time(now, state)
        started_at = station.started_at or timestamp
        updated_station = _copy_station(
            station,
            {
                "status": JourneyProgressStatus.COMPLETED,
                "decision_refs": _merge_refs(
                    station.decision_refs, tuple(decision_refs)
                ),
                "artifact_refs": _merge_refs(
                    station.artifact_refs, tuple(artifact_refs)
                ),
                "started_at": started_at,
                "completed_at": timestamp,
                "updated_at": timestamp,
                "attention_reason": None,
            },
        )
        stations = list(state.stations)
        stations[number] = updated_station
        journey_status, current_station = _projection_after_completion(stations)
        updated = _copy_status(
            state,
            {
                "status": journey_status,
                "current_station": current_station,
                "stations": tuple(stations),
                "updated_at": timestamp,
            },
        )
        self.save(updated)
        return updated

    def mark_needs_attention(
        self,
        number: int,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> JourneyStatus:
        """Preserve a resumable problem instead of losing partial progress."""

        state = self.load()
        station = self._station(state, number)
        timestamp = _transition_time(now, state)
        updated_station = _copy_station(
            station,
            {
                "status": JourneyProgressStatus.NEEDS_ATTENTION,
                "started_at": station.started_at or timestamp,
                "completed_at": None,
                "updated_at": timestamp,
                "attention_reason": reason,
            },
        )
        updated = _replace_station(
            state,
            updated_station,
            status=JourneyProgressStatus.NEEDS_ATTENTION,
            current_station=number,
            updated_at=timestamp,
        )
        self.save(updated)
        return updated

    def record_usage(
        self, usage: Usage, *, now: datetime | None = None
    ) -> JourneyStatus:
        """Add one paid-engine usage observation to the cumulative display total."""

        state = self.load()
        timestamp = _transition_time(now, state)
        try:
            experiment_usage: ExperimentUsage = state.experiment_usage.add(usage)
            updated = _copy_status(
                state,
                {
                    "experiment_usage": experiment_usage,
                    "updated_at": timestamp,
                },
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise JourneyStateError("engine usage is invalid for this journey") from exc
        self.save(updated)
        return updated

    def replace_usage(
        self, usage: Usage, *, now: datetime | None = None
    ) -> JourneyStatus:
        """Idempotently replace totals with cumulative usage rebuilt from evidence."""

        state = self.load()
        try:
            experiment_usage = ExperimentUsage.from_usage(
                usage,
                cost_currency=state.experiment_usage.cost_currency,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise JourneyStateError("engine usage is invalid for this journey") from exc
        if experiment_usage == state.experiment_usage:
            return state

        timestamp = _transition_time(now, state)
        updated = _copy_status(
            state,
            {
                "experiment_usage": experiment_usage,
                "updated_at": timestamp,
            },
        )
        self.save(updated)
        return updated

    @staticmethod
    def _station(state: JourneyStatus, number: int) -> StationProgress:
        if type(number) is not int or not 0 <= number < STATION_COUNT:
            raise JourneyStateError(
                "station number must be an integer from 0 through 7"
            )
        return state.stations[number]

    def _validate_boundary(self, *, allow_missing: bool) -> None:
        try:
            resolved = self.workspace.resolve(strict=False)
        except OSError as exc:
            raise JourneyStateError("workspace path cannot be resolved safely") from exc
        if resolved != self.workspace:
            raise JourneyStateError("workspace path cannot contain symlinks")
        if self.workspace.is_symlink():
            raise JourneyStateError("workspace path cannot be a symlink")
        if self.workspace.exists() and not self.workspace.is_dir():
            raise JourneyStateError("workspace path must be a directory")
        if not allow_missing and not self.workspace.is_dir():
            raise JourneyStateError("workspace does not exist")

        if self.status_directory.is_symlink():
            raise JourneyStateError("journey status directory cannot be a symlink")
        if self.status_directory.exists() and not self.status_directory.is_dir():
            raise JourneyStateError("journey status directory must be a directory")
        if self.status_path.is_symlink():
            raise JourneyStateError("journey status cannot be a symlink")

    def _ensure_status_directory(self) -> None:
        self._validate_boundary(allow_missing=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._validate_boundary(allow_missing=False)
        self.status_directory.mkdir(mode=0o700, exist_ok=True)
        self._validate_boundary(allow_missing=False)

    def _open_status_directory(self, *, create: bool) -> int:
        if create:
            self._ensure_status_directory()
        else:
            self._validate_boundary(allow_missing=False)
            if not self.status_directory.is_dir():
                raise JourneyStateError("journey status does not exist")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            return os.open(self.status_directory, flags)
        except OSError as exc:
            raise JourneyStateError("journey status directory is unsafe") from exc

    def _read_status_bytes(self) -> bytes:
        directory_descriptor = self._open_status_directory(create=False)
        file_descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                file_descriptor = os.open(
                    _STATUS_FILENAME,
                    flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise JourneyStateError("journey status does not exist") from exc
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise JourneyStateError("journey status must be a regular file")
            with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            os.close(directory_descriptor)

    def _atomic_write(self, payload: bytes) -> None:
        directory_descriptor = self._open_status_directory(create=True)
        temporary_name: str | None = None
        temporary_descriptor: int | None = None
        try:
            try:
                existing = os.stat(
                    _STATUS_FILENAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise JourneyStateError("journey status must be a regular file")

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            for _ in range(10):
                candidate = f".{_STATUS_FILENAME}.{uuid.uuid4().hex}.tmp"
                try:
                    temporary_descriptor = os.open(
                        candidate,
                        flags,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_descriptor is None or temporary_name is None:
                raise JourneyStateError("cannot allocate an atomic status file")

            os.fchmod(temporary_descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(temporary_descriptor, remaining)
                if written <= 0:
                    raise OSError("journey status write made no progress")
                remaining = remaining[written:]
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None

            os.replace(
                temporary_name,
                _STATUS_FILENAME,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_name = None
            os.fsync(directory_descriptor)
        except JourneyStateError:
            raise
        except OSError as exc:
            raise JourneyStateError("cannot atomically write journey status") from exc
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except OSError:
                    pass
            os.close(directory_descriptor)

    @staticmethod
    def _status_bytes(state: JourneyStatus) -> bytes:
        return artifact_json_bytes(state) + b"\n"

    @staticmethod
    def _assert_credential_free(payload: bytes) -> None:
        try:
            text = payload.decode("utf-8", errors="strict")
            value = json.loads(text)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise JourneyStateError("journey status must use valid UTF-8 JSON") from exc
        secrets = credential_values(os.environ)
        if JourneyStatusStore._contains_credential(value, secrets):
            raise JourneyStateError("journey status contains credential material")

    @staticmethod
    def _contains_credential(value: object, secrets: Sequence[str]) -> bool:
        if isinstance(value, str):
            return redact(value, secrets) != value
        if isinstance(value, Mapping):
            return any(
                JourneyStatusStore._contains_credential(key, secrets)
                or JourneyStatusStore._contains_credential(child, secrets)
                for key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(
                JourneyStatusStore._contains_credential(child, secrets)
                for child in value
            )
        return False


__all__ = ["JourneyStateError", "JourneyStatusStore"]
