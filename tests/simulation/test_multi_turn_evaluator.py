from __future__ import annotations

import asyncio

from ses.contracts import TextDeltaPayload, Usage, UsagePayload
from ses.engines.fake import FakeEngine, FakeFixture, FakeStep
from ses.evaluator.multi_turn import MultiTurnEvaluator, MultiTurnOutcome
from ses.simulation import ConstrainedUserSimulator, UserIntent


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

    assert result.outcome is MultiTurnOutcome.NOT_EVALUATED
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
