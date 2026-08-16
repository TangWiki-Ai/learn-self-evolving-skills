"""Minimal stdio MCP server for the pinned deterministic shop case."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence

from ses.contracts import ToolResult, ToolResultStatus
from ses.shop.environment import CASE_ID, CaseEnvironment, ShopRole


def _send(payload: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: Mapping[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _mcp_tool_result(tool_result: ToolResult) -> dict[str, object]:
    structured = tool_result.model_dump(mode="json")
    result: dict[str, object] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, sort_keys=True, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
    }
    if tool_result.status is ToolResultStatus.ERROR:
        result["isError"] = True
    return result


def _handle(environment: CaseEnvironment, message: Mapping[str, object]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        supplied = (
            params.get("protocolVersion") if isinstance(params, Mapping) else None
        )
        protocol_version = supplied if isinstance(supplied, str) else "2025-06-18"
        _result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ses-shop", "version": "0.1.0"},
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "ping":
        _result(request_id, {})
        return
    if method == "tools/list":
        _result(request_id, {"tools": list(environment.available_tools())})
        return
    if method == "tools/call":
        if not isinstance(params, Mapping):
            _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": "tools/call params must be an object"}
                    ],
                    "isError": True,
                },
            )
            return
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str):
            _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "tool name must be a string"}],
                    "isError": True,
                },
            )
            return
        _result(request_id, _mcp_tool_result(environment.execute(name, arguments)))
        return
    if request_id is not None:
        _error(request_id, -32601, f"method not found: {method!r}")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the pinned SES shop MCP server.")
    parser.add_argument("--case", default=CASE_ID, choices=[CASE_ID])
    parser.add_argument(
        "--role",
        default=ShopRole.AGENT.value,
        choices=[role.value for role in ShopRole],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    environment = CaseEnvironment(role=ShopRole(args.role))
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError("message must be an object")
                _handle(environment, message)
            except (json.JSONDecodeError, ValueError) as exc:
                _error(None, -32700, str(exc))
    finally:
        environment.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
