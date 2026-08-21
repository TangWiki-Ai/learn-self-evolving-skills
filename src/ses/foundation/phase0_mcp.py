"""Credential-free stdio MCP server used by the doctor live check."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from typing import TextIO

TOOL_NAME = "phase0_ping"


def _response(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle(message: Mapping[str, object]) -> dict[str, object] | None:
    """Handle one JSON-RPC message without reading process credentials."""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, Mapping):
        params = {}

    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ses-phase0", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(
            request_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "Return a deterministic doctor pong value.",
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
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return _response(
                request_id,
                {
                    "content": [{"type": "text", "text": "unknown tool"}],
                    "isError": True,
                },
            )
        arguments = params.get("arguments") or {}
        value = arguments.get("value") if isinstance(arguments, Mapping) else None
        if not isinstance(value, str):
            return _response(
                request_id,
                {
                    "content": [{"type": "text", "text": "value must be a string"}],
                    "isError": True,
                },
            )
        return _response(
            request_id,
            {"content": [{"type": "text", "text": f"pong:{value}"}]},
        )
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(lines: Iterable[str], output: TextIO) -> None:
    """Serve JSONL requests and keep parse failures structured."""
    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            response = handle(message)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            output.write(json.dumps(response, separators=(",", ":")) + "\n")
            output.flush()


def main() -> int:
    """Remove inherited credentials before entering the local MCP loop."""
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CHATANYWHERE_API_KEY",
        "SILICONFLOW_API_KEY",
    ):
        os.environ.pop(name, None)
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
