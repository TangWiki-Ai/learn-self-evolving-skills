"""Pinned offline evaluator used by the baseline CLI."""

from __future__ import annotations

import asyncio
from importlib.resources import as_file, files

from ses.contracts import EngineEvent, EngineRequest, RecordType, SchemaVersion
from ses.engines.fake import FakeEngine, load_fake_fixture
from ses.evaluation import build_trace, trace_messages, trace_tool_calls
from ses.runner.baseline import CaseEvaluation, IterationStatus
from ses.shop import CASE_DEFINITION
from ses.simulation import FakeSimulator, UserIntent


async def _events(
    engine: FakeEngine, request: EngineRequest
) -> tuple[EngineEvent, ...]:
    return tuple([event async for event in engine.stream(request)])


class PinnedFakeEvaluator:
    """Replay the checked-in successful case without network or credentials."""

    def __call__(
        self, case_id: str, iteration_id: str, max_turns: int
    ) -> CaseEvaluation:
        if case_id != CASE_DEFINITION.case_id:
            raise ValueError(f"unsupported fake baseline case: {case_id}")
        resource = files("ses").joinpath(
            "evaluator/fixtures/pinned_return_success.json"
        )
        with as_file(resource) as fixture_path:
            fixture = load_fake_fixture(fixture_path)
        simulator = FakeSimulator(UserIntent(want=CASE_DEFINITION.user_prompt))
        user_turn = simulator.next_turn(())
        request = EngineRequest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ENGINE_REQUEST,
            request_id=f"run-fake-baseline:{case_id}:{iteration_id}:turn-0",
            prompt=user_turn.message or CASE_DEFINITION.user_prompt,
            allowed_tools=CASE_DEFINITION.required_tools,
            timeout_seconds=30,
        )
        engine = FakeEngine(fixture)
        canonical_events = asyncio.run(_events(engine, request))
        trace = build_trace(
            canonical_events,
            request=request,
            run_id="run-fake-baseline",
            case_id=case_id,
            iteration_id=iteration_id,
        )
        traces = (trace,)
        timeline: list[dict[str, object]] = []
        transcript: list[dict[str, object]] = [
            {"role": "user", "content": CASE_DEFINITION.user_prompt}
        ]
        for trace in traces:
            transcript.extend(
                {"role": "assistant", "content": message.text}
                for message in trace_messages(trace)
            )
            timeline.extend(
                {
                    "sequence": call.sequence,
                    "tool_name": call.tool_name,
                    "arguments": dict(call.arguments),
                    "is_error": (None if call.result is None else call.result.is_error),
                }
                for call in trace_tool_calls(trace)
            )
        return CaseEvaluation(
            case_id=case_id,
            iteration_id=iteration_id,
            status=IterationStatus.PASS,
            turn_count=len(traces),
            input_tokens=trace.usage.input_tokens if trace.usage else 0,
            output_tokens=trace.usage.output_tokens if trace.usage else 0,
            latency_ms=0,
            evidence=(
                {
                    "assertion_id": "fake-fixture-success",
                    "status": "pass",
                    "reason": "checked-in FakeEngine success fixture",
                },
            ),
            tool_timeline=tuple(timeline),
            state_diff={
                "summary": "defective item returned and refund recorded",
                "changed": [
                    "/order_items/ITEM-9050/item_status",
                    "/order_items/ITEM-9050/refund_amount",
                ],
            },
            transcript=tuple(transcript),
        )
