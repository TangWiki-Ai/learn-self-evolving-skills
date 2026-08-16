"""Normalize Claude Code stream-json lines into public event payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import cast

from pydantic import JsonValue, ValidationError

from ses.contracts import (
    CompletedPayload,
    EngineEventPayload,
    EngineExitStatus,
    ErrorPayload,
    TextDeltaPayload,
    ToolCallPayload,
    ToolResultPayload,
    UnknownPayload,
    Usage,
    UsagePayload,
)
from ses.foundation.credentials import redact_data


class StreamParseError(ValueError):
    """A critical stream-json line cannot be normalized safely."""


class ClaudeStreamParser:
    """Stateful parser for Claude Code's documented JSONL output shapes."""

    def __init__(
        self,
        *,
        secrets: Sequence[str] = (),
        expects_structured_output: bool = False,
    ) -> None:
        self._secrets = tuple(secrets)
        self._expects_structured_output = expects_structured_output
        self._structured_output_ids: set[str] = set()
        self.session_id: str | None = None
        self.completed = False

    def parse_line(self, line: str) -> list[EngineEventPayload]:
        """Parse one JSONL record; never return a provider-private payload."""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StreamParseError("stream-json line is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise StreamParseError("stream-json line must be a JSON object")
        event = cast(dict[str, object], redact_data(raw, self._secrets))
        event_type = event.get("type")
        try:
            if event_type == "system":
                session = event.get("session_id")
                if isinstance(session, str) and session.strip():
                    self.session_id = session
                subtype = event.get("subtype")
                return [UnknownPayload(source_type=f"system:{subtype or 'unknown'}")]
            if event_type == "assistant":
                return self._assistant_payloads(event)
            if event_type == "user":
                return self._tool_result_payloads(event)
            if event_type == "result":
                return self._result_payloads(event)
            source_type = str(event_type) if event_type is not None else "missing_type"
            return [UnknownPayload(source_type=source_type)]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise StreamParseError(f"malformed critical {event_type!r} event") from exc

    def _assistant_payloads(
        self, event: Mapping[str, object]
    ) -> list[EngineEventPayload]:
        message = event.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("assistant.message must be an object")
        raw_message_id = message.get("id") or event.get("uuid") or "assistant-message"
        if not isinstance(raw_message_id, str):
            raise ValueError("assistant message id must be a string")
        content = message.get("content")
        if not isinstance(content, list):
            raise ValueError("assistant.message.content must be an array")
        payloads: list[EngineEventPayload] = []
        for block in content:
            if not isinstance(block, Mapping):
                raise ValueError("assistant content block must be an object")
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if (
                    isinstance(text, str)
                    and text
                    and not self._expects_structured_output
                ):
                    payloads.append(
                        TextDeltaPayload(message_id=raw_message_id, text=text)
                    )
            elif kind == "tool_use":
                tool_id = block.get("id")
                name = block.get("name")
                arguments = block.get("input", {})
                if not isinstance(tool_id, str) or not isinstance(name, str):
                    raise ValueError("tool_use id and name must be strings")
                if not isinstance(arguments, Mapping):
                    raise ValueError("tool_use input must be an object")
                if name == "StructuredOutput" and self._expects_structured_output:
                    if self._structured_output_ids:
                        raise ValueError("duplicate structured output")
                    self._structured_output_ids.add(tool_id)
                else:
                    payloads.append(
                        ToolCallPayload(
                            message_id=raw_message_id,
                            tool_call_id=tool_id,
                            tool_name=name,
                            arguments=cast(dict[str, JsonValue], dict(arguments)),
                        )
                    )
            else:
                payloads.append(
                    UnknownPayload(source_type=f"assistant_block:{kind or 'unknown'}")
                )
        return payloads

    def _tool_result_payloads(
        self, event: Mapping[str, object]
    ) -> list[EngineEventPayload]:
        message = event.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("user.message must be an object")
        content = message.get("content")
        if not isinstance(content, list):
            raise ValueError("user.message.content must be an array")
        payloads: list[EngineEventPayload] = []
        for block in content:
            if not isinstance(block, Mapping):
                raise ValueError("user content block must be an object")
            kind = block.get("type")
            if kind != "tool_result":
                payloads.append(
                    UnknownPayload(source_type=f"user_block:{kind or 'unknown'}")
                )
                continue
            tool_id = block.get("tool_use_id")
            if not isinstance(tool_id, str):
                raise ValueError("tool_result.tool_use_id must be a string")
            if tool_id in self._structured_output_ids:
                continue
            content_value = cast(JsonValue, block.get("content"))
            payloads.append(
                ToolResultPayload(
                    tool_call_id=tool_id,
                    content=content_value,
                    is_error=bool(block.get("is_error", False)),
                )
            )
        return payloads

    def _result_payloads(self, event: Mapping[str, object]) -> list[EngineEventPayload]:
        if self.completed:
            raise ValueError("duplicate result event")
        session = event.get("session_id")
        if isinstance(session, str) and session.strip():
            self.session_id = session
        is_error = bool(event.get("is_error", False))
        subtype = event.get("subtype")
        status = (
            EngineExitStatus.ERROR
            if is_error or subtype in {"error", "error_max_turns"}
            else EngineExitStatus.SUCCESS
        )
        payloads: list[EngineEventPayload] = []
        if self._expects_structured_output and status is EngineExitStatus.SUCCESS:
            if "structured_output" not in event:
                raise ValueError("successful result is missing structured output")
            payloads.append(
                TextDeltaPayload(
                    message_id=f"{self.session_id or 'claude'}-structured-output",
                    text=json.dumps(
                        event["structured_output"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            )
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            if not isinstance(input_tokens, int) or isinstance(input_tokens, bool):
                raise ValueError("usage.input_tokens must be an integer")
            if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
                raise ValueError("usage.output_tokens must be an integer")
            cost = self._decimal_cost(event.get("total_cost_usd"))
            payloads.append(
                UsagePayload(
                    usage=Usage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_amount=cost,
                        cost_currency="USD" if cost is not None else None,
                    )
                )
            )
        if status is EngineExitStatus.ERROR:
            raw_message = (
                event.get("result") or event.get("error") or "claude result error"
            )
            payloads.append(
                ErrorPayload(
                    error_code=str(subtype or "result_error"),
                    message=str(raw_message),
                )
            )
        self.completed = True
        payloads.append(
            CompletedPayload(exit_status=status, session_id=self.session_id)
        )
        return payloads

    @staticmethod
    def _decimal_cost(value: object) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("total_cost_usd must be numeric")
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("total_cost_usd is invalid") from exc
        if not result.is_finite() or result < 0:
            raise ValueError("total_cost_usd must be finite and nonnegative")
        return result
