from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from ses.contracts import (
    EngineEvent,
    EngineRequest,
    TextDeltaPayload,
    Trace,
    Usage,
    UsagePayload,
)
from ses.engines.fake import FakeEngine, FakeFixture, FakeStep
from ses.evaluator.multi_turn import MultiTurnEvaluator, MultiTurnOutcome
from ses.simulation import ConstrainedUserSimulator, SimulatorTurn, UserIntent


def _engine() -> FakeEngine:
    return FakeEngine(
        FakeFixture(
            session_id="session-case-a",
            events=(
                FakeStep(
                    payload=TextDeltaPayload(
                        message_id="assistant-1", text="Please share the order id."
                    )
                ),
                FakeStep(
                    payload=UsagePayload(usage=Usage(input_tokens=11, output_tokens=7))
                ),
            ),
        )
    )


def test_first_turn_is_fresh_and_followup_resumes_the_same_session() -> None:
    evaluator = MultiTurnEvaluator(_engine())
    simulator = ConstrainedUserSimulator(
        UserIntent(
            want="I want to return a defective item.",
            allowed_facts={"order_id": "ORD-6006"},
        )
    )

    result = asyncio.run(
        evaluator.evaluate(
            run_id="run-multi",
            case_id="case-a",
            iteration_id="iteration-0",
            simulator=simulator,
            max_turns=3,
        )
    )

    assert result.outcome is MultiTurnOutcome.COMPLETED
    assert len(result.traces) == 2
    assert result.traces[0].request.resume_session_id is None
    assert result.traces[1].request.resume_session_id == "session-case-a"
    assert {trace.session_id for trace in result.traces} == {"session-case-a"}
    assert result.usage == Usage(input_tokens=22, output_tokens=14)


def test_turn_budget_preserves_completed_trace_and_stops_structurally() -> None:
    evaluator = MultiTurnEvaluator(_engine())
    simulator = ConstrainedUserSimulator(
        UserIntent(
            want="I want to return a defective item.",
            allowed_facts={"order_id": "ORD-6006"},
        )
    )

    result = asyncio.run(
        evaluator.evaluate(
            run_id="run-budget",
            case_id="case-a",
            iteration_id="iteration-0",
            simulator=simulator,
            max_turns=1,
        )
    )

    assert result.outcome is MultiTurnOutcome.BUDGET_STOP
    assert result.stop_reason == "turn_limit"
    assert len(result.traces) == 1


def test_a_new_case_never_receives_an_old_case_session() -> None:
    evaluator = MultiTurnEvaluator(_engine())

    first = asyncio.run(
        evaluator.evaluate(
            run_id="run-isolation",
            case_id="case-a",
            iteration_id="iteration-0",
            simulator=ConstrainedUserSimulator(UserIntent(want="I want help.")),
            max_turns=1,
        )
    )
    second = asyncio.run(
        evaluator.evaluate(
            run_id="run-isolation",
            case_id="case-b",
            iteration_id="iteration-0",
            simulator=ConstrainedUserSimulator(UserIntent(want="I want help.")),
            max_turns=1,
        )
    )

    assert first.traces[0].request.resume_session_id is None
    assert second.traces[0].request.resume_session_id is None


def test_simulator_error_preserves_completed_trace_and_usage() -> None:
    class BrokenSimulator(ConstrainedUserSimulator):
        def next_turn(self, assistant_messages: Sequence[str]) -> SimulatorTurn:
            if assistant_messages:
                raise RuntimeError("simulator failed after paid turn")
            return super().next_turn(assistant_messages)

    persisted: list[Trace] = []
    evaluator = MultiTurnEvaluator(_engine(), on_trace=persisted.append)
    result = asyncio.run(
        evaluator.evaluate(
            run_id="run-simulator-error",
            case_id="case-a",
            iteration_id="iteration-0",
            simulator=BrokenSimulator(UserIntent(want="I want help.")),
            max_turns=2,
        )
    )

    assert result.outcome is MultiTurnOutcome.SIMULATOR_ERROR
    assert len(result.traces) == 1
    assert persisted == [result.traces[0]]
    assert result.usage == Usage(input_tokens=11, output_tokens=7)


def test_engine_error_on_resume_preserves_prior_trace_and_usage() -> None:
    class FailOnResume:
        def __init__(self) -> None:
            self.engine = _engine()

        async def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]:
            if request.resume_session_id is not None:
                raise RuntimeError("resume transport failed")
            async for event in self.engine.stream(request):
                yield event

        async def cancel(self, request_id: str) -> bool:
            return await self.engine.cancel(request_id)

    persisted: list[Trace] = []
    result = asyncio.run(
        MultiTurnEvaluator(FailOnResume(), on_trace=persisted.append).evaluate(
            run_id="run-engine-error",
            case_id="case-a",
            iteration_id="iteration-0",
            simulator=ConstrainedUserSimulator(
                UserIntent(want="I need help.", allowed_facts={"order_id": "ORD-6006"})
            ),
            max_turns=2,
        )
    )

    assert result.outcome is MultiTurnOutcome.INFRASTRUCTURE_ERROR
    assert result.traces == tuple(persisted)
    assert result.usage == Usage(input_tokens=11, output_tokens=7)
