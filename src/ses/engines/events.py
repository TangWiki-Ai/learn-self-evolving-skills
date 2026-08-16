"""Helpers for creating canonical engine events without leaking provider data."""

from __future__ import annotations

from datetime import UTC, datetime

from ses.contracts import (
    EngineEvent,
    EngineEventPayload,
    RecordType,
    SchemaVersion,
)


def make_event(
    *,
    request_id: str,
    sequence: int,
    payload: EngineEventPayload,
) -> EngineEvent:
    """Create one canonical event with a stable scope-local ID."""
    return EngineEvent(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_EVENT,
        event_id=f"{request_id}:event:{sequence}",
        request_id=request_id,
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        payload=payload,
    )
