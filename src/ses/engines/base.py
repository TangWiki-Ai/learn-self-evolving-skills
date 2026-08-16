"""The narrow asynchronous Engine seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ses.contracts import EngineEvent, EngineRequest


class Engine(Protocol):
    """Stream canonical events and support cooperative cancellation by request ID."""

    def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]: ...

    async def cancel(self, request_id: str) -> bool:
        """Request cancellation; return whether a live request was found."""
        ...
