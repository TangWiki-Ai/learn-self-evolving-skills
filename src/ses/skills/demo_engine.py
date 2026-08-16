"""One deterministic offline Engine whose behavior reads the installed workspace."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from ses.contracts import EngineEvent, EngineRequest
from ses.engines.fake import FakeEngine, FakeFixture

from .applicability import is_applicable_return_skill

ENGINE_ID = "offline-workspace-skill-engine-v1"


def _applicable_return_skill(workspace: Path) -> bool:
    skills_root = workspace / ".claude" / "skills"
    if skills_root.is_symlink() or not skills_root.is_dir():
        return False
    for entrypoint in sorted(skills_root.glob("*/SKILL.md")):
        if entrypoint.is_symlink() or not entrypoint.is_file():
            continue
        try:
            content = entrypoint.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if is_applicable_return_skill(content):
            return True
    return False


def _fixture(*, complete_return: bool, session_id: str) -> FakeFixture:
    events: list[dict[str, object]] = [
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
                "content": {"offline_placeholder": True},
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
                "content": {"offline_placeholder": True},
                "is_error": False,
            }
        },
        {
            "payload": {
                "kind": "tool_call",
                "message_id": "message-1",
                "tool_call_id": "tool-preview-return",
                "tool_name": "process_return",
                "arguments": {"item_id": "ITEM-9050", "reason": "defective"},
            }
        },
        {
            "payload": {
                "kind": "tool_result",
                "tool_call_id": "tool-preview-return",
                "content": {"offline_placeholder": True},
                "is_error": False,
            }
        },
    ]
    if complete_return:
        events.extend(
            [
                {
                    "payload": {
                        "kind": "tool_call",
                        "message_id": "message-2",
                        "tool_call_id": "tool-confirm-return",
                        "tool_name": "process_return",
                        "arguments": {
                            "item_id": "ITEM-9050",
                            "reason": "defective",
                            "confirm": True,
                            "amount_minor": 129900,
                        },
                    }
                },
                {
                    "payload": {
                        "kind": "tool_result",
                        "tool_call_id": "tool-confirm-return",
                        "content": {"offline_placeholder": True},
                        "is_error": False,
                    }
                },
            ]
        )
    events.extend(
        [
            {
                "payload": {
                    "kind": "text_delta",
                    "message_id": "message-3",
                    "text": (
                        "The return is complete and verified."
                        if complete_return
                        else "I prepared the return, but I did not confirm it."
                    ),
                }
            },
            {
                "payload": {
                    "kind": "usage",
                    "usage": {"input_tokens": 103, "output_tokens": 38},
                }
            },
        ]
    )
    return FakeFixture.model_validate({"session_id": session_id, "events": events})


class OfflineSkillDemoEngine:
    """Derive the offline action plan from the actual workspace Skill files."""

    def __init__(self, workspace: Path) -> None:
        workspace_identity = hashlib.sha256(
            str(workspace.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        self._delegate = FakeEngine(
            _fixture(
                complete_return=_applicable_return_skill(workspace),
                session_id=f"offline-skill-demo-{workspace_identity}",
            )
        )

    def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]:
        return self._delegate.stream(request)

    async def cancel(self, request_id: str) -> bool:
        return await self._delegate.cancel(request_id)
