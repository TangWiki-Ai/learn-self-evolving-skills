from __future__ import annotations

import json

import pytest

from ses.contracts import (
    CompletedPayload,
    EngineEventKind,
    EngineExitStatus,
    ErrorPayload,
    TextDeltaPayload,
    ToolCallPayload,
)
from ses.engines.stream_json import ClaudeStreamParser, StreamParseError


def test_parser_normalizes_real_claude_stream_shapes() -> None:
    parser = ClaudeStreamParser(secrets=("exact-secret",))
    lines = [
        {"type": "system", "subtype": "init", "session_id": "session-1"},
        {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "get_order",
                        "input": {"note": "exact-secret"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "order is eligible",
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-1",
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "total_cost_usd": 0.0012,
        },
    ]

    payloads = [
        payload for line in lines for payload in parser.parse_line(json.dumps(line))
    ]

    assert [payload.kind for payload in payloads] == [
        EngineEventKind.UNKNOWN,
        EngineEventKind.TEXT_DELTA,
        EngineEventKind.TOOL_CALL,
        EngineEventKind.TOOL_RESULT,
        EngineEventKind.USAGE,
        EngineEventKind.COMPLETED,
    ]
    tool_call = payloads[2]
    assert isinstance(tool_call, ToolCallPayload)
    assert isinstance(payloads[-1], CompletedPayload)
    assert "exact-secret" not in json.dumps(dict(tool_call.arguments))
    assert payloads[-1].exit_status is EngineExitStatus.SUCCESS
    assert parser.session_id == "session-1"


@pytest.mark.parametrize(
    "line",
    [
        "not-json",
        "[]",
        json.dumps({"type": "assistant", "message": {"content": "bad"}}),
        json.dumps({"type": "result", "usage": {"input_tokens": "1"}}),
    ],
)
def test_parser_rejects_malformed_critical_events(line: str) -> None:
    with pytest.raises(StreamParseError):
        ClaudeStreamParser().parse_line(line)


def test_parser_keeps_unknown_events_provider_neutral() -> None:
    payload = ClaudeStreamParser().parse_line(
        json.dumps({"type": "provider_heartbeat", "private": {"foo": "bar"}})
    )[0]

    assert payload.kind is EngineEventKind.UNKNOWN
    assert payload.source_type == "provider_heartbeat"
    assert payload.artifact is None


def test_parser_normalizes_claude_structured_output_without_exposing_a_tool() -> None:
    parser = ClaudeStreamParser(expects_structured_output=True)
    assistant = {
        "type": "assistant",
        "message": {
            "id": "message-structured",
            "content": [
                {
                    "type": "tool_use",
                    "id": "structured-1",
                    "name": "StructuredOutput",
                    "input": {"confidence": 0.9, "mappable": True},
                }
            ],
        },
    }
    acknowledgement = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "structured-1",
                    "content": "Structured output provided successfully",
                }
            ]
        },
    }
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "session-structured",
        "structured_output": {"confidence": 0.9, "mappable": True},
    }

    assert parser.parse_line(json.dumps(assistant)) == []
    assert parser.parse_line(json.dumps(acknowledgement)) == []
    payloads = parser.parse_line(json.dumps(result))

    assert len(payloads) == 2
    assert isinstance(payloads[0], TextDeltaPayload)
    assert json.loads(payloads[0].text) == {"confidence": 0.9, "mappable": True}
    assert isinstance(payloads[1], CompletedPayload)


def test_parser_rejects_missing_structured_result() -> None:
    parser = ClaudeStreamParser(expects_structured_output=True)

    with pytest.raises(StreamParseError):
        parser.parse_line(
            json.dumps({"type": "result", "subtype": "success", "is_error": False})
        )


def test_parser_emits_structured_error_before_failed_completion() -> None:
    payloads = ClaudeStreamParser().parse_line(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "result": "maximum turns reached",
            }
        )
    )

    assert isinstance(payloads[-2], ErrorPayload)
    assert isinstance(payloads[-1], CompletedPayload)
    assert payloads[-2].error_code == "error_max_turns"
    assert payloads[-1].exit_status is EngineExitStatus.ERROR


def test_parser_removes_nested_sensitive_field_names_and_values() -> None:
    payload = ClaudeStreamParser().parse_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "message-1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "shop",
                            "input": {
                                "nested": {"SHOP_API_KEY": "nested-plain-secret"}
                            },
                        }
                    ],
                },
            }
        )
    )[0]

    rendered = payload.model_dump_json()
    assert "SHOP_API_KEY" not in rendered
    assert "nested-plain-secret" not in rendered
