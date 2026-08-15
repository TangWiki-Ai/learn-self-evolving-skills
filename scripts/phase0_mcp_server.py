#!/usr/bin/env python3
"""Minimal stdio MCP server used only by the Phase 0 smoke check."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

TOOL_NAME = "phase0_ping"


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _handle(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        protocol_version = params.get("protocolVersion", "2025-06-18")
        _result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ses-phase0", "version": "0.1.0"},
            },
        )
        return

    if method == "ping":
        _result(request_id, {})
        return

    if method == "tools/list":
        _result(
            request_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "Return a deterministic Phase 0 pong value.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )
        return

    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "unknown tool"}],
                    "isError": True,
                },
            )
            return

        arguments = params.get("arguments") or {}
        value = arguments.get("value")
        if not isinstance(value, str):
            _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "value must be a string"}],
                    "isError": True,
                },
            )
            return

        _result(
            request_id,
            {"content": [{"type": "text", "text": f"pong:{value}"}]},
        )
        return

    # MCP notifications do not have an id and must not receive a response.
    if request_id is None:
        return

    _error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    # Claude needs the key, but this local tool does not.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SILICONFLOW_API_KEY"):
        os.environ.pop(name, None)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            _handle(message)
        except (json.JSONDecodeError, ValueError) as exc:
            _error(None, -32700, str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
