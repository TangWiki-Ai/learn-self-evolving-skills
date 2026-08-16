"""Declarative offline engine used by tests and course exercises."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventPayload,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
)
from ses.engines.events import make_event


class FakeFixtureError(ValueError):
    """A fake-engine fixture is not declarative or canonical."""


class FakeStep(BaseModel):
    """One event and optional deterministic delay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    delay_seconds: float = Field(default=0, ge=0)
    payload: EngineEventPayload

    @field_validator("delay_seconds", mode="before")
    @classmethod
    def _strict_delay(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("delay_seconds must be numeric")
        return value


class FakeFixture(BaseModel):
    """A complete replay, including process-like terminal behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    events: tuple[FakeStep, ...] = ()
    session_id: str = Field(default="fake-session-1", min_length=1)
    exit_code: int = 0
    timeout: bool = False
    malformed_event: bool = False
    exception_message: str | None = None

    @field_validator("events", mode="before")
    @classmethod
    def _json_events_to_tuple(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("exit_code", mode="before")
    @classmethod
    def _strict_exit_code(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("exit_code must be an integer")
        return value

    @field_validator("exception_message")
    @classmethod
    def _nonblank_exception(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("exception_message must not be blank")
        return value

    @model_validator(mode="after")
    def _terminal_shape_is_unambiguous(self) -> FakeFixture:
        completed = [
            index
            for index, step in enumerate(self.events)
            if isinstance(step.payload, CompletedPayload)
        ]
        if completed and completed != [len(self.events) - 1]:
            raise ValueError("a completed event must be the fixture's final event")
        terminal_modes = sum(
            (
                self.exit_code != 0,
                self.timeout,
                self.malformed_event,
                self.exception_message is not None,
            )
        )
        if terminal_modes > 1 or (completed and terminal_modes):
            raise ValueError("fake fixture terminal modes are mutually exclusive")
        return self


def load_fake_fixture(path: Path) -> FakeFixture:
    """Load a strict fixture without accessing the network or process environment."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return FakeFixture.model_validate(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise FakeFixtureError(f"invalid fake-engine fixture {path}: {exc}") from exc


class FakeEngine:
    """Replay canonical event payloads and simulate exit, timeout, and cancellation."""

    def __init__(self, fixture: FakeFixture) -> None:
        self._fixture = fixture
        self._cancelled: set[str] = set()
        self._active: set[str] = set()

    async def cancel(self, request_id: str) -> bool:
        if request_id not in self._active:
            return False
        self._cancelled.add(request_id)
        return True

    async def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]:
        sequence = 0
        terminal = False
        session_id = request.resume_session_id or self._fixture.session_id
        self._active.add(request.request_id)
        try:
            for step in self._fixture.events:
                if step.delay_seconds:
                    await asyncio.sleep(step.delay_seconds)
                if request.request_id in self._cancelled:
                    break
                payload = step.payload
                if isinstance(payload, CompletedPayload):
                    terminal = True
                    if payload.exit_status is EngineExitStatus.SUCCESS:
                        payload = payload.model_copy(update={"session_id": session_id})
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=payload,
                )
                sequence += 1
            if request.request_id in self._cancelled:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=CompletedPayload(exit_status=EngineExitStatus.CANCELLED),
                )
            elif self._fixture.timeout:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=ErrorPayload(
                        error_code="timeout",
                        message=f"engine timed out after {request.timeout_seconds:g}s",
                    ),
                )
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence + 1,
                    payload=CompletedPayload(exit_status=EngineExitStatus.TIMEOUT),
                )
            elif self._fixture.malformed_event:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=ErrorPayload(
                        error_code="malformed_stream",
                        message="fixture injected a malformed stream event",
                    ),
                )
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence + 1,
                    payload=CompletedPayload(exit_status=EngineExitStatus.ERROR),
                )
            elif self._fixture.exception_message is not None:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=ErrorPayload(
                        error_code="fixture_exception",
                        message=self._fixture.exception_message,
                    ),
                )
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence + 1,
                    payload=CompletedPayload(exit_status=EngineExitStatus.ERROR),
                )
            elif self._fixture.exit_code != 0:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=ErrorPayload(
                        error_code="process_exit",
                        message=f"engine exited with code {self._fixture.exit_code}",
                    ),
                )
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence + 1,
                    payload=CompletedPayload(exit_status=EngineExitStatus.ERROR),
                )
            elif not terminal:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=CompletedPayload(
                        exit_status=EngineExitStatus.SUCCESS,
                        session_id=session_id,
                    ),
                )
        finally:
            self._active.discard(request.request_id)
            self._cancelled.discard(request.request_id)
