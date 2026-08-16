"""Deterministic fixtures that make the Lesson 1 difference visible offline."""

from __future__ import annotations

from importlib.resources import as_file, files

from ses.engines.fake import FakeFixture, load_fake_fixture


def without_skill_fixture() -> FakeFixture:
    """Replay an agent that previews the return but never confirms it."""
    return FakeFixture.model_validate(
        {
            "session_id": "fake-session-without-skill",
            "events": [
                {
                    "payload": {
                        "kind": "text_delta",
                        "message_id": "message-1",
                        "text": "I will inspect the order and return policy first.",
                    }
                },
                {
                    "payload": {
                        "kind": "tool_call",
                        "message_id": "message-1",
                        "tool_call_id": "tool-get-order",
                        "tool_name": "get_order",
                        "arguments": {"order_id": "ORD-6006"},
                    }
                },
                {
                    "payload": {
                        "kind": "tool_result",
                        "tool_call_id": "tool-get-order",
                        "content": {"fixture_placeholder": True},
                        "is_error": False,
                    }
                },
                {
                    "payload": {
                        "kind": "tool_call",
                        "message_id": "message-1",
                        "tool_call_id": "tool-get-policy",
                        "tool_name": "get_policies",
                        "arguments": {"topic": "return"},
                    }
                },
                {
                    "payload": {
                        "kind": "tool_result",
                        "tool_call_id": "tool-get-policy",
                        "content": {"fixture_placeholder": True},
                        "is_error": False,
                    }
                },
                {
                    "payload": {
                        "kind": "tool_call",
                        "message_id": "message-1",
                        "tool_call_id": "tool-preview-return",
                        "tool_name": "process_return",
                        "arguments": {
                            "item_id": "ITEM-9050",
                            "reason": "defective",
                        },
                    }
                },
                {
                    "payload": {
                        "kind": "tool_result",
                        "tool_call_id": "tool-preview-return",
                        "content": {"fixture_placeholder": True},
                        "is_error": False,
                    }
                },
                {
                    "payload": {
                        "kind": "text_delta",
                        "message_id": "message-2",
                        "text": "I prepared the return, but I did not confirm it.",
                    }
                },
                {
                    "payload": {
                        "kind": "usage",
                        "usage": {"input_tokens": 103, "output_tokens": 38},
                    }
                },
            ],
        }
    )


def with_skill_fixture() -> FakeFixture:
    """Replay the checked-in successful path with a fresh fake session."""
    resource = files("ses.evaluator").joinpath("fixtures/pinned_return_success.json")
    with as_file(resource) as path:
        fixture = load_fake_fixture(path)
    return fixture.model_copy(update={"session_id": "fake-session-with-skill"})
