from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventKind,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    RecordType,
    SchemaVersion,
    Trace,
    UsagePayload,
)
from ses.engines.fake import (
    FakeEngine,
    FakeFixture,
    FakeFixtureError,
    load_fake_fixture,
)

FIXTURES = Path(__file__).with_name("fixtures")


def _request(*, timeout: float = 1) -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Handle the return.",
        timeout_seconds=timeout,
    )


async def _collect(engine: FakeEngine) -> list[EngineEvent]:
    return [event async for event in engine.stream(_request())]


def test_fake_replays_text_tools_usage_and_terminal_event() -> None:
    events = asyncio.run(
        _collect(FakeEngine(load_fake_fixture(FIXTURES / "success.json")))
    )

    assert [event.sequence for event in events] == list(range(5))
    assert [event.payload.kind for event in events] == [
        EngineEventKind.TEXT_DELTA,
        EngineEventKind.TOOL_CALL,
        EngineEventKind.TOOL_RESULT,
        EngineEventKind.USAGE,
        EngineEventKind.COMPLETED,
    ]
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-1].payload.exit_status is EngineExitStatus.SUCCESS
    assert events[-1].payload.session_id == "fake-session-1"


@pytest.mark.parametrize(
    ("fixture", "error_code", "status"),
    [
        (FakeFixture(timeout=True), "timeout", EngineExitStatus.TIMEOUT),
        (FakeFixture(exit_code=17), "process_exit", EngineExitStatus.ERROR),
        (
            FakeFixture(malformed_event=True),
            "malformed_stream",
            EngineExitStatus.ERROR,
        ),
        (
            FakeFixture(exception_message="fixture crashed"),
            "fixture_exception",
            EngineExitStatus.ERROR,
        ),
    ],
)
def test_fake_simulates_terminal_failures(
    fixture: FakeFixture, error_code: str, status: EngineExitStatus
) -> None:
    events = asyncio.run(_collect(FakeEngine(fixture)))

    assert isinstance(events[-2].payload, ErrorPayload)
    assert isinstance(events[-1].payload, CompletedPayload)
    assert events[-2].payload.error_code == error_code
    assert events[-1].payload.exit_status is status


def test_fake_can_be_cancelled_during_a_delayed_replay() -> None:
    fixture = FakeFixture.model_validate(
        {
            "events": [
                {
                    "delay_seconds": 0.05,
                    "payload": {
                        "kind": "text_delta",
                        "message_id": "message-1",
                        "text": "late",
                    },
                }
            ]
        }
    )
    engine = FakeEngine(fixture)

    async def scenario() -> list[EngineEvent]:
        task = asyncio.create_task(_collect(engine))
        await asyncio.sleep(0)
        assert await engine.cancel("request-1")
        return await task

    events = asyncio.run(scenario())

    assert len(events) == 1
    assert isinstance(events[0].payload, CompletedPayload)
    assert events[0].payload.exit_status is EngineExitStatus.CANCELLED


def test_fake_fixture_rejects_unknown_and_malformed_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"events": [], "api_key": "not-allowed"}', encoding="utf-8")

    with pytest.raises(FakeFixtureError):
        load_fake_fixture(path)


def test_fake_fixture_rejects_conflicting_terminal_modes() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        FakeFixture(timeout=True, exit_code=1)


def test_fake_events_form_a_canonical_trace() -> None:
    request = _request()
    events = asyncio.run(
        _collect(FakeEngine(load_fake_fixture(FIXTURES / "success.json")))
    )
    usage = next(
        event.payload.usage
        for event in events
        if isinstance(event.payload, UsagePayload)
    )

    trace = Trace(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TRACE,
        trace_id="trace-1",
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-1",
        session_id="fake-session-1",
        request=request,
        events=tuple(events),
        usage=usage,
        exit_status=EngineExitStatus.SUCCESS,
    )

    assert trace.events == tuple(events)
    assert trace.session_id == "fake-session-1"
